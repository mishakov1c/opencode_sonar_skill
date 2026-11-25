#!/usr/bin/env python3
"""Read-only GitLab MR artifacts. Never contacts SonarQube."""
import argparse
from collections import Counter
import hashlib
import io
import json
import os
from pathlib import Path
import re
import sys
from urllib.parse import quote, urlsplit
from urllib.request import Request, build_opener, HTTPRedirectHandler
from urllib.error import HTTPError, URLError
import zipfile


class GitLabError(ValueError):
    pass


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def read_token():
    token = os.environ.get('GITLAB_TOKEN', '')
    if sys.platform == 'win32':
        import winreg
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, 'Environment') as key:
                token = winreg.QueryValueEx(key, 'GITLAB_TOKEN')[0]
        except OSError:
            pass
    if not token:
        raise GitLabError('GITLAB_TOKEN не найден в окружении процесса или пользователя.')
    return token


def parse_mr(url):
    parsed = urlsplit(url)
    match = re.fullmatch(r'/(.+)/-/merge_requests/([1-9]\d*)/?', parsed.path)
    # Не отправляем корпоративный токен на произвольный хост.
    if (parsed.scheme != 'https' or parsed.netloc != 'gitlab.kdvm.ru'
            or parsed.query or parsed.fragment or not match):
        raise GitLabError('Ожидается HTTPS-ссылка MR на gitlab.kdvm.ru.')
    return 'https://gitlab.kdvm.ru', match[1], int(match[2])


class Client:
    def __init__(self, origin, token):
        self.origin, self.token = origin, token
        self.opener = build_opener(NoRedirect)

    def get(self, path, binary=False):
        if not path.startswith('/api/v4/'):
            raise GitLabError('Разрешено только чтение GitLab API v4.')
        try:
            with self.opener.open(Request(self.origin + path, headers={
                    'PRIVATE-TOKEN': self.token}), timeout=45) as response:
                data = response.read(50 * 1024 * 1024 + 1)
                if len(data) > 50 * 1024 * 1024:
                    raise GitLabError('Ответ превышает ограничение 50 MiB.')
                return (data if binary else json.loads(data)), response.headers.get('X-Next-Page')
        except HTTPError as exc:
            raise GitLabError('GitLab API: HTTP ' + str(exc.code) +
                              '. 403 означает отсутствие доступа, а не отсутствие артефактов.') from None
        except (URLError, TimeoutError, ValueError):
            raise GitLabError('Ошибка соединения или некорректный ответ GitLab API.') from None

    def pages(self, path):
        result, page, seen = [], '1', set()
        while page:
            if page in seen or len(seen) >= 100:
                raise GitLabError('Ошибка пагинации GitLab API.')
            seen.add(page)
            data, page = self.get(path + ('&' if '?' in path else '?') + 'per_page=100&page=' + page)
            if not isinstance(data, list):
                raise GitLabError('GitLab API не вернул список.')
            result.extend(data)
        return result


def inspect(client, project, iid):
    root = '/api/v4/projects/' + quote(project, safe='')
    mr, _ = client.get(root + '/merge_requests/' + str(iid))
    pipelines = client.pages(root + '/merge_requests/' + str(iid) + '/pipelines')
    head = mr.get('head_pipeline')
    if head and not any(p['id'] == head['id'] for p in pipelines):
        pipelines.insert(0, head)
    result = {'mr': mr.get('web_url'), 'source_branch': mr.get('source_branch'),
              'target_branch': mr.get('target_branch'), 'sha': mr.get('sha'),
              'head_pipeline': head, 'pipelines': []}
    # Исторические конвейеры не подменяют текущий.
    for pipeline in pipelines:
        entry = {key: pipeline.get(key) for key in ('id', 'sha', 'ref', 'status', 'web_url')}
        try:
            jobs = client.pages(root + '/pipelines/' + str(pipeline['id']) + '/jobs?include_retried=true')
            entry['jobs'] = [{key: job.get(key) for key in (
                'id', 'name', 'status', 'stage', 'web_url', 'artifacts_file',
                'artifacts', 'artifacts_expire_at', 'erased_at')} for job in jobs]
        except GitLabError as exc:
            entry['error'] = str(exc)
        result['pipelines'].append(entry)
    return result


def archive_summary(data):
    try:
        archive = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile:
        raise GitLabError('Артефакт не является ZIP-архивом.') from None
    files, reports = [], []
    for info in archive.infolist():
        if info.is_dir():
            continue
        files.append({'path': info.filename, 'size': info.file_size})
        if not info.filename.lower().endswith('.json') or info.file_size > 10 * 1024 * 1024:
            continue
        try:
            payload = json.loads(archive.read(info).decode('utf-8-sig'))
        except (ValueError, UnicodeError):
            continue
        if isinstance(payload, dict) and isinstance(payload.get('issues'), list):
            issues = payload['issues']
            reports.append({'path': info.filename, 'total': payload.get('total'),
                            'received': len(issues), 'paging': payload.get('paging'),
                            'rules': sorted({str(i.get('rule', '?')) for i in issues if isinstance(i, dict)}),
                            'projects': sorted({str(i.get('project', '?')) for i in issues if isinstance(i, dict)}),
                            'pullRequests': sorted({str(i.get('pullRequest', '?')) for i in issues if isinstance(i, dict)})})
    return {'files': files, 'sonar_reports': reports}

