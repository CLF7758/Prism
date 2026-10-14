import threading
from unittest.mock import Mock, patch
from PyQt6 import QtCore, QtGui, QtWidgets
from prism import fileio, image_decode, lod
from prism.items import PrismPixmapItem, release_stale_pixmaps
from tests.test_image_decode import source
from tests.test_asset_browser import photo
from tests.test_search_controller import submit


def test_levels_and_budget_preserve_original_and_adjusted_pixels(view, qapp):
    image = QtGui.QImage(800, 400, QtGui.QImage.Format.Format_RGB32)
    image.fill(QtGui.QColor('red'))
    item = PrismPixmapItem(image)
    view.scene.addItem(item)
    assert lod.display_pixmap(item, 50).width() == 64
    assert lod.display_pixmap(item, 300).width() == 512
    assert QtWidgets.QGraphicsPixmapItem.pixmap(item).width() == 800
    item._preview_pixmap = QtGui.QPixmap.fromImage(image.scaled(192, 96))
    adjusted = QtGui.QPixmap(800, 400)
    adjusted.fill(QtGui.QColor('blue'))
    item.set_preview_pixmap(adjusted)
    assert lod.display_pixmap(item, 50).toImage().pixelColor(0, 0) == QtGui.QColor('blue')
    store = lod.DisplayCache(qapp)
    store.budget = 10000
    for index in range(3):
        store.store(item, index, QtGui.QPixmap(32, 32))
    assert store.bytes <= store.budget
    assert len(store.entries) == 2


def test_clear_thumbnail_survives_eviction_and_save_without_original_decode(view, tmp_path, monkeypatch):
    item = fileio._load_image_item(str(source(tmp_path)))
    view.scene.addItem(item)
    lod.cache().track_preview(item)
    metadata = item.get_extra_save_data()
    assert 'displayThumbnail' in metadata
    original_bytes = item.original_bytes()
    item._preview_pixmap = None
    lod.cache().invalidate(item)
    with monkeypatch.context() as context:
        requested = Mock()
        context.setattr(image_decode, 'request_display', requested)
        assert max(lod.display_pixmap(item, 40).size().width(), lod.display_pixmap(item, 40).size().height()) == 64
        requested.assert_not_called()
    assert item.original_bytes() == original_bytes
    item._display_thumbnail = [64, 64, 'A'*24001]
    lod.cache().invalidate(item)
    assert lod.thumbnail_pixmap(item) is None


def test_zoom_out_reuses_clear_higher_level_without_decode(view, monkeypatch):
    from prism.asset_records import ImageRecord
    item = ImageRecord({'data': {'imageWidth': 1600, 'imageHeight': 900}}, ('unused.prism', 1))
    view.scene.addItem(item)
    clear = QtGui.QPixmap(512, 288)
    clear.fill(QtGui.QColor('blue'))
    lod.cache().store(item, 512, clear, notify=False)
    requested = Mock()
    monkeypatch.setattr(image_decode, 'request_display', requested)
    assert lod.display_pixmap(item, 150).width() == 192
    assert lod.display_pixmap(item, 50).width() == 64
    requested.assert_not_called()


def test_old_64px_thumbnail_does_not_claim_192px_cache_level(view, monkeypatch):
    from prism.asset_records import ImageRecord
    item = ImageRecord({'data': {'imageWidth': 1600, 'imageHeight': 900}}, ('unused.prism', 1))
    view.scene.addItem(item)
    small = QtGui.QPixmap(64, 36)
    small.fill(QtGui.QColor('red'))
    lod.retain_thumbnail(item, small)
    requested = Mock()
    monkeypatch.setattr(image_decode, 'request_display', requested)
    assert lod.display_pixmap(item, 150).width() == 64
    assert lod.cache().get(item, 192) is None
    requested.assert_called_once()
    clear = QtGui.QPixmap(192, 108)
    clear.fill(QtGui.QColor('blue'))
    lod.retain_thumbnail(item, clear)
    lod.cache().invalidate(item)
    requested.reset_mock()
    assert lod.display_pixmap(item, 150).width() == 192
    requested.assert_not_called()


def test_material_layout_preserves_aspect_and_packs_without_overlaps(main_window):
    from prism.compact_layout import pack_sizes
    sizes = [(156, 88), (88, 156), (156, 156)]*50
    positions, bounds = pack_sizes(sizes)
    rectangles = [QtCore.QRectF(x, y, w, h) for (x, y), (w, h) in zip(positions, sizes)]
    assert all(not a.intersects(b) for i, a in enumerate(rectangles) for b in rectangles[i+1:])
    assert sum(w*h for w, h in sizes)/(bounds[0]*bounds[1]) > .65
    assert .5 < bounds[0]/bounds[1] < 2
    a = photo(main_window, 'default-canvas', 'Landscape')
    a.crop = QtCore.QRectF(0, 0, 200, 100)
    main_window.asset_browser.open('all')
    card = main_window.asset_browser.list.cards[0]
    assert card.boundingRect().width()/card.boundingRect().height() == 2


