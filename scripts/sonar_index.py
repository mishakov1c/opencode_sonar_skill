#!/usr/bin/env python3
"""Compact SonarQube issue index. Python standard library only."""

import hashlib
import json
import sys
from collections import OrderedDict
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

KEEP = (
    "componentKeys",
    "projects",
    "pullRequest",
    "branch",
    "resolved",
    "resolutions",
    "statuses",
    "issueStatuses",
    "severities",
    "types",
    "rules",
    "tags",
    "assigned",
    "assignees",
    "author",
    "authors",
    "createdAfter",
    "createdBefore",
    "createdInLast",
    "files",
    "directories",
    "scopes",
    "languages",
    "inNewCodePeriod",
    "sinceLeakPeriod",
    "onComponentOnly",
    "fixedInPullRequest",
    "cleanCodeAttributeCategories",
    "impactSeverities",
    "impactSoftwareQualities",
    "codeVariants",
    "prioritizedRule",
    "cwe",
    "owaspTop10",
    "sansTop25",
    "sonarsourceSecurity",
)
DROP = {"facets", "s", "asc", "open", "myIssues", "selected", "line", "id"}
DOC_RULES = {
    "bsl-language-server:MissingParameterDescription",
    "bsl-language-server:MissingReturnedValueDescription",
}


def api_url(source):
    parts = urlsplit(source.strip())
    if not parts.scheme or not parts.netloc:
        raise SystemExit("URL must include scheme and host")
    pairs = parse_qsl(parts.query, keep_blank_values=True)
    has_keys = any(key == "componentKeys" for key, _ in pairs)
    kept = []
    page_size = None
    page = None
    for key, value in pairs:
        if key == "id" and not has_keys:
            key = "componentKeys"
        if key == "ps":
            page_size = value
            continue
        if key == "p":
            page = value
            continue
        if key in DROP or key not in KEEP:
            continue
        kept.append((key, value))
    if page_size is None or int(page_size) > 100:
        page_size = "100"
    if not page:
        page = "1"
    query = urlencode(kept + [("ps", page_size), ("p", page)], safe=",")
    return urlunsplit((parts.scheme, parts.netloc, "/api/issues/search", query, ""))


def short_path(component):
    path = component.split(":", 1)[1] if ":" in component else component
    return path.replace("\\", "/")


def load_pages(paths):
    issues = []
    total = None
    errors = []
    for path in paths:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        if isinstance(data, dict) and data.get("errors"):
            errors.extend(data["errors"])
            continue
        if not isinstance(data, dict) or "issues" not in data or "paging" not in data:
            raise SystemExit("JSON has no issues/paging: " + path)
        page_total = data.get("total", data["paging"].get("total"))
        if total is None:
            total = page_total
        elif total != page_total:
            raise SystemExit("page totals differ")
        if data["paging"].get("pageSize", 0) > 100:
            raise SystemExit("pageSize exceeds 100")
        issues.extend(data["issues"])
    if errors:
        raise SystemExit("API errors: " + json.dumps(errors, ensure_ascii=False))
    return issues, total


def suggest_groups(rows):
    groups = []
    by_line = OrderedDict()
    for row in rows:
        by_line.setdefault((row["file"], row["line"]), []).append(row)
    for (path, line), bucket in by_line.items():
        rules = {item["rule"] for item in bucket}
        if DOC_RULES <= rules:
            groups.append({
                "reason": "one-function-doc",
                "file": path,
                "line": line,
                "keys": [item["key"] for item in bucket if item["rule"] in DOC_RULES],
            })
    return groups


def build_index(issues, total):
    rows = []
    for issue in issues:
        path = short_path(issue.get("component", ""))
        rows.append({
            "key": issue.get("key"),
            "rule": issue.get("rule"),
            "severity": issue.get("severity"),
            "file": path,
            "line": issue.get("line") or 0,
            "message": issue.get("message", ""),
        })
    rows.sort(key=lambda item: (item["file"].lower(), item["line"], item["key"]))
    modules = []
    current = None
    for row in rows:
        if current is None or current["file"] != row["file"]:
            current = {"file": row["file"], "issues": []}
            modules.append(current)
        current["issues"].append(row)
    return {
        "received": len(rows),
        "total": total,
        "complete": total == len(rows),
        "modules": modules,
        "groups": suggest_groups(rows),
    }


def source_id(url):
    return hashlib.sha1(url.encode("utf-8")).hexdigest()[:8]


def main(argv):
    if len(argv) < 2:
        raise SystemExit("usage: sonar_index.py api-url|index|stats|order|module ...")
    command = argv[1]
    if command == "api-url":
        print(api_url(argv[2]))
        print("WAITING_FOR_USER_JSON")
        return
    if command == "source-id":
        print(source_id(argv[2]))
        return
    if command == "index":
        out = None
        paths = []
        args = argv[2:]
        while args:
            if args[0] == "-o":
                out = args[1]
                args = args[2:]
            else:
                paths.append(args[0])
                args = args[1:]
        issues, total = load_pages(paths)
        payload = build_index(issues, total)
        text = json.dumps(payload, ensure_ascii=False, indent=2)
        if out:
            Path(out).write_text(text, encoding="utf-8")
        else:
            print(text)
        return
    index = json.loads(Path(argv[2]).read_text(encoding="utf-8"))
    if command == "stats":
        severities = {}
        for module in index["modules"]:
            for issue in module["issues"]:
                severity = issue.get("severity") or "?"
                severities[severity] = severities.get(severity, 0) + 1
        print(json.dumps({
            "received": index.get("received"),
            "total": index.get("total"),
            "complete": index.get("complete"),
            "modules": len(index.get("modules", [])),
            "severities": severities,
            "groups": len(index.get("groups", [])),
        }, ensure_ascii=False))
        return
    if command == "order":
        for number, module in enumerate(index["modules"], 1):
            print("{}\t{}\t{}".format(number, len(module["issues"]), module["file"]))
        return
    if command == "module":
        needle = argv[3].replace("\\", "/").lower()
        for module in index["modules"]:
            if needle in module["file"].lower():
                print(json.dumps(module, ensure_ascii=False, indent=2))
                return
        raise SystemExit("module not found")
    raise SystemExit("unknown command")


if __name__ == "__main__":
    main(sys.argv)
