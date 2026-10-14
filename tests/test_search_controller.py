"""Search navigation must never become an edit of the original project."""
from unittest.mock import patch
from PyQt6 import QtCore, QtGui, QtWidgets
from tests.test_asset_browser import photo
from tests.test_unified_project_search import page
from prism.project_search import item_key


def submit(window, text):
    controller = window.search_controller
    controller.input.setText(text)
    controller.submit()
    return controller


def test_fixed_input_survives_page_and_sidebar_changes(main_window):
    controller = main_window.search_controller
    assert controller.input.parentWidget() is controller.header
    assert controller.type_combo.parentWidget() is controller.header
    for widget in (main_window.asset_browser, main_window.canvas_page):
        main_window.tabs.setCurrentWidget(widget)
        assert not controller.input.isHidden()
        assert main_window.view.category_panel.search.isHidden()


def test_cards_drag_preview_and_refresh_leave_original_unchanged(main_window):
    record = page(main_window, 'canvas', '原位置')
    item = photo(main_window, record['id'], '雨夜')
    item.setPos(250, 340)
    item.grayscale = True
    item.setSelected(True)
    before = (item.pos(), item.isVisible(), item.isSelected(), item.grayscale,
              getattr(item, '_last_used', None), main_window.view.undo_stack.count(), main_window.view.is_dirty())
    controller = submit(main_window, '雨夜')
    card = controller.page.cards['asset:'+item_key(item)]
    card.setSelected(True)
    card.setPos(1000, 500)
    controller.page.preview_selected()
    controller.page.close_preview()
    controller.submit()
    assert card.pos() == QtCore.QPointF(1000, 500)
    assert before == (item.pos(), item.isVisible(), item.isSelected(), item.grayscale,
                      getattr(item, '_last_used', None), main_window.view.undo_stack.count(), main_window.view.is_dirty())
    assert controller.page.view.scene() is not main_window.view.scene


def test_gray_selection_and_real_undo_do_not_create_false_commands(main_window):
    view = main_window.view
    a = photo(main_window, 'default-canvas', '灰度')
    b = photo(main_window, 'default-canvas', '彩色')
    a.grayscale = True
    before = view.undo_stack.count()
    for item in (a, b, a, b):
        view.scene.clearSelection()
        item.setSelected(True)
    assert view.undo_stack.count() == before
    b.setSelected(True)
    view.on_action_grayscale(True)
    assert view.undo_stack.count() == before+1
    for _ in range(3):
        view.undo_stack.undo()
        assert not b.grayscale
        view.undo_stack.redo()
        assert b.grayscale
    assert view.undo_stack.count() == before+1


def test_undo_stack_rejects_callback_push(main_window):
    stack = main_window.view.undo_stack
    class Reenter(QtGui.QUndoCommand):
        def undo(self):
            stack.push(QtGui.QUndoCommand('false edit'))
    stack.push(Reenter('real edit'))
    stack.undo()
    assert stack.count() == 1
    assert stack.index() == 0
    assert not stack.executing


def test_same_word_all_types_and_counts_include_over_200_results(main_window):
    doc = page(main_window, 'document', '记录', '<p>雨夜 雨夜 雨夜</p>')
    mind = page(main_window, 'mindmap', '故事', {'topic': '雨夜'})
    canvas = page(main_window, 'canvas', '雨夜画布')
    for index in range(205):
        photo(main_window, canvas['id'], f'雨夜{index}')
    controller = submit(main_window, '雨夜')
    assert len(controller.results) == 208
    assert len(controller.page.cards) == 208
    hit = next(r for r in controller.results if r.get('page_id') == doc['id'])
    assert len([m for m in hit['matches'] if m['field'] == 'body']) == 3
    assert '205' in controller.page.type_buttons['asset'].text()
    assert main_window.view.category_panel.resource_tree.isHidden() is False
    assert main_window.view.category_panel.search_results.isHidden()


