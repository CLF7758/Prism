"""跑测试，但保证不会卡住。

本机跑全量测试会在 tests/test_view.py 附近停住不动（跑到 73% 就没了），
而且卡住的进程不会自己退出。这个脚本做两件事：

  1. 每个文件（或每个测试）独立进程跑，带硬超时，超时就杀
  2. 进度**直接写进结果文件并立即 flush**，不看 stdout —— 后台跑的时候
     stdout 经常一点都看不到，写文件才可靠

用法：
    python tools/run_tests_safe.py                    # 全部文件，每个 120 秒
    python tools/run_tests_safe.py tests/test_view.py
    python tools/run_tests_safe.py tests/test_view.py --per-test
    python tools/run_tests_safe.py --timeout 60

结果写在 tools/test_report.txt，随时可以读，不用等它跑完。
"""
import argparse
import json
from pathlib import Path
import xml.etree.ElementTree as ET
import os
import subprocess
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PY = sys.executable
PLATFORM = 'offscreen'
RESULT_DIR = None
RESULTS = []

#: 默认报告按时间戳命名。写死成一个固定的名字（以前是 test_report.txt）
#: 会让并发跑测试的几个人互相覆盖 —— T3 就踩到了这个。
REPORT = os.path.join(ROOT, 'output', 'test_reports',
                      time.strftime('run-%m%d-%H%M%S.txt'))


def write(line=''):
    """写一行到报告文件，立刻落盘 —— 后台跑的时候 stdout 靠不住。"""
    os.makedirs(os.path.dirname(REPORT), exist_ok=True)
    with open(REPORT, 'a', encoding='utf-8') as handle:
        handle.write(line + '\n')
        handle.flush()
        os.fsync(handle.fileno())


def collect_files(targets):
    if targets:
        return [t if os.path.isabs(t) else os.path.join(ROOT, t)
                for t in targets]
    found = []
    for base, dirs, names in os.walk(os.path.join(ROOT, 'tests')):
        dirs[:] = [d for d in dirs if d != '__pycache__']
        for name in sorted(names):
            if name.startswith('test_') and name.endswith('.py'):
                found.append(os.path.join(base, name))
    return sorted(found)


def collect_test_ids(path, timeout):
    """列出文件里的测试 ID，用于逐个跑。"""
    try:
        proc = subprocess.run(
            [PY, '-u', '-m', 'pytest', path, '--collect-only', '-q',
             '-p', 'no:cacheprovider'],
            cwd=ROOT, env=test_env(), capture_output=True, text=True,
            encoding='utf-8', errors='replace', timeout=timeout)
    except subprocess.TimeoutExpired:
        return None
    ids = []
    for line in (proc.stdout or '').splitlines():
        line = line.strip()
        if '::' in line and not line.startswith('<'):
            ids.append(line)
    return ids


def test_env():
    env = dict(os.environ)
    env['QT_QPA_PLATFORM'] = PLATFORM
    env['PYTHONIOENCODING'] = 'utf-8'
    env['PYTHONUNBUFFERED'] = '1'
    return env


