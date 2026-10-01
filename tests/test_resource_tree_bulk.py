"""C7：多选与批量（§7.5）。

规范那一段：Ctrl 非连续多选、Shift 同分区可见行范围；**选择只在同一分区内，
跨分区操作清除旧选择**；父子同时选中时归一化为最上层祖先；批量时右键给出
移动/删除，并显示「已选择 N 项」。
"""
from unittest.mock import patch

from PyQt6 import QtCore, QtWidgets
from PyQt6.QtCore import Qt

from prism.workspace_service import ResourceTreeService
from prism.widgets.move_to_dialog import MoveToDialog
from prism.widgets.resource_tree_model import ResourceTreeModel
from prism.widgets.resource_tree_view import ResourceTreeView
from tests.resource_tree_helpers import create_page, orphan_report


def make_tree(qtbot):
    service = ResourceTreeService('bulk-project')
    model = ResourceTreeModel(service)
    view = ResourceTreeView()
    qtbot.addWidget(view)
    view.setModel(model)
    view.resize(280, 420)
    return service, model, view


def select_rows(view, model, node_ids):
    selection = view.selectionModel()
    for position, node_id in enumerate(node_ids):
        index = model.index_for_id(node_id)
        flag = (QtCore.QItemSelectionModel.SelectionFlag.ClearAndSelect
                if position == 0 else
                QtCore.QItemSelectionModel.SelectionFlag.Select)
        selection.select(index, flag | QtCore.QItemSelectionModel.SelectionFlag.Rows)


# ── 选择只在同一分区 ─────────────────────────────────────────────────

def test_a_selection_never_spans_two_sections(qtbot):
    service, model, view = make_tree(qtbot)
    doc = service.createFolder('document', '文档夹')
    mind = service.createFolder('mindmap', '脑图夹')

    # 先选文档的，再 Ctrl 加选脑图的：后者成了焦点，越界的旧选择要被清掉
    view.setCurrentIndex(model.index_for_id(doc['id']))
    select_rows(view, model, [doc['id']])
    view.setCurrentIndex(model.index_for_id(mind['id']))
    select_rows(view, model, [mind['id']])

    assert view.selected_ids() == [mind['id']], '不该同时选着两个分区'


def test_rows_of_the_same_section_stay_selected(qtbot):
    service, model, view = make_tree(qtbot)
    first = service.createFolder('document', '一')
    second = service.createFolder('document', '二')
    view.setCurrentIndex(model.index_for_id(first['id']))
    select_rows(view, model, [first['id'], second['id']])
    assert set(view.selected_ids()) == {first['id'], second['id']}


# ── 批量菜单 ──────────────────────────────────────────────────────────

def test_the_menu_shows_bulk_actions_only_for_a_multi_selection(qtbot):
    service, model, view = make_tree(qtbot)
    first = service.createFolder('document', '一')
    second = service.createFolder('document', '二')
    index = model.index_for_id(first['id'])

    select_rows(view, model, [first['id']])
    labels = [action.text() for action in view.menu_for_index(index).actions()]
    assert not any('已选择' in label for label in labels)

    select_rows(view, model, [first['id'], second['id']])
    menu = view.menu_for_index(index)
    labels = [action.text() for action in menu.actions()]
    assert labels[0] == '已选择 2 项'
    assert not menu.actions()[0].isEnabled()
    assert '移动到…' in labels and '删除' in labels


def test_bulk_delete_asks_once_with_the_counts(main_window):
    window = main_window
    panel = window.view.category_panel
    service = panel.resource_service
    first = service.createFolder('document', '一')
    second = service.createFolder('document', '二')
    second_child = service.createFolder('document', '二的孩子', second['id'])
    create_page(window, 'document', '二里的页', second_child['id'])

    asked = {}

    def fake_question(parent, title, text, *args, **kwargs):
        asked['text'] = text
        return QtWidgets.QMessageBox.StandardButton.Yes

    with patch('PyQt6.QtWidgets.QMessageBox.question',
               side_effect=fake_question):
        window._delete_resource_items([first['id'], second['id']])

    assert '含 1 个文件夹、1 个页面' in asked['text'], asked['text']
    assert service._by_id(first['id'])['deletedAt']
    assert service._by_id(second['id'])['deletedAt']
    assert window._trash_toast is not None


def test_an_empty_selection_is_not_deleted(main_window):
    window = main_window
    window._delete_resource_items([])
    assert window._trash_toast is None


def test_a_parent_and_child_in_the_batch_are_handled_once(main_window):
    window = main_window
    panel = window.view.category_panel
    service = panel.resource_service
    parent = service.createFolder('document', '父')
    child = service.createFolder('document', '子', parent['id'])

    assert window._top_ancestors([parent['id'], child['id']]) == [parent['id']]
    assert window._top_ancestors([child['id'], parent['id']]) == [parent['id']]


