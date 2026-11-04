from types import SimpleNamespace
from concurrent.futures import Future
from unittest.mock import patch

from PyQt6 import QtGui, QtWidgets

from prism import image_pyramid, image_decode
from prism.source_fingerprint import file_fingerprint
from tests.test_partial_save import project  # noqa: F401


def test_only_proven_save_preserves_untouched_archive_row_keys(tmp_path):
    path = tmp_path / 'isolated.prism'
    path.write_bytes(b'initial')
    original = image_pyramid.source_key(None, None, (str(path), 1))
    changed = image_pyramid.source_key(None, None, (str(path), 2))
    before = file_fingerprint(path)
    path.write_bytes(b'controlled metadata save')
    receipt = SimpleNamespace(source_before=before, source_after=file_fingerprint(path),
                              catalog_item_ids=(2,))
    assert image_pyramid.acknowledge_archive_save(path, receipt)
    assert image_pyramid.source_key(None, None, (str(path), 1)) == original
    assert image_pyramid.source_key(None, None, (str(path), 2)) != changed
    path.write_bytes(b'unknown external replacement')
    assert not image_pyramid.acknowledge_archive_save(path, receipt)
    assert image_pyramid.source_key(None, None, (str(path), 1)) != original


def test_missing_save_proof_never_reuses_archive_version(tmp_path):
    path = tmp_path / 'isolated.prism'
    path.write_bytes(b'initial')
    old = image_pyramid.source_key(None, None, (str(path), 1))
    path.write_bytes(b'unconfirmed save')
    assert not image_pyramid.acknowledge_archive_save(path, SimpleNamespace())
    assert image_pyramid.source_key(None, None, (str(path), 1)) != old


def test_real_partial_receipt_reuses_unmodified_pyramid(project, tmp_path, qapp, monkeypatch):
    from PyQt6 import QtCore
    from prism import thumbnail_cache
    from tests.test_catalog_incremental_receipts import save
    store = thumbnail_cache.ThumbnailCache(str(tmp_path / 'pyramid'))
    monkeypatch.setattr(image_pyramid, 'cache', lambda: store)
    decoded = []

    def decode(*args, **kwargs):
        decoded.append(args)
        image = QtGui.QImage(64, 32, QtGui.QImage.Format.Format_RGB32)
        image.fill(QtGui.QColor('red'))
        return image, QtCore.QSize(100, 50)

    monkeypatch.setattr(image_decode, 'decode', decode)
    try:
        image_pyramid.decode_display(archive=(str(project), 1), max_side=64)
        receipt = save(project, 2, tags=['metadata edit'])
        assert image_pyramid.acknowledge_archive_save(project, receipt)
        result = image_pyramid.decode_display(archive=(str(project), 1), max_side=64)
        assert len(decoded) == 1
        assert result[1] == QtCore.QSize(100, 50)
    finally:
        store.close()


def test_real_request_entry_can_preempt_and_counts_deferred(view, monkeypatch):
    from prism.items import PrismPixmapItem
    app = QtWidgets.QApplication.instance()
    decoder = image_decode.DisplayDecoder(app)
    monkeypatch.setattr(app, '_prism_display_decoder', decoder, raising=False)
    monkeypatch.setattr(decoder.pool, 'submit', lambda *a, **kw: Future())

    class Owner(QtWidgets.QWidget):
        def isVisible(self): return True
        def needs_display(self, item): return True
        def display_priority(self, item): return item.priority

    owner = Owner()
    items = []
    try:
        for priority in range(10, 18):
            item = PrismPixmapItem(QtGui.QImage())
            view.scene.addItem(item)
            item.priority = priority
            items.append(item)
            image_decode.request_display(item, 192, owner)
        nearest = PrismPixmapItem(QtGui.QImage())
        nearest.priority = 0
        view.scene.addItem(nearest)
        image_decode.request_display(nearest, 192, owner)
        assert nearest._display_decode_request is not None
        assert items[-1]._display_decode_request is None
        assert len(decoder.requests) == decoder.max_pending
        decoder._priority_round[owner] = image_decode.time.monotonic()
        later = PrismPixmapItem(QtGui.QImage())
        later.priority = 1
        view.scene.addItem(later)
        image_decode.request_display(later, 192, owner)
        assert decoder.deferred_full == 1
        assert later._display_decode_request is None
        assert owner in decoder.waiting_owners
    finally:
        decoder.shutdown()


def test_unchanged_setting_does_not_dirty_ini_but_preserves_callback(settings):
    from prism.config.settings import PrismSettings
    from PyQt6 import QtCore
    settings.setValue('test/same', 'value')
    callback = []
    with patch.dict(PrismSettings.FIELDS, {'test/same': {'post_save_callback': callback.append}}):
        with patch.object(QtCore.QSettings, 'setValue', side_effect=AssertionError('redundant write')):
            settings.setValue('test/same', 'value')
    assert callback == ['value']
