#!/usr/bin/env python3
"""Compact Sonar review state. Python standard library only."""

import json
import argparse
import os
import re
import tempfile
import sys
from pathlib import Path


def load(path):
    file_path = Path(path)
    if not file_path.exists():
        return {"decisions": {}, "groups": {}}
    return json.loads(file_path.read_text(encoding="utf-8"))


def save(path, state):
    Path(path).write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


def set_context(path, repo, branch, target_branch, pace, target_evidence=None):
    file_path = Path(path)
    if not file_path.is_file():
        raise ValueError("Файл состояния не найден; создание нового состояния запрещено.")
    repo_path = Path(repo)
    if not repo_path.is_absolute() or not repo_path.is_dir():
        raise ValueError("repo должен быть абсолютным путём к существующему каталогу.")
    for name, value in (("branch", branch), ("targetBranch", target_branch)):
        if (not value or value != value.strip() or value.startswith("-")
                or re.search(r"[\s\x00-\x1f\x7f~^:?*\[\\]", value)
                or ".." in value or "@{" in value or "//" in value
                or value.startswith(("/", ".")) or value.endswith(("/", ".", ".lock"))):
            raise ValueError(name + ": некорректное имя ветки.")
    if pace not in ("fast", "detailed"):
        raise ValueError("pace должен быть fast или detailed.")
    if target_evidence is not None and (not target_evidence.strip() or len(target_evidence) > 2000):
        raise ValueError("Основание целевой ветки должно содержать от 1 до 2000 символов.")
    state = load(file_path)
    if not isinstance(state, dict):
        raise ValueError("Состояние должно быть JSON-объектом.")
    if state.get("targetBranchEvidence") and state.get("targetBranch") != target_branch and target_evidence is None:
        raise ValueError("Для новой целевой ветки укажите актуальное основание через --target-evidence.")
    state.update(repo=str(repo_path), branch=branch, targetBranch=target_branch, pace=pace)
    if target_evidence is not None:
        state["targetBranchEvidence"] = target_evidence.strip()
    fd, temp = tempfile.mkstemp(prefix=".sonar-state-", dir=file_path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as stream:
            json.dump(state, stream, ensure_ascii=False, indent=2)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp, file_path)
    finally:
        if os.path.exists(temp):
            os.unlink(temp)


def summary(state):
    counts = {"agreed": 0, "applied": 0, "skipped": 0, "deferred": 0}
    for item in state.get("decisions", {}).values():
        status = item.get("status")
        if status in counts:
            counts[status] += 1
    return {
        "state": state.get("state"),
        "pace": state.get("pace", "fast"),
        "currentModule": state.get("currentModule"),
        "counts": counts,
        "decided": len(state.get("decisions", {})),
    }


def route(argument):
    text = (argument or "").strip()
    parts = text.split()
    head = parts[0].lower() if parts else ""
    detailed = head == "detailed" or any(part.lower() == "detailed" for part in parts[1:])
    pace = "detailed" if detailed else "fast"
    if head in ("", "new", "detailed"):
        mode = "new"
    elif head == "resume":
        mode = "resume"
    elif head == "skipped":
        mode = "skipped"
    else:
        mode = "unknown"
    return {"mode": mode, "pace": pace, "argument": text}


def unfinished(state):
    counts = summary(state)["counts"]
    if state.get("state") != "complete":
        return True
    return counts["agreed"] > 0 or counts["deferred"] > 0


def find_states(reports_dir, mode="any", match=""):
    root = Path(reports_dir)
    rows = []
    if not root.exists():
        return rows
    needle = (match or "").lower()
    for path in sorted(root.glob("*-state.json"), key=lambda item: item.stat().st_mtime, reverse=True):
        state = load(path)
        counts = summary(state)["counts"]
        blob = " ".join([
            path.name,
            state.get("sourceUrl") or "",
            state.get("repo") or "",
            state.get("branch") or "",
        ]).lower()
        if needle and needle not in blob:
            continue
        if mode == "resume" and not unfinished(state):
            continue
        if mode == "skipped" and counts["skipped"] == 0:
            continue
        rows.append({
            "path": str(path),
            "state": state.get("state"),
            "pace": state.get("pace", "fast"),
            "repo": state.get("repo"),
            "branch": state.get("branch"),
            "unfinished": unfinished(state),
            "counts": counts,
        })
    return rows


