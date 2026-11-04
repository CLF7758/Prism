"""Material feature regression checklist.

The 2026-10-22 handoff lists "the material features must not have regressed"
as one of the few acceptance items that never got evidence: the performance
work touched the catalog, the asset browser and the shared byte budget, and
the material library sits on top of exactly those pieces.

This script turns that line into a repeatable checklist.  It runs the
material test files that already exist in the repository -- one file per user
visible capability -- plus the real-asset round trip that uses the user's own
textures when they are present, and writes a per-feature verdict.

    python tools/material_regression.py --out artifacts/scale-acceptance-20261022

The report is deliberately not a "tests passed" boast: every row names the
capability, the file that covers it, how many cases ran, how many were
skipped (the real-asset round trip skips itself when the user's textures are
not on this machine) and how long the file took.
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
import xml.etree.ElementTree as ET
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

#: file name -> the capability it is the evidence for.
FEATURES = {
    'test_material_service.py': '材质记录服务：创建/读取/更新/校验',
    'test_material_records.py': '材质记录与导入数据',
    'test_material_slots.py': '槽位绑定与通道映射',
    'test_material_features.py': '材质功能面板',
    'test_material_cards.py': '材质卡片渲染与选择',
    'test_material_view_controls.py': '视图控件（列表/卡片/尺寸）',
    'test_material_details.py': '材质详情检视器',
    'test_material_lifecycle.py': '材质生命周期（新增/删除/重命名）',
    'test_material_auto.py': '材质自动流程',
    'test_material_library_entry.py': '材质库入口',
    'test_material_global_search.py': '全局搜索中的材质',
    'test_material_query_navigation.py': '材质查询与导航',
    'test_material_navigation_async.py': '材质导航异步加载',
    'test_material_canvas_context.py': '画布上的材质右键操作',
    'test_material_selection_export.py': '材质选择与导出',
    'test_material_texture_drag.py': '贴图拖拽',
    'test_material_adjustment_reset.py': '调整参数重置',
    'test_material_menu_save_regression.py': '菜单保存回归',
    'test_material_performance.py': '材质性能（300 材质）',
    '_verify_material_real_assets.py': '真实材质原件往返（用户自己的贴图）',
}


def collect_files():
    tests = ROOT / 'tests'
    files = sorted(tests.glob('test_material*.py'))
    real = tests / '_verify_material_real_assets.py'
    if real.exists():
        files.append(real)
    return files


def run(files, out_dir, timeout=3600):
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    junit = out_dir / 'material-junit.xml'
    command = [sys.executable, '-m', 'pytest', '-q', '-p', 'no:cacheprovider',
               '--junitxml', str(junit)] + [str(path) for path in files]
    print('RUN', ' '.join(command), flush=True)
    started = time.perf_counter()
    completed = subprocess.run(command, cwd=ROOT, capture_output=True, text=True,
                               timeout=timeout, encoding='utf-8', errors='replace')
    elapsed = time.perf_counter() - started
    (out_dir / 'material-pytest.log').write_text(
        (completed.stdout or '') + '\n' + (completed.stderr or ''), encoding='utf-8')
    return completed.returncode, elapsed, junit


def parse_junit(junit):
    """Per-file outcome from the pytest junit report."""
    if not Path(junit).exists():
        return []
    root = ET.parse(junit).getroot()
    suites = root.findall('testsuite') or [root]
    rows = []
    for suite in suites:
        for case in suite.findall('testcase'):
            file_name = Path(case.get('classname', '') or '').name
            if not file_name:
                file_name = Path(case.get('name', '')).name
            outcome = 'passed'
            if case.find('failure') is not None:
                outcome = 'failed'
            elif case.find('error') is not None:
                outcome = 'error'
            elif case.find('skipped') is not None:
                outcome = 'skipped'
            rows.append({'file': file_name, 'name': case.get('name', ''),
                         'time': float(case.get('time', 0) or 0),
                         'outcome': outcome})
    return rows


def summarise(rows):
    per_file = {}
    for row in rows:
        entry = per_file.setdefault(row['file'], {'tests': 0, 'passed': 0, 'failed': 0,
                                                  'error': 0, 'skipped': 0, 'seconds': 0.0})
        entry['tests'] += 1
        entry['seconds'] = round(entry['seconds'] + row['time'], 2)
        if row['outcome'] in entry:
            entry[row['outcome']] += 1
    return per_file


def verdict(entry):
    if entry is None:
        return '未覆盖', '没有对应的测试文件'
    if entry['failed'] or entry['error']:
        return '已退化', f"{entry['failed']} 失败 / {entry['error']} 错误"
    if entry['skipped'] == entry['tests']:
        return '已跳过', '全部用例被跳过（通常是缺少用户素材）'
    if entry['skipped']:
        return '通过', f"{entry['skipped']} 项被跳过"
    return '通过', ''


def build_report(exit_code, elapsed, rows, per_file, files):
    checklist = []
    for path in files:
        name = path.name
        entry = per_file.get(name)
        state, note = verdict(entry)
        checklist.append({
            'feature': FEATURES.get(name, name),
            'file': f'tests/{name}',
            'state': state,
            'note': note,
            'tests': (entry or {}).get('tests', 0),
            'passed': (entry or {}).get('passed', 0),
            'failed': (entry or {}).get('failed', 0),
            'error': (entry or {}).get('error', 0),
            'skipped': (entry or {}).get('skipped', 0),
            'seconds': (entry or {}).get('seconds', 0.0),
        })
    covered = [row for row in checklist if row['state'] != '未覆盖']
    regressed = [row for row in checklist if row['state'] == '已退化']
    return {
        'pytest_exit_code': exit_code,
        'seconds': round(elapsed, 2),
        'files': len(files),
        'features_covered': len(covered),
        'features_regressed': len(regressed),
        'features_skipped': len([row for row in checklist if row['state'] == '已跳过']),
        'total_tests': sum(row['tests'] for row in checklist),
        'total_passed': sum(row['passed'] for row in checklist),
        'total_failed': sum(row['failed'] for row in checklist),
        'total_error': sum(row['error'] for row in checklist),
        'total_skipped': sum(row['skipped'] for row in checklist),
        'checklist': checklist,
    }


def render_markdown(report):
    lines = ['# 材质功能逐项不退化清单', '']
    lines.append(f"- pytest 退出码：{report['pytest_exit_code']}")
    lines.append(f"- 用例总数：{report['total_tests']}"
                 f"（通过 {report['total_passed']} / 失败 {report['total_failed']}"
                 f" / 错误 {report['total_error']} / 跳过 {report['total_skipped']}）")
    lines.append(f"- 覆盖能力项：{report['features_covered']}"
                 f"，已退化 {report['features_regressed']}"
                 f"，跳过 {report['features_skipped']}")
    lines.append(f"- 耗时：{report['seconds']} s")
    lines.append('')
    lines.append('| 能力项 | 证据文件 | 结论 | 用例 | 通过 | 失败 | 跳过 | 耗时(s) | 说明 |')
    lines.append('| --- | --- | --- | --- | --- | --- | --- | --- | --- |')
    for row in report['checklist']:
        lines.append(f"| {row['feature']} | `{row['file']}` | {row['state']} | {row['tests']} | "
                     f"{row['passed']} | {row['failed'] + row['error']} | {row['skipped']} | "
                     f"{row['seconds']} | {row['note']} |")
    lines.append('')
    return '\n'.join(lines) + '\n'


def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--out', type=Path, required=True)
    parser.add_argument('--timeout', type=int, default=3600)
    args = parser.parse_args()

    files = collect_files()
    if not files:
        raise SystemExit('no material test files found')
    exit_code, elapsed, junit = run(files, args.out, timeout=args.timeout)
    rows = parse_junit(junit)
    per_file = summarise(rows)
    report = build_report(exit_code, elapsed, rows, per_file, files)
    (Path(args.out) / 'material-regression.json').write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    (Path(args.out) / 'material-regression.md').write_text(
        render_markdown(report), encoding='utf-8')
    print(json.dumps({key: value for key, value in report.items() if key != 'checklist'},
                     ensure_ascii=False, indent=2))
    return 0 if report['features_regressed'] == 0 else 1


if __name__ == '__main__':
    sys.exit(main())
