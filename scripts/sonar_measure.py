#!/usr/bin/env python3
"""Локальная диагностика экспорта OpenCode; границы основного разбора задаются явно."""
import argparse
from collections import Counter, defaultdict
import json
import os
from pathlib import Path
import re
import sys


def measure(data, ranges):
    messages = data.get("messages")
    if not isinstance(messages, list):
        raise ValueError("В экспорте отсутствует массив messages.")
    if not ranges or any(a < 0 or b < a or b >= len(messages) for a, b in ranges):
        raise ValueError("Границы основного разбора выходят за пределы массива messages.")
    indices = [i for a, b in ranges for i in range(a, b + 1)]
    if len(indices) != len(set(indices)):
        raise ValueError("Диапазоны основного разбора пересекаются.")
    main = set(indices)
    groups = {name: {"tokens": Counter(), "tools": Counter(), "tool_output_chars": 0,
                     "tool_output_utf8_bytes": 0, "assistant_messages": 0, "reads": []}
              for name in ("main", "other")}
    previous = defaultdict(list)
    overlaps = []
    for index, message in enumerate(messages):
        group = "main" if index in main else "other"
        result = groups[group]
        info = message.get("info", {})
        if info.get("role") != "assistant":
            continue
        result["assistant_messages"] += 1
        tokens = info.get("tokens") or {}
        for key in ("input", "output", "reasoning", "total"):
            result["tokens"][key] += tokens.get(key, 0) or 0
        for key in ("read", "write"):
            result["tokens"]["cache_" + key] += (tokens.get("cache") or {}).get(key, 0) or 0
        for part in message.get("parts", []):
            if part.get("type") != "tool":
                continue
            tool = part.get("tool", "?")
            state = part.get("state") or {}
            arguments = state.get("input") or {}
            output = state.get("output") or ""
            result["tools"][tool] += 1
            result["tool_output_chars"] += len(output)
            result["tool_output_utf8_bytes"] += len(output.encode("utf-8"))
            if tool != "read" or not arguments.get("filePath"):
                continue
            path = arguments["filePath"]
            numbered = {int(n): text for n, text in re.findall(r"(?m)^(\d+): (.*)$", output)}
            entry = {"message": index, "path": path, "offset": arguments.get("offset", 1),
                     "limit": arguments.get("limit"), "observed_lines": len(numbered)}
            result["reads"].append(entry)
            key = os.path.normcase(path.replace("\\", "/")).casefold()
            for old_index, old_group, old_lines in previous[key]:
                common = numbered.keys() & old_lines.keys()
                if common:
                    overlaps.append({"path": path, "earlier_message": old_index, "later_message": index,
                                     "earlier_group": old_group, "later_group": group,
                                     "overlapping_lines": len(common),
                                     "identical_lines": sum(numbered[n] == old_lines[n] for n in common)})
            previous[key].append((index, group, numbered))
    for result in groups.values():
        result["tokens"] = dict(result["tokens"])
        result["tools"] = dict(result["tools"])
        result["tool_calls"] = sum(result["tools"].values())
    return {"main_ranges_inclusive_zero_based": ranges, "groups": groups, "read_overlaps": overlaps,
            "limitations": ["Границы задаются пользователем; other включает все сообщения вне них.",
                            "Пересечения определены только по видимым пронумерованным строкам read; "
                            "отсутствие изменений между чтениями не доказано.",
                            "Токены суммированы по записям ассистента; стоимость и уникальный контекст не вычисляются."]}


def parse_range(value):
    try:
        first, last = value.split(":", 1)
        return int(first), int(last)
    except ValueError:
        raise argparse.ArgumentTypeError("Нужен диапазон START:END, номера с нуля, включая END.") from None


def main():
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("export", help="Путь к локальному JSON-экспорту")
    parser.add_argument("--main", action="append", required=True, type=parse_range,
                        help="Диапазон сообщений основного разбора START:END включительно; можно повторить")
    parser.add_argument("--output", required=True, help="Путь к новому файлу полного отчёта")
    args = parser.parse_args()
    try:
        export = Path(args.export).resolve()
        output = Path(args.output).resolve()
        if output == export or output.exists():
            raise ValueError("Выходной файл уже существует либо совпадает с экспортом.")
        if not output.parent.is_dir():
            raise ValueError("Каталог выходного файла не существует.")
        report = measure(json.loads(export.read_text(encoding="utf-8-sig")), args.main)
        # Создание без перезаписи: даже при конкурентном запуске существующий файл не изменится.
        with output.open("x", encoding="utf-8", newline="\n") as stream:
            json.dump(report, stream, ensure_ascii=False, indent=2)
        compact = {name: {"tokens": row["tokens"], "tool_calls": row["tool_calls"],
                          "tool_output_chars": row["tool_output_chars"], "reads": len(row["reads"])}
                   for name, row in report["groups"].items()}
        print(json.dumps({"groups": compact, "overlapping_reads": len(report["read_overlaps"]),
                          "report": str(output)}, ensure_ascii=False))
    except (OSError, ValueError, TypeError) as exc:
        parser.exit(1, "Ошибка анализа: " + str(exc) + "\n")


if __name__ == "__main__":
    main()
