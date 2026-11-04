"""Visible metadata must not turn sparse rows into whole-library traversal."""
from types import SimpleNamespace
import weakref

from PyQt6 import QtCore, QtWidgets

from prism.catalog_browser import CatalogDisplayReader
from prism.widgets.asset_browser import AssetModel


class Signals(QtCore.QObject):
    changed = QtCore.pyqtSignal(object)


class SparseItems(list):
    def __init__(self, size):
        super().__init__([None]*size)
        self.visited = []

    def __iter__(self):
        raise AssertionError('Whole-library metadata traversal')

    def __getitem__(self, row):
        self.visited.append(row)
        return super().__getitem__(row)


class Item:
    pass


class Owner(QtWidgets.QWidget):
    def __init__(self):
        super().__init__()
        self.data = AssetModel(self)
        self.batches = []
        self.wakes = 0

    def model(self):
        return self.data

    def needs_display(self, item):
        return True

    def catalog_rows_changed(self, rows):
        self.batches.append(tuple(rows))
        for row in rows:
            self.data.items[row]  # Represents the canvas's exact-row processing.

    def wake_display_queue(self):
        self.wakes += 1


def fixture(qapp, monkeypatch):
    owner = Owner()
    owner.show()
    owner.data.items = SparseItems(100000)
    owner._item_rows = {}
    requests, rows = [], []
    for index in (1, 99998):
        item = Item()
        item.save_id = index
        item._catalog_display_ready = item._catalog_display_failed = False
        owner.data.items[index] = item
        owner._item_rows[id(item)] = index
        requests.append((item, weakref.ref(owner)))
        rows.append(dict(id=index, overview=None, thumbnail=None,
                         crop=[0,0,12,24], grayscale=False, flip=1, opacity=1))
    signals = Signals()
    monkeypatch.setattr('prism.lod.cache', lambda: signals)
    return owner, CatalogDisplayReader(None, owner), requests, rows, signals


def test_ready_notifies_exact_sparse_rows_once_without_rebuilding_identity_map(qapp, monkeypatch):
    owner, reader, requests, rows, signals = fixture(qapp, monkeypatch)
    reader._ready((requests, rows, None))
    assert owner.batches == [(1, 99998)]
    assert owner.data.items.visited == [1, 99998]
    assert owner._catalog_geometry_rows is owner._item_rows
    assert all(item._catalog_display_ready for item, _ in requests)
    owner.close()


def test_legacy_notification_splits_only_real_contiguous_ranges(qapp):
    model = AssetModel()
    model.items = [None]*100000
    ranges = []
    model.dataChanged.connect(lambda first, last: ranges.append((first.row(), last.row())))
    CatalogDisplayReader._notify_sparse_rows(SimpleNamespace(), model, (99998, 1, 2, 2))
    assert ranges == [(1, 2), (99998, 99998)]


def test_model_replaced_during_preview_callback_skips_stale_row_notification(qapp, monkeypatch):
    owner, reader, requests, rows, signals = fixture(qapp, monkeypatch)
    signals.changed.connect(lambda _: setattr(owner.data, 'items', []))
    reader._ready((requests, rows, None))
    assert owner.batches == []
    owner.close()


def test_failed_rows_do_not_create_geometry_notifications(qapp, monkeypatch):
    owner, reader, requests, rows, signals = fixture(qapp, monkeypatch)
    reader._ready((requests, (), 'query failed'))
    assert owner.batches == []
    assert all(item._catalog_display_failed for item, _ in requests)
    owner.close()
