from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from PyQt6 import QtCore, sip

from prism.widgets.editor_snapshot import request_editor_snapshot, read_editor_snapshot


class Page(QtCore.QObject):
    def __init__(self):
        super().__init__()
        self.callbacks = []
    def runJavaScript(self, script, callback):
        self.callbacks.append(callback)


def test_destroyed_page_completes_async_failure_once_without_waiting_for_timeout(qapp):
    page = Page()
    values = []
    request_editor_snapshot(page, 'snapshot', values.append, timeout_ms=3000)
    reply = page.callbacks[0]
    sip.delete(page)
    assert values == [None]
    reply('late destroyed-page result')
    assert values == [None]


def test_deleted_validator_fails_closed_instead_of_raising_in_qt_callback(qapp):
    page = Page()
    values = []
    def invalid():
        raise RuntimeError('wrapped C/C++ owner has been deleted')
    request_editor_snapshot(page, 'snapshot', values.append, valid=invalid)
    page.callbacks[0]('must not install')
    assert values == [None]
    sip.delete(page)


def test_sync_page_removed_before_dispatch_does_not_escape_snapshot_boundary(qapp):
    def run(script, callback):
        raise RuntimeError('page deleted between the alive check and dispatch')
    assert read_editor_snapshot(SimpleNamespace(runJavaScript=run), 'snapshot') is None


@pytest.mark.parametrize('kind', ['document', 'mindmap'])
def test_search_reply_after_panel_destruction_is_rejected(main_window, monkeypatch, kind):
    if kind == 'document':
        from prism.widgets.document_panel import DocumentPanel
        panel = DocumentPanel(main_window.view, parent=main_window, page_id='detached-doc')
        panel._html = '<p>old</p>'
        panel._pending_push = False
        mutation = Mock()
        monkeypatch.setattr(panel, '_on_content_changed', mutation)
        reply = '<p>late</p>'
    else:
        from prism.widgets.mindmap_panel import MindMapPanel
        panel = MindMapPanel(main_window.view, parent=main_window, page_id='detached-map')
        panel._pending_tree = None
        panel._tree_loading = False
        mutation = Mock()
        monkeypatch.setattr(panel, '_on_tree_changed', mutation)
        reply = '{"tree":{"data":{"text":"late"},"children":[]}}'
    panel._ready = True
    page = Page()
    panel._page = page
    if kind == 'mindmap':
        panel.web_view = SimpleNamespace(page=lambda: page)
    values = []
    panel.search_snapshot(values.append)
    callback = page.callbacks[0]
    sip.delete(panel)
    callback(reply)
    assert values == [False]
    mutation.assert_not_called()
    sip.delete(page)


def test_absent_page_rejects_snapshot_without_dispatch(qapp):
    values = []
    request_editor_snapshot(None, 'snapshot', values.append)
    assert values == [None]
    assert read_editor_snapshot(None, 'snapshot') is None


@pytest.mark.parametrize('kind', ['document', 'mindmap'])
def test_search_reply_cannot_write_a_rebound_page_with_same_project_epoch(main_window, monkeypatch, kind):
    panel = main_window.document_panel if kind == 'document' else main_window.mindmap_panel
    page = Page()
    old_id = panel.page_id
    panel._ready = True
    panel._page = page
    mutation = Mock()
    if kind == 'document':
        panel._html = '<p>old</p>'
        panel._pending_push = False
        monkeypatch.setattr(panel, '_on_content_changed', mutation)
    else:
        panel._pending_tree = None
        panel._tree_loading = False
        panel.web_view = SimpleNamespace(page=lambda: page)
        monkeypatch.setattr(panel, '_on_tree_changed', mutation)
    values = []
    try:
        panel.search_snapshot(values.append)
        panel.page_id = 'another-page-with-same-project'
        page.callbacks[0]('<p>late</p>' if kind == 'document' else '{"tree":{}}')
        assert values == [False]
        mutation.assert_not_called()
    finally:
        panel.page_id = old_id
        panel._ready = False
        panel._page = None
        if kind == 'mindmap':
            panel.web_view = None
        sip.delete(page)
