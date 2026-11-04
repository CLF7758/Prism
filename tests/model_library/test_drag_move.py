"""Existing card drag -> shared tree drop -> durable model directory move."""
import hashlib

from PyQt6 import QtCore, QtGui, QtWidgets

from prism.model_library.contracts import ModelQuery
from prism.model_library.project_storage import open_model_context
from prism.model_library.repository import ModelRepositoryAdapter
from prism.model_library.service import ModelLibraryService
from prism.widgets.library_transfer import read_library_drag
from tests.test_material_cards import javascript


def test_model_single_and_multi_drag_reuse_material_path(view, qtbot, tmp_path, monkeypatch):
    monkeypatch.setattr(ModelLibraryService, 'probe_native', lambda self: None)
    project = tmp_path / 'drag.prism'
    view.do_save(str(project), create_new=True, silent=True)
    qtbot.waitUntil(lambda: getattr(view, '_save_operation', None) is None and bool(view.filename), timeout=30000)
    window = view.parent
    window.open_model_library()
    qtbot.waitUntil(lambda: window.model_composition.service is not None, timeout=15000)
    service = window.model_composition.service
    folders = view.category_panel.resource_service
    a = folders.createFolder('model', 'A')
    b = folders.createFolder('model', 'B')
    files = []
    for i in range(3):
        path = tmp_path / f'model-{i}.obj'
        path.write_text(f'# model {i}\nv 0 0 0\nv 1 0 0\nv 0 1 0\nf 1 2 3\n')
        files.append(path)
    hashes = [hashlib.sha256(path.read_bytes()).hexdigest() for path in files]
    service.import_paths(tuple(map(str, files)), folder_id=a['id'])
    qtbot.waitUntil(lambda: not service.pending_writes and service.query(ModelQuery()).total == 3, timeout=15000)
    window.open_model_library(a['id'])
    library = window.model_library
    qtbot.waitUntil(lambda: library.cards is not None and library.cards.ready
                   and len(library.cards._record_ids) == 3, timeout=20000)
    cards = library.cards
    ids = list(cards._record_ids)
    originals = {key: service.repository.get_payload(key)['files'] for key in ids}
    tree = view.category_panel.resource_tree
    tree.expand(tree.model()._index_for('model'))
    tree.resize(350, 600)
    tree.show()
    received = []

    def native_drop(drag, actions, default=QtCore.Qt.DropAction.MoveAction):
        assert actions & QtCore.Qt.DropAction.MoveAction
        assert drag.mimeData().hasUrls()
        value = read_library_drag(drag.mimeData(), folders)
        assert value['kind'] == 'model'
        received.append(value['ids'])
        index = tree.model().index_for_id(b['id'])
        tree.scrollTo(index)
        position = tree.visualRect(index).center()
        enter = QtGui.QDragEnterEvent(position, actions, drag.mimeData(),
                                     QtCore.Qt.MouseButton.LeftButton, QtCore.Qt.KeyboardModifier.NoModifier)
        tree.dragEnterEvent(enter)
        assert enter.isAccepted()
        move = QtGui.QDragMoveEvent(position, actions, drag.mimeData(),
                                   QtCore.Qt.MouseButton.LeftButton, QtCore.Qt.KeyboardModifier.NoModifier)
        tree.dragMoveEvent(move)
        assert move.isAccepted()
        drop = QtGui.QDropEvent(QtCore.QPointF(position), actions, drag.mimeData(),
                               QtCore.Qt.MouseButton.LeftButton, QtCore.Qt.KeyboardModifier.NoModifier)
        tree.dropEvent(drop)
        assert drop.isAccepted()
        return QtCore.Qt.DropAction.MoveAction

    monkeypatch.setattr(QtGui.QDrag, 'exec', native_drop)
    monkeypatch.setattr(QtWidgets.QApplication, 'mouseButtons',
                        lambda: QtCore.Qt.MouseButton.LeftButton)
    # Actual DOM click/ctrl-click crosses WebChannel and synchronously mirrors
    # the model-owned selection back to the same shared cards.
    javascript(cards, "document.querySelectorAll('.asset-card')[0].click(); true", qtbot)
    qtbot.waitUntil(lambda: library.selection.count == 1, timeout=15000)
    javascript(cards, "document.querySelectorAll('.asset-card')[1].dispatchEvent(new MouseEvent('click', {bubbles:true,ctrlKey:true})); true", qtbot)
    qtbot.waitUntil(lambda: library.selection.count == 2, timeout=15000)
    assert cards.selected_ids == set(ids[:2])
    assert cards.selected_id == ids[1]
    assert library.selected_id == ids[1]
    dom = cards._dom_id(ids[1])
    javascript(cards, f"document.querySelector('[data-asset=\"{dom}\"]').dispatchEvent(new PointerEvent('pointerdown', {{bubbles:true,button:0,buttons:1,clientX:10,clientY:10}})); document.dispatchEvent(new PointerEvent('pointermove', {{bubbles:true,cancelable:true,buttons:1,clientX:30,clientY:10}})); true", qtbot)
    qtbot.waitUntil(lambda: service.query(ModelQuery(folder_id=b['id'])).total == 2, timeout=10000)
    assert set(received[0]) == set(ids[:2])
    qtbot.waitUntil(lambda: cards._record_ids == [ids[2]], timeout=10000)
    # Dragging a previously unselected remaining card selects/moves it alone.
    dom = cards._dom_id(ids[2])
    javascript(cards, f"document.querySelector('[data-asset=\"{dom}\"]').dispatchEvent(new PointerEvent('pointerdown', {{bubbles:true,button:0,buttons:1,clientX:10,clientY:10}})); document.dispatchEvent(new PointerEvent('pointermove', {{bubbles:true,cancelable:true,buttons:1,clientX:30,clientY:10}})); true", qtbot)
    qtbot.waitUntil(lambda: service.query(ModelQuery(folder_id=a['id'])).total == 0, timeout=10000)
    assert received[1] == [ids[2]]
    assert service.query(ModelQuery(folder_id=b['id'])).total == 3
    view.do_save(view.filename, create_new=False, silent=True)
    qtbot.waitUntil(lambda: getattr(view, '_save_operation', None) is None, timeout=30000)
    reopened = ModelRepositoryAdapter(open_model_context(project))
    for key in ids:
        assert reopened.get(key).folder_id == b['id']
        assert reopened.get_payload(key)['files'] == originals[key]
    assert [hashlib.sha256(path.read_bytes()).hexdigest() for path in files] == hashes
