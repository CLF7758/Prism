from PyQt6.QtCore import Qt
from PyQt6 import QtCore

from prism.workspace_service import ResourceTreeService
from prism.widgets.resource_tree_model import (
    NODE_ID_ROLE, SECTION_ROLE, ASSET_COUNT_ROLE, ResourceTreeModel)


def test_count_changes_notify_only_changed_page_and_ancestors(qapp):
    service = ResourceTreeService('project')
    folder = service.createFolder('canvas', 'folder')
    page = service.createPage('canvas', 'one', 'one', folder['id'])
    other = service.createPage('canvas', 'two', 'two')
    model = ResourceTreeModel(service)
    model.set_asset_counts({'one': 1, 'two': 2})
    changes = []
    model.dataChanged.connect(lambda first, last, roles:
        changes.append(model.data(first, NODE_ID_ROLE)))
    model.set_asset_counts({'one': 3, 'two': 2})
    assert set(changes) == {page['id'], folder['id']}
    assert model.data(model.index_for_id(folder['id']), ASSET_COUNT_ROLE) == 3
    assert model.data(model.index_for_id(other['id']), ASSET_COUNT_ROLE) == 2


def test_true_ten_level_indexes_and_updates(qapp):
    service = ResourceTreeService('project')
    model = ResourceTreeModel(service)
    assert model.rowCount() == 6  # Model-library scaffold between materials and tags.
    document = model.index(2, 0)
    assert model.data(document, SECTION_ROLE) == 'document'
    assert model.data(model.index(3, 0)) == '材质库'
    assert model.data(model.index(4, 0)) == '模型库'
    assert model.data(model.index(5, 0)) == '标签'
    blank = model.index(0, 0, document)
    assert model.data(blank, SECTION_ROLE) == 'document'
    assert model.data(blank, Qt.ItemDataRole.SizeHintRole).height() == 14
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
    assert model.rowCount(model.index(0, 0)) == 3  # A, permanent default canvas, creation row
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


def test_batch_move_keeps_unrelated_nodes_and_persistent_subtrees(qapp):
    from PyQt6.QtTest import QAbstractItemModelTester
    service = ResourceTreeService('project')
    a = service.createFolder('document', 'A')
    b = service.createFolder('document', 'B')
    folder = service.createFolder('document', 'Subtree', a['id'])
    child = service.createPage('document', 'Child', 'child', folder['id'])
    page = service.createPage('document', 'Other', 'other')
    model = ResourceTreeModel(service)
    tester = QAbstractItemModelTester(model, QAbstractItemModelTester.FailureReportingMode.Fatal)
    unchanged = model._nodes[child['id']]
    persistent = QtCore.QPersistentModelIndex(model.index_for_id(child['id']))
    layouts = []
    model.layoutChanged.connect(lambda: layouts.append(True))
    service.moveBatch([folder['id'], page['id']], b['id'], 'document')
    assert model._nodes[child['id']] is unchanged
    assert persistent.isValid()
    assert persistent.parent().parent() == model.index_for_id(b['id'])
    assert not layouts
    assert [model.data(model.index(i, 0, model.index_for_id(b['id'])))
            for i in range(2)] == ['Subtree', 'Other']


def test_same_parent_batch_move_appends_in_requested_order(qapp):
    from PyQt6.QtTest import QAbstractItemModelTester
    service = ResourceTreeService('project')
    pages = [service.createPage('document', str(i), str(i)) for i in range(4)]
    model = ResourceTreeModel(service)
    tester = QAbstractItemModelTester(model, QAbstractItemModelTester.FailureReportingMode.Fatal)
    service.moveBatch([pages[2]['id'], pages[0]['id']], None, 'document')
    section = model.index(2, 0)
    assert [model.data(model.index(i, 0, section)) for i in range(4)] == ['1', '3', '2', '0']


def test_bulk_removal_then_insert_never_keeps_out_of_range_persistent_rows(qapp, qtlog):
    service = ResourceTreeService('project')
    pages = [service.createPage('document', f'Page {i}', f'page-{i}')
             for i in range(501)]
    model = ResourceTreeModel(service)
    tail = QtCore.QPersistentModelIndex(model.index_for_id(pages[-1]['id']))
    blank = QtCore.QPersistentModelIndex(model.index(501, 0, model.index(2, 0)))
    resets = []
    model.modelReset.connect(lambda: resets.append(True))
    service.trash([pages[0]['id'], pages[1]['id']], '2026-10-29')
    service.createPage('document', 'New page', 'new-page')
    for persistent in (tail, blank):
        assert persistent.isValid()
        assert persistent.row() < model.rowCount(persistent.parent())
        assert model.index(persistent.row(), 0, persistent.parent()) == persistent
    assert not resets
    assert not any('Invalid index' in message.message for message in qtlog.records)


def test_reordering_preserves_persistent_identity_and_row(qapp):
    service = ResourceTreeService('project')
    pages = [service.createPage('document', f'Page {i}', f'page-{i}')
             for i in range(4)]
    model = ResourceTreeModel(service)
    persistent = QtCore.QPersistentModelIndex(model.index_for_id(pages[-1]['id']))
    service.reorder('document', None,
                    [pages[-1]['id']] + [page['id'] for page in pages[:-1]])
    assert persistent.isValid()
    assert persistent.data(NODE_ID_ROLE) == pages[-1]['id']
    assert model.index_for_id(pages[-1]['id']) == persistent
