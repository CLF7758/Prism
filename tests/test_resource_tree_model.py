from PyQt6.QtCore import Qt

from prism.workspace_service import ResourceTreeService
from prism.widgets.resource_tree_model import (
    NODE_ID_ROLE, SECTION_ROLE, ResourceTreeModel)


def test_true_ten_level_indexes_and_updates(qapp):
    service = ResourceTreeService('project')
    model = ResourceTreeModel(service)
    assert model.rowCount() == 4
    document = model.index(2, 0)
    assert model.data(document, SECTION_ROLE) == 'document'
    assert model.data(model.index(3, 0)) == '标签'
    blank = model.index(0, 0, document)
    assert model.data(blank, SECTION_ROLE) == 'document'
    assert model.data(blank, Qt.ItemDataRole.SizeHintRole).height() == 32
    parent_id = None
    parent_index = document
    for depth in range(10):
        node = service.createFolder('document', f'Folder {depth}', parent_id)
        assert model.rowCount(parent_index) == (2 if depth == 0 else 1)
        child_index = model.index(0, 0, parent_index)
        assert model.data(child_index, NODE_ID_ROLE) == node['id']
        assert model.parent(child_index) == parent_index
        parent_id = node['id']
        parent_index = child_index
    page = service.createPage('document', 'Page', 'page-1', parent_id)
    page_index = model.index_for_id(page['id'])
    assert page_index.isValid()
    assert model.data(page_index) == 'Page'
    assert model.setData(page_index, '改名', Qt.ItemDataRole.EditRole)
    assert model.data(model.index_for_id(page['id'])) == '改名'


def test_move_and_trash_do_not_reset_model(qapp):
    service = ResourceTreeService('project')
    a = service.createFolder('canvas', 'A')
    b = service.createFolder('canvas', 'B')
    page = service.createPage('canvas', 'Page', 'page-1', a['id'])
    model = ResourceTreeModel(service)
    resets = []
    removed_rows = []
    moved_rows = []
    model.modelReset.connect(lambda: resets.append(True))
    model.rowsRemoved.connect(lambda *_: removed_rows.append(True))
    model.rowsMoved.connect(lambda *_: moved_rows.append(True))
    service.moveBatch([page['id']], b['id'], 'canvas')
    index = model.index_for_id(page['id'])
    assert model.parent(index) == model.index_for_id(b['id'])
    assert moved_rows == [True]
    service.trash([b['id']], '2026-10-01')
    assert not model.index_for_id(page['id']).isValid()
    assert model.rowCount(model.index(0, 0)) == 2
    assert removed_rows == [True]
    assert resets == []
    service.restore_deleted([b['id']])
    assert model.index_for_id(page['id']).isValid()


def test_create_and_rename_emit_local_model_signals(qapp):
    service = ResourceTreeService('project')
    model = ResourceTreeModel(service)
    inserted = []
    changed = []
    model.rowsInserted.connect(lambda *_: inserted.append(True))
    model.dataChanged.connect(lambda *_: changed.append(True))
    folder = service.createFolder('document', 'Folder')
    assert inserted == [True]
    service.rename(folder['id'], 'Renamed')
    assert changed == [True]
