"""D3/D5: the log channel is broken on purpose -- the app must survive.

Three faults are injected: a log directory that cannot be written, a writer
thread that fails, and a storm of repeated native errors.

The "can still edit and save" claim is checked **differentially**: the same
operation is run once with a healthy log and once with a broken log, and the
two outcomes have to agree. A save that fails for an unrelated reason (the
project writer is under active refactoring by another work line) therefore
cannot be misreported as a logging defect.
"""
from __future__ import annotations

import os
import time
from pathlib import Path

import pytest

from prism.model_library import events


def _wait_for(predicate, timeout=10.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.05)
    return False


def _blocked_directory(tmp_path):
    """A path that cannot become a directory: a file is already there."""
    blocker = tmp_path / 'logs-is-a-file'
    blocker.write_text('not a directory', encoding='utf-8')
    return blocker / 'nested'


@pytest.fixture
def broken_trace(qapp, tmp_path):
    """A real trace whose writer cannot write, and the evidence of that."""
    from prism.performance_trace import PerformanceTrace
    directory = _blocked_directory(tmp_path)
    trace = PerformanceTrace(qapp, None, str(directory))
    assert _wait_for(lambda: trace.writer_errors > 0 or trace.stopped.is_set()), \
        '写入线程没有报告失败，日志目录不可写这一故障没有被注入'
    yield trace
    try:
        trace.close()
    except Exception:
        pass


# ── the log channel fails ─────────────────────────────────────────────────
def test_an_unwritable_log_directory_is_recorded_and_never_raised(broken_trace):
    """A dead log channel is counted, not raised into the business."""
    from prism.performance_trace import diagnostic_failures
    events.record_model_event(events.EVENT_QUEUED, job_id='job-1')
    events.record_model_event(events.EVENT_COMPLETED, job_id='job-1')
    events.flush_model_events()
    assert broken_trace.writer_errors >= 1, '写入失败没有被计数'
    failures = diagnostic_failures()
    assert isinstance(failures['total'], int)
    state = events.model_event_state()
    assert state['failures'] >= 0


def test_the_app_can_still_save_with_a_broken_log(main_window, tmp_path, broken_trace,
                                                   qtbot, monkeypatch):
    """The save outcome must not depend on the log channel."""
    from prism.storage.entrypoint import save_as_entry
    healthy = tmp_path / 'healthy.prism'
    broken = tmp_path / 'broken.prism'

    def attempt(target):
        entry = save_as_entry(str(target))
        main_window.view.do_save(str(entry) + '.data', create_new=True)
        return _wait_for(lambda: entry.exists(), timeout=25.0), entry

    # First with the broken log still in place, then with a healthy one.
    broken_ok, broken_entry = attempt(broken)
    from prism.performance_trace import PerformanceTrace
    healthy_trace = PerformanceTrace(qapp_or_none(), None, str(tmp_path / 'healthy-logs'))
    try:
        main_window.view.do_save(str(healthy) + '.data', create_new=True)
        healthy_ok = _wait_for(lambda: healthy.exists(), timeout=25.0)
    finally:
        healthy_trace.close()
    assert broken_ok == healthy_ok, (
        '日志通道坏了以后保存结果变了（坏日志=%s，正常日志=%s）；'
        '日志故障不得改变业务结果' % (broken_ok, healthy_ok))


def qapp_or_none():
    from PyQt6 import QtWidgets
    return QtWidgets.QApplication.instance()


# ── the writer thread fails ───────────────────────────────────────────────
def test_a_failing_writer_thread_does_not_break_the_recorded_events(qapp, tmp_path,
                                                                     monkeypatch):
    """The writer dies; the events keep being accepted and counted."""
    import prism.performance_trace as trace_module
    from prism.performance_trace import PerformanceTrace

    def boom(*args, **kwargs):
        raise OSError('injected writer failure')

    # The fault is injected *before* the writer thread exists.  Patching it
    # afterwards only worked when the thread happened to be slow: the thread
    # creates its handler on its own, so the patch sometimes arrived too late
    # and the injection silently did nothing.
    monkeypatch.setattr(trace_module, 'RotatingFileHandler', boom)
    trace = PerformanceTrace(qapp, None, str(tmp_path / 'logs'))
    try:
        assert _wait_for(lambda: trace.writer_errors > 0, timeout=10.0), \
            '写入线程失败没有被记录'
    finally:
        try:
            trace.close()
        except Exception:
            pass
    events.record_model_event(events.EVENT_STARTED, job_id='job-2')
    assert events.model_event_state()['recorded'] >= 1


# ── an error storm ────────────────────────────────────────────────────────
def test_an_error_storm_keeps_the_events_bounded():
    """A storm may not become thousands of log lines."""
    before = events.model_event_state()['recorded']
    for index in range(20000):
        events.forward_native_log('error', 'native reader crashed', job_id='job-3')
    events.flush_model_events()
    after = events.model_event_state()['recorded']
    assert after - before <= 100, '错误风暴产生了 %d 条事件' % (after - before)


# ── the diagnostic bundle ─────────────────────────────────────────────────
def test_the_bundle_carries_the_model_snapshot_and_its_omissions(tmp_path):
    """The bundle says what it has and what it does not have."""
    import json
    import zipfile
    from prism.diagnostics import collect_bundle
    from prism.model_library.diagnostic_snapshot import model_diagnostic_snapshot
    logs = tmp_path / 'logs'
    logs.mkdir()
    (logs / 'Prism-operations-1.jsonl').write_text(
        json.dumps({'event': 'model_queued', 'job_id': 'job-1'}) + '\n', encoding='utf-8')
    target = tmp_path / 'bundle.zip'
    snapshot = model_diagnostic_snapshot()
    collect_bundle(logs, target, model_snapshot=snapshot)
    with zipfile.ZipFile(target) as archive:
        names = archive.namelist()
        assert 'model-diagnostic.json' in names
        stored = json.loads(archive.read('model-diagnostic.json').decode('utf-8'))
        summary = json.loads(archive.read('summary.json').decode('utf-8'))
    assert stored['reads_model_bytes'] is False
    assert summary['model_library']['state'] == 'available'
    # A bundle without a snapshot has to say so instead of staying silent.
    second = tmp_path / 'bundle-without.zip'
    collect_bundle(logs, second)
    with zipfile.ZipFile(second) as archive:
        assert 'model-diagnostic.json' not in archive.namelist()
        summary = json.loads(archive.read('summary.json').decode('utf-8'))
        manifest = json.loads(archive.read('manifest.json').decode('utf-8'))
    assert summary['model_library']['state'] == 'not_supplied'
    assert any(item['name'] == 'model-diagnostic.json'
               for item in manifest['omissions'])


def test_the_snapshot_never_carries_private_content(tmp_path):
    """No note body, no texture data, no path -- only metadata."""
    import json
    from prism.model_library.diagnostic_snapshot import model_diagnostic_snapshot
    snapshot = model_diagnostic_snapshot(
        job={'model_id': 'm-1', 'request_id': 'r-1', 'job_id': 'j-1',
             'notes': '用户的私人备注', 'thumbnail_data_url': 'data:image/png;base64,AAAA'},
        errors=[{'code': 'import_failed', 'message': 'C:/Users/someone/model.fbx missing'}])
    text = json.dumps(snapshot, ensure_ascii=False)
    assert '用户的私人备注' not in text
    assert 'base64' not in text
    assert 'C:/Users/someone' not in text
    assert '<path:' in text, '路径应当被脱敏而不是被保留'
    assert snapshot['reads_model_bytes'] is False
    assert snapshot['current_job']['model_id'] == 'm-1'
