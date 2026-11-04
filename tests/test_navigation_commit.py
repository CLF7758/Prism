"""Each navigation commits the outgoing editor exactly once, never globally."""
from unittest.mock import Mock

import pytest


def install_commit(panel, monkeypatch, store):
    """Exercise navigation's callback API and save/export's synchronous API."""
    monkeypatch.setattr(panel, 'store_to_scene', store)
    monkeypatch.setattr(panel, 'store_to_scene_async',
                        lambda callback: callback(store()))


@pytest.mark.parametrize('target', ['canvas', 'material', 'assets', 'mindmap'])
def test_explicit_navigation_commits_outgoing_document_once(main_window, monkeypatch, target):
    window = main_window
    window.open_workspace_page('document', 'default-document')
    store = Mock(return_value=True)
    install_commit(window.document_panel, monkeypatch, store)
    if target == 'material':
        window.open_material_library()
        expected = window.material_library
    elif target == 'assets':
        window.asset_browser.open()
        expected = window.asset_browser
    else:
        window.open_workspace_page(target, f'default-{target}')
        expected = window.canvas_page if target == 'canvas' else window.mindmap_panel
    store.assert_called_once_with()
    assert window.tabs.currentWidget() is expected
    assert window._navigation_committed_page is None


def test_direct_tab_switch_and_later_save_are_not_skipped(main_window, monkeypatch):
    window = main_window
    window.open_workspace_page('document', 'default-document')
    document_store = Mock(return_value=True)
    install_commit(window.document_panel, monkeypatch, document_store)
    window.tabs.setCurrentWidget(window.canvas_page)
    document_store.assert_called_once()
    window.open_workspace_page('document', 'default-document')
    assert window._store_current_page()
    assert document_store.call_count == 2


def test_nested_navigation_cannot_reenter_editor_read(main_window, monkeypatch):
    window = main_window
    window.open_workspace_page('document', 'default-document')
    seen = []
    def store():
        seen.append(True)
        window.open_material_library()
        assert window.tabs.currentWidget() is window.document_panel
        assert window._store_current_page() is False
        window.view.scene.note_html = '<p>last edit retained</p>'
        return True
    install_commit(window.document_panel, monkeypatch, store)
    window.open_workspace_page('canvas', 'default-canvas')
    assert seen == [True]
    # A navigation requested while the first commit is pending supersedes it.
    assert window.tabs.currentWidget() is window.material_library
    assert window.view.scene.note_html == '<p>last edit retained</p>'
    assert not window._page_store_in_progress


def test_failed_commit_has_no_reusable_token_and_navigation_stays_put(main_window, monkeypatch):
    window = main_window
    window.open_workspace_page('document', 'default-document')
    store = Mock(return_value=False)
    install_commit(window.document_panel, monkeypatch, store)
    window.open_material_library()
    store.assert_called_once()
    assert window.tabs.currentWidget() is window.document_panel
    assert getattr(window, '_navigation_committed_page', None) is None
    assert not window._page_store_in_progress


def test_save_export_recovery_and_direct_tab_cannot_reenter_commit(main_window, monkeypatch, tmp_path):
    from prism.actions.export_workflow import _store_open_panel
    window = main_window
    window.open_workspace_page('document', 'default-document')
    window.view.mark_content_dirty()
    factory = Mock(side_effect=AssertionError('must not save a half-committed editor'))
    monkeypatch.setattr('prism.view.fileio.ThreadedIO', factory)
    target = tmp_path/'preserved.prism'
    target.write_bytes(b'previous contents')
    before = window.view.settings.value('Recovery/snapshot')
    def store():
        window.tabs.setCurrentWidget(window.canvas_page)
        assert window.tabs.currentWidget() is window.document_panel
        window.view.do_save(str(target), create_new=False, silent=True)
        assert _store_open_panel(window.view) is False
        window._write_recovery_snapshot()
        assert window.view.settings.value('Recovery/snapshot') == before
        return True
    install_commit(window.document_panel, monkeypatch, store)
    window.open_material_library()
    factory.assert_not_called()
    assert target.read_bytes() == b'previous contents'
    assert window.tabs.currentWidget() is window.canvas_page
