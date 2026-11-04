"""T05/T06: preserve ownership/validation while removing repeated work."""
import copy
from unittest.mock import patch

import pytest

from prism.workspace_service import ResourceTreeService


def page(index, title=None):
    return {'id': f'page-{index}', 'kind': 'canvas',
            'title': title or f'Canvas {index}'}


def test_missing_pages_are_one_atomic_model_change(qapp):
    service = ResourceTreeService('project')
    folder = service.createFolder('canvas', 'Folder')
    nested = service.createPage('canvas', 'Nested', 'page-0', folder['id'])
    before = copy.deepcopy(service.nodes)
    changes = []
    service.changed.connect(changes.append)
    with patch.object(service, 'createPage', side_effect=AssertionError('quadratic path')):
        ids = service.ensurePages([page(i) for i in range(2000)])
    assert len(ids) == 1999
    assert len(changes) == 1
    assert service.nodes[:2] == before
    assert service._by_id(nested['id'])['parentId'] == folder['id']
    assert len({n['pageId'] for n in service.nodes if n['nodeType'] == 'page'}) == 2000
    assert service.ensurePages([page(i) for i in range(2000)]) == []
    assert len(changes) == 1


@pytest.mark.parametrize('pages', [
    [page(1, 'Same'), page(2, 'Same')],
    [page(1), page(2, 'invalid/name')],
    [page(1), page(1)],
    [page(1), dict(page(2), kind='material')],
])
def test_invalid_batch_never_partially_changes_tree(qapp, pages):
    service = ResourceTreeService('project')
    changes = []
    service.changed.connect(changes.append)
    with pytest.raises(ValueError):
        service.ensurePages(pages)
    assert service.nodes == []
    assert service._node_index == {}
    assert changes == []


def test_repeat_resource_install_reuses_owned_services(main_window):
    panel = main_window.view.category_panel
    folders, materials = panel.resource_service, panel.material_service
    material = materials.create(name='Keep edits')
    revision = materials.revision
    with patch('prism.material_service.validate_material_records',
               side_effect=AssertionError('unchanged records revalidated')):
        panel._install_resource_service()
        panel.rebuild_workspace_tree()
    assert panel.resource_service is folders
    assert panel.material_service is materials
    assert materials.get(material['id'])['name'] == 'Keep edits'
    assert materials.revision == revision


def test_folder_replacement_reuses_materials_but_record_replacement_does_not(main_window):
    panel = main_window.view.category_panel
    scene = panel.scene
    original = panel.material_service
    record = original.create(name='Keep')
    scene.workspace_nodes = copy.deepcopy(scene.workspace_nodes)
    with patch('prism.material_service.validate_material_records',
               side_effect=AssertionError('folder rebind revalidated materials')):
        panel.rebuild_workspace_tree()
    assert panel.material_service is original
    assert original.folders is panel.resource_service
    scene.material_records = copy.deepcopy(scene.material_records)
    panel.rebuild_workspace_tree()
    assert panel.material_service is not original
    assert panel.material_service.get(record['id'])['name'] == 'Keep'


def test_same_project_reload_still_validates_replacement_records(main_window):
    panel = main_window.view.category_panel
    previous = panel.material_service
    panel.scene.material_records = [{'id': 'invalid'}]
    with pytest.raises((ValueError, KeyError)):
        panel._install_resource_service()
    assert panel.material_service is previous


def test_5000_records_validate_once_then_reuse_without_copy(main_window):
    from prism.material_service import validate_material_records
    panel = main_window.view.category_panel
    template = panel.material_service.create(name='Template')
    incoming = [dict(copy.deepcopy(template), id=f'material-{i}') for i in range(5000)]
    panel.scene.material_records = incoming
    with patch('prism.material_service.validate_material_records',
               wraps=validate_material_records) as validate:
        panel._install_resource_service()
        owned = panel.material_service.records
        assert owned is not incoming
        assert owned == incoming
        panel._install_resource_service()
        assert panel.material_service.records is owned
        assert validate.call_count == 1
    panel.material_service.update('material-1', name='Template', notes='Owned edit')
    assert incoming[1]['notes'] != 'Owned edit'
