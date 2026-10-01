import pytest

from prism.workspace_service import ResourceTreeService


def test_ten_levels_and_stable_ids():
    service = ResourceTreeService('project')
    parent = None
    for depth in range(10):
        folder = service.createFolder('document', f'Folder {depth}', parent)
        parent = folder['id']
    page = service.createPage('document', 'Overview', 'page-1', parent)
    assert service.listChildren('document', parent)[0]['id'] == page['id']
    assert service.rename(page['id'], 'Renamed')['id'] == page['id']
    assert service.listChildren('document', parent)[0]['title'] == 'Renamed'


def test_parent_validation_and_atomic_move():
    service = ResourceTreeService('project')
    a = service.createFolder('canvas', 'A')
    b = service.createFolder('canvas', 'B', a['id'])
    page = service.createPage('canvas', 'P', 'page-p', b['id'])
    with pytest.raises(ValueError):
        service.createFolder('mindmap', 'Wrong', a['id'])
    with pytest.raises(ValueError):
        service.createFolder('canvas', 'Wrong', page['id'])
    with pytest.raises(ValueError):
        service.moveBatch([a['id']], b['id'], 'canvas')
    assert service.listChildren('canvas', None)[0]['id'] == a['id']
    assert service.moveBatch([b['id'], page['id']], None, 'canvas') == [b['id']]
    assert service.listChildren('canvas', None)[-1]['id'] == b['id']
    assert service.listChildren('canvas', b['id'])[0]['id'] == page['id']


def test_names_unique_only_within_same_parent_and_type():
    service = ResourceTreeService('project')
    a = service.createFolder('canvas', 'Same')
    b = service.createFolder('canvas', 'Other')
    service.createPage('canvas', 'Same', 'page-1')
    service.createFolder('canvas', 'Same', b['id'])
    with pytest.raises(ValueError, match='同名'):
        service.rename(b['id'], 'Same')
    with pytest.raises(ValueError):
        service.moveBatch([a['id']], b['id'], 'mindmap')
    with pytest.raises(ValueError):
        service.rename(a['id'], 'bad/name')


def test_reorder_trash_restore_search():
    service = ResourceTreeService('project')
    a = service.createFolder('mindmap', 'Alpha')
    b = service.createFolder('mindmap', 'Beta')
    child = service.createPage('mindmap', 'Inside', 'page-inside', a['id'])
    assert service.reorder('mindmap', None, [b['id'], a['id']]) == [b['id'], a['id']]
    assert service.listChildren('mindmap')[0]['id'] == b['id']
    assert service.search('ALPHA')[0]['id'] == a['id']
    assert set(service.trash([a['id']], '2026-10-01')) == {a['id'], child['id']}
    assert service.search('inside') == []
    with pytest.raises(ValueError):
        service.restore_deleted([child['id']])
    assert set(service.restore_deleted([a['id']])) == {a['id'], child['id']}
    assert service.search('inside')[0]['id'] == child['id']