def run_one(target, timeout):
    """Bound each test file, retain detailed evidence, count crashes as failures."""
    name = target.replace('::', '__').replace('/', '_').replace(chr(92), '_')
    xml_path = Path(RESULT_DIR)/(name + '.xml') if RESULT_DIR else None
    cmd = [PY, '-u', '-m', 'pytest', target, '-v', '--tb=short',
           '-o', 'faulthandler_timeout=25', '-p', 'no:cacheprovider']
    if xml_path:
        cmd.append('--junitxml=' + str(xml_path))
    started = time.perf_counter()
    proc = subprocess.Popen(cmd, cwd=ROOT, env=test_env(), stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT, text=True, encoding='utf-8', errors='replace')
    try:
        out, _ = proc.communicate(timeout=timeout)
        timed_out = False
    except subprocess.TimeoutExpired:
        # Kill only this test process tree, including the Windows venv launcher child.
        if os.name == 'nt':
            subprocess.run(['taskkill', '/PID', str(proc.pid), '/T', '/F'],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        else:
            proc.kill()
        out, _ = proc.communicate()
        timed_out = True
    result = dict(target=target, seconds=round(time.perf_counter()-started, 3),
                  returncode=proc.returncode, timed_out=timed_out)
    if xml_path and xml_path.exists():
        root = ET.parse(xml_path).getroot()
        suites = [root] if root.tag == 'testsuite' else list(root.iter('testsuite'))
        for key in ('tests', 'failures', 'errors', 'skipped'):
            result[key] = sum(int(s.get(key, 0)) for s in suites)
        result['passed'] = result['tests']-result['failures']-result['errors']-result['skipped']
    else:
        result['partial_passed'] = sum(line.startswith('tests/') and ' PASSED ' in line
                                      for line in out.splitlines())
    result['status'] = 'HANG' if timed_out else 'PASS' if proc.returncode == 0 else 'FAIL'
    RESULTS.append(result)
    if RESULT_DIR:
        Path(RESULT_DIR, name+'.log').write_text(out, encoding='utf-8')
        Path(RESULT_DIR, 'summary.json').write_text(json.dumps(RESULTS, indent=2), encoding='utf-8')
    if timed_out:
        return 'HANG', f'timeout after {timeout}s', out[-4000:]
    summary = next((line.strip() for line in reversed(out.splitlines())
                    if ' passed' in line or ' failed' in line or ' error' in line), '')
    if proc.returncode == 0:
        return 'PASS', summary, out
    return 'FAIL', summary + f' [exit {proc.returncode}]', out


def main():
    global REPORT, PLATFORM, RESULT_DIR

    parser = argparse.ArgumentParser()
    parser.add_argument('targets', nargs='*')
    parser.add_argument('--timeout', type=int, default=120)
    parser.add_argument('--platform', choices=['windows', 'offscreen'], default='offscreen')
    parser.add_argument('--report', default=REPORT,
                        help='报告写到哪（默认 tools/test_report.txt）')
    parser.add_argument('--per-test', action='store_true',
                        help='卡住的文件再一个个测试跑，精确定位')
    args = parser.parse_args()

    REPORT = args.report
    PLATFORM = args.platform
    RESULT_DIR = str(Path(REPORT).with_suffix(''))
    Path(RESULT_DIR).mkdir(parents=True, exist_ok=True)
    os.makedirs(os.path.dirname(REPORT), exist_ok=True)
    open(REPORT, 'w', encoding='utf-8').close()
    files = collect_files(args.targets)
    write('=' * 70)
    write(f'测试报告  {time.strftime("%Y-%m-%d %H:%M:%S")}')
    write(f'共 {len(files)} 个文件，每个限时 {args.timeout} 秒')
    write('=' * 70)
    write()

    hangs, failures, abnormal_exits = [], [], []
    passed_total = failed_total = 0

    for path in files:
        rel = os.path.relpath(path, ROOT)
        start = time.time()
        status, summary, out = run_one(rel, args.timeout)
        elapsed = time.time() - start

        if status == 'HANG':
            hangs.append(rel)
            write(f'[卡住] {rel}  >{args.timeout}s')
            if out:
                write(f'        卡住前的输出: {out.strip()[-200:]}')
            if args.per_test:
                write('        逐个测试定位中…')
                ids = collect_test_ids(rel, args.timeout)
                if ids is None:
                    write('        连收集清单都卡住了（收集阶段就停）')
                else:
                    for test_id in ids:
                        sub_status, sub_summary, _ = run_one(
                            test_id, args.timeout)
                        if sub_status == 'HANG':
                            write(f'        ★ 卡住的是: {test_id}')
                            hangs.append(test_id)
                        elif sub_status == 'FAIL':
                            failures.append(f'{test_id} :: {sub_summary}')
            continue

        write(f'[{status}] {rel}  {summary}  ({elapsed:.1f}s)')
        if status == 'PASS*':
            abnormal_exits.append(rel)
        if status == 'FAIL':
            failures.append(f'{rel} :: {summary or "process failed"}')
            for line in out.splitlines():
                if line.startswith('FAILED') or '.py:' in line:
                    failures.append(f'{rel} :: {line.strip()[:150]}')
            if summary:
                head = summary.split(' ')[0]
                if head.isdigit():
                    failed_total += int(head)
        else:
            if summary:
                head = summary.split(' ')[0]
                if head.isdigit():
                    passed_total += int(head)

    passed_total = sum(r.get('passed', 0) for r in RESULTS)
    failed_total = sum(r.get('failures', 0)+r.get('errors', 0) for r in RESULTS)
    write()
    write('=' * 70)
    write(f'合计 {passed_total} 过 / {failed_total} 败')
    write(f'卡住 {len(hangs)} 个:')
    for item in hangs:
        write(f'    {item}')
    write(f'失败 {len(failures)} 条:')
    for item in failures:
        write(f'    {item}')
    write('=' * 70)
    for item in abnormal_exits:
        write(f'异常退出: {item}')
    return 1 if hangs or failures or abnormal_exits else 0


if __name__ == '__main__':
    sys.exit(main())
