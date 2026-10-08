"""Canvas annotations and grayscale must stay local and avoid eager decoding."""
import hashlib
import time
from unittest.mock import Mock

from PyQt6 import QtCore, QtGui, QtWidgets

from prism import fileio
from prism.fileio.sql import SQLiteIO
from prism.items import PrismPixmapItem, PrismPathItem, PrismTextItem


def two_canvases(window):
    window.view.scene.workspace_pages = [
        {'id': name, 'kind': 'canvas', 'title': name, 'content': None}
        for name in ('A', 'B')]
    window.open_workspace_page('canvas', 'A')


def test_annotations_stay_on_their_canvas_and_survive_filters(main_window):
    two_canvases(main_window)
    view = main_window.view
    view._begin_stroke(QtCore.QPointF(10, 20))
    stroke = view._draw_item
    stroke.set_points([(10, 20), (120, 150)])
    view._finish_pending_stroke()
    view.on_action_insert_text()
    text = next(item for item in view.scene.items() if isinstance(item, PrismTextItem))
    text.setPlainText('A annotation')
    text.exit_edit_mode()
    assert stroke.canvas_id == text.canvas_id == 'A'
    positions = (stroke.pos(), text.pos())
    main_window.open_workspace_page('canvas', 'B')
    assert not stroke.isVisible() and not text.isVisible()
    main_window.open_workspace_page('canvas', 'A')
    view.category_panel._active_filter = ('category', 'unrelated')
    view.category_panel._min_rating = 5
    view.category_panel._apply_filter()
    assert stroke.isVisible() and text.isVisible()
    assert (stroke.pos(), text.pos()) == positions


def test_annotation_canvas_membership_survives_save_reopen(main_window, tmp_path):
    two_canvases(main_window)
    view = main_window.view
    for canvas in ('A', 'B'):
        for item in (PrismPathItem([(0, 0), (10, 10)]), PrismTextItem(canvas)):
            item._canvas_id = canvas
            view.scene.addItem(item)
    path = str(tmp_path / 'annotations.prism')
    writer = SQLiteIO(path, view.scene, create_new=True)
    writer.write()
    writer.connection.close()
    view.clear_scene()
    reader = SQLiteIO(path, view.scene, readonly=True)
    reader.read()
    view.scene.add_queued_items()
    reader.connection.close()
    for canvas in ('A', 'B'):
        main_window.open_workspace_page('canvas', canvas)
        items = list(view.scene.items_for_save())
        assert len(items) == 4
        assert all(item.isVisible() == (item.canvas_id == canvas) for item in items)


