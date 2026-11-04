"""The performance analyser must turn "卡不卡" into a mechanical verdict.

背景：``tools/analyze_performance_log.py`` 原来只出账单（热点、阶段耗时、
GC），"这个会话卡不卡"仍然要人自己读一遍数字再下结论，于是每次讨论都要
重新吵一遍。现在它出 ``verdict: PASS/FAIL`` + ``violations`` 清单，阈值
可以覆盖，并且能跟上一份报告对比（只报**新增**超标项）。
"""
import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest

MODULE_PATH = Path(__file__).resolve().parents[1] / 'tools' / 'analyze_performance_log.py'


def _load_analyser():
    spec = importlib.util.spec_from_file_location(
        'analyze_performance_log', MODULE_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


analyser = _load_analyser()


def _write_log(path, entries):
    with open(path, 'w', encoding='utf-8') as handle:
        for entry in entries:
            handle.write(json.dumps(entry) + '\n')
    return str(path)


def _clean_log(path):
    return _write_log(path, [
        {'event': 'operation', 'name': 'page.open', 'duration_ms': 120,
         'time': 1000.0},
        {'event': 'operation', 'name': 'page.open.canvas_ui', 'duration_ms': 90,
         'time': 1001.0},
        {'event': 'operation', 'name': 'project.sql_read', 'duration_ms': 300,
         'time': 1002.0},
        {'event': 'gc', 'duration_ms': 12, 'time': 1003.0},
        {'event': 'performance', 'time': 1004.0},
    ])


def _stalling_log(path):
    return _write_log(path, [
        {'event': 'operation', 'name': 'page.open', 'duration_ms': 120,
         'time': 1000.0},
        {'event': 'gui_stall', 'phase': 'end', 'duration_ms': 3000,
         'time': 1001.0, 'stack_histogram': [[1, 'scene.py:740 itemsBoundingRect']]},
        {'event': 'slow_input_to_paint', 'latency_ms': 2500, 'time': 1002.0},
        {'event': 'sql.fetch', 'mode': 'execute', 'query': 'SELECT * FROM items',
         'duration_ms': 900, 'time': 1003.0},
        {'event': 'gc', 'duration_ms': 900, 'time': 1004.0},
    ])


def test_a_clean_log_passes(tmp_path):
    result = analyser.analyse([_clean_log(str(tmp_path / 'clean.jsonl'))])

    assert result['verdict'] == 'PASS', result['violations']
    assert result['violations'] == []
    assert result['evidence']['measured_events'] > 0
    assert result['warnings'] == []


def test_a_stalling_log_fails_with_the_reason(tmp_path):
    result = analyser.analyse([_stalling_log(str(tmp_path / 'bad.jsonl'))])

    assert result['verdict'] == 'FAIL'
    checks = {violation['check'] for violation in result['violations']}
    assert 'stall_single_ms' in checks
    assert 'input_to_paint_single_ms' in checks
    assert 'sql_single_ms' in checks
    assert 'gc_single_ms' in checks
    for violation in result['violations']:
        assert violation['value'] > violation['threshold']
        assert violation['detail']


def test_every_threshold_can_be_overridden(tmp_path):
    log = _stalling_log(str(tmp_path / 'bad.jsonl'))

    strict = analyser.analyse([log], thresholds={'stall_single_ms': 100.0})
    assert strict['verdict'] == 'FAIL'

    relaxed = analyser.analyse([log], thresholds={'stall_single_ms': 100000.0})
    assert 'stall_single_ms' not in {
        violation['check'] for violation in relaxed['violations']}


def test_an_unknown_threshold_is_refused():
    with pytest.raises(ValueError, match='unknown threshold'):
        analyser.parse_threshold_overrides(['not_a_threshold=1'])


def test_the_baseline_only_reports_new_violations(tmp_path):
    """跟上次比：老问题不算新问题，新问题必须报出来。"""
    log = _stalling_log(str(tmp_path / 'bad.jsonl'))
    baseline = analyser.analyse([log])
    assert baseline['verdict'] == 'FAIL'

    same = analyser.analyse([log])
    analyser.compare_with_baseline(same, baseline)
    assert same['violations'] == [], '老问题被当成新问题报了一遍'
    assert same['baseline']['carried_over'], '老问题没有被记下来'
    assert same['verdict'] == 'PASS'

    # 同一份日志再加一个新问题：只有新问题该被报出来
    worse = analyser.analyse([log])
    worse['violations'].append({
        'check': 'page_operation_single_ms', 'id': 'page_operation_single_ms:page.open',
        'subject': 'page.open', 'value': 2000, 'threshold': 1500, 'detail': 'new'})
    analyser.compare_with_baseline(worse, baseline)
    assert [v['id'] for v in worse['violations']] == [
        'page_operation_single_ms:page.open']
    assert worse['verdict'] == 'FAIL'


def test_a_log_without_measurements_says_so(tmp_path):
    """没有可测事件的日志**不许**给出"没事"的绿灯。

    素材库压测日志只有 ``material_performance`` 这类事件，判定没有证据
    支撑 —— 这种日志必须把话说清楚，而不是报 PASS。
    """
    log = _write_log(str(tmp_path / 'idle.jsonl'), [
        {'event': 'material_performance', 'records': 5000, 'time': 1000.0},
        {'event': 'material_performance', 'records': 5000, 'time': 1001.0},
    ])
    result = analyser.analyse([log])

    assert result['evidence']['measured_events'] == 0
    assert result['warnings'], '没有证据的日志必须给出警告'
    assert 'PASS' in result['verdict']


def test_the_exit_code_is_one_when_the_verdict_fails(tmp_path):
    """退出码：PASS=0，FAIL=1 —— 可以直接进 CI。"""
    clean = tmp_path / 'clean.jsonl'
    bad = tmp_path / 'bad.jsonl'
    _clean_log(str(clean))
    _stalling_log(str(bad))

    passed = subprocess.run(
        [sys.executable, str(MODULE_PATH), str(clean), '--output',
         str(tmp_path / 'clean.json')],
        capture_output=True, text=True)
    assert passed.returncode == 0, passed.stderr

    failed = subprocess.run(
        [sys.executable, str(MODULE_PATH), str(bad), '--output',
         str(tmp_path / 'bad.json')],
        capture_output=True, text=True)
    assert failed.returncode == 1, failed.stderr


def test_the_report_can_be_written_and_read_back(tmp_path):
    log = _stalling_log(str(tmp_path / 'bad.jsonl'))
    out = tmp_path / 'report.json'
    result = subprocess.run(
        [sys.executable, str(MODULE_PATH), log, '--output', str(out)],
        capture_output=True, text=True)
    assert result.returncode == 1

    report = json.loads(out.read_text(encoding='utf-8'))
    assert report['verdict'] == 'FAIL'
    assert report['violations']
    assert report['metrics']['stall_max_ms'] == 3000
