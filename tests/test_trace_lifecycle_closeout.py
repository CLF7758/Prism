import json
import threading
import time

from prism import performance_trace as pt


def test_explicit_span_end_is_recorded_once(qapp, tmp_path):
    pt.set_mode('normal')
    trace = pt.install(qapp, None, tmp_path)
    try:
        with pt.span('manual.cancel') as handle:
            handle.end(status='cancelled')
    finally:
        trace.close()
    rows = [json.loads(line) for line in trace.path.read_text(encoding='utf-8').splitlines()]
    ends = [row for row in rows if row.get('event') == 'operation' and row.get('name') == 'manual.cancel']
    assert len(ends) == 1
    assert ends[0]['status'] == 'cancelled'
    assert not trace._open_spans


def test_unwritable_log_directory_does_not_break_app_or_cleanup(qapp, qtbot, tmp_path):
    blocked = tmp_path / 'file'
    blocked.write_text('not a directory')
    trace = pt.install(qapp, None, blocked / 'logs')
    qtbot.waitUntil(lambda: trace.writer_errors == 1, timeout=2000)
    trace.record('business_still_runs')
    trace.close()
    assert trace._closed
    assert not trace.timer.isActive()
    assert pt._active is None
    assert trace._gc_callback not in __import__('gc').callbacks


def test_off_mode_does_not_build_fields_or_sample(qapp, tmp_path, monkeypatch):
    trace = pt.install(qapp, None, tmp_path)
    try:
        pt.set_mode('off')
        before = trace.events.qsize() + trace.important.qsize()
        class Poison:
            def __str__(self):
                raise AssertionError('off mode formatted business payload')
        trace.record('expensive', value=Poison())
        with pt.span('off.stage') as handle:
            assert handle.span_id is None
        with pt.operation('off.operation'):
            pass
        assert not any(row.get('event') == 'expensive' for row in trace.recent)
        assert not trace._open_spans
        monkeypatch.setattr(pt.sys, '_current_frames', lambda: (_ for _ in ()).throw(AssertionError('off sampled stacks')))
        time.sleep(trace.WATCH_INTERVAL * 2)
    finally:
        trace.close()
        pt.set_mode('normal')


def test_restored_project_identity_survives_capture(qapp):
    previous = pt.capture_context()
    try:
        pt.bind_project('new-gui-project', open_epoch=9)
        old = pt.TraceContext(project_token='origin-project', open_epoch=2, generation=3)
        with pt.restore_context(old):
            frozen = pt.capture_context()
            assert frozen.project_token == 'origin-project'
            assert frozen.open_epoch == 2 and frozen.generation == 3
    finally:
        with pt.restore_context(previous):
            pt.bind_project(previous.project_token, open_epoch=previous.open_epoch,
                            generation=previous.generation, source_revision=previous.source_revision)


def test_restored_unknown_identity_fields_do_not_inherit_new_gui_project(monkeypatch):
    monkeypatch.setattr(pt, '_project_token', 'new-project')
    monkeypatch.setattr(pt, '_open_epoch', 9)
    monkeypatch.setattr(pt, '_generation', 12)
    monkeypatch.setattr(pt, '_source_revision', 'new-source')
    old = pt.TraceContext(trace_id='old-task', project_token='old-project', open_epoch=2)
    with pt.restore_context(old):
        assert pt.capture_context() == old
        with pt.restore_context(pt.TraceContext(trace_id='nested')):
            assert pt.capture_context() == pt.TraceContext(trace_id='nested')
        assert pt.capture_context() == old
