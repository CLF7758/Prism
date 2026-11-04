"""Delayed catalog installation must not access disposed Qt controls."""
from PyQt6 import QtCore, sip
import threading


def test_queued_catalog_install_ignores_deleted_summary(main_window, qtbot):
    browser = main_window.asset_browser
    pending = object()
    browser._catalog_pending = pending
    sip.delete(browser.summary)
    # Reproduce the delayed continuation after the native child was destroyed.
    # There must be no access to pending fields, model, scene or the label.
    QtCore.QTimer.singleShot(0, lambda: browser._install_catalog_rows(pending))
    qtbot.wait(10)
    assert browser._catalog_install_valid(pending) is False
    browser._catalog_pending = None


def test_destroyed_panel_retires_pending_catalog_work(main_window):
    browser = main_window.asset_browser
    cancelled = browser._catalog_cancel = threading.Event()
    browser._catalog_pending = object()
    browser._catalog_query_job = object()
    sip.delete(browser)
    assert cancelled.is_set()
    assert browser._catalog_pending is None
    assert browser._catalog_query_job is None
