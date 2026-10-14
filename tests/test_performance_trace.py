import json
import time
import pytest
from PyQt6 import QtCore, QtGui, QtWidgets
from prism import performance_trace as trace_module


def rows(trace):
    trace.close()
    return [json.loads(line) for line in trace.path.read_text(encoding='utf-8').splitlines()]


def test_click_action_and_slow_operation_are_recorded(main_window, qapp, tmp_path):
    trace = trace_module.install(qapp, main_window, tmp_path)
    try:
        action = QtGui.QAction('Fit canvas', main_window)
        trace.hook_actions(main_window)
        action.trigger()
        event = QtGui.QMouseEvent(QtCore.QEvent.Type.MouseButtonPress, QtCore.QPointF(12, 34),
            QtCore.Qt.MouseButton.LeftButton, QtCore.Qt.MouseButton.LeftButton, QtCore.Qt.KeyboardModifier.NoModifier)
        trace.eventFilter(main_window.view.viewport(), event)
        @trace_module.measured('test.block')
        def operation():
            time.sleep(.01)
        operation()
    finally:
        entries = rows(trace)
    assert any(r['event']=='action' and r['name']=='Fit canvas' for r in entries)
    assert any(r['event']=='input' and r['x']==12 for r in entries)
    assert any(r['event']=='operation' and r['duration_ms'] >= 8 for r in entries)
    assert trace_module._active is None


def test_stall_captures_gui_stack_before_recovery(main_window, qapp, tmp_path):
    trace = trace_module.install(qapp, main_window, tmp_path)
    try:
        trace.record('input', operation='save_test')
        time.sleep(.8)
        trace._heartbeat()
    finally:
        entries = rows(trace)
    stalls = [r for r in entries if r['event']=='gui_stall']
    assert stalls and stalls[0]['stack']
    assert stalls[0]['last_action']['operation']=='save_test'
    assert any(r['event']=='gui_recovered' for r in entries)


def test_diagnostics_preserve_exceptions_and_bound_queue(main_window, qapp, tmp_path):
    trace = trace_module.install(qapp, main_window, tmp_path)
    @trace_module.measured('test.failure')
    def fail():
        raise ValueError('original error')
    try:
        with pytest.raises(ValueError, match='original error'):
            fail()
        assert trace.events.maxsize == 512
    finally:
        entries = rows(trace)
    assert any(r['event']=='operation' and r['failed'] for r in entries)


def test_event_bytes_and_crash_summary_are_bounded(main_window, qapp, tmp_path):
    trace = trace_module.install(qapp, main_window, tmp_path)
    try:
        for _ in range(150):
            trace.record('test_long', detail='x'*100000)
        assert len(trace.recent) == 128
        assert all(len(entry) <= trace.MAX_EVENT_BYTES for entry in trace.recent)
        try:
            raise ValueError('crash example')
        except ValueError as error:
            import sys
            report = trace.crash_report(type(error), error, sys.exc_info()[2])
        data = json.loads(report.read_text(encoding='utf-8'))
        assert data['error'] == 'crash example'
        assert len(data['recent']) == 128
        assert data['traceback']
    finally:
        trace.close()