def issue_rows(path):
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if "issues" in data:
        rows = []
        for issue in data["issues"]:
            component = issue.get("component", "")
            file_name = component.split(":", 1)[1] if ":" in component else component
            rows.append({
                "key": issue.get("key"),
                "rule": issue.get("rule"),
                "file": file_name.replace("\\", "/"),
                "line": issue.get("line") or 0,
                "severity": issue.get("severity"),
                "type": issue.get("type"),
                "message": issue.get("message", ""),
            })
        return rows
    rows = []
    for module in data.get("modules", []):
        for issue in module.get("issues", []):
            row = dict(issue)
            row["file"] = module.get("file", row.get("file", "")).replace("\\", "/")
            rows.append(row)
    return rows


def compare(old_state, new_issues, old_issues=None):
    new_rows = {row["key"]: row for row in issue_rows(new_issues)}
    old_rows = {row["key"]: row for row in issue_rows(old_issues)} if old_issues else {}
    decisions = old_state.get("decisions", {})
    new_keys = set(new_rows)
    old_keys = set(decisions)
    kept = sorted(new_keys & old_keys)
    fresh = sorted(new_keys - old_keys)
    gone = sorted(old_keys - new_keys)
    unstable = []
    if old_rows:
        for key in kept:
            old = old_rows.get(key)
            new = new_rows[key]
            if not old:
                unstable.append(key)
                continue
            if old.get("rule") != new.get("rule") or old.get("file") != new.get("file"):
                unstable.append(key)
    unstable_set = set(unstable)
    confirmed = [key for key in kept if key not in unstable_set]
    applied_open = [
        key for key in confirmed
        if decisions.get(key, {}).get("status") == "applied"
    ]
    return {
        "match": "key+rule+file" if old_rows else "key-only",
        "disappeared": len(gone),
        "kept": len(confirmed),
        "new": len(fresh),
        "unstable": unstable,
        "appliedStillOpen": applied_open,
        "newKeys": fresh,
        "copied": False,
    }


def remember(item):
    history = list(item.get("history") or [])
    history.append({
        "choice": item.get("choice"),
        "status": item.get("status"),
        "note": item.get("note"),
        "group": item.get("group"),
    })
    return history


def decide(state, keys, choice, status, note, group):
    state.setdefault("decisions", {})
    state.setdefault("groups", {})
    if group:
        state["groups"][group] = {"keys": keys, "choice": choice, "status": status, "note": note}
    for key in keys:
        previous = state["decisions"].get(key)
        item = {
            "choice": choice,
            "status": status,
            "group": group,
            "note": note,
            "sonarConfirmed": False,
        }
        if previous:
            item["history"] = remember(previous)
        state["decisions"][key] = item
    return state


def carry(new_state, old_state, new_issues, strategy, exclude_modules=None, exclude_rules=None, old_issues=None, previous_path=""):
    report = compare(old_state, new_issues, old_issues)
    rows = {row["key"]: row for row in issue_rows(new_issues)}
    unstable = set(report["unstable"])
    exclude_modules = [item.lower() for item in (exclude_modules or [])]
    exclude_rules = set(exclude_rules or [])
    copied = []
    review = []
    new_state.setdefault("decisions", {})
    new_state.setdefault("groups", {})
    new_state["strategy"] = strategy
    new_state["previousState"] = previous_path
    new_state["compare"] = {
        "disappeared": report["disappeared"],
        "kept": report["kept"],
        "new": report["new"],
        "appliedStillOpen": report["appliedStillOpen"],
        "unstable": report["unstable"],
    }
    for key, row in rows.items():
        old = old_state.get("decisions", {}).get(key)
        if not old or key in unstable:
            if key in unstable:
                review.append(key)
            continue
        status = old.get("status")
        if status == "applied":
            review.append(key)
            continue
        if status not in ("skipped", "deferred", "agreed"):
            continue
        selected = any(part in row.get("file", "").lower() for part in exclude_modules) or row.get("rule") in exclude_rules
        if strategy == "review-all" or (strategy == "selected" and selected):
            review.append(key)
            continue
        if strategy not in ("keep", "selected"):
            raise SystemExit("unknown strategy")
        item = dict(old)
        item["carriedFrom"] = previous_path or "previous"
        item["sonarConfirmed"] = False
        item["history"] = remember(old)
        new_state["decisions"][key] = item
        copied.append(key)
    return {
        "strategy": strategy,
        "copied": len(copied),
        "review": len(review),
        "new": report["new"],
        "appliedStillOpen": report["appliedStillOpen"],
        "unstable": report["unstable"],
    }


