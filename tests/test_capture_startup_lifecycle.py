"""Startup failure is one terminal receipt, including nested modal callbacks."""
import sys
import time
from unittest.mock import Mock

import pytest
from PyQt6 import QtCore

from prism import capture_service as capture


@pytest.fixture
def adapter(qapp, tmp_path, monkeypatch, qtbot):
    try:
        real_command = capture.component_command()
    except OSError:
        real_command = None
    directory = tmp_path/'capture'
    directory.mkdir()
    monkeypatch.setattr(capture, 'capture_directory', lambda: directory)
    monkeypatch.setattr(capture, 'inbox_directory', lambda: tmp_path/'inbox')
    service = capture.CaptureService(qapp)
    service._test_component_command = real_command
    yield service
    service.close()
    if isinstance(service.process, QtCore.QProcess):
        qtbot.waitUntil(lambda: service.process.state() ==
                       QtCore.QProcess.ProcessState.NotRunning, timeout=5000)


def test_timeout_tears_down_before_modal_and_reports_once(adapter):
    process = Mock()
    process.state.return_value = QtCore.QProcess.ProcessState.Running
    process.readAllStandardOutput.return_value = b''
    process.readAllStandardError.return_value = b''
    process.errorString.return_value = 'Process crashed'
    adapter.process = process
    adapter.pending = [dict(command='capture')]
    adapter.jobs['request'] = dict(kind='capture')
    failures = []

    def modal(message):
        failures.append(message)
        process.kill.assert_called_once()
        assert not adapter.pending and not adapter.jobs
        # These can arrive while QMessageBox runs its nested event loop.
        adapter._message(dict(event='ready', protocol_version=1))
        adapter._error(QtCore.QProcess.ProcessError.Crashed)
        adapter._finished(-1, QtCore.QProcess.ExitStatus.CrashExit)

    adapter.failed.connect(modal)
    adapter._timeout()
    assert len(failures) == 1
    assert not adapter.ready
    process.write.assert_not_called()


def test_timer_drains_ready_already_waiting_in_pipe(adapter):
    process = Mock()
    process.readAllStandardOutput.return_value = b'{"event":"ready","protocol_version":1}\n'
    adapter.process = process
    failures = Mock()
    adapter.failed.connect(failures)
    adapter.pending = [dict(command='capture', request_id='current')]
    adapter._timeout()
    assert adapter.ready and not adapter.pending
    failures.assert_not_called()
    process.kill.assert_not_called()
    process.write.assert_called_once()


def test_unexpected_exit_does_not_repeat_process_error(adapter):
    process = Mock()
    process.state.return_value = QtCore.QProcess.ProcessState.NotRunning
    process.readAllStandardError.return_value = b''
    process.errorString.return_value = 'Failed to start'
    adapter.process = process
    failures = Mock()
    adapter.failed.connect(failures)
    adapter._error(QtCore.QProcess.ProcessError.FailedToStart)
    adapter._finished(1, QtCore.QProcess.ExitStatus.NormalExit)
    failures.assert_called_once()


def test_failed_start_can_retry_with_real_process(adapter, tmp_path, monkeypatch, qtbot):
    failures = []
    adapter.failed.connect(failures.append)
    monkeypatch.setattr(capture, 'component_command', lambda: (sys.executable, str(tmp_path)))
    # First launch is a process which never completes its handshake.
    command = ['-u', '-c', 'import time; time.sleep(30)']
    monkeypatch.setattr(capture, 'invocation', lambda *args: (sys.executable, command))
    adapter.start()
    qtbot.waitUntil(lambda: adapter.process.state() == QtCore.QProcess.ProcessState.Running)
    adapter._timeout()
    qtbot.waitUntil(lambda: adapter.process.state() == QtCore.QProcess.ProcessState.NotRunning)
    assert len(failures) == 1
    command[:] = ['-u', '-c',
        'import sys; print(\'{"event":"ready","protocol_version":1}\', flush=True); sys.stdin.readline()']
    adapter.start()
    qtbot.waitUntil(lambda: adapter.ready, timeout=5000)
    assert len(failures) == 1
    assert not adapter._failure_reported


def test_busy_gui_does_not_timeout_helper_already_ready(adapter, tmp_path, monkeypatch):
    monkeypatch.setattr(capture, 'component_command', lambda: (sys.executable, str(tmp_path)))
    command = ['-u', '-c',
        'import sys; print(\'{"event":"ready","protocol_version":1}\', flush=True); sys.stdin.readline()']
    monkeypatch.setattr(capture, 'invocation', lambda *args: (sys.executable, command))
    failures = []
    adapter.failed.connect(failures.append)
    adapter.start()
    assert adapter.process.waitForStarted(1000)
    # Child completes while the GUI is occupied. Its OS pipe is readable, but
    # Qt has not yet delivered the notifier/readyRead to CaptureService.
    time.sleep(.5)
    adapter._timeout()
    assert adapter.ready and not failures


def test_actual_component_handshake_and_shutdown(adapter, qtbot, monkeypatch):
    if adapter._test_component_command is None:
        pytest.skip('Optional capture runtime is not installed')
    monkeypatch.setattr(capture, 'component_command', lambda: adapter._test_component_command)
    failures = []
    adapter.failed.connect(failures.append)
    adapter.start()
    qtbot.waitUntil(lambda: adapter.ready or bool(failures), timeout=25000)
    assert adapter.ready and not failures, adapter.diagnostics.decode('utf-8', errors='replace')
    adapter.close()
    qtbot.waitUntil(lambda: adapter.process.state() == QtCore.QProcess.ProcessState.NotRunning,
                   timeout=5000)
    assert not failures
