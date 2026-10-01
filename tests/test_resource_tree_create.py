"""C5：就地新建与重命名（§7.4）。

三层的分工：
- 服务层：默认名自动编号「未命名文档 2」，用户自己输的名字**不**自动编号；
- 模型层：草稿行 —— 临时节点只活在模型里，确认之前服务层一个字节都没写过；
- 视图层：Enter/失焦确认、Esc 取消、重名保留输入、中文输入法组合期间 Enter 只提交候选。
"""
from PyQt6 import QtCore, QtGui, QtWidgets
from PyQt6.QtCore import Qt

from prism.workspace_service import ResourceTreeService
from prism.widgets.resource_tree_model import (
    DRAFT_PREFIX, ResourceTreeModel)
from prism.widgets.resource_tree_view import ResourceTreeView, _NameEditor
from tests.resource_tree_helpers import (
    cancel_the_editor, confirm_the_editor, current_editor,
    orphan_report)


def make_model(service=None):
    service = service or ResourceTreeService('project')
    return service, ResourceTreeModel(service)


# ── 服务层：默认名自动编号 ────────────────────────────────────────────

def test_default_names_are_numbered_when_taken(qapp):
    service = ResourceTreeService('project')
    assert service.unique_suggestion('document', None, 'page',
                                     '未命名文档') == '未命名文档'
    service.createPage('document', '未命名文档', 'page-1')
    assert service.unique_suggestion('document', None, 'page',
                                     '未命名文档') == '未命名文档 2'
    service.createPage('document', '未命名文档 2', 'page-2')
    assert service.unique_suggestion('document', None, 'page',
                                     '未命名文档') == '未命名文档 3'


def test_numbering_only_counts_the_same_parent_and_type(qapp):
    service = ResourceTreeService('project')
    folder = service.createFolder('document', '未命名文件夹')
    service.createPage('document', '未命名文档', 'page-1', folder['id'])
    # 另一个父级下的同名不影响
    assert service.unique_suggestion('document', None, 'folder',
                                     '未命名文件夹') == '未命名文件夹 2'
    assert service.unique_suggestion('document', folder['id'], 'page',
                                     '未命名文档') == '未命名文档 2'


def test_a_typed_name_is_refused_not_renumbered(qapp):
    service = ResourceTreeService('project')
    service.createFolder('document', '第一章')
    try:
        service.createFolder('document', '第一章')
        raised = False
    except ValueError as exc:
        raised = '同名' in str(exc)
    assert raised, '用户自己输的名字冲突时应该报错，而不是悄悄改成「第一章 2」'


# ── 模型层：草稿行 ────────────────────────────────────────────────────

def test_a_draft_row_shows_up_and_leaves_again(qapp):
    service, model = make_model()
    section = model.index(2, 0)
    before = model.rowCount(section)
    draft = model.begin_draft('document', 'folder', None, '未命名文件夹')
    assert draft.isValid()
    assert model.rowCount(section) == before + 1
    assert model.data(draft) == '未命名文件夹'
    assert model.draft_active()
    model.clear_draft()
    assert model.rowCount(section) == before
    assert not model.draft_active()


def test_escape_leaves_no_trace_in_the_service(qapp):
    """Esc 的硬证据：服务层的节点列表从头到尾没变过。"""
    service, model = make_model()
    before = [dict(n) for n in service.nodes]
    model.begin_draft('canvas', 'page', None, '未命名画布')
    assert service.nodes == before, '草稿阶段不许写进服务'
    model.clear_draft()
    assert service.nodes == before
    assert service.search('未命名') == []


def test_committing_a_draft_creates_the_node(qapp):
    service, model = make_model()
    committed = []
    model.draft_committed.connect(committed.append)
    draft = model.begin_draft('mindmap', 'folder', None, '未命名文件夹')
    assert model.setData(draft, '灵感', Qt.ItemDataRole.EditRole)
    assert not model.draft_active()
    assert [n['title'] for n in service.listChildren('mindmap')] == ['灵感']
    assert len(committed) == 1 and committed[0]['title'] == '灵感'


def test_a_duplicate_name_keeps_the_draft_and_the_typed_text(qapp):
    service, model = make_model()
    service.createFolder('document', '第一章')
    refused = []
    model.draft_refused.connect(refused.append)
    draft = model.begin_draft('document', 'folder', None, '未命名文件夹')

    assert model.setData(draft, '第一章', Qt.ItemDataRole.EditRole) is False
    assert model.draft_active(), '被拒之后那一行要留着，让用户接着改'
    assert model.data(model.draft_index()) == '第一章', '输入要保留'
    assert refused and '同名' in refused[0]
    assert len(service.listChildren('document')) == 1, '服务里不能多出节点'


def test_an_illegal_name_is_refused_the_same_way(qapp):
    service, model = make_model()
    draft = model.begin_draft('document', 'folder', None, '素材')
    assert model.setData(draft, '素材/参考', Qt.ItemDataRole.EditRole) is False
    assert model.draft_active()
    assert model.data(model.draft_index()) == '素材/参考'


def test_a_draft_row_hangs_under_its_folder(qapp):
    service, model = make_model()
    folder = service.createFolder('document', '第一章')
    draft = model.begin_draft('document', 'page', folder['id'], '未命名文档')
    assert model.parent(draft) == model.index_for_id(folder['id'])
    assert model.data(draft, Qt.ItemDataRole.UserRole + 3) == 'page'