def test_bulk_delete_can_be_undone_in_one_click(main_window):
    window = main_window
    panel = window.view.category_panel
    service = panel.resource_service
    first = service.createFolder('document', '一')
    second = service.createFolder('document', '二')
    with patch('PyQt6.QtWidgets.QMessageBox.question',
               return_value=QtWidgets.QMessageBox.StandardButton.Yes):
        window._delete_resource_items([first['id'], second['id']])

    window._trash_toast.undo_button.click()

    assert service._by_id(first['id'])['deletedAt'] is None
    assert service._by_id(second['id'])['deletedAt'] is None


def test_bulk_move_goes_through_the_service(main_window):
    window = main_window
    panel = window.view.category_panel
    service = panel.resource_service
    target = service.createFolder('document', '目标')
    first = service.createFolder('document', '一')
    second = service.createFolder('document', '二')

    with patch.object(MoveToDialog, 'exec',
                      return_value=QtWidgets.QDialog.DialogCode.Accepted), \
            patch.object(MoveToDialog, 'target',
                         return_value=('document', target['id'])):
        window._move_resource_items([first['id'], second['id']])

    moved = [n['id'] for n in service.listChildren('document', target['id'])]
    assert set(moved) == {first['id'], second['id']}
    assert orphan_report(window.view.scene) == {'只在树里': [],
                                                '只在页面表里': []}


def test_a_cancelled_bulk_move_changes_nothing(main_window):
    window = main_window
    panel = window.view.category_panel
    service = panel.resource_service
    target = service.createFolder('document', '目标')
    mover = service.createFolder('document', '一')

    with patch.object(MoveToDialog, 'exec',
                      return_value=QtWidgets.QDialog.DialogCode.Rejected):
        window._move_resource_items([mover['id']])

    assert service._by_id(mover['id'])['parentId'] is None
    assert service.listChildren('document', target['id']) == []


# ── 「移动到…」对话框 ────────────────────────────────────────────────

def make_dialog(qtbot, view, node_ids):
    dialog = MoveToDialog(view, node_ids, None)
    qtbot.addWidget(dialog)
    return dialog


def collect_labels(tree):
    labels = []

    def walk(item):
        for row in range(item.childCount()):
            child = item.child(row)
            labels.append(child.text(0))
            walk(child)

    for row in range(tree.topLevelItemCount()):
        top = tree.topLevelItem(row)
        labels.append(top.text(0))
        walk(top)
    return labels


def test_the_dialog_lists_folders_but_not_pages(main_window, qtbot):
    window = main_window
    panel = window.view.category_panel
    folder = panel.resource_service.createFolder('document', '第一章')
    create_page(window, 'document', '页不能当目标', folder['id'])
    mover = panel.resource_service.createFolder('document', '要移的')

    dialog = make_dialog(qtbot, window.view, [mover['id']])
    labels = collect_labels(dialog.tree)

    assert '第一章' in labels
    assert '页不能当目标' not in labels, '页面不能当移动目标'
    assert dialog.tree.topLevelItemCount() == 1
    assert dialog.tree.topLevelItem(0).data(0, Qt.ItemDataRole.UserRole)[0] == 'document'


def test_the_dialog_hides_the_nodes_being_moved(main_window, qtbot):
    window = main_window
    panel = window.view.category_panel
    service = panel.resource_service
    parent = service.createFolder('document', '被移的')
    child = service.createFolder('document', '它的孩子', parent['id'])
    other = service.createFolder('document', '可以去的')

    dialog = make_dialog(qtbot, window.view, [parent['id']])
    labels = collect_labels(dialog.tree)

    assert '可以去的' in labels
    assert '被移的' not in labels, '不能把自己当目标'
    assert '它的孩子' not in labels, '不能移进自己的后代'


def test_the_dialog_shows_the_full_target_path(main_window, qtbot):
    window = main_window
    panel = window.view.category_panel
    first = panel.resource_service.createFolder('document', '第一章')
    second = panel.resource_service.createFolder('document', '外景', first['id'])
    mover = panel.resource_service.createFolder('document', '要移的')

    dialog = make_dialog(qtbot, window.view, [mover['id']])
    dialog.tree.setCurrentItem(_find_item(dialog.tree, '外景'))

    assert '文档 / 第一章 / 外景' in dialog.path_label.text()
    assert dialog.target() == ('document', second['id'])


def _find_item(tree, title):
    def walk(item):
        for row in range(item.childCount()):
            child = item.child(row)
            if child.text(0) == title:
                return child
            found = walk(child)
            if found:
                return found
        return None

    for row in range(tree.topLevelItemCount()):
        top = tree.topLevelItem(row)
        if top.text(0) == title:
            return top
        found = walk(top)
        if found:
            return found
    return None


def test_the_dialog_searches_folders(main_window, qtbot):
    window = main_window
    panel = window.view.category_panel
    service = panel.resource_service
    first = service.createFolder('document', '第一章')
    service.createFolder('document', '外景', first['id'])
    service.createFolder('document', '人物设定')
    mover = service.createFolder('document', '要移的')

    dialog = make_dialog(qtbot, window.view, [mover['id']])
    dialog.search.setText('外景')
    labels = collect_labels(dialog.tree)

    assert '外景' in labels
    assert '人物设定' not in labels
