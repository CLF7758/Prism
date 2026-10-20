"""Crop handles must work through the view and keep recoverable originals."""
import hashlib

import pytest
from PyQt6 import QtCore, QtGui, QtTest
from prism.items import PrismPixmapItem
from prism.fileio.sql import SQLiteIO


@pytest.mark.parametrize('zoom,rotation', [(1., 0.), (2., 35.)])
def test_drag_corner_commit_undo_save_preserves_original(main_window, qtbot, tmp_path, zoom, rotation):
    image = QtGui.QImage(200, 160, QtGui.QImage.Format.Format_RGB32)
    image.fill(QtGui.QColor('red'))
    painter = QtGui.QPainter(image)
    painter.fillRect(0, 0, 100, 80, QtGui.QColor('blue'))
    painter.end()
    buffer = QtCore.QBuffer()
    buffer.open(QtCore.QIODevice.OpenModeFlag.WriteOnly)
    image.save(buffer, 'PNG')
    original = bytes(buffer.data())
    item = PrismPixmapItem(QtGui.QImage())
    item.pixmap_from_bytes(original)
    item.setRotation(rotation)
    view = main_window.view
    view.scene.addItem(item)
    main_window.resize(1200, 850)
    main_window.show()
    main_window.open_workspace_page('canvas', 'default-canvas')
    item.setSelected(True)
    view.setTransform(QtGui.QTransform.fromScale(zoom, zoom))
    view.centerOn(item)
    item.enter_crop_mode()
    qtbot.wait(30)
    start = view.mapFromScene(item.mapToScene(QtCore.QPointF(200, 160)))
    target = view.mapFromScene(item.mapToScene(QtCore.QPointF(150, 120)))
    QtTest.QTest.mousePress(view.viewport(), QtCore.Qt.MouseButton.LeftButton, pos=start)
    QtTest.QTest.mouseMove(view.viewport(), target, 30)
    QtTest.QTest.mouseRelease(view.viewport(), QtCore.Qt.MouseButton.LeftButton, pos=target)
    assert item.crop_temp.width() == pytest.approx(150, abs=1)
    assert item.crop_temp.height() == pytest.approx(120, abs=1)
    item.exit_crop_mode(True)
    cropped = QtCore.QRectF(item.crop)
    view.undo_stack.undo()
    assert item.crop == QtCore.QRectF(0, 0, 200, 160)
    view.undo_stack.redo()
    assert item.crop == cropped
    path = tmp_path/'crop-roundtrip.prism'
    io = SQLiteIO(str(path), view.scene, create_new=True)
    io.write()
    io._close_connection()
    view.clear_scene()
    io = SQLiteIO(str(path), view.scene, readonly=True)
    io.read()
    io._close_connection()
    view.scene.add_queued_items()
    restored = next(view.scene.items_for_save())
    assert restored.crop == cropped
    assert hashlib.sha256(restored.original_bytes()).digest() == hashlib.sha256(original).digest()