def import_report(data, result, job):
    from sonar_ingest import validate_page, immutable
    from sonar_index import build_index
    head = result.get('head_pipeline') or {}
    pipeline = next((p for p in result['pipelines'] if p['id'] == head.get('id')), None)
    if not pipeline or pipeline.get('sha') != result['sha']:
        raise GitLabError('SHA текущего pipeline не совпадает с MR.')
    if not any(j['id'] == job for j in pipeline.get('jobs', [])):
        raise GitLabError('Импорт разрешён только из текущего pipeline MR.')
    summary = archive_summary(data)
    reports = summary['sonar_reports']
    if len(reports) != 1:
        raise GitLabError('Нужен ровно один JSON-отчёт с issues; найдены: ' + str(len(reports)))
    name = reports[0]['path']
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        raw = archive.read(name).decode('utf-8-sig')
    payload = json.loads(raw)
    page, size, total = validate_page(payload)
    if page != 1 or total != len(payload['issues']):
        raise GitLabError('Артефакт содержит неполную выгрузку замечаний.')
    _, _, iid = parse_mr(result['mr'])
    project_keys = {c['key'] for c in payload.get('components', []) if c.get('qualifier') == 'TRK'}
    if len(project_keys) != 1:
        raise GitLabError('В отчёте не подтверждён единственный проект Sonar.')
    project_key = next(iter(project_keys))
    source = 'https://sonar.invalid/project/issues?id=' + quote(project_key, safe='') + '&pullRequest=' + str(iid)
    # Только локальная валидация: этот URL никогда не запрашивается.
    validate_page(payload, source)
    issues = payload['issues']
    root = Path.home() / '.config/opencode/sonar-reports'
    root.mkdir(parents=True, exist_ok=True)
    digest = hashlib.sha256(data).hexdigest()
    stem = 'gitlab-' + hashlib.sha256(result['mr'].encode()).hexdigest()[:12] + '-pr-' + str(iid) + '-pipeline-' + str(head['id']) + '-job-' + str(job) + '-' + digest[:16]
    issues_path, index_path, state_path = [root / (stem + suffix) for suffix in ('-issues.json', '-index.json', '-state.json')]
    dump = lambda value: json.dumps(value, ensure_ascii=False, indent=2)
    immutable(issues_path, raw)
    immutable(index_path, dump(build_index(issues, total)))
    if not state_path.exists():
        immutable(state_path, dump({
            'state': 'in_progress', 'sourceUrl': result['mr'],
            'issuesPath': str(issues_path), 'indexPath': str(index_path),
            'decisions': {}, 'groups': {},
            'gitlab': {'mr': result['mr'], 'sha': result['sha'], 'pipeline': head['id'],
                       'job': job, 'artifact': name, 'archiveSha256': digest,
                       'sonarProject': project_key}}))
    return {'status': 'READY', 'total': total,
            'modules': len({i['component'] for i in issues}),
            'severities': dict(Counter(i.get('severity', '?') for i in issues)),
            'issuesPath': str(issues_path), 'indexPath': str(index_path),
            'statePath': str(state_path), 'sha': result['sha'], 'pipeline': head['id'], 'job': job}


def main():
    if hasattr(sys.stdout, 'reconfigure'):
        sys.stdout.reconfigure(encoding='utf-8')
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('mr_url')
    parser.add_argument('--job', type=int, help='Проверить ZIP артефактов задания из конвейера MR')
    parser.add_argument('--import-report', action='store_true', help='Сохранить полный отчёт текущего pipeline и создать состояние')
    args = parser.parse_args()
    try:
        origin, project, iid = parse_mr(args.mr_url)
        client = Client(origin, read_token())
        result = inspect(client, project, iid)
        if args.import_report and args.job is None:
            raise GitLabError('Для импорта укажите --job после проверки списка артефактов.')
        if args.job is not None:
            allowed = {j['id'] for p in result['pipelines'] for j in p.get('jobs', [])}
            if args.job not in allowed:
                raise GitLabError('Задание не найдено в конвейерах указанного MR.')
            data, _ = client.get('/api/v4/projects/' + quote(project, safe='') +
                                 '/jobs/' + str(args.job) + '/artifacts', binary=True)
            result = (import_report(data, result, args.job) if args.import_report else
                      {'mr': result['mr'], 'job': args.job, **archive_summary(data)})
        print(json.dumps(result, ensure_ascii=False))
        return 0
    except (GitLabError, OSError) as exc:
        print(json.dumps({'status': 'ERROR', 'message': str(exc)}, ensure_ascii=False))
        return 1


if __name__ == '__main__':
    sys.exit(main())
