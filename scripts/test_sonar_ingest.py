#!/usr/bin/env python3
"""Изолированные проверки приёма JSON без сети и рабочих проектов."""
import json
import contextlib
import io
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from sonar_index import api_url
from sonar_ingest import IntakeError, connect, current_session, extract_json, ingest, validate_page
import sonar_ingest

# Synthetic project, PR and invocation marker; no production exports are used.
URL = 'https://sonar.example/project/issues?id=demo&pullRequest=4242&resolved=false'
TOKEN = '0123456789abcdef01234567'


def payload(total=77, page=1, size=100):
    return {'total': total, 'paging': {'total': total, 'pageIndex': page, 'pageSize': size},
            'issues': [{'key': str(n), 'rule': 'bsl:rule', 'component': 'demo:Module.bsl',
                        'project': 'demo', 'pullRequest': '4242', 'line': n + 1,
                        'severity': 'MAJOR', 'message': 'Проверка'}
                       for n in range((page - 1) * size, min(page * size, total))]}


class IntakeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.db = sqlite3.connect(self.root / 'opencode.db')
        self.addCleanup(self.db.close)
        self.db.executescript('''
            CREATE TABLE session(id TEXT, directory TEXT);
            CREATE TABLE message(id TEXT,session_id TEXT,data TEXT,time_created INT);
            CREATE TABLE part(id TEXT,message_id TEXT,session_id TEXT,data TEXT,time_created INT);
            INSERT INTO session VALUES('current','project'),('other','project');
        ''')
        self.number = 0
        self.reports = self.root / 'reports'
        for target in ('socket.socket', 'socket.create_connection', 'urllib.request.urlopen', 'subprocess.Popen'):
            guard = patch(target, side_effect=AssertionError('Сеть и внешние программы запрещены'))
            guard.start()
            self.addCleanup(guard.stop)
        self.add('user', '# Sonar: разбор замечаний по модулям\nПример: {"issues": []}')
        self.add('user', URL)
        self.handoff()

    def add(self, role, text=None, part=None, session='current'):
        self.number += 1
        mid = 'message-' + str(self.number).zfill(4)
        self.db.execute('INSERT INTO message VALUES(?,?,?,?)', (mid, session, json.dumps({'role': role}), self.number))
        self.db.execute('INSERT INTO part VALUES(?,?,?,?,?)', (mid, mid, session,
                        json.dumps(part or {'type': 'text', 'text': text}), self.number))
        self.db.commit()
        return mid

    def handoff(self):
        self.add('assistant', part={'type': 'tool', 'tool': 'bash', 'state': {'status': 'completed',
                 'input': {'command': 'python scripts/sonar_index.py api-url "' + URL + '"'},
                 'output': api_url(URL) + '\nWAITING_FOR_USER_JSON\n'}})

    def run_ingest(self):
        return ingest(self.db, 'current', self.reports)

    def test_realistic_77_and_repeat_preserves_decisions(self):
        text = json.dumps(payload(), ensure_ascii=False)
        mid = self.add('user', text)
        self.add('user', json.dumps(payload(1)), session='other')
        result = self.run_ingest()
        self.assertEqual((result['total'], result['modules'], result['messageIDs']), (77, 1, [mid]))
        self.assertLess(len(json.dumps(result)), 2400)
        state = Path(result['statePath'])
        data = json.loads(state.read_text())
        data['decisions'] = {'1': {'status': 'skipped', 'history': [{'status': 'agreed'}]}}
        state.write_text(json.dumps(data))
        before = {p.name: p.read_bytes() for p in self.reports.iterdir()}
        self.assertEqual(self.run_ingest(), result)
        self.assertEqual(before, {p.name: p.read_bytes() for p in self.reports.iterdir()})
        raw = [p for p in self.reports.iterdir() if '-p1.json' in p.name][0]
        self.assertEqual(raw.read_text(encoding='utf-8'), text)

    def test_json_and_fence(self):
        text = json.dumps(payload())
        self.assertEqual(extract_json(text)[0], text)
        self.assertEqual(extract_json('```json\n' + text + '\n```')[0], text)
        for bad in ('инструкция: ' + text, text + text, '[1]', '{', '```json\n{}', '{"total":NaN}', '{"total":1,"total":2}'):
            with self.subTest(bad=bad[:20]), self.assertRaises(IntakeError):
                extract_json(bad)

    def test_arbitrary_project_key_and_report_name(self):
        project = 'example_service_feature'
        url = URL.replace('id=demo', 'id=' + project)
        self.add('user', url)
        self.add('assistant', part={'type': 'tool', 'tool': 'bash', 'state': {
            'status': 'completed',
            'input': {'command': 'python scripts/sonar_index.py api-url "' + url + '"'},
            'output': api_url(url) + '\nWAITING_FOR_USER_JSON\n'}})
        data = payload(1)
        data['issues'][0].update(project=project, component=project + ':Module.bsl')
        self.add('user', json.dumps(data))
        result = self.run_ingest()
        self.assertEqual(result['status'], 'READY')
        self.assertTrue(Path(result['statePath']).name.startswith(project + '__pr-4242__'))
        self.assertEqual(self.run_ingest(), result)

    def test_invalid_last_message_no_old_fallback(self):
        self.add('user', json.dumps(payload()))
        self.add('user', '{"issues":')
        with self.assertRaises(IntakeError):
            self.run_ingest()
        self.assertFalse(self.reports.exists())
        self.add('user', json.dumps(payload()))
        self.assertEqual(self.run_ingest()['status'], 'READY')

    def test_wrong_project_pr_and_components(self):
        for field, value in [('project', 'other'), ('pullRequest', '4243')]:
            d = payload()
            d['issues'][0][field] = value
            with self.subTest(field=field), self.assertRaises(IntakeError):
                validate_page(d, URL)
        d = payload()
        d['components'] = [{'key': 'demo', 'pullRequest': '4243'}]
        with self.assertRaises(IntakeError):
            validate_page(d, URL)

    def test_schema_and_api_errors(self):
        cases = []
        for field in ('total', 'paging', 'issues'):
            d = payload(); del d[field]; cases.append(d)
        for field in ('total', 'pageIndex', 'pageSize'):
            d = payload(); del d['paging'][field]; cases.append(d)
        for field, value in [('pageSize', 101), ('pageIndex', 0), ('total', True), ('pageSize', '100')]:
            d = payload(); d['paging'][field] = value; cases.append(d)
        d = payload(); d['errors'] = [{'msg': 'Ошибка'}]; cases.append(d)
        d = payload(); d['issues'] = {}; cases.append(d)
        d = payload(); d['issues'].pop(); cases.append(d)
        d = payload(); d['issues'][1]['key'] = d['issues'][0]['key']; cases.append(d)
        d = payload(); d['p'] = 2; cases.append(d)
        d = payload(); d['components'] = {}; cases.append(d)
        d = payload(); d['issues'][0]['severity'] = []; cases.append(d)
        for n, data in enumerate(cases):
            with self.subTest(case=n), self.assertRaises(IntakeError):
                validate_page(data, URL)

    def test_multipage_and_repeat_page(self):
        self.add('user', json.dumps(payload(150)))
        result = self.run_ingest()
        self.assertEqual(result['status'], 'WAITING_FOR_USER_JSON')
        self.assertEqual(result['missingPages'], [2])
        self.assertIn('p=2', result['urls'][0])
        self.assertFalse(list(self.reports.glob('*-state.json')))
        self.add('user', json.dumps(payload(150, 2)))
        self.assertEqual(self.run_ingest()['total'], 150)
        self.add('user', json.dumps(payload(150, 2)))
        self.assertEqual(self.run_ingest()['total'], 150)
        self.assertEqual(len(list(self.reports.glob('*-state.json'))), 1)

    def test_mixed_totals_and_duplicate_keys(self):
        self.add('user', json.dumps(payload(150)))
        self.add('user', json.dumps(payload(149, 2)))
        with self.assertRaises(IntakeError):
            self.run_ingest()
        self.add('user', URL)
        self.handoff()
        self.add('user', json.dumps(payload(150)))
        d = payload(150, 2); d['issues'][0]['key'] = '0'
        self.add('user', json.dumps(d))
        with self.assertRaises(IntakeError):
            self.run_ingest()

    def test_session_binding_running_only_unique(self):
        def call(status, session='current'):
            self.add('assistant', part={'type': 'tool', 'tool': 'bash', 'state': {'status': status,
                     'input': {'command': 'python scripts/sonar_ingest.py --call-id ' + TOKEN}}}, session=session)
        with self.assertRaises(IntakeError):
            current_session(self.db, TOKEN)
        call('running')
        self.assertEqual(current_session(self.db, TOKEN), 'current')
        call('completed', 'other')
        with self.assertRaises(IntakeError):
            current_session(self.db, TOKEN)

    def test_main_single_compact_response(self):
        self.add('user', json.dumps(payload()))
        self.add('assistant', part={'type': 'tool', 'tool': 'bash', 'state': {'status': 'running',
                 'input': {'command': 'python scripts/sonar_ingest.py --call-id ' + TOKEN}}})
        out = io.StringIO()
        with patch.object(sonar_ingest, 'database_path', return_value=self.root / 'opencode.db'), \
             patch.object(sonar_ingest.Path, 'home', return_value=self.root), \
             patch('sys.argv', ['sonar_ingest.py', '--call-id', TOKEN]), contextlib.redirect_stdout(out):
            self.assertEqual(sonar_ingest.main(), 0)
        result = json.loads(out.getvalue())
        self.assertEqual((result['status'], result['total']), ('READY', 77))
        self.assertNotIn('Проверка', out.getvalue())
        self.assertLess(len(out.getvalue()), 2400)

    def test_url_in_slash_argument(self):
        self.add('user', '# Sonar: разбор замечаний по модулям\nАргумент команды: ' + URL)
        self.handoff()
        self.add('user', json.dumps(payload()))
        self.assertEqual(self.run_ingest()['status'], 'READY')

    def test_other_session_has_no_source(self):
        self.add('user', json.dumps(payload()), session='other')
        with self.assertRaises(IntakeError):
            ingest(self.db, 'other', self.reports)

    def test_new_url_requires_new_handoff(self):
        self.add('user', URL.replace('4242', '4243'))
        self.add('user', json.dumps(payload()))
        with self.assertRaises(IntakeError):
            self.run_ingest()

    def test_empty_issues_identity(self):
        d = payload(0)
        with self.assertRaises(IntakeError):
            validate_page(d, URL)
        d['components'] = [{'key': 'demo', 'qualifier': 'TRK', 'pullRequest': '4242'}]
        validate_page(d, URL)

    def test_readonly_and_unknown_schema(self):
        with connect(self.root / 'opencode.db') as db:
            with self.assertRaises(sqlite3.OperationalError):
                db.execute('DELETE FROM session')
        bad = self.root / 'bad.db'
        sqlite3.connect(bad).close()
        with self.assertRaises(IntakeError):
            with connect(bad):
                pass


if __name__ == '__main__':
    unittest.main()
