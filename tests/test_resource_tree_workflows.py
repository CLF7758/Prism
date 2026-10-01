"""资源树的集成行为：右键创建、F2 中文改名、删除/恢复、保存重开。

交接单《Prism-开发交接单-2026-10-01.md》第 5 节第 1 项要的就是这一组：
**实际右键分区空白/文件夹、创建后保存重开、F2 中文输入、删除后恢复**，
并且每一步都验证 `workspace_nodes`（新树）与 `workspace_pages`（旧页面表）
仍然一致 —— 这两套数据现在并存，最容易出的问题就是孤儿节点和显示不一致。

和 `tests/test_resource_tree_integration.py` 的分工：那个文件测「能不能建」，
这个文件测「建完、改名、删掉、重开之后两边还对得上」。
"""
from unittest.mock import patch

from PyQt6 import QtWidgets
from PyQt6.QtCore import Qt

from prism.fileio.sql import SQLiteIO
from tests.resource_tree_helpers import (
    cancel_the_editor, confirm_the_editor, create_page, orphan_report)


# ── 右键：分区空白 / 文件夹 ───────────────────────────────────────────

def test_right_click_section_blank_creates_a_page(main_window):
    """分区末尾的空白行 → 右键「新建文档」→ 就地命名 → 页面和树节点同时出现。"""
    window = main_window
    panel = window.view.category_panel
    tree = panel.resource_tree
    model = tree.model()
    document = model.index(2, 0)
    blank = model.index(0, 0, document)

    menu = tree.menu_for_index(blank)
    labels = [action.text() for action in menu.actions()]
    assert labels[:2] == ['新建文档', '新建文件夹']

    menu.actions()[0].trigger()
    assert model.draft_active(), '右键之后应该先出现一条临时行'
    confirm_the_editor(tree, '访谈记录')

    node = next(n for n in panel.resource_service.listChildren('document')
                if n['title'] == '访谈记录')
    assert node['pageId'] in {p['id'] for p in window.view.scene.workspace_pages}
    assert model.index_for_id(node['id']).isValid()
    assert window.current_page_id == node['pageId'], '新建后应打开这一页'
    assert orphan_report(window.view.scene) == {'只在树里': [],
                                                '只在页面表里': []}


def test_right_click_folder_creates_a_child_page(main_window):
    """文件夹上的右键 → 新建页面 → 就地命名 → 挂在它下面。"""
    window = main_window
    panel = window.view.category_panel
    tree = panel.resource_tree
    folder = panel.resource_service.createFolder('mindmap', '素材整理')
    index = tree.model().index_for_id(folder['id'])

    menu = tree.menu_for_index(index)
    assert menu.actions()[0].text() == '新建脑图'
    menu.actions()[0].trigger()
    confirm_the_editor(tree, '参考图')

    child = panel.resource_service.listChildren('mindmap', folder['id'])[0]
    assert child['nodeType'] == 'page'
    assert child['pageId'] in {p['id'] for p in window.view.scene.workspace_pages}
    assert orphan_report(window.view.scene) == {'只在树里': [],
                                                '只在页面表里': []}


def test_creating_a_duplicate_name_keeps_the_draft_and_refuses(
        main_window):
    """同级同名要被挡住：给出提示、临时行和输入都留着，服务里不多东西。"""
    window = main_window
    panel = window.view.category_panel
    tree = panel.resource_tree
    panel.resource_service.createFolder('document', '第一章')
    before = [n['title'] for n in panel.resource_service.listChildren('document')]

    window._create_resource_item('document', 'folder', None)
    refused = []
    tree.model().draft_refused.connect(refused.append)
    confirm_the_editor(tree, '第一章')

    after = [n['title'] for n in panel.resource_service.listChildren('document')]
    assert after == before, '被拒之后服务里不能多出节点'
    assert refused and '同名' in refused[0], '应该给出「已有同名项目」之类的提示'
    assert tree.model().draft_active(), '临时行要留着让用户接着改'
    cancel_the_editor(tree)


# ── F2 中文改名 ───────────────────────────────────────────────────────

