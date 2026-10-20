"""Canvas output must contain source detail rather than enlarged display LOD."""
from PyQt6 import QtCore, QtGui
from prism import fileio


def test_export_resolves_original_detail_without_retaining_full_pixmap(view, tmp_path):
    source = QtGui.QImage(1600, 900, QtGui.QImage.Format.Format_RGB32)
    source.fill(QtGui.QColor('white'))
    painter = QtGui.QPainter(source)
    for x in range(0, 1600, 2):
        painter.fillRect(x, 0, 1, 900, QtGui.QColor('black'))
    painter.end()
    path = tmp_path/'fine-stripes.png'
    assert source.save(str(path))
    original = path.read_bytes()
    item = fileio._load_image_item(str(path))
    view.scene.addItem(item)
    assert not item.has_full_pixmap()
    target = QtGui.QImage(source.size(), QtGui.QImage.Format.Format_RGB32)
    target.fill(QtGui.QColor('red'))
    painter = QtGui.QPainter(target)
    try:
        view.scene.render(painter, QtCore.QRectF(0, 0, 1600, 900), QtCore.QRectF(0, 0, 1600, 900))
    finally:
        painter.end()
    assert target.pixelColor(700, 300) == QtGui.QColor('black')
    assert target.pixelColor(701, 300) == QtGui.QColor('white')
    assert not item.has_full_pixmap()
    assert item.original_bytes() == original
