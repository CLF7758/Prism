from unittest.mock import patch

from prism.widgets.resource_tree_model import NODE_ID_ROLE
from tests.resource_tree_helpers import confirm_the_editor


def test_sidebar_tree_creates_nested_folder_and_page(main_window):
    window = main_window
    panel = window.view.category_panel
    # 左栏现在只有资源树，「筛选与旧分类」页签按用户要求删掉了。
    assert not hasattr(panel, 'resource_tabs')
    assert not hasattr(panel, 'list_widget')
    assert panel.resource_tree.model() is not None
    # C5：新建走就地命名 —— 建出来之后要把名字确认掉。
    window._create_resource_item('document', 'folder', None)
    confirm_the_editor(panel.resource_tree, '项目资料')
    folder = panel.resource_service.listChildren('document')[0]
    assert folder['title'] == '项目资料'
    window._create_resource_item('document', 'page', folder['id'])
    confirm_the_editor(panel.resource_tree, '正文')
    page = panel.resource_service.listChildren('document', folder['id'])[0]
    assert page['pageId'] in {p['id'] for p in window.view.scene.workspace_pages}
    index = panel.resource_tree.model().index_for_id(page['id'])
    assert index.data(NODE_ID_ROLE) == page['id']
    assert window.current_page_id == page['pageId']


def test_sidebar_legacy_pages_are_mapped_without_copying_categories(main_window):
    window = main_window
    scene = window.view.scene
    scene.workspace_pages = [
        {'id': 'old-document', 'kind': 'document', 'title': '旧文档',
         'content': '<p>保留</p>', 'tags': []},
    ]
    scene.category_names = ['古风/山水']
    scene.project_id = 'existing-project'
    window.view.category_panel.rebuild_workspace_tree()
    nodes = scene.workspace_nodes
    assert [n['pageId'] for n in nodes if n['nodeType'] == 'page'] == [
        'old-document']
    assert scene.workspace_pages[0]['content'] == '<p>保留</p>'
    assert scene.category_names == ['古风/山水']
