"""回归跑批：逐文件跑 pytest，遇挂死就停下并报出是哪一个文件。

用法：
    $env:PYTHONIOENCODING='utf-8'
    $env:QT_QPA_PLATFORM='offscreen'
    .\\.venv\\Scripts\\python.exe tests/_audit_run_suite.py <file1> <file2> ...
"""
from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PYTHON = ROOT / '.venv' / 'Scripts' / 'python.exe'
TIMEOUT = int(os.environ.get('SUITE_TIMEOUT', '240'))


def main() -> int:
    files = sys.argv[1:]
    env = dict(os.environ)
    env['PYTHONIOENCODING'] = 'utf-8'
    env['QT_QPA_PLATFORM'] = 'offscreen'
    failed = []
    for name in files:
        path = Path(name)
        if not path.is_absolute():
            path = ROOT / 'tests' / name
        started = time.perf_counter()
        try:
            proc = subprocess.run(
                [str(PYTHON), '-m', 'pytest', str(path), '-q', '--no-header',
                 '-p', 'no:cacheprovider', '--no-cov'],
                cwd=str(ROOT), env=env, capture_output=True, text=True,
                encoding='utf-8', errors='replace', timeout=TIMEOUT)
        except subprocess.TimeoutExpired:
            print(f'HANG  {path.name}  (> {TIMEOUT}s)', flush=True)
            failed.append((path.name, 'HANG'))
            break
        elapsed = time.perf_counter() - started
        tail = [line for line in (proc.stdout or '').splitlines() if line.strip()][-1:]
        print(f'{"FAIL" if proc.returncode else "ok  "}  {path.name:<52} '
              f'{elapsed:6.1f}s  {tail[0][:110] if tail else ""}', flush=True)
        if proc.returncode:
            failed.append((path.name, (proc.stdout or '')[-1800:]))
    print('\n===== SUMMARY =====')
    for name, detail in failed:
        print(f'--- {name} ---')
        print(detail)
    return 1 if failed else 0


if __name__ == '__main__':
    raise SystemExit(main())
