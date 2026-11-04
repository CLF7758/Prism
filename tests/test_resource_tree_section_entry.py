"""一级分类行进入该分类的**第一个具体内容**，而不是停在"全部"。

画布 / 脑图 / 文档 → 第一个页面（第一个子节点是文件夹就进它的第一个页面）；
材质库 / 模型库 → 第一个文件夹（没有文件夹就退回该分类本身）；
标签 → 标签没有文件夹可进，这一行不导航。
"""
from prism.widgets.resource_tree_model import UI_SECTIONS
from tests.resource_tree_helpers import confirm_the_editor, create_page


def section_index(model, section):
    return model.index(UI_SECTIONS.index(section), 0)


def create_folder(window, section, title, parent_id=None):
    window._create_resource_item(section, 'folder', parent_id)
    confirm_the_editor(window.view.category_panel.resource_tree, title)
    service = window.view.category_panel.resource_service
    return next(node for node in service.nodes
                if node['parentId'] == parent_id and node['title'] == title
                and node['nodeType'] == 'folder')


# ── 树模型：第一个具体内容的解析 ────────────────────────────────

def test_first_page_prefers_the_stored_canvas_over_the_implicit_default(qapp):
    from prism.workspace_service import ResourceTreeService
    from prism.widgets.resource_tree_model import ResourceTreeModel
    service = ResourceTreeService('project')
    model = ResourceTreeModel(service)
    # 只有隐式默认画布时，它就是这个分类的第一个画布。
    assert model.first_page_under('canvas')['pageId'] == 'default-canvas'
    service.createPage('canvas', '画布 A', 'canvas-a')
    # 存下来的画布按工程顺序排在隐式默认画布前面。
    assert model.first_page_under('canvas')['pageId'] == 'canvas-a'
    assert model.first_page_under('document') is None


def test_first_page_descends_into_the_first_folder(qapp):
    from prism.workspace_service import ResourceTreeService
    from prism.widgets.resource_tree_model import ResourceTreeModel
    service = ResourceTreeService('project')
    model = ResourceTreeModel(service)
    folder = service.createFolder('canvas', '画布夹')
    service.createPage('canvas', '夹里的画布', 'canvas-b', folder['id'])
    assert model.first_page_under('canvas')['pageId'] == 'canvas-b'


def test_first_folder_returns_the_first_folder_of_the_section(qapp):
    from prism.workspace_service import ResourceTreeService
    from prism.widgets.resource_tree_model import ResourceTreeModel
    service = ResourceTreeService('project')
    model = ResourceTreeModel(service)
    assert model.first_folder_under('material') is None
    first = service.createFolder('material', '常用')
    service.createFolder('material', '收藏')
    assert model.first_folder_under('material')['id'] == first['id']


# ── 窗口：一级分类行的行为 ─────────────────────────────────────

def test_canvas_section_row_opens_the_first_canvas(main_window):
    window = main_window
    create_page(window, 'canvas', '画布 A')
    tree = window.view.category_panel.resource_tree
    tree._on_clicked(section_index(tree.model(), 'canvas'))
    assert window.current_page_kind == 'canvas'
    assert window.tabs.currentWidget() is window.canvas_page
    assert window.current_page_id != 'default-canvas'


def test_canvas_section_row_opens_the_first_canvas_inside_the_first_folder(main_window):
    window = main_window
    folder = create_folder(window, 'canvas', '画布夹')
    create_page(window, 'canvas', '夹里的画布', folder['id'])
    tree = window.view.category_panel.resource_tree
    tree._on_clicked(section_index(tree.model(), 'canvas'))
    assert window.tabs.currentWidget() is window.canvas_page
    assert window.current_page_id is not None


def test_material_section_row_opens_the_first_folder(main_window):
    window = main_window
    service = window.view.category_panel.resource_service
    first = service.createFolder('material', '常用')
    service.createFolder('material', '收藏')
    tree = window.view.category_panel.resource_tree
    tree._on_clicked(section_index(tree.model(), 'material'))
    assert window.tabs.currentWidget() is window.material_library
    assert window.material_library.folder_id == first['id']


def test_material_section_row_inside_the_library_returns_to_everything(main_window):
    window = main_window
    service = window.view.category_panel.resource_service
    service.createFolder('material', '常用')
    tree = window.view.category_panel.resource_tree
    tree._on_clicked(section_index(tree.model(), 'material'))
    assert window.material_library.folder_id is not None
    # 已经在材质库里时，同一行回到该分类本身 —— 这是回到"全部"的唯一入口。
    tree._on_clicked(section_index(tree.model(), 'material'))
    assert window.material_library.folder_id is None


def test_model_section_row_without_folders_keeps_every_model(main_window, qtbot):
    window = main_window
    tree = window.view.category_panel.resource_tree
    tree._on_clicked(section_index(tree.model(), 'model'))
    qtbot.waitUntil(
        lambda: window.tabs.currentWidget() is window.model_library, timeout=10000)
    assert window.model_library._folder_id() is None


def test_tag_section_row_does_not_navigate(main_window):
    window = main_window
    before = window.tabs.currentWidget()
    tree = window.view.category_panel.resource_tree
    tree._on_clicked(section_index(tree.model(), 'tag'))
    assert window.tabs.currentWidget() is before
    assert window.current_page_kind == 'canvas'