def test_3000_record_overview_and_zoom_out_use_thumbnails_without_original_decodes(view, monkeypatch):
    from prism.asset_records import ImageRecord
    image = QtGui.QPixmap(64, 36)
    image.fill(QtGui.QColor('#335577'))
    seed = PrismPixmapItem(image.toImage())
    lod.retain_thumbnail(seed, image)
    for index in range(3000):
        record = ImageRecord({'x': (index % 60)*1603, 'y': (index // 60)*903,
                              'data': {'imageWidth': 1600, 'imageHeight': 900,
                                       'displayThumbnail': seed._display_thumbnail}}, ('unused.prism', index+1))
        view.scene.addItem(record)
    view.fitInView(view.scene.itemsBoundingRect(), QtCore.Qt.AspectRatioMode.KeepAspectRatio)
    with monkeypatch.context() as context:
        requested = Mock()
        context.setattr(image_decode, 'request_display', requested)
        view._overview_renderer.image_for_view()
        view.scale(1/1.2, 1/1.2)
        view._overview_renderer.invalidate()
        view._overview_renderer.image_for_view()
        requested.assert_not_called()
    assert lod.cache().bytes <= lod.DISPLAY_BUDGET_BYTES


def test_medium_decode_is_off_thread_and_does_not_load_original(view, qtbot, tmp_path, monkeypatch):
    item = fileio._load_image_item(str(source(tmp_path)))
    view.scene.addItem(item)
    requests = []
    real = image_decode.decode
    def checked(*args, **kwargs):
        requests.append((kwargs.get('max_side'), QtCore.QThread.currentThread()))
        return real(*args, **kwargs)
    monkeypatch.setattr(image_decode, 'decode', checked)
    monkeypatch.setattr(item, 'original_bytes', Mock(side_effect=AssertionError('GUI read')))
    assert lod.display_pixmap(item, 300).width() <= 192
    qtbot.waitUntil(lambda: lod.cache().get(item, 512) is not None, timeout=5000)
    assert lod.display_pixmap(item, 300).width() == 512
    assert not item.has_full_pixmap()
    assert requests[0][0] == 512
    assert requests[0][1] != QtWidgets.QApplication.instance().thread()


def test_decode_queue_is_bounded_and_discards_offscreen_work(view, qtbot, tmp_path, monkeypatch):
    path = source(tmp_path)
    items = [fileio._load_image_item(str(path)) for _ in range(30)]
    for item in items:
        view.scene.addItem(item)
    entered, release = threading.Event(), threading.Event()
    real = image_decode.decode
    def delayed(*args, **kwargs):
        entered.set()
        assert release.wait(5)
        return real(*args, **kwargs)
    monkeypatch.setattr(image_decode, 'decode', delayed)
    class Owner(QtWidgets.QWidget):
        needed = True
        def isVisible(self): return True
        def needs_display(self, item): return self.needed
    owner = Owner()
    service = image_decode.DisplayDecoder(QtWidgets.QApplication.instance())
    try:
        for item in items:
            service.request(item, 512, owner)
        qtbot.waitUntil(entered.is_set)
        assert len(service.requests) <= service.max_pending
        assert items[0]._display_decode_request is not None
        assert items[-1]._display_decode_request is None
        assert owner in service.waiting_owners
        owner.needed = False
        release.set()
        qtbot.waitUntil(lambda: not service.requests, timeout=5000)
        assert all(lod.cache().get(item, 512) is None for item in items)
    finally:
        release.set()
        service.shutdown()


def test_recent_images_do_not_override_pixel_budget(view):
    image = QtGui.QImage(64, 64, QtGui.QImage.Format.Format_RGB32)
    image.fill(QtGui.QColor('red'))
    buffer = QtCore.QBuffer();buffer.open(QtCore.QIODevice.OpenModeFlag.WriteOnly)
    image.save(buffer, 'PNG')
    items = []
    for _ in range(4):
        item = PrismPixmapItem(image)
        item.set_source_blob(bytes(buffer.data()))
        item._preview_pixmap = QtGui.QPixmap.fromImage(image.scaled(32, 32))
        item._last_painted = 99999
        view.scene.addItem(item)
        items.append(item)
    assert release_stale_pixmaps(view.scene, budget_pixels=4096, keep_recent=24) >= 3
    assert sum(i.has_full_pixmap() for i in items) <= 1


def test_search_paint_resolver_scans_once_and_survives_save_id_change(main_window):
    items = [photo(main_window, 'default-canvas', '素材'+str(i)) for i in range(100)]
    controller = submit(main_window, '素材')
    from prism.search_controller import searchable_items
    controller._invalidate_asset_index()
    with patch('prism.search_controller.searchable_items', wraps=searchable_items) as scanned:
        for result in controller.results:
            controller.resolve_asset(result)
        assert scanned.call_count == 1
    items[0].save_id = 1234
    controller._invalidate_asset_index()
    assert controller.resolve_asset({'item_id': '1234'}) is items[0]


def test_material_display_only_builds_one_qt_layer(main_window):
    from prism.widgets.asset_browser import AssetModel
    canvas = main_window.asset_browser.list
    model = AssetModel()
    canvas.setModel(model)
    item = photo(main_window, 'default-canvas', '素材')
    model.replace([item]*20000)
    assert len(canvas.cards) == 20000
    assert len(canvas.scene().items()) == 1
    viewport = canvas.mapToScene(canvas.viewport().rect()).boundingRect()
    assert len(canvas.visible_cards(viewport)) < 300
    canvas.select_all()
    assert len(canvas.selectedIndexes()) == 20000
