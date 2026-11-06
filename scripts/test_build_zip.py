#!/usr/bin/env python3
"""Check installation archive isolation and extracted CLI operation."""
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from zipfile import ZipFile

ROOT = Path(__file__).resolve().parent.parent
RUNTIME = ('SKILL.md', 'README.md', 'scripts/sonar_index.py',
           'scripts/sonar_ingest.py', 'scripts/sonar_state.py')


class BuildTests(unittest.TestCase):
    def test_archive_excludes_local_data_and_runs_after_extraction(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / 'source'
            (root / 'scripts').mkdir(parents=True)
            for name in (*RUNTIME, 'scripts/build_zip.py'):
                shutil.copy2(ROOT / name, root / name)
            for name in ('session-export.json', 'review-state.json', '.git/config',
                         'sonar-reports/issues.json', 'scripts/test_private.py',
                         'scripts/__pycache__/private.pyc', '.env'):
                path = root / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text('LOCAL DATA MUST NOT BE PACKAGED', encoding='utf-8')
            output = Path(directory) / 'skill.zip'
            subprocess.run([sys.executable, str(root / 'scripts/build_zip.py'),
                            '--output', str(output)], check=True, capture_output=True)
            install = Path(directory) / 'other user установка'
            with ZipFile(output) as archive:
                self.assertIsNone(archive.testzip())
                self.assertEqual(set(archive.namelist()), {'sonar/' + name for name in RUNTIME})
                for name in RUNTIME:
                    self.assertEqual(archive.read('sonar/' + name), (ROOT / name).read_bytes())
                archive.extractall(install)
            scripts = install / 'sonar/scripts'
            route = subprocess.run([sys.executable, str(scripts / 'sonar_state.py'),
                                    'route', '--arg', 'detailed'], cwd=directory,
                                   check=True, capture_output=True, text=True, encoding='utf-8')
            self.assertEqual(json.loads(route.stdout)['pace'], 'detailed')
            result = subprocess.run([sys.executable, str(scripts / 'sonar_index.py'),
                                     'api-url', 'https://sonar.example/project/issues?id=demo'],
                                    cwd=directory, check=True, capture_output=True, text=True)
            self.assertEqual(result.stdout.splitlines()[-1], 'WAITING_FOR_USER_JSON')
            subprocess.run([sys.executable, str(scripts / 'sonar_ingest.py'), '--help'],
                           cwd=directory, check=True, capture_output=True)


if __name__ == '__main__':
    unittest.main()
