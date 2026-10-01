"""C9：键盘与持久状态（§7.6）。

三块：方向键之外的分发（Enter / Home / End / Shift+F10）、
焦点与展开的**记忆**（activeNodeId、expandedIds、滚动锚点）、
以及删掉当前页面之后按「后继 → 前驱 → 父级」打开下一个。
"""
from PyQt6 import QtCore, QtGui, QtWidgets
from PyQt6.QtCore import Qt

from prism.config import PrismSettings
from prism.workspace_service import ResourceTreeService
from prism.widgets.resource_tree_model import ResourceTreeModel
from prism.widgets.resource_tree_view import ResourceTreeView
from tests.resource_tree_helpers import create_page


def make_tree(qtbot, project_id='keys-project', show=False):
    service = ResourceTreeService(project_id)
    model = ResourceTreeModel(service)
    view = ResourceTreeView()
    qtbot.addWidget(view)
    view.setModel(model)
    view.resize(280, 420)
    if show:
        view.show()
        qtbot.waitExposed(view)
    return service, model, view


def select(view, model, node_id):
    view.selectionModel().select(
        model.index_for_id(node_id),
        QtCore.QItemSelectionModel.SelectionFlag.ClearAndSelect |
        QtCore.QItemSelectionModel.SelectionFlag.Rows)


# ── 键盘分发 ──────────────────────────────────────────────────────────

def test_enter_opens_a_page(qtbot):
    service, model, view = make_tree(qtbot)
    page = service.createPage('document', '正文', 'page-1')
    index = model.index_for_id(page['id'])
    view.setCurrentIndex(index)
    opened = []
    view.open_page_requested.connect(lambda *args: opened.append(args))

    view._open_current(index)

    assert opened == [('document', 'page-1')]


def test_enter_on_a_folder_does_not_open_anything(qtbot):
    """§7.6：上下移动焦点不自动打开重型编辑器，Enter 只对页面有意义。"""
    service, model, view = make_tree(qtbot)
    folder = service.createFolder('document', '文件夹')
    index = model.index_for_id(folder['id'])
    opened = []
    view.open_page_requested.connect(lambda *args: opened.append(args))

    view._open_current(index)

    assert opened == []


def test_home_and_end_walk_the_visible_rows_of_the_section(qtbot):
    service, model, view = make_tree(qtbot)
    first = service.createFolder('document', '第一个')
    middle = service.createPage('document', '中间', 'page-1')
    last = service.createFolder('document', '最后一个')
    rows = view.visible_rows_of_section(model.index_for_id(middle['id']))
    assert [r.data(Qt.ItemDataRole.UserRole + 1) for r in rows] == \
        [first['id'], middle['id'], last['id']]

    view._focus_section_edge(model.index_for_id(middle['id']), first=True)
    assert view.currentIndex().data(Qt.ItemDataRole.UserRole + 1) == first['id']

    view._focus_section_edge(model.index_for_id(first['id']), first=False)
    assert view.currentIndex().data(Qt.ItemDataRole.UserRole + 1) == last['id']


def test_home_skips_the_rows_of_other_sections(qtbot):
    service, model, view = make_tree(qtbot)
    mindmap = service.createFolder('mindmap', '脑图里的')
    document = service.createFolder('document', '文档里的')

    view._focus_section_edge(model.index_for_id(document['id']), first=True)

    assert view.currentIndex().data(Qt.ItemDataRole.UserRole + 1) == \
        document['id'], '不该跑到别的分区去'


def test_shift_f10_opens_the_menu_for_the_current_row(qtbot):
    service, model, view = make_tree(qtbot)
    folder = service.createFolder('document', '文件夹')
    select(view, model, folder['id'])
    view.setCurrentIndex(model.index_for_id(folder['id']))
    shown = []
    view._show_menu_for_current = lambda *args, **kwargs: shown.append(True)

    event = QtGui.QKeyEvent(QtCore.QEvent.Type.KeyPress, Qt.Key.Key_F10,
                            Qt.KeyboardModifier.ShiftModifier)
    view.keyPressEvent(event)

    assert shown == [True]
    assert event.isAccepted()


# ── 记忆焦点 / 展开 / 滚动 ────────────────────────────────────────────

