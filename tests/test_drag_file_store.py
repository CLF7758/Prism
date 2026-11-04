from types import SimpleNamespace
from pathlib import Path
from PyQt6 import QtGui
from prism.external_drag import DragFileStore


def test_original_and_derived_file_lifetimes(qapp, tmp_path):
    original = tmp_path / 'original.png'
    original.write_bytes(b'original')
    store = DragFileStore()
    assert store.path_for(SimpleNamespace(filename=str(original)), 1) == str(original)
    pixmap = QtGui.QPixmap(2, 2)
    pixmap.fill(QtGui.QColor('red'))
    derived = Path(store.path_for(SimpleNamespace(pixmap=lambda: pixmap), 2))
    assert derived.exists()
    assert store.path_for(SimpleNamespace(), 3) is None
    store.directory.cleanup()
    assert not derived.exists()
    assert original.read_bytes() == b'original'
