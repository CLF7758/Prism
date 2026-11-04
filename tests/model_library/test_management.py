"""Management must survive real saves; originals and other records stay intact."""
import hashlib
import json
import sqlite3

import pytest
from PyQt6 import QtCore, QtGui, QtWidgets

from prism.model_library.contracts import ModelQuery
from prism.model_library.project_storage import open_model_context, ModelRevisionConflict
from prism.model_library.repository import ModelRepositoryAdapter
from prism.model_library.service import ModelLibraryService
from prism.widgets.library_transfer import ASSET_MIME, read_library_drag
from prism.workspace_service import ResourceTreeService
from tests.model_library.test_storage import create_project, make_scene, sample_payload


def test_batch_conflict_rolls_back_and_preserves_future_fields(tmp_path, qapp):
    path = create_project(tmp_path / 'batch.prism', make_scene())
    repo = ModelRepositoryAdapter(open_model_context(path))
    repo.insert(sample_payload('one', future={'keep': True}))
    repo.insert(sample_payload('two'))
    before = repo.get_payload('one')
    with pytest.raises(ModelRevisionConflict):
        repo.patch_batch([('one', 1, {'folder_id': 'a'}), ('two', 999, {'folder_id': 'a'})])
    assert repo.get_payload('one') == before
    repo.patch_batch([('one', 1, {'name': 'Renamed', 'folder_id': 'a'})])
    after = repo.get_payload('one')
    assert after['future'] == {'keep': True}
    assert after['files'] == before['files']
    assert after['appearance'] == before['appearance']
    assert repo.query(ModelQuery(folder_id='a')).total == 1


def test_drag_rejects_other_project_and_old_session(qapp):
    folders = ResourceTreeService('p')
    data = QtCore.QMimeData()
    payload = dict(project='p', scope=str(id(folders)), kind='model', ids=['a', 'b'])
    data.setData(ASSET_MIME, json.dumps(payload).encode())
    assert read_library_drag(data, folders)['ids'] == ['a', 'b']
    assert read_library_drag(data, ResourceTreeService('p')) is None
    payload['project'] = 'other'
    data.setData(ASSET_MIME, json.dumps(payload).encode())
    assert read_library_drag(data, folders) is None


def test_folder_drop_uses_full_batch_and_accepts_root(qtbot):
    from prism.widgets.resource_tree_model import ResourceTreeModel, UI_SECTIONS
    from prism.widgets.resource_tree_view import ResourceTreeView
    folders = ResourceTreeService('p', model_section_enabled=True)
    folder = folders.createFolder('model', 'Target')
    other = folders.createFolder('material', 'Wrong section')
    model = ResourceTreeModel(folders)
    tree = ResourceTreeView()
    qtbot.addWidget(tree)
    tree.setModel(model)
    tree.resize(350, 500)
    tree.show()
    tree.expandAll()
    received = []
    tree.library_drop_handler = lambda *args: received.append(args) or True
    mime = QtCore.QMimeData()
    mime.setData(ASSET_MIME, json.dumps(dict(project='p', scope=str(id(folders)),
        kind='model', ids=['a', 'b', 'c'])).encode())
    def drop(index):
        event = QtGui.QDropEvent(QtCore.QPointF(tree.visualRect(index).center()),
            QtCore.Qt.DropAction.MoveAction, mime, QtCore.Qt.MouseButton.LeftButton,
            QtCore.Qt.KeyboardModifier.NoModifier)
        tree.dropEvent(event)
        return event.isAccepted()
    assert drop(model.index_for_id(folder['id']))
    assert received[-1] == ('model', ['a', 'b', 'c'], folder['id'])
    assert not drop(model.index_for_id(other['id']))
    assert drop(model.index(UI_SECTIONS.index('model'), 0))
    assert received[-1] == ('model', ['a', 'b', 'c'], None)


