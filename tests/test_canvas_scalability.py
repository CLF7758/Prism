"""Bounded rendering and geometry-only invalidation regressions."""
import sqlite3
from unittest.mock import Mock

import pytest
from PyQt6 import QtCore, QtGui, QtWidgets
from prism import fileio, image_decode, lod, source_store
from prism.items import PrismPixmapItem, PrismTextItem, PrismPathItem
from tests.test_image_decode import source


def test_repaints_do_not_rescan_geometry(view, tmp_path, qapp, monkeypatch):
    item = fileio._load_image_item(str(source(tmp_path)))
    view.scene.addItem(item)
    qapp.processEvents()
    bounds = QtCore.QRectF(view._content_bounds())
    scan = Mock(wraps=view.scene.itemsBoundingRect)
    monkeypatch.setattr(view.scene, 'itemsBoundingRect', scan)
    for _ in range(20):
        item.update()
        qapp.processEvents()
        assert view._content_bounds() == bounds
    assert scan.call_count == 0
    item.moveBy(100, 100)
    assert view._content_bounds() != bounds
    assert scan.call_count == 1


def test_bounds_follow_rotation_crop_text_and_path(view, tmp_path, qapp):
    item = fileio._load_image_item(str(source(tmp_path)))
    view.scene.addItem(item)
    before = QtCore.QRectF(view._content_bounds())
    item.setRotation(45)
    assert view._content_bounds() != before
    before = QtCore.QRectF(view._content_bounds())
    item.crop = QtCore.QRectF(0, 0, 100, 100)
    assert view._content_bounds() != before
    text = PrismTextItem('a')
    view.scene.addItem(text)
    view._content_bounds()
    text.setPlainText('long text ' * 200)
    assert view._cached_scene_bounds is None
    view._content_bounds()
    path = PrismPathItem(points=[(0, 0), (10, 10)])
    view.scene.addItem(path)
    view._content_bounds()
    path.set_points([(0, 0), (10000, 10000)])
    assert view._cached_scene_bounds is None


def test_preview_eviction_keeps_source_and_geometry(view, tmp_path, qapp, monkeypatch):
    store = lod.DisplayCache(qapp)
    store.budget = 192 * 192 * 4 * 2
    monkeypatch.setattr(lod, 'cache', lambda: store)
    filename = source(tmp_path)
    items = [fileio._load_image_item(str(filename)) for _ in range(20)]
    for item in items:
        view.scene.addItem(item)
    assert store.bytes <= store.budget
    assert sum(item._preview_pixmap is not None for item in items) <= 3
    assert items[0]._preview_pixmap is None
    assert lod.overview_pixmap(items[0]).width() <= lod.OVERVIEW_MAX_PIXELS
    assert items[0]._image_size == QtCore.QSize(1600, 900)
    assert items[0].original_bytes() == filename.read_bytes()
    assert not items[0].has_full_pixmap()


def test_tiny_overview_skips_decode_but_export_and_selection_do_not(view, tmp_path, monkeypatch):
    item = fileio._load_image_item(str(source(tmp_path)))
    view.scene.addItem(item)
    item._preview_pixmap = None
    resolver = Mock(return_value=(QtGui.QPixmap(), QtCore.QRectF()))
    monkeypatch.setattr(item, '_pixmap_for_painting', resolver)
    image = QtGui.QImage(40, 40, QtGui.QImage.Format.Format_RGB32)
    image.fill(QtGui.QColor('white'))
    painter = QtGui.QPainter(image)
    painter.scale(0.004, 0.004)
    option = QtWidgets.QStyleOptionGraphicsItem()
    try:
        item.paint(painter, option, view.viewport())
        assert resolver.call_count == 0
        assert image.pixelColor(2, 2) != QtGui.QColor('white')
        item.paint(painter, option, None)
        assert resolver.call_count == 1
        item.setSelected(True)
        item.paint(painter, option, view.viewport())
        assert resolver.call_count == 2
    finally:
        painter.end()


def test_overloaded_decoder_retries_all_visible_items(view, qtbot, tmp_path):
    filename = source(tmp_path)
    items = [fileio._load_image_item(str(filename)) for _ in range(24)]
    for item in items:
        view.scene.addItem(item)
    service = image_decode.DisplayDecoder(QtWidgets.QApplication.instance())
    class Owner(QtWidgets.QWidget):
        def isVisible(self): return True
        def needs_display(self, item): return True
        def update(self):
            for item in items:
                if lod.cache().get(item, 512) is None:
                    service.request(item, 512, self)
    owner = Owner()
    try:
        owner.update()
        assert len(service.requests) <= service.max_pending
        assert owner in service.waiting_owners
        qtbot.waitUntil(lambda: all(lod.cache().get(item, 512) is not None for item in items),
                        timeout=10000)
        qtbot.waitUntil(lambda: not service.requests, timeout=1000)
    finally:
        service.shutdown()


