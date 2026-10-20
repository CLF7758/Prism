import json
import time
import threading
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
    assert 'last_dispatch_age_ms' in stalls[0]
    assert 'completed_dispatches' in stalls[0]
    assert 'timer_starved' in stalls[0]
    assert stalls[0]['last_action']['operation']=='save_test'
    assert any(r['event']=='gui_recovered' for r in entries)


def test_heartbeat_delay_can_be_distinguished_from_busy_gui_events(main_window, qapp, qtbot, tmp_path):
    trace = trace_module.install(qapp, main_window, tmp_path)
    pulse = QtCore.QTimer()
    pulse.setInterval(10)
    pulse.timeout.connect(lambda: trace.dispatch_finished(main_window, 1000, 1))
    try:
        trace.timer.stop()  # Delay only the diagnostic heartbeat, not the GUI.
        pulse.start()
        qtbot.waitUntil(lambda: any(json.loads(value)['event'] == 'gui_stall'
                                   for value in list(trace.recent)), timeout=3000)
    finally:
        pulse.stop()
        entries = rows(trace)
    stalls = [row for row in entries if row['event'] == 'gui_stall']
    assert stalls[0]['timer_starved'] is True
    assert stalls[0]['completed_dispatches'] > 0
    assert stalls[0]['last_dispatch_age_ms'] < 100


def test_diagnostic_failure_cannot_break_a_delivered_gui_event(main_window, qapp, tmp_path, monkeypatch, caplog):
    trace = trace_module.install(qapp, main_window, tmp_path)
    delivered = []

    class Receiver(QtCore.QObject):
        def event(self, event):
            delivered.append(event.type())
            return True

    receiver = Receiver()
    def fail(*args, **kwargs):
        raise TypeError('broken diagnostic adapter')
    monkeypatch.setattr(trace, 'dispatch_finished', fail)
    try:
        assert qapp.notify(receiver, QtCore.QEvent(QtCore.QEvent.Type.User)) is True
        assert delivered == [QtCore.QEvent.Type.User]
        assert trace.dispatch_capture_failed is True
        assert 'GUI dispatch diagnostics disabled' in caplog.text
        # A second event still reaches the app without repeating the fault.
        assert qapp.notify(receiver, QtCore.QEvent(QtCore.QEvent.Type.User)) is True
        assert len(delivered) == 2
    finally:
        trace.close()


def test_slow_view_dispatch_handles_shadowed_qobject_parent(main_window, qapp, tmp_path):
    trace = trace_module.install(qapp, main_window, tmp_path)
    try:
        assert main_window.view.parent is main_window
        trace.dispatch_finished(main_window.view, 1000, 700)
    finally:
        entries = rows(trace)
    events = [entry for entry in entries if entry['event'] == 'slow_dispatch']
    assert events[0]['target'] == 'PrismGraphicsView'
    assert events[0]['owner'] == 'QWidget'


def test_worker_events_cannot_masquerade_as_gui_progress(main_window, qapp, tmp_path):
    trace = trace_module.install(qapp, main_window, tmp_path)
    try:
        worker = threading.Thread(target=lambda: trace.dispatch_finished(None, 1000, 700))
        worker.start()
        worker.join(timeout=2)
        assert not worker.is_alive()
        assert trace.last_dispatch is None
        assert trace.dispatches_since_heartbeat == 0
    finally:
        trace.close()


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
