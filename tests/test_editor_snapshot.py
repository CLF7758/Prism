"""Editor read-back cannot leak late replies into another project."""
from types import SimpleNamespace
from unittest.mock import Mock

from PyQt6 import QtGui

from prism.widgets.editor_snapshot import read_editor_snapshot


def test_synchronous_reply_is_validated_before_use(qapp):
    page = SimpleNamespace(runJavaScript=lambda script, callback: callback('current content'))
    assert read_editor_snapshot(page, 'snapshot') == 'current content'
    assert read_editor_snapshot(page, 'snapshot', valid=lambda: False) is None


def test_timeout_ignores_late_renderer_reply(qapp):
    callbacks = []
    page = SimpleNamespace(runJavaScript=lambda script, callback: callbacks.append(callback))
    assert read_editor_snapshot(page, 'snapshot', timeout_ms=5) is None
    callbacks[0]('late old content')  # Must not operate on a finished event loop.


def test_project_changed_during_document_flush_cannot_receive_old_content(main_window, monkeypatch):
    panel = main_window.document_panel
    panel._content_loaded = True
    panel._html = '<p>Old project</p>'
    def flush():
        panel.view.scene.project_id = 'different-project'
        panel._set_scene_html('<p>New project</p>')
        return '<p>Old project</p>'
    monkeypatch.setattr(panel, '_flush_from_page', flush)
    panel.store_to_scene()
    assert panel._scene_html() == '<p>New project</p>'


def test_failed_document_snapshot_blocks_save_navigation_and_close(main_window, monkeypatch, tmp_path):
    window = main_window
    window.open_workspace_page('document', 'default-document')
    panel = window.document_panel
    panel._content_loaded = True
    panel._html = '<p>cached</p>'
    panel._ready = True
    panel._page = SimpleNamespace()
    panel._pending_push = False
    monkeypatch.setattr(panel, '_flush_from_page', lambda: None)
    factory = Mock(side_effect=AssertionError('must not save stale content'))
    monkeypatch.setattr('prism.view.fileio.ThreadedIO', factory)
    window.view.mark_content_dirty()
    target = tmp_path / 'protected.prism'
    target.write_bytes(b'original file')
    window.view.do_save(str(target), create_new=False, silent=True)
    factory.assert_not_called()
    assert target.read_bytes() == b'original file'
    assert window.view.is_dirty()
    window.open_workspace_page('canvas', 'default-canvas')
    assert window.tabs.currentWidget() is panel
    assert window.current_page_id == 'default-document'
    window.tabs.setCurrentWidget(window.canvas_page)
    assert window.tabs.currentWidget() is panel
    window.asset_browser.open()
    assert window.tabs.currentWidget() is panel
    event = QtGui.QCloseEvent()
    window.closeEvent(event)
    assert not event.isAccepted()
    panel._ready = False


def test_failed_snapshot_blocks_document_and_resource_exports(main_window, monkeypatch):
    from prism.actions import export_workflow
    window = main_window
    window.open_workspace_page('document', 'default-document')
    monkeypatch.setattr(window.document_panel, 'store_to_scene', lambda: False)
    chooser = Mock(side_effect=AssertionError('must not begin stale export'))
    monkeypatch.setattr(export_workflow.QtWidgets.QFileDialog, 'getSaveFileName', chooser)
    notice = Mock()
    monkeypatch.setattr(export_workflow.widgets, 'PrismNotification', notice)
    export_workflow.export_document(window.view)
    assert export_workflow._store_open_panel(window.view) is False
    chooser.assert_not_called()
    notice.assert_called_once()


def test_failed_mindmap_readback_does_not_overwrite_scene(main_window, monkeypatch):
    panel = main_window.mindmap_panel
    original = {'root': 'keep saved map', 'children': []}
    panel._set_scene_tree(original)
    panel._tree = {'root': 'cached map', 'children': []}
    panel._ready = True
    panel._pending_tree = None
    panel._tree_loading = False
    panel.web_view = SimpleNamespace(page=lambda: None)
    monkeypatch.setattr('prism.widgets.editor_snapshot.read_editor_snapshot', lambda *a, **k: None)
    assert panel.store_to_scene() is False
    assert panel._scene_tree() == original
    panel._ready = False
    panel.web_view = None