def test_expanded_folders_and_focus_come_back(qtbot):
    project = 'remember-me'
    service, model, view = make_tree(qtbot, project)
    folder = service.createFolder('document', '文件夹')
    inner = service.createFolder('document', '子夹', folder['id'])
    page = service.createPage('document', '正文', 'page-1', inner['id'])
    view.expand(model.index_for_id(folder['id']))
    view.expand(model.index_for_id(inner['id']))
    view.setCurrentIndex(model.index_for_id(page['id']))
    view.save_state()

    # 换一个视图（等价于重开）：同一个工程 id，会把状态读回来
    fresh_service = ResourceTreeService(project, service.nodes)
    fresh_model = ResourceTreeModel(fresh_service)
    fresh_view = ResourceTreeView()
    qtbot.addWidget(fresh_view)
    fresh_view.setModel(fresh_model)

    assert fresh_view.isExpanded(fresh_model.index_for_id(folder['id']))
    assert fresh_view.isExpanded(fresh_model.index_for_id(inner['id']))
    assert fresh_view.currentIndex().data(Qt.ItemDataRole.UserRole + 1) == \
        page['id']


def test_state_is_kept_per_project(qtbot):
    """§7.6：状态记在 projectId 下面，不会串到别的工程。"""
    service, model, view = make_tree(qtbot, 'project-one')
    folder = service.createFolder('document', '一号工程的夹子')
    view.expand(model.index_for_id(folder['id']))
    view.save_state()

    other_service, other_model, other_view = make_tree(qtbot, 'project-two')
    other = other_service.createFolder('document', '二号工程的夹子')
    other_view.expand(other_model.index_for_id(other['id']))
    other_view.save_state()

    settings = PrismSettings()
    settings.sync()
    assert settings.value(
        'Workspace/resource_tree/project-one/expanded', '', type=str) == \
        folder['id']
    assert settings.value(
        'Workspace/resource_tree/project-two/expanded', '', type=str) == \
        other['id']
    assert settings.value(
        'Workspace/resource_tree/project-two/expanded', '', type=str) == \
        other['id']


def test_an_unknown_id_is_skipped_when_restoring(qtbot):
    project = 'stale-ids'
    settings = PrismSettings()
    settings.setValue(f'Workspace/resource_tree/{project}/expanded',
                      'nope-1,nope-2')
    settings.setValue(f'Workspace/resource_tree/{project}/active', 'nope-3')
    settings.setValue(f'Workspace/resource_tree/{project}/anchor', 'nope-4|40')

    service = ResourceTreeService(project)
    model = ResourceTreeModel(service)
    view = ResourceTreeView()
    qtbot.addWidget(view)

    view.setModel(model)          # 恢复就发生在这里

    assert view.currentIndex().isValid() is False, '无效 id 就保持默认'


def test_changes_are_written_after_a_short_delay(qtbot):
    """焦点/展开一变就攒一条待写，不等下一次操作。"""
    service, model, view = make_tree(qtbot)
    folder = service.createFolder('document', '文件夹')
    assert not view._state_timer.isActive()
    view.expand(model.index_for_id(folder['id']))
    assert view._state_timer.isActive(), '展开之后应该排一次保存'


# ── 删掉当前页面之后打开哪一个 ────────────────────────────────────────

def test_deleting_the_current_page_opens_the_next_sibling(main_window):
    window = main_window
    first = create_page(window, 'document', 'A')
    second = create_page(window, 'document', 'B')
    third = create_page(window, 'document', 'C')
    window.open_workspace_page('document', second['pageId'])

    window._delete_resource_item(second['id'])

    assert window.current_page_id == third['pageId'], '优先打开后继'


def test_deleting_the_last_page_opens_the_previous_one(main_window):
    window = main_window
    first = create_page(window, 'document', 'A')
    second = create_page(window, 'document', 'B')
    window.open_workspace_page('document', second['pageId'])

    window._delete_resource_item(second['id'])

    assert window.current_page_id == first['pageId'], '没有后继就找前驱'


def test_deleting_the_only_page_falls_back_to_the_canvas(main_window):
    window = main_window
    only = create_page(window, 'document', '独苗')
    assert window.current_page_id == only['pageId']

    window._delete_resource_item(only['id'])

    assert window.current_page_id == 'default-canvas'
    assert window.current_page_kind == 'canvas'


def test_deleting_another_page_leaves_the_current_one_alone(main_window):
    window = main_window
    other = create_page(window, 'document', '别的')
    keep = create_page(window, 'document', '正在看的')
    assert window.current_page_id == keep['pageId']

    window._delete_resource_item(other['id'])

    assert window.current_page_id == keep['pageId'], '删的不是当前页就别换'