def test_management_real_project_save_reopen(view, qtbot, tmp_path, monkeypatch):
    monkeypatch.setattr(ModelLibraryService, 'probe_native', lambda self: None)
    target = tmp_path / 'management.prism'
    view.do_save(str(target), create_new=True, silent=True)
    qtbot.waitUntil(lambda: getattr(view, '_save_operation', None) is None and bool(view.filename), timeout=30000)
    window = view.parent
    window.open_model_library()
    qtbot.waitUntil(lambda: window.model_composition.service is not None, timeout=15000)
    service = window.model_composition.service
    tree = view.category_panel.resource_service
    from tests.resource_tree_helpers import confirm_the_editor
    window.model_library.create_folder()
    confirm_the_editor(view.category_panel.resource_tree, 'Created from toolbar')
    created = next(n for n in tree.nodes if n['title'] == 'Created from toolbar')
    assert created['section'] == 'model'
    first = tree.createFolder('model', 'Characters')
    second = tree.createFolder('model', 'Props')
    nested = tree.createFolder('model', 'Nested', second['id'])
    assert window.model_library.folder_button.isEnabled()
    sources = []
    for name in ('one.obj', 'two.obj'):
        file = tmp_path / name
        file.write_text('v 0 0 0\nv 1 0 0\nv 0 1 0\nf 1 2 3\n')
        sources.append(file)
    expected_hashes = [hashlib.sha256(p.read_bytes()).hexdigest() for p in sources]
    window.model_library.import_paths(tuple(map(str, sources)))
    qtbot.waitUntil(lambda: not service.pending_writes and service.query(ModelQuery()).total == 2, timeout=15000)
    ids = [m.model_id for m in service.query(ModelQuery()).items]
    qtbot.waitUntil(lambda: window.model_library.cards is not None
        and window.model_library.cards.ready, timeout=20000)
    grid = window.model_library.cards
    from tests.test_material_cards import javascript
    # The card page renames in place: F2 puts an input into the card, the
    # new name is typed into it and Enter commits it.  The dialog belongs
    # to the panel's own key handler, not to the card page.
    javascript(grid, "document.querySelector('.asset-card').dispatchEvent(new KeyboardEvent('keydown', {key:'F2', bubbles:true})); true", qtbot)
    assert javascript(grid, """(()=>{
        const input=document.querySelector('.asset-card h3 input');
        if(!input)return false;
        input.value='Card rename';
        input.dispatchEvent(new KeyboardEvent('keydown',{key:'Enter',bubbles:true}));
        return true;})()""", qtbot), 'F2 did not open the rename input in the card'
    qtbot.waitUntil(lambda: any(m.name == 'Card rename' for m in service.query(ModelQuery()).items), timeout=5000)
    originals = {i: service.repository.get_payload(i)['files'] for i in ids}
    assert window.model_composition.move_library_assets('model', ids, first['id'])
    assert service.query(ModelQuery(folder_id=first['id'])).total == 2
    assert window.model_composition.move_library_assets('model', ids, nested['id'])
    window.open_model_library(nested['id'])
    assert window.model_library._folder_id() == nested['id']
    library = window.model_library
    library.rename_model(ids[0], 'Renamed model')
    library.selection.set_single(ids[0])
    library._load_record(ids[0])
    qtbot.waitUntil(lambda: library.details._record is not None, timeout=10000)
    library.details.title.setText('Sidebar rename')
    library.details.title.editingFinished.emit()
    assert service.get(ids[0]).name == 'Sidebar rename'
    library.rename_model(ids[0], 'Renamed model')
    library.selection.set_single(ids[0])
    library.selection.toggle(ids[1])
    # A DOM Delete goes through the actual WebChannel signal to the controller.
    javascript(grid, "document.body.dispatchEvent(new KeyboardEvent('keydown', {key:'Delete', bubbles:true})); true", qtbot)
    qtbot.waitUntil(lambda: service.query(ModelQuery()).total == 0, timeout=5000)
    assert service.query(ModelQuery()).total == 0
    library._delete_toast.undo()
    assert service.query(ModelQuery()).total == 2
    material_folders = tree.createFolder('material', 'Material folder')
    materials = view.category_panel.material_service
    a, b = materials.create(name='A'), materials.create(name='B')
    assert window.model_composition.move_library_assets('material', [a['id'], b['id']], material_folders['id'])
    with monkeypatch.context() as readonly:
        readonly.setattr(view.scene, 'readonly_project', True, raising=False)
        readonly.setattr(view.scene, 'readonly_project_path', view.filename, raising=False)
        assert not window.model_composition.move_library_assets('material', [a['id']], None)
        assert materials.get(a['id'])['folderId'] == material_folders['id']
    materials.update(a['id'], name='Renamed material', notes='kept')
    with pytest.raises(ValueError):
        materials.move_records([a['id'], 'missing'], None)
    assert materials.get(a['id'])['folderId'] == material_folders['id']
    view.do_save(view.filename, create_new=False, silent=True)
    qtbot.waitUntil(lambda: getattr(view, '_save_operation', None) is None, timeout=30000)
    reopened = ModelRepositoryAdapter(open_model_context(target))
    assert reopened.get(ids[0]).name == 'Renamed model'
    for i in ids:
        assert reopened.get(i).folder_id == nested['id']
        assert reopened.get_payload(i)['files'] == originals[i]
    assert [hashlib.sha256(p.read_bytes()).hexdigest() for p in sources] == expected_hashes
    from prism.storage.managed import database_path
    with sqlite3.connect(database_path(target)) as db:
        rows = db.execute('SELECT payload FROM workspace_nodes').fetchall()
        assert sum(json.loads(row[0])['section'] == 'model' for row in rows) == 4
    # Actual GUI reopen exercises reconstruction of model folders and material metadata.
    view.open_from_file(str(target))
    qtbot.waitUntil(lambda: getattr(view, '_opening_operation', None) is None
        or view._opening_operation.terminal, timeout=30000)
    qtbot.waitUntil(lambda: view.category_panel.resource_service._by_id(nested['id']) is not None, timeout=15000)
    assert view.category_panel.material_service.get(a['id'])['name'] == 'Renamed material'