def test_archive_reader_does_not_create_missing_projects(tmp_path):
    path = tmp_path / 'missing.prism'
    with pytest.raises(sqlite3.Error):
        source_store.read_archive((str(path), 7))
    assert not path.exists()


def test_overview_frame_reuses_during_navigation_and_invalidates(view, monkeypatch):
    from prism import canvas_overview
    monkeypatch.setattr(canvas_overview, 'MIN_ITEMS', 1)
    image = QtGui.QImage(1000, 1000, QtGui.QImage.Format.Format_RGB32)
    image.fill(QtGui.QColor('red'))
    item = PrismPixmapItem(image)
    view.scene.addItem(item)
    view.scale(0.01, 0.01)
    renderer = view._overview_renderer
    assert renderer.eligible()
    draw = Mock(wraps=view.scene.render)
    monkeypatch.setattr(view.scene, 'render', draw)
    first, _ = renderer.image_for_view()
    assert max(first.width(), first.height()) <= canvas_overview.MAX_CACHE_SIDE
    assert getattr(view.scene, '_overview_recording_owner', None) is None
    assert renderer.image_for_view()[0] is first
    assert draw.call_count == 1
    view.pan(QtCore.QPointF(100, 20))
    assert renderer.image_for_view()[0] is first
    assert draw.call_count == 1
    view._end_interaction()
    assert renderer.frame[0] is first
    # A pan outside the buffered area still needs a new frame.
    renderer.image_for_view()
    assert draw.call_count <= 2
    view.scene.changed.emit([QtCore.QRectF()])
    assert renderer.frame is None
    view.scene.edit_item = item
    assert not renderer.eligible()
    view.scene.edit_item = None


def test_overview_preserves_stacking_and_export_scope(view, monkeypatch):
    from prism import canvas_overview
    monkeypatch.setattr(canvas_overview, 'MIN_ITEMS', 1)
    items = []
    for colour in ('red', 'blue'):
        image = QtGui.QImage(1000, 1000, QtGui.QImage.Format.Format_RGB32)
        image.fill(QtGui.QColor(colour))
        item = PrismPixmapItem(image)
        view.scene.addItem(item)
        items.append(item)
    items[1].setZValue(1)
    view.scale(0.004, 0.004)
    renderer = view._overview_renderer
    def centre_pixel():
        image, rect = renderer.image_for_view()
        x = round((500-rect.x())/rect.width()*image.width())
        y = round((500-rect.y())/rect.height()*image.height())
        return image.pixelColor(x, y)
    assert centre_pixel() == QtGui.QColor('blue')
    items[1].setVisible(False)
    renderer.invalidate()
    assert centre_pixel() == QtGui.QColor('red')
    # The recording marker is scoped to this renderer, not scene exports.
    assert getattr(view.scene, '_overview_recording_owner', None) is None
    output = QtGui.QImage(1000, 1000, QtGui.QImage.Format.Format_RGB32)
    painter = QtGui.QPainter(output)
    try:
        view.scene.render(painter, QtCore.QRectF(output.rect()), QtCore.QRectF(0, 0, 1000, 1000))
    finally:
        painter.end()
    assert output.pixelColor(500, 500) == QtGui.QColor('red')


def test_storage_statistics_are_shared_cached_and_read_only(tmp_path):
    from types import SimpleNamespace
    archive = tmp_path / 'board.prism'
    with sqlite3.connect(archive) as connection:
        connection.execute('CREATE TABLE sqlar (item_id INTEGER, sz INTEGER, data BLOB)')
        connection.execute('INSERT INTO sqlar VALUES (7, 3, ?)', (b'png',))
    item = SimpleNamespace(_prism_source=(str(archive), 7))
    cache = {}
    assert source_store.storage_bytes([item, item], cache) == 6
    assert source_store.read_archive(item._prism_source) == b'png'
    assert len(cache) == 1
    with sqlite3.connect(archive) as connection:
        connection.execute('UPDATE sqlar SET sz = 9')
    assert source_store.storage_bytes([item], cache) == 9
    assert len(cache) == 1
    missing = SimpleNamespace(_prism_source=(str(tmp_path/'missing.prism'), 1))
    assert source_store.storage_bytes([missing], cache) is None
    assert not (tmp_path/'missing.prism').exists()


