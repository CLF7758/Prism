"""Canvas annotations and grayscale must stay local and avoid eager decoding."""
import hashlib
import time
import pytest
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


def test_toolbar_filters_legacy_canvas_with_new_pages(main_window):
    window = main_window
    images = []
    for color in ('red', 'blue'):
        image = QtGui.QImage(40, 40, QtGui.QImage.Format.Format_RGB32)
        image.fill(QtGui.QColor(color))
        item = PrismPixmapItem(image)
        item.color_group = color
        window.view.scene.addItem(item)
        images.append(item)
    window.create_workspace_page_quietly('canvas', 'Empty')
    new_canvas = window.current_page_id
    assert not any(item.isVisible() for item in images)
    window._reload_workspace_pages()
    assert window.current_page_id == 'default-canvas'
    window.canvas_toolbar.gray.click()
    assert all(item.grayscale for item in images)
    assert window.canvas_toolbar.gray.isChecked()
    window.canvas_toolbar.colours['red'].click()
    assert images[0].opacity() == 1.0
    assert images[1].opacity() == 0.3
    window.open_workspace_page('canvas', new_canvas)
    assert not window.canvas_toolbar.gray.isChecked()
    assert window.color_filter._active_group is None
    assert all(item.opacity() == 1.0 for item in images)
    assert window.color_filter.all_chip._count == 0
    window.open_workspace_page('canvas', 'default-canvas')
    assert window.canvas_toolbar.gray.isChecked()
    window.canvas_toolbar.gray.click()
    assert all(not item.grayscale for item in images)


def test_color_filter_only_changes_current_canvas(main_window):
    two_canvases(main_window)
    items = []
    for canvas in ('A', 'B'):
        image = QtGui.QImage(40, 40, QtGui.QImage.Format.Format_RGB32)
        image.fill(QtGui.QColor('blue'))
        item = PrismPixmapItem(image)
        item._canvas_id = canvas
        item.color_group = 'blue'
        main_window.view.scene.addItem(item)
        items.append(item)
    main_window.color_filter.recount()
    main_window.canvas_toolbar.colours['red'].click()
    assert items[0].opacity() == 0.3
    assert items[1].opacity() == 1.0
    assert main_window.color_filter.all_chip._count == 1


@pytest.mark.parametrize('previous_match_opacity', [0.3, 1.0])
def test_saved_opacity_is_not_guessed_from_color_groups(
        main_window, previous_match_opacity):
    images = []
    for color in ('red', 'blue'):
        image = QtGui.QImage(40, 40, QtGui.QImage.Format.Format_RGB32)
        image.fill(QtGui.QColor(color))
        item = PrismPixmapItem(image)
        item.color_group = color
        item.setOpacity(previous_match_opacity if color == 'blue' else 0.3)
        main_window.view.scene.addItem(item)
        images.append(item)
    main_window.color_filter.apply_filter('red')
    assert images[0].opacity() == 0.3
    assert images[1].opacity() == 0.3
    assert images[0].get_extra_save_data()['opacity'] == 0.3
    assert images[1].get_extra_save_data()['opacity'] == previous_match_opacity
    main_window.color_filter.clear_filter()
    assert images[0].opacity() == 0.3
    assert images[1].opacity() == previous_match_opacity


def test_filter_preserves_custom_opacity_when_saving(main_window):
    image = QtGui.QImage(40, 40, QtGui.QImage.Format.Format_RGB32)
    image.fill(QtGui.QColor('blue'))
    item = PrismPixmapItem(image)
    item.color_group = 'blue'
    item.setOpacity(0.6)
    main_window.view.scene.addItem(item)
    main_window.color_filter.apply_filter('red')
    assert item.opacity() == 0.3
    assert item.get_extra_save_data()['opacity'] == 0.6
    main_window.color_filter.clear_filter()
    assert item.opacity() == 0.6


def test_manual_opacity_edit_with_filter_saves_and_restores(main_window):
    from prism.commands import ChangeOpacity
    image = QtGui.QImage(40, 40, QtGui.QImage.Format.Format_RGB32)
    image.fill(QtGui.QColor('red'))
    item = PrismPixmapItem(image)
    item.color_group = 'red'
    main_window.view.scene.addItem(item)
    bar = main_window.color_filter
    bar.apply_filter('red')
    main_window.view.undo_stack.push(ChangeOpacity([item], 0.5))
    assert item.get_extra_save_data()['opacity'] == 0.5
    main_window.view.undo_stack.undo()
    assert item.get_extra_save_data()['opacity'] == 1.0
    main_window.view.undo_stack.redo()
    bar.apply_filter('blue')
    bar.apply_filter('red')
    assert item.opacity() == 0.5
    bar.clear_filter()
    assert item.opacity() == 0.5


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