def test_f2_rename_with_chinese_keeps_both_sides_in_step(main_window):
    window = main_window
    panel = window.view.category_panel
    scene = window.view.scene
    node = create_page(window, 'document', '未命名文档')
    model = panel.resource_tree.model()
    index = model.index_for_id(node['id'])

    assert model.setData(index, '分镜脚本·初稿', Qt.ItemDataRole.EditRole)

    stored = panel.resource_service._by_id(node['id'])
    assert stored['title'] == '分镜脚本·初稿'
    assert stored['id'] == node['id'], '改名不能换 ID'
    legacy = next(p for p in scene.workspace_pages if p['id'] == node['pageId'])
    assert legacy['title'] == '分镜脚本·初稿'
    assert model.index_for_id(node['id']).data() == '分镜脚本·初稿'


def test_a_title_with_a_slash_is_refused(main_window):
    """「/」是层级分隔符，放进名字会毁掉路径，必须在服务层挡住。"""
    window = main_window
    panel = window.view.category_panel
    node = create_page(window, 'canvas', '未命名画布')
    model = panel.resource_tree.model()
    index = model.index_for_id(node['id'])
    assert model.setData(index, '素材/参考', Qt.ItemDataRole.EditRole) is False
    assert panel.resource_service._by_id(node['id'])['title'] == '未命名画布'


def test_a_chinese_name_survives_rename_then_reopen(main_window, tmp_path):
    window = main_window
    panel = window.view.category_panel
    scene = window.view.scene
    node = create_page(window, 'document', '未命名文档')
    model = panel.resource_tree.model()
    index = model.index_for_id(node['id'])
    assert model.setData(index, '第一章·雨夜分镜', Qt.ItemDataRole.EditRole)

    path = str(tmp_path / 'renamed.prism')
    SQLiteIO(path, scene, create_new=True).write()
    scene.clear()
    SQLiteIO(path, scene, readonly=True).read()

    stored = next(n for n in scene.workspace_nodes if n['id'] == node['id'])
    assert stored['title'] == '第一章·雨夜分镜'
    assert stored['pageId'] == node['pageId']


# ── 保存重开 ─────────────────────────────────────────────────────────

def test_a_created_folder_tree_survives_a_save_and_reopen(main_window,
                                                          tmp_path):
    """文件夹套文件夹套页面，存下来再打开：层级、顺序、父引用都要在。"""
    window = main_window
    panel = window.view.category_panel
    scene = window.view.scene
    service = panel.resource_service
    outer = service.createFolder('document', '第一章')
    inner = service.createFolder('document', '外景', outer['id'])
    page = create_page(window, 'document', '湖边', inner['id'])

    path = str(tmp_path / 'nested.prism')
    SQLiteIO(path, scene, create_new=True).write()
    scene.clear()
    SQLiteIO(path, scene, readonly=True).read()

    nodes = {n['id']: n for n in scene.workspace_nodes}
    assert nodes[outer['id']]['title'] == '第一章'
    assert nodes[inner['id']]['parentId'] == outer['id']
    assert nodes[page['id']]['parentId'] == inner['id']
    assert nodes[page['id']]['pageId'] == page['pageId']
    assert nodes[page['id']]['order'] == page['order']
    assert orphan_report(scene) == {'只在树里': [], '只在页面表里': []}


def test_the_reopened_tree_shows_the_same_rows(main_window, tmp_path):
    """重开之后真的能在左栏里看到同样的层级，而不是只有数据对。"""
    window = main_window
    panel = window.view.category_panel
    scene = window.view.scene
    outer = panel.resource_service.createFolder('mindmap', '灵感')
    inner = panel.resource_service.createFolder('mindmap', '配色', outer['id'])
    path = str(tmp_path / 'rows.prism')
    SQLiteIO(path, scene, create_new=True).write()
    scene.clear()
    SQLiteIO(path, scene, readonly=True).read()

    # 打开工程时左栏服务会跟着工程重建；这里显式走同一条路。
    panel._install_resource_service()
    model = panel.resource_tree.model()
    outer_index = model.index_for_id(outer['id'])
    inner_index = model.index_for_id(inner['id'])
    assert outer_index.isValid() and inner_index.isValid()
    assert model.parent(inner_index) == outer_index
    assert inner_index.data() == '配色'


