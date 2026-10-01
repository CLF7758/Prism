"""C8：回收站（§7.6）。

规范那一段的三件事：**空文件夹/单个页面直接删 + 撤销 Toast 8 秒**、
**非空文件夹弹确认（含 X 文件夹、Y 页面）**、**永久删除只在回收站里做**；
另外还有一条红线：「仅有 UI 删除而数据不可恢复视为不合格」。
"""
from unittest.mock import patch

from PyQt6 import QtWidgets

from prism.widgets.toast import UndoToast
from prism.workspace_service import ResourceTreeService
from tests.resource_tree_helpers import create_page, orphan_report


def make_service():
    return ResourceTreeService('project')


# ── 服务层：回收站 ────────────────────────────────────────────────────

def test_trashed_nodes_lists_the_recycle_bin(qapp):
    service = make_service()
    kept = service.createFolder('document', '留着的')
    dropped = service.createFolder('document', '删掉的')
    service.trash([dropped['id']], '2026-10-01T00:00:00+00:00')

    trashed = service.trashed_nodes()
    assert [n['id'] for n in trashed] == [dropped['id']]
    assert service.listChildren('document')[0]['id'] == kept['id']


def test_purge_only_works_on_the_recycle_bin(qapp):
    service = make_service()
    alive = service.createFolder('document', '还在的')
    try:
        service.purge([alive['id']])
        raised = False
    except ValueError as exc:
        raised = '回收站' in str(exc)
    assert raised, '没进回收站的东西不能被永久删除'
    assert service._by_id(alive['id']) is not None


def test_purge_removes_the_whole_subtree(qapp):
    service = make_service()
    folder = service.createFolder('document', '父')
    child = service.createFolder('document', '子', folder['id'])
    page = service.createPage('document', '正文', 'page-1', child['id'])
    service.trash([folder['id']], '2026-10-01T00:00:00+00:00')

    removed = service.purge([folder['id']])

    assert set(removed) == {folder['id'], child['id'], page['id']}
    assert service._by_id(folder['id']) is None
    assert service._by_id(child['id']) is None
    assert service.trashed_nodes() == []


def test_purge_leaves_everything_else_alone(qapp):
    service = make_service()
    keep = service.createFolder('document', '留着的')
    drop = service.createFolder('document', '删掉的')
    service.trash([drop['id']], '2026-10-01T00:00:00+00:00')

    service.purge([drop['id']])

    assert [n['id'] for n in service.listChildren('document')] == [keep['id']]


# ── Toast ─────────────────────────────────────────────────────────────

def test_the_undo_button_calls_back_once_and_dismisses(qtbot):
    calls = []
    toast = UndoToast(None, '已移到回收站', lambda: calls.append(True))
    qtbot.addWidget(toast)
    assert toast.undo_button is not None
    toast.undo_button.click()
    toast.undo_button.click()
    assert calls == [True], '撤销只能生效一次'
    assert not toast.is_alive()


def test_an_undo_toast_lives_eight_seconds_and_a_plain_one_four(qtbot):
    undo = UndoToast(None, '可以撤销的', lambda: None)
    plain = UndoToast(None, '只是通知')
    qtbot.addWidget(undo)
    qtbot.addWidget(plain)
    assert undo._timer.interval() == 8000, '撤销 Toast 8 秒（§7.6 / §505）'
    assert plain._timer.interval() == 4000, '普通 Toast 4 秒'
    assert plain.undo_button is None


def test_a_toast_times_out_by_itself(qtbot):
    calls = []
    toast = UndoToast(None, '已移到回收站', lambda: calls.append(True))
    qtbot.addWidget(toast)
    toast.show_at_bottom()
    assert toast.is_alive()
    toast._timer.timeout.emit()          # 等价于 8 秒到点
    assert not toast.is_alive()
    assert calls == [], '超时不该执行撤销'


# ── 删除流程（端到端）────────────────────────────────────────────────

