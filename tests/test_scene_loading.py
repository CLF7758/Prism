"""Large queues yield to the UI without losing assets or finalization."""
from unittest.mock import Mock

from PyQt6 import QtCore, QtGui, QtWidgets

from prism import image_decode, lod
from prism.items import PrismPixmapItem


def enqueue(scene, count):
    for index in range(count):
        scene.add_item_later({'type': 'text', 'data': {'text': str(index)}})


def test_scene_batches_preserve_order_selection_and_completion(view, qtbot):
    enqueue(view.scene, 401)
    done = Mock()
    scheduler = view._load_scheduler
    scheduler.finish(done)
    assert 0 < view.scene._item_count <= 128
    assert not done.called
    pulses = []
    QtCore.QTimer.singleShot(0, lambda: pulses.append(view.scene._item_count))
    qtbot.waitUntil(lambda: done.called)
    done.assert_called_once()
    assert pulses and pulses[0] < 401
    assert sorted(int(item.toPlainText()) for item in view.scene.items_for_save()) == list(range(401))
    assert view.scene.items_to_add.empty()


def test_explicit_synchronous_drain_remains_compatible(view):
    enqueue(view.scene, 180)
    view.scene.add_queued_items(max_items=3)
    assert view.scene._item_count == 3
    view.scene.add_queued_items()
    assert view.scene._item_count == 180


def test_cancel_discards_pending_finalization(view, qtbot):
    enqueue(view.scene, 300)
    done = Mock()
    view._load_scheduler.finish(done)
    view.clear_scene()
    qtbot.wait(50)
    done.assert_not_called()
    assert view.scene._item_count == 0
    assert view.scene.items_to_add.empty()


def test_partial_loading_defers_whole_scene_bounds_scan(view):
    enqueue(view.scene, 300)
    view.scene.add_queued_items(max_items=2)
    view._cached_scene_bounds = None
    view.on_scene_changed([QtCore.QRectF(0, 0, 1, 1)])
    assert view._recalc_timer.interval() == 120


def test_folder_import_waits_for_last_batch_before_cleanup(view, qtbot, monkeypatch):
    from types import SimpleNamespace
    from prism.actions import import_workflow
    enqueue(view.scene, 301)
    outcome = SimpleNamespace(errors=[], asset_targets={})
    finished, cleanup = Mock(), Mock()
    monkeypatch.setattr(import_workflow, '_finish_import', finished)
    monkeypatch.setattr(import_workflow, '_drop_staging', cleanup)
    monkeypatch.setattr(view, '_arrange_imported_items', Mock())
    view.undo_stack.beginMacro('import')
    view._import_in_progress = True
    import_workflow._on_assets_loaded(view, object(), outcome, set(), [], 'staging')
    cleanup.assert_not_called()
    assert view._import_in_progress
    qtbot.waitUntil(lambda: cleanup.called)
    assert view.scene._item_count == 301
    assert not view._import_in_progress
    finished.assert_called_once()
    cleanup.assert_called_once_with('staging', view)


def test_ready_images_are_installed_together_with_single_cache_notification(view, qtbot):
    import weakref
    app = QtWidgets.QApplication.instance()
    decoder = image_decode.DisplayDecoder(app)
    changed = Mock()
    store = lod.cache()
    store.changed.connect(changed)
    image = QtGui.QImage(512, 256, QtGui.QImage.Format.Format_RGB32)
    image.fill(QtGui.QColor('red'))
    items = []
    try:
        for _ in range(8):
            item = PrismPixmapItem(QtGui.QImage())
            item._source_path = 'unused-source.png'
            item._image_size = image.size()
            item.reset_crop()
            view.scene.addItem(item)
            token = (weakref.ref(item), weakref.ref(view.scene),
                     image_decode.source_snapshot(item), 512, None)
            item._display_decode_request = token
            decoder._queue_complete(token, (image, image.size()))
            items.append(item)
        assert all(store.get(item, 512) is None for item in items)
        qtbot.waitUntil(lambda: all(store.get(item, 512) is not None for item in items))
        assert changed.call_count == 8
        assert not decoder.ready
    finally:
        store.changed.disconnect(changed)
        decoder.shutdown()


def test_retracking_same_preview_does_not_repeat_notifications(view):
    image = QtGui.QImage(50, 30, QtGui.QImage.Format.Format_RGB32)
    image.fill(QtGui.QColor('blue'))
    item = PrismPixmapItem(image)
    item._source_path = 'unused.png'
    view.scene.addItem(item)
    store = lod.cache()
    preview = item._preview_pixmap
    if preview is None:
        item._preview_pixmap = QtGui.QPixmap.fromImage(image)
    store.track_preview(item)
    changed = Mock()
    store.changed.connect(changed)
    try:
        for _ in range(20):
            store.track_preview(item)
        changed.assert_not_called()
    finally:
        store.changed.disconnect(changed)


def test_nearer_visible_image_replaces_only_unstarted_decode(view, monkeypatch):
    from concurrent.futures import Future
    decoder = image_decode.DisplayDecoder(QtWidgets.QApplication.instance())
    monkeypatch.setattr(decoder.pool, 'submit', lambda *a, **kw: Future())
    class Owner(QtWidgets.QWidget):
        priorities = {}
        def isVisible(self): return True
        def needs_display(self, item): return True
        def display_priority(self, item): return self.priorities[item]
    owner = Owner()
    items = []
    try:
        for distance in range(10, 19):
            item = PrismPixmapItem(QtGui.QImage())
            view.scene.addItem(item)
            owner.priorities[item] = distance if distance < 18 else 0
            decoder.request(item, 192, owner)
            items.append(item)
        assert len(decoder.requests) == decoder.max_pending
        assert items[-1]._display_decode_request is not None
        assert items[-2]._display_decode_request is None
        assert owner in decoder.waiting_owners
    finally:
        decoder.shutdown()
    assert all(item._display_decode_request is None for item in items)


def test_unselected_batches_do_not_repeat_selection_recounts(view):
    enqueue(view.scene, 300)
    selected, assets = Mock(), Mock()
    view.scene.selectionChanged.connect(selected)
    view.scene.assets_changed.connect(assets)
    view.scene.add_queued_items(max_items=128)
    selected.assert_not_called()
    assets.assert_called_once()
    view.scene.add_queued_items()
    selected.assert_called_once()
    assert assets.call_count == 2
