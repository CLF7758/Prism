"""Material reveal is one committed navigation intent, including selection."""
from unittest.mock import Mock


def delayed_document(window, monkeypatch):
    window.open_workspace_page('document', 'default-document')
    callbacks = []
    monkeypatch.setattr(window.document_panel, 'store_to_scene_async', callbacks.append)
    return callbacks


def test_material_reveal_waits_for_editor_commit_before_open_and_select(main_window, monkeypatch):
    window = main_window
    page = window.material_library
    record = page.records.create(name='Target')
    replies = delayed_document(window, monkeypatch)
    selected = Mock(wraps=page._selection_changed)
    monkeypatch.setattr(page, '_selection_changed', selected)
    reveal = Mock()
    monkeypatch.setattr(page.materials, 'reveal_record', reveal)
    previous_selection = page.selected_id
    assert page.reveal_material(record['id'], from_search=True)
    assert len(replies) == 1
    assert window.tabs.currentWidget() is window.document_panel
    assert page.selected_id == previous_selection
    selected.assert_not_called()
    reveal.assert_not_called()
    replies[0](True)
    assert window.tabs.currentWidget() is page
    assert page.selected_id == record['id']
    selected.assert_called_once_with(record['id'])
    reveal.assert_called_once_with(record['id'])
    page._auto_timer.stop()


def test_old_project_material_reveal_cannot_select_current_project(main_window, monkeypatch):
    window = main_window
    page = window.material_library
    record = page.records.create(name='Old target')
    replies = delayed_document(window, monkeypatch)
    selected = Mock()
    monkeypatch.setattr(page, '_selection_changed', selected)
    assert page.reveal_material(record['id'])
    # Test the consumer's own guard, independently of the navigation host guard.
    intent = window._navigation_intent
    window.view.scene.project_id += '-replacement'
    intent()
    replies[0](True)
    selected.assert_not_called()
    assert window.tabs.currentWidget() is window.document_panel


def test_material_reveal_revalidates_deleted_target_after_commit(main_window, monkeypatch):
    window = main_window
    page = window.material_library
    record = page.records.create(name='Removed while committing')
    replies = delayed_document(window, monkeypatch)
    selected = Mock()
    monkeypatch.setattr(page, '_selection_changed', selected)
    assert page.reveal_material(record['id'])
    page.records.records[0]['deletedAt'] = '2026-10-21T00:00:00+00:00'
    replies[0](True)
    selected.assert_not_called()
    assert window.tabs.currentWidget() is window.document_panel


def test_material_reveal_revalidates_folder_after_commit(main_window, monkeypatch):
    window = main_window
    page = window.material_library
    record = page.records.create(name='Moved while committing')
    folder = page.service.createFolder('material', 'New folder')
    replies = delayed_document(window, monkeypatch)
    assert page.reveal_material(record['id'])
    page.records.records[0]['folderId'] = folder['id']
    replies[0](True)
    assert window.tabs.currentWidget() is page
    assert page.folder_id == folder['id'] and page.selected_id == record['id']
    page._auto_timer.stop()