def test_deleting_an_empty_folder_does_not_interrupt(main_window):
    """空文件夹直接删 —— 不弹确认（§7.6）。"""
    window = main_window
    panel = window.view.category_panel
    service = panel.resource_service
    folder = service.createFolder('document', '空夹')

    with patch('PyQt6.QtWidgets.QMessageBox.question',
               return_value=QtWidgets.QMessageBox.StandardButton.No) as asked:
        window._delete_resource_item(folder['id'])

    assert asked.call_count == 0, '空文件夹不该打断用户'
    assert service._by_id(folder['id'])['deletedAt'], '应该已经进回收站'
    assert window._trash_toast is not None and window._trash_toast.is_alive()


def test_deleting_a_single_page_does_not_interrupt(main_window):
    window = main_window
    panel = window.view.category_panel
    node = create_page(window, 'document', '单独一页')

    with patch('PyQt6.QtWidgets.QMessageBox.question',
               return_value=QtWidgets.QMessageBox.StandardButton.No) as asked:
        window._delete_resource_item(node['id'])

    assert asked.call_count == 0
    assert panel.resource_service._by_id(node['id'])['deletedAt']


def test_deleting_a_full_folder_asks_with_the_counts(main_window):
    """非空文件夹要先问，而且写清里面有多少东西（§7.6）。"""
    window = main_window
    panel = window.view.category_panel
    folder = panel.resource_service.createFolder('document', '有东西的')
    panel.resource_service.createFolder('document', '一层子夹', folder['id'])
    create_page(window, 'document', '一层页面', folder['id'])
    first = panel.resource_service.createFolder('document', '二层子夹',
                                               folder['id'])
    panel.resource_service.createPage('document', '二层页面', 'page-x',
                                      first['id'])

    seen = {}

    def fake_question(parent, title, text, *args, **kwargs):
        seen['title'] = title
        seen['text'] = text
        return QtWidgets.QMessageBox.StandardButton.Yes

    with patch('PyQt6.QtWidgets.QMessageBox.question',
               side_effect=fake_question):
        window._delete_resource_item(folder['id'])

    assert '含 2 个文件夹、2 个页面' in seen['text'], seen['text']
    assert '回收站' in seen['title']
    assert panel.resource_service._by_id(folder['id'])['deletedAt']


def test_cancelling_the_question_keeps_the_folder(main_window):
    window = main_window
    panel = window.view.category_panel
    folder = panel.resource_service.createFolder('document', '有东西的')
    panel.resource_service.createFolder('document', '子夹', folder['id'])

    with patch('PyQt6.QtWidgets.QMessageBox.question',
               return_value=QtWidgets.QMessageBox.StandardButton.Cancel):
        window._delete_resource_item(folder['id'])

    assert panel.resource_service._by_id(folder['id'])['deletedAt'] is None
    assert window._trash_toast is None


def test_the_toast_undo_brings_the_node_back(main_window):
    """撤销那条路：点一下就把整棵子树收回来（TREE-08 的核心）。"""
    window = main_window
    panel = window.view.category_panel
    service = panel.resource_service
    folder = service.createFolder('document', '要删的')
    child = service.createFolder('document', '子夹', folder['id'])
    page = create_page(window, 'document', '正文', child['id'])
    # 离开这一页再设正文：切页/删除会把"当前面板"写回 scene，
    # 而这里要验的是 scene 里那条正文在删除后还在。
    window.open_workspace_page('canvas', 'default-canvas')
    body = next(p for p in window.view.scene.workspace_pages
                if p['id'] == page['pageId'])
    body['content'] = '<p>正文不能因为删一下就没了</p>'

    with patch('PyQt6.QtWidgets.QMessageBox.question',
               return_value=QtWidgets.QMessageBox.StandardButton.Yes):
        window._delete_resource_item(folder['id'])
    assert service._by_id(folder['id'])['deletedAt']

    window._trash_toast.undo_button.click()

    assert service._by_id(folder['id'])['deletedAt'] is None
    assert service._by_id(page['id'])['deletedAt'] is None
    kept = next(p for p in window.view.scene.workspace_pages
                if p['id'] == page['pageId'])
    assert kept['content'] == '<p>正文不能因为删一下就没了</p>'
    assert orphan_report(window.view.scene) == {'只在树里': [],
                                                '只在页面表里': []}


