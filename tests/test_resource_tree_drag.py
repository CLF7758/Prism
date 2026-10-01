"""C6：拖拽移动（§7.5）。

分三层测：
- 模型/服务：拖放的语义（移入文件夹、插到兄弟前/后、移到分区末尾、拒绝非法目标）；
- 视图几何：顶部/底部 25% = 同级插入、文件夹中间 50% = 移入、页面中间不接受子项；
- 视图配置与悬停展开。

真机鼠标拖动（含禁止光标）不在自动化范围内，见报告。
"""
import pytest
from PyQt6 import QtCore, QtGui, QtWidgets
from PyQt6.QtCore import Qt

from prism.workspace_service import ResourceTreeService
from prism.widgets.resource_tree_model import (
    NODE_MIME, NODE_TYPE_ROLE, ResourceTreeModel)
from prism.widgets.resource_tree_view import ResourceTreeView


def make_tree(qtbot, show=False):
    service = ResourceTreeService('project')
    model = ResourceTreeModel(service)
    view = ResourceTreeView()
    qtbot.addWidget(view)
    view.setModel(model)
    view.resize(280, 420)
    if show:
        view.show()
        qtbot.waitExposed(view)
    return service, model, view


# ── 模型/服务：拖放语义 ───────────────────────────────────────────────

def test_mime_data_carries_the_node_ids(qtbot):
    service, model, view = make_tree(qtbot)
    folder = service.createFolder('document', '第一章')
    page = service.createPage('document', '正文', 'page-1', folder['id'])
    data = model.mimeData([model.index_for_id(folder['id']),
                           model.index_for_id(page['id'])])
    assert data.hasFormat(NODE_MIME)
    assert set(model.dragged_ids(data)) == {folder['id'], page['id']}


def test_dropping_into_a_folder_moves_the_node(qtbot):
    service, model, view = make_tree(qtbot)
    target = service.createFolder('document', '目标')
    page = service.createPage('document', '正文', 'page-1')

    assert model.apply_drop([page['id']], 'into', target['id'])
    assert service._by_id(page['id'])['parentId'] == target['id']
    assert [n['id'] for n in service.listChildren('document', target['id'])] == \
        [page['id']]


def test_dropping_before_a_sibling_reorders_it(qtbot):
    service, model, view = make_tree(qtbot)
    first = service.createFolder('document', 'A')
    second = service.createFolder('document', 'B')

    assert model.apply_drop([second['id']], 'before', first['id'])
    assert [n['title'] for n in service.listChildren('document')] == ['B', 'A']


def test_dropping_after_a_sibling_reorders_it(qtbot):
    service, model, view = make_tree(qtbot)
    first = service.createFolder('document', 'A')
    second = service.createFolder('document', 'B')

    assert model.apply_drop([first['id']], 'after', second['id'])
    assert [n['title'] for n in service.listChildren('document')] == ['B', 'A']


def test_dropping_on_the_section_appends_to_the_end(qtbot):
    service, model, view = make_tree(qtbot)
    first = service.createFolder('document', 'A')
    second = service.createFolder('document', 'B')

    assert model.apply_drop([first['id']], 'append', None,
                            section='document')
    assert [n['title'] for n in service.listChildren('document')] == ['B', 'A']


def test_a_page_refuses_to_accept_children(qtbot):
    service, model, view = make_tree(qtbot)
    page = service.createPage('document', '正文', 'page-1')
    other = service.createFolder('document', '文件夹')

    reason = model.can_accept([other['id']], 'into', page['id'])
    assert reason, '页面中间不该接受子节点（§7.5）'
    assert model.apply_drop([other['id']], 'into', page['id']) is False
    assert service._by_id(other['id'])['parentId'] is None


def test_dragging_a_folder_into_its_own_child_is_refused(qtbot):
    service, model, view = make_tree(qtbot)
    parent = service.createFolder('document', '父')
    child = service.createFolder('document', '子', parent['id'])

    reason = model.can_accept([parent['id']], 'into', child['id'])
    assert reason, '不能移动到自己的子项里'
    assert model.apply_drop([parent['id']], 'into', child['id']) is False
    assert service._by_id(parent['id'])['parentId'] is None


