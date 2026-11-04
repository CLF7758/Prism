"""D3: produce the evidence a diagnostic bundle has to carry.

Three outputs, all under ``artifacts/model-library-acceptance/d3/``:

* ``bundle.zip`` -- a real diagnostic bundle of a real (synthetic but
  genuinely recorded) task, plus ``problem-index.json``: a number -> event
  chain map, so "问题 P-2 停在哪一步" is answered by the bundle itself.
* ``overhead.json`` -- the same workload with the trace disabled, enabled,
  and enabled with a diagnostic snapshot taken: median and P95 per operation.
* ``event-contract.md`` -- the "which event, which fields" table generated
  from the live ``model_event_contract()``, so it cannot drift from the code.

    .venv/Scripts/python.exe tests/model_library/acceptance/diagnostics_evidence.py

The script never opens a project, never reads a model byte and never touches
the user's settings, clipboard or capture service. It runs offscreen.
"""
from __future__ import annotations

import json
import os
import statistics
import sys
import time
import zipfile
from datetime import datetime, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.abspath(os.path.join(HERE, '..', '..', '..'))
OUT = os.path.join(REPO, 'artifacts', 'model-library-acceptance', 'd3')
sys.path.insert(0, REPO)

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

from prism.model_library import events  # noqa: E402

JOB = dict(
    project_session='sess-7f3a', open_epoch=3, model_id='model-armchair',
    request_id='req-11', job_id='job-11', source_revision=4,
    appearance_revision=2, camera_revision=1, backend_version='f3d-3.1.0',
)


def utc_now():
    return datetime.now(timezone.utc).isoformat()


# ── the recorded task ─────────────────────────────────────────────────────
def record_a_task():
    """One import that fails because a dependency is missing.

    The sequence is the one a user can actually hit: accepted, started,
    source prepared, native load attempted, failed with a code. Nothing here
    invents a step the product does not have; the events are the model
    library's own vocabulary.
    """
    with events.model_identity(**JOB):
        events.record_model_event(events.EVENT_QUEUED, queue='import', depth=2)
        events.record_model_event(events.EVENT_STARTED, queue='import', depth=1)
        events.record_model_event(
            events.EVENT_IMPORT_DEPENDENCIES_DETECTED,
            dependency_count=8, missing_count=1,
            missing=['textures/Armchair_01_diff_1k.jpg'])
        events.record_model_event(events.EVENT_SOURCE_PREPARED,
                                  file_count=7, total_bytes=5_242_880)
        events.record_model_event(events.EVENT_NATIVE_LOADED,
                                  native_ms=812.5, triangles=5626)
        events.record_model_event(
            events.EVENT_FAILED, stage='import.validate', error_code='missing_dependency',
            error_message='贴图缺失：textures/Armchair_01_diff_1k.jpg')
    # A second job that is superseded by the user switching models.
    with events.model_identity(**dict(JOB, job_id='job-12', request_id='req-12',
                                      model_id='model-barrel')):
        events.record_model_event(events.EVENT_QUEUED, queue='render')
        events.record_model_event(events.EVENT_STARTED, queue='render')
        events.record_model_event(events.EVENT_STALE, superseded_by='job-13',
                                  reason='user switched model')
    # A native error storm: 2000 identical lines must not become 2000 events.
    for _ in range(2000):
        events.forward_native_log('error', 'f3d: texture unit exhausted',
                                  job_id='job-13')
    events.flush_model_events()


def problem_index():
    """The questions a user would ask, and the events that answer them."""
    return [
        {'id': 'P-1',
         'question': '导入为什么失败了？',
         'answered_by': ['model_queued', 'model_started', 'model_import.dependencies_detected',
                         'model_source_prepared', 'model_native_loaded', 'model_failed'],
         'fields': ['job_id', 'stage', 'error_code', 'missing_count', 'missing']},
        {'id': 'P-2',
         'question': '旧任务有没有回写？',
         'answered_by': ['model_stale'],
         'fields': ['job_id', 'superseded_by', 'request_id', 'open_epoch']},
        {'id': 'P-3',
         'question': '原生日志刷屏有没有丢证据？',
         'answered_by': ['model_native_log', 'model_native_log_merged'],
         'fields': ['native_key', 'occurrences_total', 'occurrences_merged',
                    'first_mono', 'last_mono']},
        {'id': 'P-4',
         'question': '这次日志自己是不是不完整？',
         'answered_by': ['pipeline.diagnostic_failures', 'pipeline.model_log_completeness'],
         'fields': ['complete', 'writer_errors', 'dropped', 'failures']},
    ]


