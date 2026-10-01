import copy

from prism.workspace_service import ResourceTreeService, legacy_page_nodes


def test_legacy_parent_page_keeps_content_as_overview():
    pages = [
        {'id': 'parent', 'kind': 'document', 'title': '计划',
         'content': '<p>原正文</p>', 'parent': None},
        {'id': 'child', 'kind': 'document', 'title': '细节',
         'content': '<p>子页</p>', 'parent': 'parent'},
    ]
    before = copy.deepcopy(pages)
    nodes = legacy_page_nodes(pages, 'project')
    service = ResourceTreeService('project', nodes)
    folder = service.listChildren('document')[0]
    assert folder['nodeType'] == 'folder'
    assert folder['title'] == '计划'
    children = service.listChildren('document', folder['id'])
    assert {n['title'] for n in children} == {'概述', '细节'}
    assert {n['pageId'] for n in children} == {'parent', 'child'}
    assert pages == before
    assert legacy_page_nodes(pages, 'project') == nodes


def test_categories_not_turned_into_duplicate_canvas_pages():
    pages = [{'id': 'canvas', 'kind': 'canvas', 'title': '主画布',
              'content': None}]
    nodes = legacy_page_nodes(pages, 'project')
    assert len(nodes) == 1
    assert nodes[0]['pageId'] == 'canvas'


def test_missing_parent_is_visible_recovery_item():
    pages = [{'id': 'orphan', 'kind': 'mindmap', 'title': '未找到父页',
              'parent': 'lost-id', 'content': {'data': {'text': '保留'}}}]
    service = ResourceTreeService('project', legacy_page_nodes(pages, 'project'))
    folder = service.listChildren('mindmap')[0]
    assert folder['title'] == '恢复项'
    assert service.listChildren('mindmap', folder['id'])[0]['pageId'] == 'orphan'