# ── 删除与恢复 ───────────────────────────────────────────────────────

def test_deleting_a_folder_hides_it_but_keeps_the_page_body(main_window):
    """交接单里的临时措施：树里删掉 ≠ 内容真没了。"""
    window = main_window
    panel = window.view.category_panel
    scene = window.view.scene
    folder = panel.resource_service.createFolder('document', '待删')
    page = create_page(window, 'document', '正文页', folder['id'])
    # 先离开这一页：切页/删除时会把「当前面板」的内容写回 scene，
    # 而这里要验的是"删掉之后 scene 里的正文还在"。
    window.open_workspace_page('canvas', 'default-canvas')
    body = next(p for p in scene.workspace_pages if p['id'] == page['pageId'])
    body['content'] = '<p>这段文字不能因为删除就没了</p>'

    with patch('PyQt6.QtWidgets.QMessageBox.question',
               return_value=QtWidgets.QMessageBox.StandardButton.Yes):
        window._delete_resource_item(folder['id'])

    model = panel.resource_tree.model()
    assert not model.index_for_id(folder['id']).isValid(), '树里应该看不见了'
    assert not model.index_for_id(page['id']).isValid()
    assert any(n['id'] == page['id'] and n['deletedAt']
               for n in panel.resource_service.nodes), '软删除标记要在'
    kept = next(p for p in scene.workspace_pages if p['id'] == page['pageId'])
    assert kept['content'] == '<p>这段文字不能因为删除就没了</p>'
    assert orphan_report(scene) == {'只在树里': [], '只在页面表里': []}


def test_restoring_brings_back_the_whole_subtree(main_window):
    window = main_window
    panel = window.view.category_panel
    service = panel.resource_service
    folder = service.createFolder('document', '待删')
    inner = service.createFolder('document', '子夹', folder['id'])
    page = create_page(window, 'document', '正文页', inner['id'])

    with patch('PyQt6.QtWidgets.QMessageBox.question',
               return_value=QtWidgets.QMessageBox.StandardButton.Yes):
        window._delete_resource_item(folder['id'])
    service.restore_deleted([folder['id']])

    model = panel.resource_tree.model()
    assert model.index_for_id(page['id']).isValid()
    assert model.parent(model.index_for_id(page['id'])) == \
        model.index_for_id(inner['id'])
    assert orphan_report(window.view.scene) == {'只在树里': [],
                                                '只在页面表里': []}


def test_a_deleted_folder_blocks_restoring_its_child_alone(main_window):
    """父夹还在回收站时单独恢复子项，会把节点挂到看不见的父上。"""
    window = main_window
    service = window.view.category_panel.resource_service
    folder = service.createFolder('document', '父夹')
    inner = service.createFolder('document', '子夹', folder['id'])
    service.trash([folder['id']], '2026-10-01T00:00:00+00:00')
    try:
        service.restore_deleted([inner['id']])
        raised = False
    except ValueError:
        raised = True
    assert raised, '应该要求先恢复父文件夹'


def test_delete_then_reopen_keeps_the_node_in_the_trash(main_window, tmp_path):
    """软删除要能存进工程文件，否则重开就"复活"了。"""
    window = main_window
    panel = window.view.category_panel
    scene = window.view.scene
    folder = panel.resource_service.createFolder('canvas', '待删画布')
    with patch('PyQt6.QtWidgets.QMessageBox.question',
               return_value=QtWidgets.QMessageBox.StandardButton.Yes):
        window._delete_resource_item(folder['id'])

    path = str(tmp_path / 'trash.prism')
    SQLiteIO(path, scene, create_new=True).write()
    scene.clear()
    SQLiteIO(path, scene, readonly=True).read()

    stored = next(n for n in scene.workspace_nodes if n['id'] == folder['id'])
    assert stored['deletedAt'], '回收站状态要跟着工程文件走'


# ── 旧工程 ───────────────────────────────────────────────────────────

