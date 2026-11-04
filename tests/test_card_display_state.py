from unittest.mock import patch
from PyQt6 import QtCore, QtGui
from prism import lod
from prism.asset_records import ImageRecord


def test_card_flip_opacity_and_painter_state(qapp):
    item = ImageRecord({'flip': -1, 'data': {'imageWidth': 20, 'imageHeight': 10, 'opacity': .5}}, ('unused', 1))
    source = QtGui.QPixmap(20, 10)
    source.fill(QtGui.QColor('red'))
    p = QtGui.QPainter(source)
    p.fillRect(10, 0, 10, 10, QtGui.QColor('blue'))
    p.end()
    image = QtGui.QImage(20, 10, QtGui.QImage.Format.Format_ARGB32_Premultiplied)
    image.fill(QtCore.Qt.GlobalColor.transparent)
    painter = QtGui.QPainter(image)
    before = painter.transform()
    with patch.object(lod, 'display_pixmap', return_value=source):
        assert lod.draw_image(painter, item, QtCore.QRectF(0, 0, 20, 10))
    assert painter.opacity() == 1
    assert painter.transform() == before
    painter.end()
    left, right = image.pixelColor(3, 5), image.pixelColor(16, 5)
    assert left.blue() > 240 and right.red() > 240
    assert 125 <= left.alpha() <= 130
