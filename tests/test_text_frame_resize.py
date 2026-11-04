from unittest.mock import MagicMock

import pytest
from PyQt6 import QtCore, QtGui, QtWidgets

from prism.items import PrismTextItem


@pytest.mark.parametrize('corner_index', range(4))
@pytest.mark.parametrize('rotation', [0, 37])
def test_corner_resize_keeps_opposite_corner_and_font(view, corner_index, rotation):
    item = PrismTextItem('Text', size=24)
    view.scene.addItem(item)
    item.set_frame_size(160, 90)
    item.setScale(2)
    item.setRotation(rotation)
    item.setSelected(True)
    corner = item.corners[corner_index]
    anchor = item.get_scale_anchor(corner)
    fixed = item.mapToScene(anchor)
    direction = item.get_corner_direction(corner)
    press = MagicMock()
    press.button.return_value = QtCore.Qt.MouseButton.LeftButton
    press.pos.return_value = corner
    item.mousePressEvent(press)
    target = anchor + QtCore.QPointF(230 * direction.x(), 130 * direction.y())
    move = MagicMock()
    move.scenePos.return_value = item.mapToScene(target)
    item.mouseMoveEvent(move)
    item.mouseReleaseEvent(move)
    assert item.width == pytest.approx(230)
    assert item.height == pytest.approx(130)
    assert item.style()['size'] == 24
    assert item.scale() == 2
    opposite = item.corners[(corner_index + 2) % 4]
    assert (item.mapToScene(opposite) - fixed).manhattanLength() < 1e-6
    view.scene.undo_stack.undo()
    assert item.width == pytest.approx(160)
    assert item.height == pytest.approx(90)
    view.scene.undo_stack.redo()
    assert item.width == pytest.approx(230)


def test_frame_save_copy_and_wrap(qapp):
    item = PrismTextItem('words ' * 20, size=18)
    item.set_frame_size(100, 180)
    assert item.document().size().height() > 30
    for copy in (item.create_copy(), PrismTextItem.create_from_data(data=item.get_extra_save_data())):
        assert copy.width == pytest.approx(100)
        assert copy.height == pytest.approx(180)
        assert copy.style() == item.style()


def test_edit_paint_uses_cosmetic_cursor(qapp):
    item = PrismTextItem('Text', color='#ffffff')
    item.edit_mode = True
    item.hasFocus = lambda: True
    image = QtGui.QImage(600, 400, QtGui.QImage.Format.Format_ARGB32)
    image.fill(QtCore.Qt.GlobalColor.transparent)
    painter = QtGui.QPainter(image)
    try:
        painter.scale(10, 10)
        item.paint(painter, QtWidgets.QStyleOptionGraphicsItem(), None)
    finally:
        painter.end()
    # Above the glyphs, the insertion caret must stay one screen pixel wide.
    row = int(item.document().documentMargin() * 10 + 1)
    lit = [x for x in range(image.width()) if image.pixelColor(x, row).red() > 200]
    assert len(lit) == 1
