#!/usr/bin/env python3
"""Сборка переносимого скилла по явному списку, без локальных данных."""
import argparse
from pathlib import Path
from zipfile import ZipFile, ZIP_DEFLATED

FILES = ('SKILL.md', 'README.md', 'scripts/sonar_index.py', 'scripts/sonar_state.py',
         'scripts/sonar_ingest.py')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', required=True)
    args = parser.parse_args()
    root = Path(__file__).resolve().parent.parent
    out = Path(args.output).resolve()
    if not out.parent.is_dir():
        parser.error('Родительский каталог архива не существует.')
    with ZipFile(out, 'w', ZIP_DEFLATED) as archive:
        for name in FILES:
            archive.write(root / name, 'sonar/' + name)
    with ZipFile(out) as archive:
        assert archive.testzip() is None
        assert set(archive.namelist()) == {'sonar/' + name for name in FILES}
    print('Архив собран и проверен: ' + str(out) + '; файлов: ' + str(len(FILES)))


if __name__ == '__main__':
    main()
