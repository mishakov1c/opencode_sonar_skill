#!/usr/bin/env python3
"""Изолированные проверки измерителя; реальные экспорты не требуются."""
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

SCRIPT = Path(__file__).with_name("sonar_measure.py")


class MeasureTests(unittest.TestCase):
    def test_ranges_counters_and_visible_overlaps(self):
        def message(path, output, tokens):
            return {"info": {"role": "assistant", "tokens": tokens}, "parts": [{"type": "tool", "tool": "read",
                    "state": {"input": {"filePath": path}, "output": output}}]}
        export = {"messages": [
            message("C:/x.bsl", "1: a\n2: b\n", {"input": 4, "output": 2, "cache": {"read": 3, "write": 1}}),
            {"info": {"role": "user"}, "parts": []},
            message("C:/x.bsl", "2: b\n3: c\n", {"input": 7, "output": 1, "cache": {"read": 5}})]}
        with tempfile.TemporaryDirectory() as folder:
            source = Path(folder) / "export.json"
            output = Path(folder) / "report.json"
            source.write_text(json.dumps(export), encoding="utf-8")
            command = [sys.executable, str(SCRIPT), str(source), "--main", "0:1", "--output", str(output)]
            proc = subprocess.run(command, capture_output=True, text=True, encoding="utf-8")
            self.assertEqual(proc.returncode, 0, proc.stderr)
            compact = json.loads(proc.stdout)
            report = json.loads(output.read_text(encoding="utf-8"))
            self.assertEqual(compact["groups"]["main"]["tool_calls"], 1)
            self.assertEqual(report["groups"]["main"]["tokens"]["cache_write"], 1)
            self.assertEqual(report["groups"]["other"]["tokens"]["input"], 7)
            self.assertEqual(report["read_overlaps"][0]["identical_lines"], 1)
            before = output.read_bytes()
            self.assertNotEqual(subprocess.run(command, capture_output=True).returncode, 0)
            self.assertEqual(output.read_bytes(), before)


if __name__ == "__main__":
    unittest.main()