# ── the overhead comparison ───────────────────────────────────────────────
def _percentiles(samples):
    ordered = sorted(samples)
    def pick(fraction):
        index = min(len(ordered) - 1, max(0, int(round(fraction * (len(ordered) - 1)))))
        return ordered[index]
    return {
        'count': len(ordered),
        'p50_us': round(statistics.median(ordered) * 1e6, 2),
        'p95_us': round(pick(0.95) * 1e6, 2),
        'p99_us': round(pick(0.99) * 1e6, 2),
        'max_us': round(ordered[-1] * 1e6, 2),
    }


def measure_overhead(rounds=5, per_round=2000):
    """A representative workload, measured with and without recording.

    The workload is one lifecycle event carrying the full identity: that is
    the hot path the UI thread actually pays for on every job transition.
    """
    import prism.performance_trace as trace_module
    from prism.performance_trace import PerformanceTrace, mode, set_mode
    from PyQt6 import QtWidgets
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    result = {'rounds': rounds, 'per_round': per_round, 'modes': {}}
    base_directory = os.path.join(OUT, 'logs')
    os.makedirs(base_directory, exist_ok=True)
    previous = trace_module._active
    trace = None
    try:
        trace = PerformanceTrace(app, None, base_directory)
        trace_module._active = trace
        for label in ('off', 'normal', 'diagnostic'):
            set_mode('off' if label == 'off' else label)
            samples = []
            for _ in range(rounds):
                started = time.perf_counter()
                for index in range(per_round):
                    events.record_model_event(
                        events.EVENT_STARTED, job_id='job-%d' % index,
                        model_id='model-%d' % index, stage='render')
                samples.append((time.perf_counter() - started) / per_round)
            entry = _percentiles(samples)
            entry['per_event_us'] = entry.pop('p50_us')
            if label == 'diagnostic':
                from prism.model_library.diagnostic_snapshot import model_diagnostic_snapshot
                started = time.perf_counter()
                model_diagnostic_snapshot()
                entry['snapshot_once_us'] = round((time.perf_counter() - started) * 1e6, 2)
            result['modes'][label] = entry
        result['repeat_samples'] = {
            label: value for label, value in result['modes'].items()}
        off = result['modes']['off']['per_event_us']
        for label in ('normal', 'diagnostic'):
            value = result['modes'][label]['per_event_us']
            result['%s_median_increase_pct' % label] = (
                round((value / off - 1) * 100, 2) if off else None)
        result['note'] = ('记录开销是每条生命周期事件的成本；关闭模式下 record_model_event '
                          '仍然被调用，只是 trace 不落盘，因此这条比较的是"记不记"的差别。')
        result['mode_after'] = mode()
    finally:
        trace_module._active = previous
        if trace is not None:
            try:
                trace.close()
            except Exception:
                pass
    return result


