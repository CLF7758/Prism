from PyQt6 import QtCore
from tests.test_asset_browser import photo
from tests.test_search_controller import submit
from prism.project_search import item_key


def test_search_inspector_edits_result_without_changing_canvas_selection(main_window):
    view = main_window.view
    other = photo(main_window, 'default-canvas', '其他图片')
    target = photo(main_window, 'default-canvas', '搜索图片', ['参考'])
    other.setSelected(True)
    controller = submit(main_window, '搜索图片')
    before = view.undo_stack.count()
    controller.page.cards['asset:' + item_key(target)].setSelected(True)
    panel = view._detail_panel
    assert panel._item is target
    assert panel._selected_items() == [target]
    assert view.scene.selectedItems(user_only=True) == [other]
    assert view.undo_stack.count() == before
    assert panel._dims_label.text() == '24 × 24'
    assert '参考' in panel._tag_checks
    panel._set_rating(4)
    assert target._rating == 4
    assert getattr(other, '_rating', 0) == 0
    view.undo_stack.undo()
    assert target._rating == 0


def test_search_double_click_opens_owning_canvas_and_selects_image(main_window):
    canvas = main_window.workspace_service.add('canvas', '图片所在画布')['id']
    target = photo(main_window, canvas, '搜索图片')
    target.setPos(123, 456)
    controller = submit(main_window, '搜索图片')
    card = controller.page.cards['asset:' + item_key(target)]
    card.setSelected(True)
    before = main_window.view.undo_stack.count()
    controller.page.open_result.emit(card.result)
    assert main_window.tabs.currentWidget() is main_window.canvas_page
    assert main_window.view.current_canvas_id == canvas
    assert target.isSelected()
    assert target.pos() == QtCore.QPointF(123, 456)
    assert main_window.view._detail_panel._item is target
    assert main_window.view.undo_stack.count() == before
    controller.return_to_results()
    assert main_window.view._detail_panel._item is target
    main_window.view._detail_panel._open_item_canvas()
    assert main_window.view.current_canvas_id == canvas
    assert main_window.tabs.currentWidget() is main_window.canvas_page


def test_color_sampling_action_and_shortcut_are_removed(main_window):
    from prism.actions.actions import actions
    assert 'sample_color' not in actions
    assert not hasattr(main_window.view, 'action_sample_color')