def skipped_report(state, issues_path, module="", rule=""):
    rows = {row["key"]: row for row in issue_rows(issues_path)}
    module_needle = module.lower()
    by_file = {}
    by_rule = {}
    selected = []
    missing_code = []
    for key, item in state.get("decisions", {}).items():
        if item.get("status") != "skipped":
            continue
        row = rows.get(key)
        file_name = row.get("file", "") if row else ""
        rule_name = row.get("rule", "") if row else ""
        by_file[file_name or "?"] = by_file.get(file_name or "?", 0) + 1
        by_rule[rule_name or "?"] = by_rule.get(rule_name or "?", 0) + 1
        if module_needle and module_needle not in file_name.lower():
            continue
        if rule and rule != rule_name:
            continue
        if not row:
            missing_code.append(key)
            continue
        selected.append({
            "key": key,
            "rule": rule_name,
            "file": file_name,
            "line": row.get("line"),
            "severity": row.get("severity"),
            "message": row.get("message", ""),
            "note": item.get("note", ""),
        })
    return {
        "skipped": sum(by_file.values()),
        "byFile": by_file,
        "byRule": by_rule,
        "selected": len(selected),
        "missingInExport": missing_code,
        "items": selected if module or rule else [],
    }


def queue(index, state, limit=8):
    decided = set(state.get("decisions", {}))
    for module in index.get("modules", []):
        pending = [item for item in module.get("issues", []) if item.get("key") not in decided]
        if not pending:
            continue
        return {
            "file": module.get("file"),
            "pending": len(pending),
            "cards": pending[:limit],
        }
    return {"done": True}


def import_md(text):
    state = {
        "sourceUrl": "",
        "repo": "",
        "branch": "",
        "state": "survey",
        "pace": "fast",
        "currentModule": "",
        "sonarConfirmed": False,
        "decisions": {},
        "groups": {},
    }
    for line in text.splitlines():
        if line.startswith("- Исходная ссылка:"):
            state["sourceUrl"] = line.split("`", 2)[1]
        elif line.startswith("- Репозиторий:"):
            state["repo"] = line.split("`", 2)[1]
        elif line.startswith("- Ветка на старте:"):
            state["branch"] = line.split("`", 2)[1]
        elif line.startswith("- Текущий модуль опроса:"):
            state["currentModule"] = line.split("`", 2)[1]
        elif line.startswith("- ") and " | " in line:
            key, rest = line[2:].split(" | ", 1)
            tail = rest.split(" | ")[-1]
            if tail.startswith("без решения"):
                continue
            if "Код ещё не менялся" in tail or tail.startswith("согласовано"):
                status, choice = "agreed", "minimal"
            elif tail.startswith("исправлено локально"):
                status, choice = "applied", "minimal"
            elif tail.startswith("пропущено"):
                status, choice = "skipped", "skip"
            elif tail.startswith("отложено"):
                status, choice = "deferred", "defer"
            else:
                continue
            state["decisions"][key] = {
                "choice": choice,
                "status": status,
                "group": None,
                "note": tail,
                "sonarConfirmed": False,
            }
    return state


def pairs(args):
    values = {}
    lists = {}
    flags = set()
    index = 0
    while index < len(args):
        item = args[index]
        if not item.startswith("--"):
            raise SystemExit("unexpected argument: " + item)
        name = item[2:]
        if index + 1 >= len(args) or args[index + 1].startswith("--"):
            flags.add(name)
            index += 1
            continue
        value = args[index + 1]
        index += 2
        if name in ("key", "module", "rule"):
            lists.setdefault(name, []).append(value)
        else:
            values[name] = value
    return values, lists, flags