def measure_business_share(rounds=3, jobs=20, work_ms=800.0):
    """The same question, measured against a *business operation*.

    "Recording costs 4 microseconds more per event" and "recording costs 3%
    of one import" are both true statements about different denominators.
    The second is the one the D6 target talks about, so it is measured
    separately instead of being inferred from the first.
    """
    import prism.performance_trace as trace_module
    from prism.performance_trace import PerformanceTrace, set_mode
    from PyQt6 import QtWidgets
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    result = {'rounds': rounds, 'jobs': jobs, 'assumed_work_ms': work_ms,
              'modes': {}}
    previous = trace_module._active
    trace = PerformanceTrace(app, None, os.path.join(OUT, 'logs-business'))
    trace_module._active = trace
    try:
        for label in ('off', 'normal'):
            set_mode(label)
            samples = []
            for _ in range(rounds):
                started = time.perf_counter()
                for index in range(jobs):
                    with events.model_identity(**dict(JOB, job_id='job-%d' % index)):
                        events.record_model_event(events.EVENT_QUEUED, queue='import')
                        events.record_model_event(events.EVENT_STARTED, queue='import')
                        events.record_model_event(events.EVENT_SOURCE_PREPARED,
                                                  file_count=7, total_bytes=5_242_880)
                        events.record_model_event(events.EVENT_NATIVE_LOADED,
                                                  native_ms=work_ms * 0.9)
                        events.record_model_event(events.EVENT_COMPLETED)
                samples.append(time.perf_counter() - started)
            events.flush_model_events()
            logging_seconds = statistics.median(samples)
            business_seconds = jobs * work_ms / 1000.0
            result['modes'][label] = {
                'logging_s': round(logging_seconds, 4),
                'business_s': round(business_seconds, 4),
                'share_of_operation_pct': round(
                    logging_seconds / (logging_seconds + business_seconds) * 100, 4),
                'events_per_job': 5,
            }
        off = result['modes']['off']['share_of_operation_pct']
        on = result['modes']['normal']['share_of_operation_pct']
        result['share_increase_pct'] = round((on / off - 1) * 100, 2) if off else None
    finally:
        trace_module._active = previous
        try:
            trace.close()
        except Exception:
            pass
    return result


# ── the bundle ────────────────────────────────────────────────────────────
def build_bundle(trace_directory):
    from prism.diagnostics import collect_bundle
    from prism.model_library.diagnostic_snapshot import model_diagnostic_snapshot
    from prism.performance_trace import QueueAccounting, resource_accounting

    queue = QueueAccounting('model-import')
    queue.submitted(1, task_id='job-11')
    queue.started(task_id='job-11')
    queue.service_done(service_ms=812.5, usable=False, task_id='job-11')
    resources = resource_accounting('model_library_cache', budget=256 * 1024 * 1024)
    resources.observe(12 * 1024 * 1024, source='measured')

    snapshot = model_diagnostic_snapshot(
        queue=queue, resources=resources,
        job=dict(JOB, state='failed', error_code='missing_dependency',
                 error_message='贴图缺失：textures/Armchair_01_diff_1k.jpg'),
        errors=[{'code': 'missing_dependency', 'message': 'C:/Users/someone/库/armchair/model.fbx'}],
    )
    target = os.path.join(OUT, 'bundle.zip')
    for path in (target, target + '.manifest.json'):
        if os.path.exists(path):
            os.remove(path)
    collect_bundle(trace_directory, target, model_snapshot=snapshot)
    with zipfile.ZipFile(target) as archive:
        names = archive.namelist()
        manifest = json.loads(archive.read('manifest.json').decode('utf-8'))
        summary = json.loads(archive.read('summary.json').decode('utf-8'))
        model = json.loads(archive.read('model-diagnostic.json').decode('utf-8'))
        model_events = {}

        def tally(event):
            model_events[event] = model_events.get(event, 0) + 1

        for name in names:
            if not name.endswith('.jsonl'):
                continue
            text = archive.read(name).decode('utf-8', 'replace')
            for line in text.splitlines():
                try:
                    payload = json.loads(line)
                except ValueError:
                    continue
                event = payload.get('event') or payload.get('kind')
                if isinstance(event, str) and event.startswith('model_'):
                    tally(event)
    return {
        'bundle': os.path.relpath(target, REPO),
        'entries': names,
        'manifest_files': len(manifest.get('files', [])),
        'omissions': manifest.get('omissions', []),
        'summary_model_library': summary.get('model_library'),
        'snapshot_reads_model_bytes': model.get('reads_model_bytes'),
        'snapshot_queue': model.get('queue'),
        'snapshot_completeness': (model.get('pipeline') or {}).get('model_log_completeness', {}).get('complete'),
        'model_events_by_name': model_events,
        'model_events_total': sum(model_events.values()),
    }