def test_current_page_scope_and_type_do_not_hide_conditions(main_window):
    doc = page(main_window, 'document', '记录', '<p>雨夜</p>')
    other = page(main_window, 'document', '别页', '<p>雨夜</p>')
    controller = submit(main_window, '雨夜')
    controller.set_scope('page', ('document', doc['id']), '记录')
    controller.submit()
    assert [r['page_id'] for r in controller.results] == [doc['id']]
    controller.set_asset_filter(3, 'red')
    assert controller.library.search_kind == 'asset'
    controller.library.set_search_kind('document')
    controller.submit()
    assert controller.min_rating == 0 and controller.color is None
    assert [r['page_id'] for r in controller.results] == [doc['id']]


def test_old_result_rejected_after_purge_or_project_change(main_window):
    doc = page(main_window, 'document', '记录', '<p>雨夜</p>')
    controller = submit(main_window, '雨夜')
    hit = next(r for r in controller.results if r.get('page_id') == doc['id'])
    main_window.view.scene.workspace_pages.remove(doc)
    with patch.object(main_window, 'open_workspace_page') as opened:
        controller.open_result(hit)
    opened.assert_not_called()
    assert not controller.valid_result(hit | {'project_id': 'another-project'})


def test_open_return_exit_preserves_result_view_and_source_origin(main_window):
    origin = page(main_window, 'canvas', '原画布')
    doc = page(main_window, 'document', '记录', '<p>雨夜</p>')
    main_window.open_workspace_page('canvas', origin['id'])
    original_transform = main_window.view.transform()
    controller = submit(main_window, '雨夜')
    hit = next(r for r in controller.results if r.get('page_id') == doc['id'])
    controller.page.cards[hit['result_id']].setSelected(True)
    controller.page.view.zoom(1.15)
    transform = controller.page.view.transform()
    with patch.object(main_window, 'open_workspace_page') as opened:
        controller.open_result(hit)
    opened.assert_called_once_with('document', doc['id'])
    controller.return_to_results()
    assert controller.input.text() == '雨夜'
    assert controller.page.view.transform() == transform
    assert controller.page.selected_result()['result_id'] == hit['result_id']
    controller.exit()
    assert main_window.current_page_id == origin['id']
    assert main_window.view.transform() == original_transform
    assert controller.input.text() == ''


def test_late_editor_snapshot_cannot_replace_new_query(main_window):
    doc = page(main_window, 'document', '记录', '<p>雨夜 红墙</p>')
    panel = main_window._page_panels[doc['id']]
    main_window.tabs.setCurrentWidget(panel)
    callbacks = []
    with patch.object(panel, 'search_snapshot', side_effect=callbacks.append):
        controller = submit(main_window, '雨夜')
        controller.input.setText('红墙')
        controller.submit()
    callbacks[0](True)
    assert controller.results == []
    callbacks[1](True)
    assert controller.results
    assert all(m['text'] == '红墙' for r in controller.results for m in r['matches'])


def test_ime_does_not_submit_until_commit(main_window):
    page(main_window, 'document', '记录', '<p>雨夜</p>')
    controller = main_window.search_controller
    controller.input.composing = True
    controller.input.setText('雨夜')
    controller.submit()
    assert controller.results == []
    controller.input.composing = False
    controller.submit()
    assert controller.results


def test_unclassified_respects_canvas_assignment(main_window):
    record = page(main_window, 'canvas', '自建')
    assigned = photo(main_window, 'default-canvas', '已分类')
    assigned._categories = ['参考']
    unassigned = photo(main_window, record['id'], '未分类')
    main_window.asset_browser.open('unclassified')
    assert main_window.asset_browser.model.items == [assigned]


def test_canvas_match_opens_and_locates_actual_object(main_window):
    record = page(main_window, 'canvas', '参考')
    item = photo(main_window, record['id'], '素材')
    item._notes = '雨夜'
    controller = submit(main_window, '雨夜')
    hit = next(r for r in controller.results if r['kind'] == 'canvas')
    controller.open_result(hit)
    assert main_window.current_page_id == record['id']
    assert item.isSelected()