def main(argv):
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8")
    if len(argv) == 2 and argv[1] in ("--help", "-h"):
        print("Команды: route, find, compare, carry, skipped, queue, summary, decide, import-md, resume, set-context.\n"
              "Подробности: python scripts/sonar_state.py set-context --help; README.md.")
        return
    if len(argv) >= 2 and argv[1] == "set-context":
        parser = argparse.ArgumentParser(prog="sonar_state.py set-context",
            description="Атомарно сохранить проверенный контекст существующего разбора.")
        parser.add_argument("state", help="Путь к существующему файлу состояния")
        parser.add_argument("--repo", required=True, help="Абсолютный путь к репозиторию")
        parser.add_argument("--branch", required=True, help="Текущая ветка")
        parser.add_argument("--target-branch", required=True, help="Подтверждённая целевая ветка")
        parser.add_argument("--target-evidence", help="Проверяемое основание выбора целевой ветки")
        parser.add_argument("--pace", required=True, choices=("fast", "detailed"))
        args = parser.parse_args(argv[2:])
        try:
            set_context(args.state, args.repo, args.branch, args.target_branch, args.pace, args.target_evidence)
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            raise SystemExit("Не удалось сохранить контекст: " + str(exc)) from None
        print(json.dumps({"saved": True}, ensure_ascii=False))
        return
    if len(argv) < 2:
        raise SystemExit(
            "usage: sonar_state.py route|find|compare|carry|skipped|queue|summary|decide|import-md|resume ..."
        )
    command = argv[1]
    if command == "route":
        values, _, _ = pairs(argv[2:])
        print(json.dumps(route(values.get("arg", "")), ensure_ascii=False))
        return
    if command == "find":
        values, _, _ = pairs(argv[2:])
        rows = find_states(values.get("reports", ""), values.get("mode", "any"), values.get("match", ""))
        print(json.dumps({"count": len(rows), "items": rows}, ensure_ascii=False))
        return
    if len(argv) < 3:
        raise SystemExit("state or issues path required")
    path = argv[2]
    if command == "import-md":
        state = import_md(Path(argv[3]).read_text(encoding="utf-8"))
        save(path, state)
        print(json.dumps(summary(state), ensure_ascii=False))
        return
    if command == "compare":
        values, _, flags = pairs(argv[3:])
        report = compare(load(path), values["issues"], values.get("old-issues"))
        if "compact" in flags:
            report.pop("newKeys", None)
            report["unstable"] = len(report["unstable"])
            report["appliedStillOpen"] = len(report["appliedStillOpen"])
        print(json.dumps(report, ensure_ascii=False))
        return
    if command == "carry":
        values, lists, flags = pairs(argv[3:])
        if "answered" not in flags:
            raise SystemExit("refusing carry without --answered after the strategy question")
        new_state = load(values["to"])
        result = carry(
            new_state,
            load(path),
            values["issues"],
            values.get("strategy", "keep"),
            lists.get("module"),
            lists.get("rule"),
            values.get("old-issues"),
            path,
        )
        save(values["to"], new_state)
        print(json.dumps(result, ensure_ascii=False))
        return
    if command == "skipped":
        values, _, _ = pairs(argv[3:])
        print(json.dumps(
            skipped_report(load(path), values["issues"], values.get("module", ""), values.get("rule", "")),
            ensure_ascii=False,
        ))
        return
    if command == "queue":
        values, _, _ = pairs(argv[3:])
        index = json.loads(Path(values["index"]).read_text(encoding="utf-8"))
        print(json.dumps(queue(index, load(path), int(values.get("limit", "8"))), ensure_ascii=False))
        return
    state = load(path)
    if command == "summary":
        print(json.dumps(summary(state), ensure_ascii=False))
        return
    if command == "resume":
        print(json.dumps({
            "state": state.get("state"),
            "currentModule": state.get("currentModule"),
            "repo": state.get("repo"),
            "counts": summary(state)["counts"],
            "next": "survey-remaining" if unfinished(state) else "done",
        }, ensure_ascii=False))
        return
    if command == "decide":
        values, lists, flags = pairs(argv[3:])
        status = values.get("status", "")
        if "answered" not in flags:
            raise SystemExit("refusing decision without --answered after the user question")
        if status == "applied" and "allow-applied" not in flags:
            raise SystemExit("refusing applied without --allow-applied after the code change")
        decide(
            state,
            lists.get("key", []),
            values.get("choice", ""),
            status,
            values.get("note", ""),
            values.get("group"),
        )
        save(path, state)
        print(json.dumps({"saved": len(lists.get("key", [])), "status": status}, ensure_ascii=False))
        return
    raise SystemExit("unknown command")


if __name__ == "__main__":
    main(sys.argv)
