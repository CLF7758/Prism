"""Material entry routes to its own page and keeps the canvas intact."""
from PyQt6.QtCore import Qt
import pytest

from prism.actions.actions import actions
from prism.fileio.sql import SQLiteIO
from prism.workspace_service import ResourceTreeService
from prism.widgets.resource_tree_model import UI_SECTIONS
from tests.resource_tree_helpers import confirm_the_editor


def test_entry_order_and_click_and_keyboard(main_window, qtbot):
    window = main_window
    tree = window.view.category_panel.resource_tree
    model = tree.model()
    assert UI_SECTIONS == ('canvas', 'mindmap', 'document', 'material', 'model', 'tag')
    assert model.index(4,0).data()=='模型库'
    root = model.index(3, 0)
    assert root.data() == '材质库'
    pages = list(window.view.scene.workspace_pages)
    tree._on_clicked(root)
    assert window.tabs.currentWidget() is window.material_library
    assert window.current_page_kind == 'material'
    assert window.current_page_id is None
    assert window.view.scene.workspace_pages == pages
    assert window.material_library.empty_title.text() == '材质库还没有内容'
    window.tabs.setCurrentWidget(window.canvas_page)
    tree.setCurrentIndex(root)
    qtbot.keyClick(tree, Qt.Key.Key_Return)
    assert window.tabs.currentWidget() is window.material_library


def test_create_nested_folder_and_go_back(main_window, qtbot):
    window = main_window
    panel = window.view.category_panel
    tree = panel.resource_tree
    window.open_material_library()
    qtbot.mouseClick(window.material_library.create_button, Qt.MouseButton.LeftButton)
    confirm_the_editor(tree, '木头')
    folder = panel.resource_service.listChildren('material')[0]
    assert folder['title'] == '木头'
    assert folder['nodeType'] == 'folder'
    assert window.material_library.folders.count() == 1
    tree._on_clicked(tree.model().index_for_id(folder['id']))
    assert window.material_library.folder_id == folder['id']
    qtbot.mouseClick(window.material_library.create_button, Qt.MouseButton.LeftButton)
    confirm_the_editor(tree, '原木')
    child = panel.resource_service.listChildren('material', folder['id'])[0]
    window.open_material_library(child['id'])
    assert window.material_library.breadcrumb.text() == '材质库 / 木头 / 原木'
    window.material_library._go_up()
    assert window.material_library.folder_id == folder['id']
    window.material_library._go_up()
    assert window.material_library.folder_id is None


def test_save_reload_and_delete_restore_material_folder(main_window, tmp_path):
    window = main_window
    panel = window.view.category_panel
    service = panel.resource_service
    outer = service.createFolder('material', '木头')
    inner = service.createFolder('material', '原木', outer['id'])
    window.open_material_library(inner['id'])
    path = str(tmp_path / 'materials.prism')
    SQLiteIO(path, window.view.scene, create_new=True).write()
    window.view.scene.clear()
    SQLiteIO(path, window.view.scene, readonly=True).read()
    panel._install_resource_service()
    assert window.material_library.service is panel.resource_service
    window.open_material_library(inner['id'])
    assert window.material_library.breadcrumb.text() == '材质库 / 木头 / 原木'
    service = panel.resource_service
    service.trash([outer['id']], '2026-10-19')
    assert window.material_library.folder_id is None
    assert window.material_library.folders.count() == 0
    service.restore_deleted([outer['id']])
    assert window.material_library.folders.count() == 1
    window.open_material_library(inner['id'])
    assert window.material_library.title.text() == '原木'


def test_material_library_blocks_hidden_canvas_actions(main_window):
    window = main_window
    view = window.view
    view.actiongroup_set_enabled('active_when_selection', True)
    assert actions['delete'].qaction.isEnabled()
    window.open_material_library()
    for name in ('delete', 'cut', 'paste', 'select_all', 'insert_text'):
        assert not actions[name].qaction.isEnabled(), name
    assert actions['save'].qaction.isEnabled()
    # A delayed scene/selection update must not enable hidden-canvas actions.
    view.actiongroup_set_enabled('active_when_selection', True)
    assert not actions['delete'].qaction.isEnabled()
    view.actiongroup_set_enabled('active_when_selection', False)
    window.tabs.setCurrentWidget(window.canvas_page)
    assert not actions['delete'].qaction.isEnabled()
    assert actions['paste'].qaction.isEnabled()
    view.actiongroup_set_enabled('active_when_selection', True)
    assert actions['delete'].qaction.isEnabled()


def test_material_section_reuses_folders_without_fake_workspace_pages(qapp):
    service = ResourceTreeService('project')
    folder = service.createFolder('material', '木头')
    assert folder['section'] == 'material'
    with pytest.raises(ValueError):
        service.createPage('material', 'Fake material page', 'page')
    invalid = dict(folder, nodeType='page', pageId='fake')
    with pytest.raises(ValueError):
        ResourceTreeService('project', [invalid])


def test_material_root_menu_only_creates_folders(main_window):
    panel = main_window.view.category_panel
    tree = panel.resource_tree
    root = tree.model().index(3, 0)
    menu = tree.menu_for_index(root)
    assert [action.text() for action in menu.actions()] == [
        '打开材质库', '新建文件夹']
    menu.actions()[1].trigger()
    confirm_the_editor(tree, '岩石')
    assert panel.resource_service.listChildren('material')[0]['title'] == '岩石'
    assert not tree.model().flags(root) & Qt.ItemFlag.ItemIsDragEnabled
    assert not tree.model().flags(root) & Qt.ItemFlag.ItemIsEditable


def test_switching_projects_does_not_retain_old_material_folders(main_window):
    window = main_window
    panel = window.view.category_panel
    old_folder = panel.resource_service.createFolder('material', '旧工程')
    window.open_material_library(old_folder['id'])
    window.view.scene.project_id = 'new-material-project'
    window.view.scene.workspace_nodes = []
    panel._install_resource_service()
    window.material_library.load_from_scene()
    assert window.material_library.folder_id is None
    assert window.material_library.folders.count() == 0
    assert window.material_library.service.project_id == 'new-material-project'


def test_folder_moves_and_rename_refresh_open_page(main_window):
    window = main_window
    service = window.view.category_panel.resource_service
    a = service.createFolder('material', '木头')
    b = service.createFolder('material', '收藏')
    child = service.createFolder('material', '原木', a['id'])
    window.open_material_library(child['id'])
    service.moveBatch([child['id']], b['id'], 'material')
    service.rename(b['id'], '常用')
    assert window.material_library.breadcrumb.text() == '材质库 / 常用 / 原木'
    with pytest.raises(ValueError):
        service.moveBatch([child['id']], None, 'document')
