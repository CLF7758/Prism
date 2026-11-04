from types import SimpleNamespace
from PyQt6 import QtCore
from prism.actions.clipboard_workflow import copy_selection


def test_copy_owns_no_view_and_empty_selection_preserves_clipboard():
    mime = QtCore.QMimeData()
    calls = []
    clipboard = SimpleNamespace(mimeData=lambda: mime)
    item = SimpleNamespace(copy_to_clipboard=lambda cb: calls.append(cb))
    scene = SimpleNamespace(selectedItems=lambda **kw: [item, item],
        copy_selection_to_internal_clipboard=lambda: calls.append('internal'))
    assert copy_selection(scene, clipboard) == 2
    assert bytes(mime.data('prism/items')) == b'2'
    assert calls == [clipboard, 'internal']
    scene.selectedItems = lambda **kw: []
    assert copy_selection(scene, clipboard) == 0
    assert bytes(mime.data('prism/items')) == b'2'
    assert len(calls) == 2