def test_a_cross_section_drop_is_refused(qtbot):
    service, model, view = make_tree(qtbot)
    doc = service.createFolder('document', '文档夹')
    mind = service.createFolder('mindmap', '脑图夹')

    reason = model.can_accept([doc['id']], 'into', mind['id'])
    assert reason, '不能跨分区移动'
    assert model.apply_drop([doc['id']], 'into', mind['id']) is False


def test_a_parent_and_its_child_move_as_one(qtbot):
    """TREE-07：父子一起选时只处理最高祖先，孩子跟着父亲走。"""
    service, model, view = make_tree(qtbot)
    target = service.createFolder('document', '目标')
    parent = service.createFolder('document', '父')
    child = service.createFolder('document', '子', parent['id'])
    grand = service.createPage('document', '孙', 'page-1', child['id'])

    assert model.apply_drop([parent['id'], child['id'], grand['id']],
                            'into', target['id'])
    assert service._by_id(parent['id'])['parentId'] == target['id']
    assert service._by_id(child['id'])['parentId'] == parent['id']
    assert service._by_id(grand['id'])['parentId'] == child['id']
    assert len(service.listChildren('document', target['id'])) == 1, \
        '只该多出父亲一项'


# ── 视图几何：25% / 50% ──────────────────────────────────────────────

def row_rect(view, index):
    # offscreen 下 view 要真的跑一轮事件循环才会把行布局出来。
    view.expandAll()
    QtWidgets.QApplication.processEvents()
    rect = view.visualRect(index)
    assert rect.height() > 0, '行还没有布局好'
    return rect


def select_only(view, model, node_id):
    """把「要拖动的那个」选中 —— 真实拖动时选中的才是被拖的东西。

    焦点也要跟着走：C7 之后「选择只在同一分区内」，焦点留在别的分区时
    程序化选出来的行会被当成越界清掉（真人点击本来就会移焦点）。
    """
    index = model.index_for_id(node_id)
    view.setCurrentIndex(index)
    view.selectionModel().select(
        index,
        QtCore.QItemSelectionModel.SelectionFlag.ClearAndSelect |
        QtCore.QItemSelectionModel.SelectionFlag.Rows)


