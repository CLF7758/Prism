"""Actions must wait for navigation before using or mutating a destination."""
from unittest.mock import Mock

from PyQt6 import QtGui
from prism.actions import capture_workflow
from tests.test_asset_browser import photo
from tests.test_unified_project_search import page


def pending_document(window, monkeypatch):
    window.open_workspace_page('document', 'default-document')
    replies = []
    monkeypatch.setattr(window.document_panel, 'store_to_scene_async', replies.append)
    return replies


def test_capture_insertion_waits_for_target_canvas(main_window, monkeypatch):
    window = main_window
    target = page(window, 'canvas', 'Capture destination')
    replies = pending_document(window, monkeypatch)
    calls = []
    monkeypatch.setattr(window.view, 'do_insert_images', lambda *args, **kwargs:
                       calls.append((window.current_page_id, window.tabs.currentWidget(), args)))
    capture_workflow.import_capture(window.view, dict(project_id=window.view.scene.project_id,
        canvas_id=target['id'], x=15, y=20), 'captured-original.png')
    assert calls == []
    assert window.tabs.currentWidget() is window.document_panel
    replies.pop()(True)
    assert calls == [(target['id'], window.canvas_page, (['captured-original.png'],))]


def test_failed_capture_commit_does_not_insert(main_window, monkeypatch):
    window = main_window
    replies = pending_document(window, monkeypatch)
    insert = Mock()
    monkeypatch.setattr(window.view, 'do_insert_images', insert)
    capture_workflow.import_capture(window.view, dict(project_id=window.view.scene.project_id,
        canvas_id='default-canvas', x=0, y=0), 'retained-capture.png')
    replies.pop()(False)
    insert.assert_not_called()
    assert window.tabs.currentWidget() is window.document_panel


def test_media_location_waits_until_page_is_open(main_window, monkeypatch):
    window = main_window
    target = page(window, 'canvas', 'Destination')
    item = photo(window, target['id'], 'to locate')
    replies = pending_document(window, monkeypatch)
    centered = []
    monkeypatch.setattr(window.view, 'centerOn', lambda *args:
                       centered.append((window.current_page_id, args)))
    window.view.category_panel.activate_project_result(dict(kind='asset', item=item,
        page_kind='canvas', page_id=target['id']))
    assert centered == []
    replies.pop()(True)
    assert centered[-1] == (target['id'], (item,))
    assert item.isSelected()


def test_import_target_helper_cannot_return_inactive_panel(main_window, monkeypatch):
    window = main_window
    page(window, 'mindmap', 'Import destination')
    replies = pending_document(window, monkeypatch)
    window.open_material_library()
    assert window.view._import_target_panel('MindMapPanel', 'mindmap') is None
    replies.pop()(False)
    assert window.tabs.currentWidget() is window.document_panel


def test_import_menu_does_not_prompt_until_commit(main_window, monkeypatch):
    window = main_window
    replies = pending_document(window, monkeypatch)
    chooser = Mock(return_value=('', ''))
    monkeypatch.setattr('prism.actions.import_workflow.QtWidgets.QFileDialog.getOpenFileName', chooser)
    window.view.on_action_import_document()
    chooser.assert_not_called()
    replies.pop()(True)
    chooser.assert_called_once()


def test_direct_import_workflow_uses_same_barrier(main_window, monkeypatch):
    from prism.actions.import_workflow import import_document
    window = main_window
    replies = pending_document(window, monkeypatch)
    chooser = Mock(return_value=('', ''))
    monkeypatch.setattr('prism.actions.import_workflow.QtWidgets.QFileDialog.getOpenFileName', chooser)
    import_document(window.view)
    chooser.assert_not_called()
    replies.pop()(False)
    chooser.assert_not_called()