def test_overview_updates_only_dirty_area_after_item_moves(view, monkeypatch):
    from prism import canvas_overview
    monkeypatch.setattr(canvas_overview, 'MIN_ITEMS', 1)
    image = QtGui.QImage(1000, 1000, QtGui.QImage.Format.Format_RGB32)
    image.fill(QtGui.QColor('red'))
    item = PrismPixmapItem(image)
    view.scene.addItem(item)
    view.scale(0.004, 0.004)
    renderer = view._overview_renderer
    frame, rect = renderer.image_for_view()
    old = item.sceneBoundingRect()
    item.moveBy(2000, 0)
    view.scene.changed.emit([old, item.sceneBoundingRect()])
    draw = Mock(wraps=view.scene.render)
    monkeypatch.setattr(view.scene, 'render', draw)
    assert renderer.image_for_view()[0] is frame
    draw.assert_called_once()
    assert draw.call_args.args[2].width() < rect.width()/2
    def pixel_at(x, y):
        return frame.pixelColor(round((x-rect.x())/rect.width()*frame.width()),
                                round((y-rect.y())/rect.height()*frame.height()))
    assert pixel_at(500, 500) != QtGui.QColor('red')
    assert pixel_at(2500, 500) == QtGui.QColor('red')


def test_drag_geometry_recalculation_waits_for_gesture(view):
    view.scene.active_mode = view.scene.MOVE_MODE
    view._cached_scene_bounds = None
    view.on_scene_changed([QtCore.QRectF(0, 0, 10, 10)])
    assert view._recalc_timer.isActive()
    assert view._recalc_timer.interval() == 120
    view.scene.active_mode = None
    view.on_scene_changed([QtCore.QRectF(0, 0, 10, 10)])
    assert view._recalc_timer.interval() == 0


def test_dense_small_images_use_overview_at_a_larger_scale(view, monkeypatch):
    from prism import canvas_overview
    monkeypatch.setattr(view.scene, '_item_count', canvas_overview.MIN_ITEMS)
    view.setTransform(QtGui.QTransform.fromScale(0.5, 0.5))
    monkeypatch.setattr(view.scene, 'items', Mock(return_value=[None]*canvas_overview.MIN_ITEMS))
    assert view._overview_renderer.eligible()
    monkeypatch.setattr(view.scene, 'items', Mock(return_value=[]))
    assert not view._overview_renderer.eligible()


def test_saved_overviews_restore_without_reading_originals(view, tmp_path, monkeypatch):
    from prism.fileio.sql import SQLiteIO
    item = fileio._load_image_item(str(source(tmp_path)))
    view.scene.addItem(item)
    archive = tmp_path/'overview.prism'
    writer = SQLiteIO(str(archive), view.scene, create_new=True)
    writer.write()
    writer._close_connection()
    original_bytes = item.original_bytes()
    view.scene.clear()
    reader = SQLiteIO(str(archive), view.scene, readonly=True)
    reader.read()
    view.scene.add_queued_items()
    reader._close_connection()
    loaded = next(view.scene.items_for_save())
    assert loaded._source_blob is None
    assert loaded._preview_pixmap is None
    assert not loaded.has_full_pixmap()
    with monkeypatch.context() as patch:
        patch.setattr(image_decode, 'request_display', Mock(side_effect=AssertionError('display decode')))
        overview = lod.overview_pixmap(loaded)
    assert overview.width() == 8
    assert overview.toImage().pixelColor(0, 0).alpha() == 128
    assert loaded.original_bytes() == original_bytes


def test_overview_metadata_never_persists_adjusted_pixels(view, tmp_path):
    item = fileio._load_image_item(str(source(tmp_path)))
    view.scene.addItem(item)
    assert lod.overview_data(item) is not None
    adjusted = QtGui.QPixmap(1600, 900)
    adjusted.fill(QtGui.QColor('blue'))
    item.set_preview_pixmap(adjusted)
    assert lod.overview_data(item) is None


@pytest.mark.parametrize('value', [None, [], [999999, 8, 'AA=='],
                                   [8, 8, 'AA=='], [8, 8, '@@@'],
                                   [8, 8, 'A'*10000], [True, 8, 'AA==']])
def test_invalid_optional_overviews_do_not_allocate_or_fail(qapp, value):
    item = PrismPixmapItem(QtGui.QImage())
    lod.restore_overview(item, value)
    assert getattr(item, '_overview_pixmap', None) is None


