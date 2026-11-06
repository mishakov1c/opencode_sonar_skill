#!/usr/bin/env python3
"""Isolated checks for the sonar skill scripts. Does not touch project BSL."""

import contextlib
import io
import json
import runpy
import subprocess
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent
STATE = ROOT / "sonar_state.py"
INDEX = ROOT / "sonar_index.py"
SKILL = ROOT.parent / "SKILL.md"


def run(args):
    completed = subprocess.run(
        [sys.executable, str(STATE), *args],
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    if completed.returncode != 0:
        raise SystemExit(completed.stderr or completed.stdout)
    return json.loads(completed.stdout)


def fail(message):
    raise SystemExit(message)


def write_issues(path, rows):
    path.write_text(json.dumps({
        "total": len(rows),
        "paging": {"pageIndex": 1, "pageSize": 100, "total": len(rows)},
        "issues": rows,
    }, ensure_ascii=False), encoding="utf-8")


def issue(key, rule, file_name, line, severity="MAJOR", issue_type="CODE_SMELL", message="x"):
    return {
        "key": key,
        "rule": rule,
        "severity": severity,
        "type": issue_type,
        "component": "demo:" + file_name,
        "line": line,
        "message": message,
        "flows": [],
    }


def test_route():
    new = run(["route", "--arg", ""])
    if new["mode"] != "new" or new["pace"] != "fast":
        fail("empty /sonar must start a new fast review, got " + json.dumps(new))
    if run(["route", "--arg", "resume"])["mode"] != "resume":
        fail("resume argument lost")
    skipped = run(["route", "--arg", "skipped"])
    if skipped["mode"] != "skipped":
        fail("skipped argument lost")
    detailed = run(["route", "--arg", "detailed"])
    if detailed["mode"] != "new" or detailed["pace"] != "detailed":
        fail("detailed must stay a new review")
    if run(["route", "--arg", "other"])["mode"] != "unknown":
        fail("unknown argument must not be guessed")


def test_context_and_help(tmp):
    for args in (["--help"], ["set-context", "--help"]):
        result = subprocess.run([sys.executable, str(STATE), *args], capture_output=True, text=True, encoding="utf-8")
        if result.returncode or "set-context" not in result.stdout:
            fail("help failed: " + result.stderr)
    state = tmp / "existing-state.json"
    original = {"sourceUrl": "example", "issuesPath": "existing.json", "indexPath": "index.json",
                "decisions": {"key": {"status": "skipped", "history": [{"status": "agreed"}]}},
                "groups": {"group": {"keys": ["key"]}}, "custom": {"data": [1, 2]}}
    state.write_text(json.dumps(original), encoding="utf-8")
    args = ["set-context", str(state), "--repo", str(tmp), "--branch", "task_1",
            "--target-branch", "release/1", "--pace", "fast"]
    if not run(args)["saved"]:
        fail("context not saved")
    saved = json.loads(state.read_text(encoding="utf-8"))
    if any(saved[k] != v for k, v in original.items()):
        fail("set-context changed previous decisions or other state")
    if (saved["repo"], saved["branch"], saved["targetBranch"], saved["pace"]) != (str(tmp), "task_1", "release/1", "fast"):
        fail("set-context saved incorrect context")
    before = state.read_bytes()
    for extra in (["--branch", "bad name"], ["--pace", "bad"], ["--repo", "relative"]):
        result = subprocess.run([sys.executable, str(STATE), *args, *extra], capture_output=True, text=True, encoding="utf-8")
        if result.returncode == 0 or not result.stderr or state.read_bytes() != before:
            fail("invalid context must not modify the state")
    implementation = runpy.run_path(str(STATE))
    with patch("os.replace", side_effect=OSError("test failure")):
        try:
            implementation["set_context"](state, str(tmp), "task_1", "release/1", "fast")
            fail("set-context did not propagate replacement error")
        except OSError:
            pass
    if state.read_bytes() != before or list(tmp.glob(".sonar-state-*")):
        fail("failed atomic replacement changed state or left temporary files")
    run(args + ["--target-evidence", "Целевая ветка указана пользователем"])
    evidence_state = json.loads(state.read_text(encoding="utf-8"))
    if evidence_state.get("targetBranchEvidence") != "Целевая ветка указана пользователем":
        fail("target branch evidence not stored")
    before = state.read_bytes()
    result = subprocess.run([sys.executable, str(STATE), *args, "--target-branch", "other"],
                            capture_output=True, text=True, encoding="utf-8")
    if result.returncode == 0 or state.read_bytes() != before:
        fail("changing target with stale evidence must fail")
    missing = tmp / "missing.json"
    result = subprocess.run([sys.executable, str(STATE), "set-context", str(missing), *args[2:]], capture_output=True, text=True, encoding="utf-8")
    if result.returncode == 0 or missing.exists():
        fail("set-context must not create missing state")


def test_api_url_synthetic_pr():
    source = (
        "http://sonar.example:9000/project/issues?"
        "id=example_app&pullRequest=4242&resolved=false"
    )
    expected = (
        "http://sonar.example:9000/api/issues/search?"
        "componentKeys=example_app"
        "&pullRequest=4242&resolved=false&ps=100&p=1"
    )
    # Run the CLI entry point once, in process, with network/process access blocked.
    blocked = [
        "socket.socket", "socket.create_connection", "socket.getaddrinfo",
        "urllib.request.urlopen", "urllib.request.OpenerDirector.open",
        "http.client.HTTPConnection.connect", "http.client.HTTPSConnection.connect",
        "subprocess.run", "subprocess.Popen", "os.system",
    ]
    output = io.StringIO()
    with contextlib.ExitStack() as stack:
        guards = [stack.enter_context(patch(target, side_effect=AssertionError(
            "api-url must not use network or external commands: " + target
        ))) for target in blocked]
        stack.enter_context(patch.object(sys, "argv", [str(INDEX), "api-url", source]))
        stack.enter_context(contextlib.redirect_stdout(output))
        runpy.run_path(str(INDEX), run_name="__main__")
    if output.getvalue().splitlines() != [expected, "WAITING_FOR_USER_JSON"]:
        fail("Synthetic PR: unexpected API URL or missing stop status: " + output.getvalue())
    if any(guard.called for guard in guards):
        fail("Synthetic PR: network or external command attempted")


def test_decisions_and_history(tmp):
    state = tmp / "review-state.json"
    state.write_text(json.dumps({"decisions": {}, "groups": {}}), encoding="utf-8")
    refused = subprocess.run(
        [sys.executable, str(STATE), "decide", str(state), "--key", "NEW", "--status", "skipped", "--choice", "skip"],
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    if refused.returncode == 0:
        fail("skip without --answered must be refused")
    run([
        "decide", str(state), "--key", "OLD", "--status", "skipped", "--choice", "skip",
        "--note", "first", "--answered",
    ])
    run([
        "decide", str(state), "--key", "OLD", "--status", "agreed", "--choice", "minimal",
        "--note", "second", "--answered",
    ])
    saved = json.loads(state.read_text(encoding="utf-8"))
    history = saved["decisions"]["OLD"].get("history") or []
    if not history or history[-1]["status"] != "skipped":
        fail("previous decision was not kept in history")


def test_compare_does_not_copy(tmp):
    old_issues = tmp / "old.json"
    new_issues = tmp / "new.json"
    old_state = tmp / "old-state.json"
    write_issues(old_issues, [
        issue("kept-skip", "bsl:Typo", "CF/A.bsl", 1, "INFO"),
        issue("gone-applied", "bsl:MagicNumber", "CF/A.bsl", 2),
        issue("still-applied", "bsl:Typo", "CF/A.bsl", 3, "INFO"),
    ])
    write_issues(new_issues, [
        issue("kept-skip", "bsl:Typo", "CF/A.bsl", 1, "INFO"),
        issue("still-applied", "bsl:Typo", "CF/A.bsl", 3, "INFO"),
        issue("fresh-bug", "bsl:InvalidCharacterInFile", "CF/B.bsl", 9, "MAJOR", "BUG", "bad dash"),
    ])
    old_state.write_text(json.dumps({
        "state": "complete",
        "decisions": {
            "kept-skip": {"status": "skipped", "choice": "skip", "note": "name"},
            "gone-applied": {"status": "applied", "choice": "minimal", "note": "fixed"},
            "still-applied": {"status": "applied", "choice": "minimal", "note": "fixed"},
        },
    }), encoding="utf-8")
    before = old_state.read_bytes()
    report = run([
        "compare", str(old_state),
        "--issues", str(new_issues),
        "--old-issues", str(old_issues),
    ])
    if old_state.read_bytes() != before:
        fail("compare must not modify the previous state")
    if report["copied"] is not False or report["new"] != 1 or report["disappeared"] != 1:
        fail("unexpected compare counts: " + json.dumps(report))
    compact = run(["compare", str(old_state), "--issues", str(new_issues), "--old-issues", str(old_issues), "--compact"])
    if "newKeys" in compact or compact["appliedStillOpen"] != 1 or compact["unstable"] != 0:
        fail("compact compare is incorrect")
    if report["appliedStillOpen"] != ["still-applied"]:
        fail("applied issue still in Sonar must not be treated as confirmed")
    if "fresh-bug" not in report["newKeys"]:
        fail("new issue was not reported")
    carried = tmp / "new-state.json"
    carried.write_text(json.dumps({"decisions": {}, "groups": {}}), encoding="utf-8")
    refused = subprocess.run(
        [sys.executable, str(STATE), "carry", str(old_state), "--to", str(carried),
         "--issues", str(new_issues), "--strategy", "keep"],
        capture_output=True, text=True, encoding="utf-8",
    )
    if refused.returncode == 0:
        fail("carry without --answered must be refused")
    result = run([
        "carry", str(old_state), "--to", str(carried), "--issues", str(new_issues),
        "--old-issues", str(old_issues), "--strategy", "keep", "--answered",
    ])
    saved = json.loads(carried.read_text(encoding="utf-8"))
    decisions = saved["decisions"]
    if "fresh-bug" in decisions or "still-applied" in decisions:
        fail("new or still-open applied decision was copied")
    if decisions.get("kept-skip", {}).get("status") != "skipped":
        fail("confirmed skip was not carried after explicit strategy")
    if not decisions["kept-skip"].get("history"):
        fail("carried decision has no history")
    if result["copied"] != 1:
        fail("carry copied an unexpected number of decisions")
    review_all = tmp / "review-all-state.json"
    review_all.write_text(json.dumps({"decisions": {}, "groups": {}}), encoding="utf-8")
    reviewed = run([
        "carry", str(old_state), "--to", str(review_all), "--issues", str(new_issues),
        "--strategy", "review-all", "--answered",
    ])
    if reviewed["copied"] != 0 or json.loads(review_all.read_text(encoding="utf-8"))["decisions"]:
        fail("review-all copied previous decisions")


def test_resume_and_skipped(tmp):
    reports = tmp / "reports"
    reports.mkdir()
    complete = reports / "demo__proj__pr-1__aaa-state.json"
    open_state = reports / "demo__proj__pr-1__bbb-state.json"
    complete.write_text(json.dumps({
        "state": "complete",
        "sourceUrl": "http://sonar/pr=1",
        "decisions": {"k": {"status": "skipped", "choice": "skip"}},
    }), encoding="utf-8")
    open_state.write_text(json.dumps({
        "state": "survey",
        "sourceUrl": "http://sonar/pr=1",
        "decisions": {},
    }), encoding="utf-8")
    resume = run(["find", "--reports", str(reports), "--mode", "resume"])
    paths = [item["path"] for item in resume["items"]]
    if str(complete) in paths or str(open_state) not in paths:
        fail("resume offered a finished review or missed an open one")
    skipped = run(["find", "--reports", str(reports), "--mode", "skipped"])
    if not any(item["path"] == str(complete) for item in skipped["items"]):
        fail("skipped review did not find a finished review with skips")
    issues = tmp / "issues.json"
    write_issues(issues, [issue("k", "bsl:Typo", "CF/Mod.bsl", 4, "INFO", message="Пин")])
    report = run(["skipped", str(complete), "--issues", str(issues)])
    if report["skipped"] != 1 or report["byRule"].get("bsl:Typo") != 1:
        fail("skipped summary is wrong: " + json.dumps(report))
    selected = run(["skipped", str(complete), "--issues", str(issues), "--rule", "bsl:Typo"])
    if selected["selected"] != 1:
        fail("rule filter did not select the skipped issue")


def test_queue_and_index(tmp):
    index = {
        "received": 2,
        "total": 2,
        "complete": True,
        "modules": [{
            "file": "CF/Mod.bsl",
            "issues": [
                {"key": "done", "rule": "bsl:Typo", "line": 1, "message": "a"},
                {"key": "todo", "rule": "bsl:MagicNumber", "line": 2, "message": "b"},
            ],
        }],
    }
    index_path = tmp / "index.json"
    index_path.write_text(json.dumps(index), encoding="utf-8")
    state = tmp / "state.json"
    state.write_text(json.dumps({
        "decisions": {"done": {"status": "skipped"}},
    }), encoding="utf-8")
    queued = run(["queue", str(state), "--index", str(index_path), "--limit", "8"])
    if queued["pending"] != 1 or queued["cards"][0]["key"] != "todo":
        fail("queue did not return only the undecided issue")
    stats = subprocess.run(
        [sys.executable, str(INDEX), "stats", str(index_path)],
        check=True, capture_output=True, text=True, encoding="utf-8",
    )
    payload = json.loads(stats.stdout)
    if payload["modules"] != 1 or payload["complete"] is not True:
        fail("index stats missing")


def test_generic_markdown_import(tmp):
    report = tmp / "legacy.md"
    state = tmp / "imported-state.json"
    report.write_text(
        '- Исходная ссылка: `https://sonar.example/project/issues?id=demo`\n'
        '- ISSUE-DEMO-A | CF/Module.bsl | согласовано: вынести поля в переменные; Код ещё не менялся\n'
        '- ISSUE-DEMO-B | CF/Module.bsl | согласовано: вынести поля в переменные; Код ещё не менялся\n'
        '- ISSUE-DEMO-C | CF/Other.bsl | пропущено пользователем\n'
        '- ISSUE-DEMO-D | CF/Other.bsl | без решения\n', encoding='utf-8')
    run(['import-md', str(state), str(report)])
    saved = json.loads(state.read_text(encoding='utf-8'))
    if set(saved['decisions']) != {'ISSUE-DEMO-A', 'ISSUE-DEMO-B', 'ISSUE-DEMO-C'}:
        fail('Markdown import must accept arbitrary issue keys and preserve decisions')
    if saved['decisions']['ISSUE-DEMO-C']['status'] != 'skipped':
        fail('Markdown import lost skipped status')
    if saved['groups'] or any(item['group'] is not None for item in saved['decisions'].values()):
        fail('Markdown import must not infer business-specific groups from notes')


def test_skill_contract():
    text = SKILL.read_text(encoding="utf-8")
    required = [
        "Аргумент команды: $ARGUMENTS",
        "route --arg",
        "/sonar resume",
        "/sonar skipped",
        "/sonar detailed",
        "Оставить прежние решения",
        "Пересмотреть выборочно",
        "Пересмотреть все",
        "5–8",
        "--answered",
        "appliedStillOpen",
    ]
    for item in required:
        if item not in text:
            fail("SKILL.md missing: " + item)
    if "Пустой аргумент — это `new`, не `resume`" not in text:
        fail("new review is not separated from resume")
    stop = text.split("## СТОП после ссылки: ожидание JSON (приоритетное правило)", 1)[1]
    stop = stop.split("Аргумент команды:", 1)[0]
    link_step = text.split("### Этап 1. Сформировать ссылку и остановиться", 1)[1]
    link_step = link_step.split("### Этап 2.", 1)[0]
    for section in (stop,):
        for item in (
            "ровно один локальный вызов", "WAITING_FOR_USER_JSON",
            "заверши текущий ответ", "до следующего сообщения пользователя",
            "curl", "Invoke-WebRequest", "Python HTTP-библиотеки",
            "Git", "BSL-код", "браузере",
        ):
            if item not in section:
                fail("JSON handoff contract missing: " + item)
    if 'приоритетное правило «СТОП после ссылки»' not in link_step:
        fail('Link step must refer to the mandatory stop rule')
    for item in ('sonar_ingest.py --call-id', 'Не проси сохранить файл', 'только русский'):
        if item not in text:
            fail('Missing chat intake contract: ' + item)
    if text.index("## СТОП после ссылки") > text.index("route --arg"):
        fail("JSON stop rules must precede routing and review instructions")


def test_external_compare(state_path, issues_path):
    before = Path(state_path).read_bytes()
    report = run(["compare", state_path, "--issues", issues_path])
    if Path(state_path).read_bytes() != before:
        fail("External compare modified the saved state")
    if report["copied"] is not False:
        fail("External compare copied decisions")
    if report["new"] < 1:
        fail("External compare did not report new issues")
    if not report["appliedStillOpen"] and report["disappeared"] < 1:
        fail("External compare did not separate disappeared and still-open fixes")
    print(json.dumps({
        "comparison": {
            "disappeared": report["disappeared"],
            "kept": report["kept"],
            "new": report["new"],
            "appliedStillOpen": len(report["appliedStillOpen"]),
            "match": report["match"],
        }
    }, ensure_ascii=False))


def main(argv):
    test_api_url_synthetic_pr()
    test_route()
    test_skill_contract()
    with tempfile.TemporaryDirectory() as directory:
        tmp = Path(directory)
        test_context_and_help(tmp)
        test_decisions_and_history(tmp)
        test_compare_does_not_copy(tmp)
        test_resume_and_skipped(tmp)
        test_queue_and_index(tmp)
        test_generic_markdown_import(tmp)
    if "--comparison-state" in argv:
        state_path = argv[argv.index("--comparison-state") + 1]
        issues_path = argv[argv.index("--comparison-issues") + 1]
        test_external_compare(state_path, issues_path)
    print("ok")


if __name__ == "__main__":
    main(sys.argv[1:])