# ── 回收站面板 ────────────────────────────────────────────────────────

def make_panel(qtbot, window):
    from prism.widgets.trash_panel import TrashPanel

    panel = TrashPanel(window.view, window)
    qtbot.addWidget(panel)
    return panel


def test_the_panel_lists_what_is_in_the_recycle_bin(main_window, qtbot):
    window = main_window
    panel_window = window.view.category_panel
    service = panel_window.resource_service
    folder = service.createFolder('document', '删掉的')
    service.createPage('document', '夹里的页', 'page-1', folder['id'])
    service.trash([folder['id']], '2026-10-01T09:30:00+00:00')

    panel = make_panel(qtbot, window)

    assert panel.list_widget.count() == 2, '父夹和被删的后代都该列出来'
    labels = [panel.list_widget.item(i).text()
              for i in range(panel.list_widget.count())]
    assert any('删掉的' in label and '文档' in label for label in labels)
    assert all('2026-10-01' in label for label in labels)


def test_the_panel_restores_a_deleted_item(main_window, qtbot):
    window = main_window
    panel_window = window.view.category_panel
    service = panel_window.resource_service
    folder = service.createFolder('document', '删掉的')
    service.trash([folder['id']], '2026-10-01T09:30:00+00:00')

    panel = make_panel(qtbot, window)
    panel.list_widget.item(0).setSelected(True)
    restored = panel.restore_selected()

    assert restored == [folder['id']]
    assert service._by_id(folder['id'])['deletedAt'] is None
    assert panel.list_widget.count() == 0, '恢复之后列表里就没有了'


def test_the_panel_refuses_to_restore_a_child_before_its_parent(main_window,
                                                                qtbot):
    window = main_window
    panel_window = window.view.category_panel
    service = panel_window.resource_service
    folder = service.createFolder('document', '父夹')
    child = service.createFolder('document', '子夹', folder['id'])
    service.trash([folder['id']], '2026-10-01T09:30:00+00:00')

    panel = make_panel(qtbot, window)
    # 列表中只选子夹（父夹在它上面）
    for row in range(panel.list_widget.count()):
        item = panel.list_widget.item(row)
        item.setSelected('子夹' in item.text())
    with patch('PyQt6.QtWidgets.QMessageBox.information') as told:
        restored = panel.restore_selected()

    assert restored == []
    assert told.called, '要告诉用户「须先恢复父文件夹」，而不是静默失败'
    assert service._by_id(child['id'])['deletedAt']


def test_purge_asks_twice_and_then_really_deletes(main_window, qtbot):
    """§7.6：永久删除只在回收站里做，而且**再次确认**。"""
    window = main_window
    panel_window = window.view.category_panel
    service = panel_window.resource_service
    folder = service.createFolder('document', '真删的')
    page = create_page(window, 'document', '夹里的页', folder['id'])
    body = next(p for p in window.view.scene.workspace_pages
                if p['id'] == page['pageId'])
    body['content'] = '<p>这页会被永久删掉</p>'
    service.trash([folder['id']], '2026-10-01T09:30:00+00:00')

    panel = make_panel(qtbot, window)
    purged_pages = []
    panel.purged.connect(purged_pages.extend)
    for row in range(panel.list_widget.count()):
        panel.list_widget.item(row).setSelected(True)

    with patch('PyQt6.QtWidgets.QMessageBox.question',
               return_value=QtWidgets.QMessageBox.StandardButton.Cancel):
        assert panel.purge_selected() == [], '取消就不该删'

    with patch('PyQt6.QtWidgets.QMessageBox.question',
               return_value=QtWidgets.QMessageBox.StandardButton.Yes):
        removed = panel.purge_selected()

    assert set(removed) == {folder['id'], page['id']}
    assert service._by_id(folder['id']) is None
    assert purged_pages == [page['pageId']], '要把页面 id 报给主窗口'

    # 主窗口据此清掉正文
    window._on_trash_purged(purged_pages)
    assert page['pageId'] not in {p['id']
                                  for p in window.view.scene.workspace_pages}