def test_legacy_pages_get_nodes_and_stay_consistent(main_window):
    window = main_window
    panel = window.view.category_panel
    scene = window.view.scene
    scene.workspace_pages = [
        {'id': 'old-doc', 'kind': 'document', 'title': '旧文档',
         'content': '<p>正文</p>', 'tags': ['标签A']},
        {'id': 'old-canvas', 'kind': 'canvas', 'title': '旧画布',
         'content': None, 'tags': []},
    ]
    scene.project_id = 'legacy-project'
    panel.rebuild_workspace_tree()

    assert orphan_report(scene) == {'只在树里': [], '只在页面表里': []}
    assert scene.workspace_pages[0]['content'] == '<p>正文</p>'
    assert scene.workspace_pages[0]['tags'] == ['标签A']


def test_pages_created_from_the_old_menu_get_a_node(main_window):
    """旧入口（菜单新建页面）也要在树里有位置，否则就是孤儿页面。"""
    window = main_window
    panel = window.view.category_panel
    with patch('PyQt6.QtWidgets.QInputDialog.getText',
               return_value=('旧入口页面', True)):
        window._create_workspace_page('document')
    panel.rebuild_workspace_tree()
    assert orphan_report(window.view.scene) == {'只在树里': [],
                                                '只在页面表里': []}


def test_the_whole_legacy_round_trip_keeps_both_tables_in_step(main_window,
                                                               tmp_path):
    """旧工程 → 映射 → 存 → 重开：两套数据不能一边多一边少。"""
    window = main_window
    panel = window.view.category_panel
    scene = window.view.scene
    scene.workspace_pages = [
        {'id': 'parent', 'kind': 'document', 'title': '计划',
         'content': '<p>总览</p>', 'tags': []},
        {'id': 'child', 'kind': 'document', 'title': '细节',
         'content': '<p>子页</p>', 'tags': [], 'parent': 'parent'},
    ]
    scene.project_id = 'legacy-round-trip'
    panel.rebuild_workspace_tree()
    assert orphan_report(scene) == {'只在树里': [], '只在页面表里': []}

    path = str(tmp_path / 'legacy.prism')
    SQLiteIO(path, scene, create_new=True).write()
    scene.clear()
    SQLiteIO(path, scene, readonly=True).read()

    assert orphan_report(scene) == {'只在树里': [], '只在页面表里': []}
    titles = {n['title'] for n in scene.workspace_nodes}
    assert {'计划', '概述', '细节'} <= titles
    assert {p['id'] for p in scene.workspace_pages} == {'parent', 'child'}


# ── 页面标签页不要被反复重建（退出期 0xC0000005 的根因）────────────────
#
# `workspace_service.changed` 曾经一路走到「把所有文档/脑图面板删掉重建」。
# 每个面板都是一个 Chromium 视图，删掉的那些只做了 deleteLater，进程退出时
# 还没被回收 —— Qt 打 "WebEnginePage still not deleted"，然后 0xC0000005。
# 下面三项钉住新的行为：还活着的页面保留自己的面板。

def test_adding_a_page_keeps_the_tabs_that_already_exist(main_window):
    window = main_window
    first = create_page(window, 'document', '第一章')
    panel = window._page_panels[first['pageId']]
    second = create_page(window, 'document', '第二章')
    assert window._page_panels[first['pageId']] is panel, '老面板不该被重建'
    assert second['pageId'] in window._page_panels


def test_renaming_a_page_only_rewrites_the_tab_text(main_window):
    window = main_window
    node = create_page(window, 'document', '原名字')
    panel = window._page_panels[node['pageId']]
    model = window.view.category_panel.resource_tree.model()
    assert model.setData(model.index_for_id(node['id']), '新名字',
                         Qt.ItemDataRole.EditRole)
    assert window._page_panels[node['pageId']] is panel
    index = window.tabs.indexOf(panel)
    assert index >= 0 and window.tabs.tabText(index) == '新名字'


def test_removing_a_page_closes_only_its_own_tab(main_window):
    window = main_window
    keep = create_page(window, 'document', '留下的')
    drop = create_page(window, 'document', '删掉的')
    window.workspace_service.remove(drop['pageId'])
    assert drop['pageId'] not in window._page_panels
    assert keep['pageId'] in window._page_panels
    assert window.tabs.indexOf(window._page_panels[keep['pageId']]) >= 0
