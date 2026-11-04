"""Renderer navigation never waits in a nested event loop."""
from prism.widgets.editor_snapshot import request_editor_snapshot


def test_snapshot_async_returns_before_renderer_and_completes_once(qtbot):
    class Page:
        def runJavaScript(self, script, callback):
            self.reply = callback
    page = Page()
    results = []
    request_editor_snapshot(page, 'snapshot', results.append, timeout_ms=50)
    assert results == []
    page.reply('latest text')
    page.reply('late duplicate')
    qtbot.wait(70)
    assert results == ['latest text']


def test_snapshot_timeout_and_invalid_result_fail_closed(qtbot):
    class Page:
        def runJavaScript(self, script, callback):
            self.reply = callback
    page = Page()
    results = []
    request_editor_snapshot(page, 'snapshot', results.append, timeout_ms=10)
    qtbot.waitUntil(lambda: bool(results))
    page.reply('too late')
    assert results == [None]
    request_editor_snapshot(page, 'snapshot', results.append, valid=lambda: False)
    page.reply('wrong project')
    assert results == [None, None]


def test_latest_navigation_wins_without_losing_commit(main_window, monkeypatch):
    window = main_window
    window.open_workspace_page('document', 'default-document')
    replies = []
    monkeypatch.setattr(window.document_panel, 'store_to_scene_async', replies.append)
    window.open_workspace_page('canvas', 'default-canvas')
    window.open_material_library()
    assert len(replies) == 1
    assert window.tabs.currentWidget() is window.document_panel
    assert not window.document_panel.isEnabled()
    assert window._store_current_page() is False
    window.view.scene.note_html = '<p>last input</p>'
    replies[0](True)
    assert window.tabs.currentWidget() is window.material_library
    assert window.view.scene.note_html == '<p>last input</p>'
    assert window.document_panel.isEnabled()
    assert not window._page_store_in_progress


def test_failed_or_old_project_commit_does_not_navigate(main_window, monkeypatch):
    window = main_window
    window.open_workspace_page('document', 'default-document')
    replies = []
    monkeypatch.setattr(window.document_panel, 'store_to_scene_async', replies.append)
    window.open_material_library()
    replies.pop()(False)
    assert window.tabs.currentWidget() is window.document_panel
    assert window.document_panel.isEnabled()
    window.open_material_library()
    window.view.scene.project_id += '-replacement'
    replies.pop()(True)
    assert window.tabs.currentWidget() is window.document_panel
    assert window._navigation_intent is None


def test_direct_tab_keeps_old_editor_until_async_commit(main_window, monkeypatch):
    window = main_window
    window.open_workspace_page('document', 'default-document')
    replies = []
    monkeypatch.setattr(window.document_panel, 'store_to_scene_async', replies.append)
    window.tabs.setCurrentWidget(window.canvas_page)
    assert window.tabs.currentWidget() is window.document_panel
    replies.pop()(True)
    assert window.tabs.currentWidget() is window.canvas_page


def test_synchronous_save_barrier_does_not_leave_a_queued_navigation(main_window):
    window = main_window
    window._page_store_in_progress = True
    try:
        window.open_material_library()
    finally:
        window._page_store_in_progress = False
    assert getattr(window, '_navigation_intent', None) is None
    assert window.tabs.currentWidget() is window.canvas_page


def test_async_commit_restores_original_disabled_state_and_ignores_duplicates(main_window, monkeypatch):
    window = main_window
    window.open_workspace_page('document', 'default-document')
    panel = window.document_panel
    panel.setEnabled(False)
    replies = []
    monkeypatch.setattr(panel, 'store_to_scene_async', replies.append)
    intents = []
    window.request_navigation(lambda: intents.append('committed'))
    replies[0](True)
    replies[0](True)
    assert intents == ['committed']
    assert not panel.isEnabled()
    panel.setEnabled(True)


def test_document_replacement_invalidates_old_renderer_snapshot(main_window):
    class Page:
        def runJavaScript(self, script, callback):
            self.reply = callback
    panel = main_window.document_panel
    page = Page()
    panel._content_loaded = True
    panel._ready = True
    panel._page = page
    panel._pending_push = False
    results = []
    panel.store_to_scene_async(results.append)
    panel._snapshot_epoch = getattr(panel, '_snapshot_epoch', 0) + 1
    panel._set_scene_html('<p>new imported content</p>')
    page.reply('<p>old renderer content</p>')
    assert results == [False]
    assert panel._scene_html() == '<p>new imported content</p>'
    panel._ready = False
    panel._page = None