# ── the contract table ────────────────────────────────────────────────────
def write_contract_table():
    contract = events.model_event_contract()
    lines = ['# 模型库日志事件与字段（机器生成）', '',
             '生成时间：%s' % utc_now(), '',
             '入口：`%s`' % contract['entry_point'], '',
             '## 每条任务事件必须携带的身份字段', '',
             '`' + '`, `'.join(contract['identity_fields']) + '`', '',
             '## 生命周期事件与必需字段', '',
             '| 事件 | 必需字段 | 终态 |', '|---|---|---|']
    for name in contract['lifecycle_events']:
        required = contract['required_fields'].get(name, [])
        lines.append('| `%s` | %s | %s |' % (
            name, ', '.join('`%s`' % item for item in required) or '—',
            '是' if name in contract['terminal_events'] else ''))
    for title, key in (('导入', 'import_events'), ('外观', 'appearance_events'),
                       ('导出', 'export_events'), ('工程保存', 'project_events')):
        lines.extend(['', '## %s子步骤' % title, ''])
        for name in contract[key]:
            lines.append('- `%s`' % name)
    lines.extend(['', '## 原生日志转发', '',
                  '`%s`' % contract['native_log']['entry_point'], '',
                  '- 窗口：%ss；合并到 %d 条时提前报告' % (
                      contract['native_log']['window_s'], contract['native_log']['flush_count']),
                  '- 规则：%s' % contract['native_log']['rule'], '',
                  '## 禁止写入的字段名', '',
                  '`' + '`, `'.join(contract['forbidden_fields'][:24]) + '`', '',
                  '## 计时规则', '', contract['timing_rule'], ''])
    path = os.path.join(OUT, 'event-contract.md')
    with open(path, 'w', encoding='utf-8') as stream:
        stream.write('\n'.join(lines))
    return os.path.relpath(path, REPO)


def main():
    os.makedirs(OUT, exist_ok=True)
    logs = os.path.join(OUT, 'logs')
    os.makedirs(logs, exist_ok=True)
    for name in os.listdir(logs):
        if name.startswith('Prism-'):
            os.remove(os.path.join(logs, name))

    from PyQt6 import QtWidgets
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    import prism.performance_trace as trace_module
    from prism.performance_trace import PerformanceTrace, set_mode
    trace = PerformanceTrace(app, None, logs)
    # The module-level ``record()`` writes through ``_active``; a trace that is
    # only constructed does not receive a single event.
    trace_module._active = trace
    set_mode('normal')
    completeness_while_active = None
    try:
        record_a_task()
        events.flush_model_events()
        time.sleep(1.5)          # let the writer thread drain
        completeness_while_active = events.model_log_completeness()
    finally:
        events.flush_model_events()
        try:
            trace.close()
        except Exception:
            pass
        trace_module._active = None

    report = {
        'schema': 'model_library_d3/1',
        'generated_utc': utc_now(),
        'source_snapshot': os.environ.get('PRISM_BASELINE_SHA', 'see baseline.json'),
        'job_identity': JOB,
        'problem_index': problem_index(),
        'completeness_while_active': {
            'complete': completeness_while_active.get('complete'),
            'trace': completeness_while_active.get('trace'),
            'diagnostic_failures': completeness_while_active.get('diagnostic_failures'),
        },
        'completeness_after_close': {
            'complete': events.model_log_completeness().get('complete'),
            'reason': 'The trace of a finished process is not active, so the log '
                      'must not claim to be complete.',
        },
        'bundle': build_bundle(logs),
        'overhead': measure_overhead(),
        'business_share': measure_business_share(),
        'contract_table': write_contract_table(),
        'model_event_state': events.model_event_state(),
    }
    target = os.path.join(OUT, 'd3-evidence.json')
    with open(target, 'w', encoding='utf-8') as stream:
        json.dump(report, stream, ensure_ascii=False, indent=2)
    print(json.dumps({
        'bundle': report['bundle']['bundle'],
        'model_events_total': report['bundle']['model_events_total'],
        'events_by_name': report['bundle']['model_events_by_name'],
        'overhead': report['overhead']['modes'],
        'contract_table': report['contract_table'],
        'written': os.path.relpath(target, REPO),
    }, ensure_ascii=False, indent=2))
    return 0


if __name__ == '__main__':
    sys.exit(main())