def test_cached_overview_continues_decoding_every_visible_image(view, qtbot, tmp_path, monkeypatch):
    from prism import canvas_overview, image_decode, lod
    monkeypatch.setattr(canvas_overview, 'MIN_ITEMS', 1)
    monkeypatch.setattr(canvas_overview, 'MAX_SCALE', 1)
    filename = source(tmp_path)
    # Pixel readiness must refill work even when Qt reports no geometry dirtiness.
    view.scene.changed.disconnect(view._overview_renderer.invalidate)
    items = []
    for index in range(24):
        item = fileio._load_image_item(str(filename))
        item._overview_pixmap = item._preview_pixmap.scaled(8, 4)
        item._overview_revision = getattr(item, '_display_source_revision', 0)
        item._preview_pixmap = None
        lod.cache().invalidate(item)
        item.color_group = 'blue'
        item.setPos((index % 6)*1800, (index // 6)*1100)
        view.scene.addItem(item)
        items.append(item)
    view.window().show()
    bounds = view.scene.itemsBoundingRect()
    view.setSceneRect(bounds)
    view.fitInView(bounds, QtCore.Qt.AspectRatioMode.KeepAspectRatio)
    renderer = view._overview_renderer
    renderer.image_for_view()
    decoder = QtWidgets.QApplication.instance()._prism_display_decoder
    assert len(decoder.requests) <= decoder.max_pending
    def ready(item):
        size = item._image_size
        pixels = max(size.width(), size.height())*item.scale()*view.get_scale()*view.viewport().devicePixelRatioF()
        return lod.base_preview_ready(item, pixels, view)
    qtbot.waitUntil(lambda: all(ready(item) for item in items), timeout=15000)
    for item in items:
        size = item._image_size
        pixels = max(size.width(), size.height())*item.scale()*view.get_scale()*view.viewport().devicePixelRatioF()
        needed = min(192, max(size.width(), size.height()), lod.level_for_pixels(pixels))
        assert max(item._preview_pixmap.width(), item._preview_pixmap.height()) >= needed
    assert len(decoder.requests) <= decoder.max_pending


def test_display_pixels_invalidate_overview_without_scene_geometry_signal(view):
    from prism import lod
    renderer = view._overview_renderer
    item = PrismPixmapItem(QtGui.QImage(100, 80, QtGui.QImage.Format.Format_RGB32))
    view.scene.addItem(item)
    rect = QtCore.QRectF(0, 0, 100, 80)
    renderer.frame = QtGui.QImage(100, 80, QtGui.QImage.Format.Format_RGB32), rect, 1
    renderer.dirty = QtCore.QRectF()
    lod.cache().changed.emit(item)
    assert renderer.dirty.isEmpty()
    assert renderer.display_dirty == rect
    renderer._publish_details()
    assert renderer.dirty == rect


def test_overview_publishes_visible_previews_together(view, monkeypatch):
    from prism import lod
    from prism.asset_records import ImageRecord
    renderer = view._overview_renderer
    items = [ImageRecord({'x': x, 'data': {'imageWidth': 100, 'imageHeight': 80}},
                         ('unused.prism', x+1)) for x in (0, 110)]
    for item in items:
        view.scene.addItem(item)
    view.setSceneRect(QtCore.QRectF(0, 0, 210, 80))
    view.centerOn(105, 40)
    image = QtGui.QImage(210, 80, QtGui.QImage.Format.Format_RGB32)
    rect = QtCore.QRectF(0, 0, 210, 80)
    renderer.frame = image, rect, 1
    renderer.frame_layout_revision = (view.scene._viewport_items.layout_revision, view.scene._bounds_revision)
    small, clear = QtGui.QPixmap(8, 6), QtGui.QPixmap(100, 80)
    pixels = {items[0]: clear, items[1]: small}
    from prism.item_lifecycle import logical_item
    monkeypatch.setattr(lod, 'display_pixmap', lambda item, *args: pixels[logical_item(item)])
    rendered = Mock()
    monkeypatch.setattr(renderer, '_render', rendered)
    renderer._display_ready(items[0])
    renderer._publish_details()
    renderer.invalidate([rect])
    renderer._refresh_dirty(image, rect)
    rendered.assert_not_called()
    assert not renderer.display_dirty.isEmpty()
    pixels[items[1]] = clear
    renderer._display_ready(items[1])
    renderer._publish_details()
    renderer._refresh_dirty(image, rect)
    assert rendered.call_count == 1
    assert renderer.display_dirty.isEmpty()


def test_geometry_changes_refresh_while_previews_are_pending(view, monkeypatch):
    from prism.asset_records import ImageRecord
    renderer = view._overview_renderer
    item = ImageRecord({'data': {'imageWidth': 100, 'imageHeight': 80}}, ('unused.prism', 1))
    view.scene.addItem(item)
    rect = QtCore.QRectF(0, 0, 300, 100)
    image = QtGui.QImage(300, 100, QtGui.QImage.Format.Format_RGB32)
    renderer.frame = image, rect, 1
    renderer.frame_layout_revision = (view.scene._viewport_items.layout_revision, view.scene._bounds_revision)
    renderer._display_ready(item)
    item.setPos(110, 0)
    renderer.invalidate([rect])
    rendered = Mock()
    monkeypatch.setattr(renderer, '_render', rendered)
    renderer._refresh_dirty(image, rect)
    rendered.assert_called_once()