def test_the_top_and_bottom_quarters_mean_sibling_insert(qtbot):
    service, model, view = make_tree(qtbot, show=True)
    mover = service.createFolder('document', '要移动的')
    target = service.createFolder('document', '目标')
    index = model.index_for_id(target['id'])
    rect = row_rect(view, index)
    select_only(view, model, mover['id'])

    top = QtCore.QPoint(rect.center().x(), rect.top() + rect.height() // 10)
    bottom = QtCore.QPoint(rect.center().x(),
                           rect.bottom() - rect.height() // 10)
    assert view.drop_intent_for(index, top)[0] == ('before', target['id'])
    assert view.drop_intent_for(index, bottom)[0] == ('after', target['id'])


def test_the_middle_of_a_folder_means_move_inside(qtbot):
    service, model, view = make_tree(qtbot, show=True)
    mover = service.createFolder('document', '要移动的')
    target = service.createFolder('document', '目标文件夹')
    index = model.index_for_id(target['id'])
    rect = row_rect(view, index)
    select_only(view, model, mover['id'])
    assert view.drop_intent_for(index, rect.center())[0] == \
        ('into', target['id'])


def test_the_middle_of_a_page_ranks_before_or_after_never_inside(qtbot):
    """§7.5：页面中间不得接受子节点，只能按上/下位置排序。"""
    service, model, view = make_tree(qtbot, show=True)
    page = service.createPage('document', '正文', 'page-1')
    mover = service.createFolder('document', '要移动的')
    index = model.index_for_id(page['id'])
    rect = row_rect(view, index)
    select_only(view, model, mover['id'])

    intent, reason = view.drop_intent_for(index, rect.center())
    assert intent is not None, '页面上还是可以排前后'
    assert intent[0] in ('before', 'after'), '但不能塞成它的子项'
    assert intent[1] == page['id']
    assert reason == ''
    # 上四分之一和中间一样是同级插入，只是落点不同
    upper = QtCore.QPoint(rect.center().x(), rect.top() + rect.height() // 10)
    assert view.drop_intent_for(index, upper)[0] == ('before', page['id'])


def test_the_partition_row_and_blank_row_mean_move_to_the_section(qtbot):
    service, model, view = make_tree(qtbot, show=True)
    service.createFolder('document', '文件夹')
    mover = service.createFolder('document', '要移动的')
    select_only(view, model, mover['id'])

    document = model.index(2, 0)
    assert view.drop_intent_for(document,
                                row_rect(view, document).center())[0] == \
        ('append', None)
    # 空白创建区是这个分区的最后一行
    blank = model.index(model.rowCount(document) - 1, 0, document)
    assert blank.data(NODE_TYPE_ROLE) == 'blank'
    assert view.drop_intent_for(blank, row_rect(view, blank).center())[0] == \
        ('append', None)


# ── 视图配置与悬停展开 ───────────────────────────────────────────────

def test_the_view_drags_internally_as_move(qtbot):
    service, model, view = make_tree(qtbot)
    assert view.dragDropMode() == \
        QtWidgets.QAbstractItemView.DragDropMode.InternalMove
    assert view.defaultDropAction() == Qt.DropAction.MoveAction


def test_hovering_a_collapsed_folder_expands_it(qtbot):
    service, model, view = make_tree(qtbot, show=True)
    folder = service.createFolder('document', '文件夹')
    service.createFolder('document', '子夹', folder['id'])
    index = model.index_for_id(folder['id'])
    view.collapse(index)
    assert not view.isExpanded(index)

    view._set_hover(index)
    assert view._hover_timer.isActive(), '悬停计时器应该起来了'
    view._hover_timer.timeout.emit()          # 不等真实 650ms
    assert view.isExpanded(index), '悬停之后应该自动展开'
    assert view._hover_index is None


def test_hovering_a_page_does_not_start_the_timer(qtbot):
    service, model, view = make_tree(qtbot, show=True)
    page = service.createPage('document', '正文', 'page-1')
    index = model.index_for_id(page['id'])
    view._set_hover(index)
    assert not view._hover_timer.isActive(), '只有折叠的文件夹才自动展开'


@pytest.mark.parametrize('section', ['canvas', 'mindmap', 'document'])
def test_real_drop_events_move_page_into_folder_and_back_to_root(qtbot, section):
    service, model, view = make_tree(qtbot, show=True)
    folder = service.createFolder(section, '文件夹')
    page = service.createPage(section, '页面', 'content-id')
    index = model.index_for_id(folder['id'])
    pos = row_rect(view, index).center()
    mime = model.mimeData([model.index_for_id(page['id'])])
    move = QtGui.QDragMoveEvent(pos, Qt.DropAction.MoveAction, mime,
                               Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier)
    view.dragMoveEvent(move)
    assert move.isAccepted()
    drop = QtGui.QDropEvent(QtCore.QPointF(pos), Qt.DropAction.MoveAction, mime,
                           Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier)
    view.dropEvent(drop)
    assert drop.isAccepted()
    assert service._by_id(page['id'])['parentId'] == folder['id']
    assert service._by_id(page['id'])['pageId'] == 'content-id'
    root = model.index(['canvas', 'mindmap', 'document'].index(section), 0)
    pos = row_rect(view, root).center()
    drop = QtGui.QDropEvent(QtCore.QPointF(pos), Qt.DropAction.MoveAction, mime,
                           Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier)
    view.dropEvent(drop)
    assert drop.isAccepted()
    assert service._by_id(page['id'])['parentId'] is None


def test_invalid_drag_move_uses_local_event_position_without_crashing(qtbot):
    service, model, view = make_tree(qtbot, show=True)
    page = service.createPage('canvas', '画布', 'page')
    target = service.createFolder('document', '文档文件夹')
    pos = row_rect(view, model.index_for_id(target['id'])).center()
    mime = model.mimeData([model.index_for_id(page['id'])])
    event = QtGui.QDragMoveEvent(pos, Qt.DropAction.MoveAction, mime,
                                Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier)
    assert not hasattr(event, 'globalPosition')
    view.dragMoveEvent(event)
    assert not event.isAccepted()
    assert service._by_id(page['id'])['parentId'] is None


@pytest.mark.parametrize('section', ['canvas', 'mindmap', 'document'])
def test_single_page_and_folder_have_move_menu(qtbot, section):
    service, model, view = make_tree(qtbot)
    nodes = [service.createFolder(section, '目录'),
             service.createPage(section, '页面', 'page')]
    requested = []
    view.move_requested.connect(requested.append)
    for node in nodes:
        menu = view.menu_for_index(model.index_for_id(node['id']))
        action = next(a for a in menu.actions() if a.text() == '移动到…')
        action.trigger()
        assert requested[-1] == [node['id']]