def test_grayscale_batch_does_not_decode_and_only_affects_current_canvas(
        main_window, tmp_path, monkeypatch):
    two_canvases(main_window)
    view = main_window.view
    image = QtGui.QImage(4000, 3000, QtGui.QImage.Format.Format_RGB32)
    image.fill(QtGui.QColor('red'))
    path = tmp_path / 'large.jpg'
    assert image.save(str(path))
    items = []
    for index in range(101):
        item = fileio._load_image_item(str(path))
        item._canvas_id = 'A' if index < 100 else 'B'
        item.setPos(index % 10 * 4100, index // 10 * 3100)
        view.scene.addItem(item)
        monkeypatch.setattr(item, 'pixmap', Mock(side_effect=AssertionError('eager decode')))
        items.append(item)
    bar = main_window.filter_popover.color_host
    started = time.monotonic()
    bar.apply_filter('grayscale')
    elapsed = time.monotonic() - started
    assert elapsed < 1.0
    assert all(item.grayscale for item in items[:100])
    assert not items[-1].grayscale
    assert all(not item.has_full_pixmap() for item in items)
    assert all(item._grayscale_cache is None for item in items)
    canvas = QtGui.QImage(900, 600, QtGui.QImage.Format.Format_ARGB32)
    canvas.fill(QtGui.QColor('transparent'))
    painter = QtGui.QPainter(canvas)
    started = time.monotonic()
    view.scene.render(painter, QtCore.QRectF(0, 0, 900, 600),
                      view.scene.itemsBoundingRect(items=items[:100]))
    painter.end()
    assert time.monotonic() - started < 2.0
    displayed = [canvas.pixelColor(x, y) for x in range(20, 900, 80)
                 for y in range(20, 600, 60)]
    opaque = [color for color in displayed if color.alpha()]
    assert opaque
    assert all(color.red() == color.green() == color.blue() for color in opaque)
    assert all(not item.has_full_pixmap() for item in items)
    main_window.open_workspace_page('canvas', 'B')
    assert not bar._grayscale_active
    main_window.open_workspace_page('canvas', 'A')
    assert bar._grayscale_active
    bar.apply_filter('grayscale')
    assert all(not item.grayscale for item in items)


def test_native_grayscale_renders_gray_and_restores_color(view):
    image = QtGui.QImage(40, 40, QtGui.QImage.Format.Format_ARGB32)
    image.fill(QtGui.QColor('red'))
    item = PrismPixmapItem(image)
    view.scene.addItem(item)

    def rendered_color():
        canvas = QtGui.QImage(40, 40, QtGui.QImage.Format.Format_ARGB32)
        canvas.fill(QtGui.QColor('transparent'))
        painter = QtGui.QPainter(canvas)
        view.scene.render(painter, QtCore.QRectF(0, 0, 40, 40),
                          QtCore.QRectF(0, 0, 40, 40))
        painter.end()
        return canvas.pixelColor(20, 20)

    item.grayscale = True
    gray = rendered_color()
    assert gray.red() == gray.green() == gray.blue()
    assert 0 < gray.red() < 255
    assert item._grayscale_cache is None
    item.grayscale = False
    assert rendered_color() == QtGui.QColor('red')


def test_explicit_grayscale_export_keeps_resolution_alpha_and_source(qapp, tmp_path):
    image = QtGui.QImage(600, 400, QtGui.QImage.Format.Format_ARGB32)
    image.fill(QtGui.QColor(180, 40, 90, 128))
    path = tmp_path / 'alpha.png'
    assert image.save(str(path))
    original = hashlib.sha256(path.read_bytes()).digest()
    item = fileio._load_image_item(str(path))
    item.grayscale = True
    assert not item.has_full_pixmap()
    data, _ = item.pixmap_to_bytes(apply_grayscale=True)
    exported = QtGui.QImage.fromData(data)
    assert exported.size() == image.size()
    color = exported.pixelColor(20, 20)
    assert color.red() == color.green() == color.blue()
    assert color.alpha() == 128
    assert hashlib.sha256(item.original_bytes()).digest() == original


def test_saved_grayscale_effect_is_installed_on_gui_thread(view, tmp_path, qtbot, qapp):
    image = QtGui.QImage(40, 40, QtGui.QImage.Format.Format_RGB32)
    image.fill(QtGui.QColor('red'))
    item = PrismPixmapItem(image)
    item.grayscale = True
    view.scene.addItem(item)
    path = str(tmp_path / 'gray.prism')
    writer = SQLiteIO(path, view.scene, create_new=True)
    writer.write()
    writer.connection.close()
    view.clear_scene()
    completed = []
    worker = fileio.ThreadedIO(fileio.load_prism, path, view.scene)
    worker.finished.connect(lambda *_: completed.append(True))
    worker.start()
    qtbot.waitUntil(lambda: bool(completed), timeout=5000)
    assert worker.wait(5000)
    view.scene.add_queued_items()
    loaded = next(view.scene.items_for_save())
    assert loaded.grayscale
    assert isinstance(loaded.graphicsEffect(), QtWidgets.QGraphicsColorizeEffect)
    assert loaded.graphicsEffect().thread() == qapp.thread()
    assert not loaded.has_full_pixmap()