def test_search_result_location_waits_for_commit(main_window, monkeypatch):
    window = main_window
    target = page(window, 'canvas', 'Destination')
    photo(window, target['id'], 'needle image')
    document = page(window, 'document', 'Document', '<p>needle text</p>')
    controller = window.search_controller
    controller.input.setText('needle')
    controller.submit()
    document_hit = next(hit for hit in controller.results if hit.get('page_id') == document['id'])
    image_hit = next(hit for hit in controller.results if hit['kind'] == 'asset')
    controller.open_result(document_hit)
    replies = []
    panel = window.tabs.currentWidget()
    monkeypatch.setattr(panel, 'store_to_scene_async', replies.append)
    located = []
    monkeypatch.setattr(controller, '_locate_item', lambda item:
                       located.append((window.current_page_id, item)))
    controller.open_result(image_hit)
    assert located == []
    replies.pop()(True)
    assert located[-1][0] == target['id']
    assert window.tabs.currentWidget() is window.canvas_page


def test_video_import_waits_for_target_canvas(main_window, monkeypatch):
    from prism.widgets.video_download import VideoDownloadDialog
    window = main_window
    replies = pending_document(window, monkeypatch)
    dialog = VideoDownloadDialog(window.view)
    result = dict(path='retained-original.mp4', context=dict(
        project_id=window.view.scene.project_id, canvas_id='default-canvas'))
    monkeypatch.setattr(dialog, 'selected_result', lambda: result)
    calls = []
    monkeypatch.setattr(window.view, 'do_insert_images', lambda *args, **kwargs:
                       calls.append((window.current_page_id, args, kwargs)))
    dialog.import_video()
    assert calls == []
    replies.pop()(True)
    assert calls == [('default-canvas', (['retained-original.mp4'],), {'force_video_embed': True})]
    dialog.service.close()
    dialog.deleteLater()


def test_detail_location_keeps_requested_item_while_commit_pending(main_window, monkeypatch):
    window = main_window
    target = page(window, 'canvas', 'Detail destination')
    item = photo(window, target['id'], 'first item')
    other = photo(window, 'default-canvas', 'second item')
    replies = pending_document(window, monkeypatch)
    detail = window.view._detail_panel
    detail._item = item
    detail._search_result = None
    located = []
    monkeypatch.setattr(window.view, 'centerOn', lambda *args:
                       located.append((window.current_page_id, args)))
    detail._open_item_canvas()
    detail._item = other
    assert located == []
    replies.pop()(True)
    assert located[-1] == (target['id'], (item,))


def test_canvas_undo_waits_for_search_page_navigation(main_window, monkeypatch):
    window = main_window
    window.tabs.setCurrentWidget(window.search_controller.page)
    window.view.undo_stack.push(QtGui.QUndoCommand('canvas edit'))
    before = window.view.undo_stack.index()
    replies = []
    monkeypatch.setattr(window.search_controller.page, 'store_to_scene_async', replies.append, raising=False)
    monkeypatch.setattr('prism.view.QtWidgets.QApplication.focusWidget', lambda: None)
    window.view.on_action_undo()
    assert window.view.undo_stack.index() == before
    replies.pop()(True)
    assert window.tabs.currentWidget() is window.canvas_page
    assert window.view.undo_stack.index() == before - 1


def test_import_without_canvas_waits_before_creating_destination(main_window, monkeypatch):
    from prism import asset_import_service
    window = main_window
    replies = pending_document(window, monkeypatch)
    window.view.current_canvas_id = None
    planned = []
    original_plan = asset_import_service.plan

    def plan(paths):
        planned.append(window.view.current_canvas_id)
        return original_plan(paths)

    monkeypatch.setattr(asset_import_service, 'plan', plan)
    monkeypatch.setattr('prism.view.widgets.PrismNotification', Mock())
    window.view.do_insert_images(['unsupported-format.zzz'])
    assert planned == []
    assert window.tabs.currentWidget() is window.document_panel
    replies.pop()(True)
    assert planned == [window.view.current_canvas_id]
    assert planned[0] is not None
    assert window.tabs.currentWidget() is window.canvas_page
