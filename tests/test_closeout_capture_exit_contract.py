"""Shutdown completion must mean that the server thread actually stopped."""
from unittest.mock import Mock
import socket
import threading

from PyQt6 import QtWidgets


def test_quit_guard_waits_for_settings_and_owned_child(main_window, monkeypatch, qtbot):
    from PyQt6 import QtCore
    from types import SimpleNamespace
    window = main_window
    qtbot.waitUntil(lambda: window._recovery_worker is None, timeout=10000)
    app = QtWidgets.QApplication.instance()
    quit_mock = Mock()
    monkeypatch.setattr(app, 'quit', quit_mock)
    monkeypatch.setattr(window.browser_capture, 'is_running', lambda: False)
    pending = [True]
    monkeypatch.setattr(window.view.settings, 'sync_pending', lambda: pending[0])
    child = Mock()
    child.state.return_value = QtCore.QProcess.ProcessState.Running
    monkeypatch.setattr(window.view, '_capture_service', SimpleNamespace(process=child, close=lambda: None), raising=False)
    window._arm_capture_exit()
    try:
        window._on_capture_stopped()
        assert not quit_mock.called
        pending[0] = False
        window._on_capture_stopped()
        assert not quit_mock.called
        child.state.return_value = QtCore.QProcess.ProcessState.NotRunning
        # A native thread can finish before its GUI receipt has been delivered.
        window._recovery_worker = SimpleNamespace(isRunning=lambda: False)
        window._on_capture_stopped()
        assert not quit_mock.called
        window._recovery_worker = None
        window._on_capture_stopped()
        quit_mock.assert_called_once()
    finally:
        if window._capture_exit_timer is not None:window._capture_exit_timer.stop()
        window._capture_exit_pending = False
        app.setQuitOnLastWindowClosed(True)


def test_exit_watchdog_does_not_quit_while_capture_is_running(main_window, monkeypatch):
    window = main_window
    app = QtWidgets.QApplication.instance()
    quit_mock = Mock()
    monkeypatch.setattr(app, 'quit', quit_mock)
    monkeypatch.setattr(window.browser_capture, 'is_running', lambda: True)
    window._arm_capture_exit()
    try:
        # Exercise the watchdog callback, without spending three real seconds.
        window._on_capture_stopped()
        assert not quit_mock.called
        assert window._capture_exit_pending
    finally:
        if window._capture_exit_timer is not None:
            window._capture_exit_timer.stop()
        window._capture_exit_pending = False
        app.setQuitOnLastWindowClosed(True)


def test_server_join_timeout_is_not_a_completed_stop(qapp):
    from prism.browser_capture import BrowserCaptureService
    service = BrowserCaptureService()
    server = Mock()
    thread = Mock()
    thread.is_alive.return_value = True
    service._stopped.clear()
    service._shutdown(server, thread)
    assert not service._stopped.is_set(), 'a still-live thread cannot be reported as stopped'


def test_delayed_real_server_releases_port_before_exit(main_window, qtbot, monkeypatch):
    from prism.browser_capture import BrowserCaptureService
    window = main_window
    app = QtWidgets.QApplication.instance()
    quit_mock = Mock()
    monkeypatch.setattr(app, 'quit', quit_mock)
    service = BrowserCaptureService(port=0, parent=window)
    window.browser_capture = service
    service.stopped.connect(window._on_capture_stopped)
    service.start()
    port = service._server.server_port
    thread = service._thread
    shutdown = service._server.shutdown
    entered, release = threading.Event(), threading.Event()

    def delayed_shutdown():
        entered.set()
        if not release.wait(5):
            raise RuntimeError('test did not release shutdown')
        shutdown()

    monkeypatch.setattr(service._server, 'shutdown', delayed_shutdown)
    window._arm_capture_exit()
    service.stop()
    try:
        qtbot.waitUntil(entered.is_set, timeout=3000)
        window._on_capture_stopped()  # Same callback used by the watchdog.
        assert service.is_running()
        assert thread.is_alive()
        assert not quit_mock.called
        assert window._capture_exit_pending
    finally:
        release.set()
    qtbot.waitUntil(lambda: not service.is_running(), timeout=5000)
    qtbot.waitUntil(lambda: quit_mock.called, timeout=3000)
    assert not thread.is_alive()
    assert service.wait_for_stop(0)
    with socket.socket() as probe:
        probe.bind(('127.0.0.1', port))
    app.setQuitOnLastWindowClosed(True)