def test_an_external_change_drops_the_draft(qapp):
    """服务被别处改了（导入、外部新建），草稿行要按正常流程收掉。"""
    service, model = make_model()
    model.begin_draft('document', 'folder', None, '未命名文件夹')
    service.createFolder('document', '别处建的')
    assert not model.draft_active()
    titles = [model.data(model.index(row, 0, model.index(2, 0)))
              for row in range(model.rowCount(model.index(2, 0)))]
    assert '别处建的' in titles


# ── 视图层 ───────────────────────────────────────────────────────────

def test_the_add_button_only_sits_on_section_rows(qtbot):
    service, model = make_model()
    folder = service.createFolder('document', 'A')
    view = ResourceTreeView()
    qtbot.addWidget(view)
    view.setModel(model)
    delegate = view.itemDelegate()
    section = model.index(2, 0)
    rect = QtCore.QRect(0, 0, 200, 32)
    assert delegate.add_button_rect(rect, section) is not None
    assert delegate.add_button_rect(rect, model.index(0, 0, section)) is None
    assert delegate.add_button_rect(
        rect, model.index_for_id(folder['id'])) is None


def test_enter_during_ime_composition_is_not_a_commit(qtbot):
    """中文输入法还在组合的时候，回车是给候选的，不能算确认（TREE-04）。"""
    service, model = make_model()
    view = ResourceTreeView()
    qtbot.addWidget(view)
    view.setModel(model)
    view.begin_create('document', 'folder', None, '未命名文件夹')
    editor = current_editor(view)
    assert editor is not None, '就地编辑器应该已经打开'

    editor.setFocus()
    event = QtGui.QInputMethodEvent('wei ming ming', [])
    QtWidgets.QApplication.sendEvent(editor, event)
    assert editor.composing, '组合期间应当被认为正在输入'

    qtbot.keyClick(editor, Qt.Key.Key_Return)
    assert model.draft_active(), '组合期间的回车不能把名字定下来'
    assert service.listChildren('document') == []

    # 组合结束（提交候选）之后，回车才作数
    QtWidgets.QApplication.sendEvent(editor, QtGui.QInputMethodEvent())
    assert not editor.composing


def test_begin_create_opens_the_editor_with_the_whole_name_selected(qtbot):
    service, model = make_model()
    view = ResourceTreeView()
    qtbot.addWidget(view)
    view.setModel(model)
    view.begin_create('canvas', 'page', None, '未命名画布')
    editor = current_editor(view)
    assert editor is not None
    assert editor.text() == '未命名画布'
    assert editor.selectedText() == '未命名画布', '打开就全选，直接输入即替换'


def test_committing_through_the_view_emits_create_committed(qtbot):
    service, model = make_model()
    view = ResourceTreeView()
    qtbot.addWidget(view)
    view.setModel(model)
    committed = []
    view.create_committed.connect(committed.append)
    view.begin_create('mindmap', 'folder', None, '未命名文件夹')
    editor = current_editor(view)
    editor.setText('配色')
    view.commitData(editor)
    view.itemDelegate().closeEditor.emit(
        editor, QtWidgets.QAbstractItemDelegate.EndEditHint.SubmitModelCache)
    assert committed and committed[0]['title'] == '配色'
    assert not model.draft_active()


def test_escape_through_the_view_removes_the_draft(qtbot):
    service, model = make_model()
    view = ResourceTreeView()
    qtbot.addWidget(view)
    view.setModel(model)
    view.begin_create('canvas', 'page', None, '未命名画布')
    editor = current_editor(view)
    view.itemDelegate().closeEditor.emit(
        editor, QtWidgets.QAbstractItemDelegate.EndEditHint.RevertModelCache)
    assert not model.draft_active()
    assert service.nodes == []


# ── 端到端（走主窗口，和右键菜单同一条路）────────────────────────────

def test_inline_create_writes_nothing_until_confirmed(main_window):
    """§7.4「Esc 取消并移除临时节点，不增加撤销记录」—— 连脏标记都不留。"""
    window = main_window
    panel = window.view.category_panel
    model = panel.resource_tree.model()
    before = [dict(node) for node in panel.resource_service.nodes]
    was_dirty = window.view.is_dirty()
    undo_depth = window.view.undo_stack.index()

    window._create_resource_item('document', 'folder', None)
    assert model.draft_active(), '临时节点应该已经出现'

    cancel_the_editor(panel.resource_tree)
    assert not model.draft_active()
    assert panel.resource_service.nodes == before, '取消之后工程里不该多东西'
    assert window.view.is_dirty() == was_dirty, '也不该把工程标脏'
    assert window.view.undo_stack.index() == undo_depth, '更不该有撤销记录'


def test_inline_create_page_writes_both_tables_after_confirm(main_window):
    window = main_window
    panel = window.view.category_panel
    window._create_resource_item('document', 'page', None)
    confirm_the_editor(panel.resource_tree, '现场记录')

    node = next(n for n in panel.resource_service.listChildren('document')
                if n['title'] == '现场记录')
    assert node['pageId'] in {p['id'] for p in window.view.scene.workspace_pages}
    assert window.current_page_id == node['pageId'], '确认后应该打开这一页'
    assert orphan_report(window.view.scene) == {'只在树里': [],
                                                '只在页面表里': []}


def test_two_creates_in_a_row_get_numbered_defaults(main_window):
    window = main_window
    panel = window.view.category_panel
    for expected in ('未命名文件夹', '未命名文件夹 2'):
        window._create_resource_item('mindmap', 'folder', None)
        editor = current_editor(panel.resource_tree)
        assert editor.text() == expected, '连续新建时默认名要自动编号'
        confirm_the_editor(panel.resource_tree)
    titles = [n['title'] for n in panel.resource_service.listChildren('mindmap')]
    assert titles == ['未命名文件夹', '未命名文件夹 2']
