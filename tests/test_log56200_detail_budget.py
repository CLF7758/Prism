"""Bounded detail scheduling keeps row IDs, not viewport-wide facades."""
from array import array
from types import SimpleNamespace
from unittest.mock import Mock
import pytest
from PyQt6 import QtCore, QtGui, QtWidgets
from prism.widgets.asset_canvas import _CardRows
from tests.test_log56008_selection_geometry import canvas


def dense(host, model, n, unique=False):
    size, crop = QtCore.QSizeF(256, 128), QtCore.QRectF(0, 0, 256, 128)
    def item(i):
        return SimpleNamespace(TYPE='pixmap', save_id=i, _canvas_id='a',
                               _image_size=size, crop=crop)
    model.items = [item(i) for i in range(n)] if unique else [item(0)]*n
    host.cards = _CardRows(host, n)
    for name, value in [('x', 20), ('y', 20), ('w', 156), ('h', 78)]:
        setattr(host._geometry, name, array('d', [value])*n)
    host._spatial = {(0, 0): set(range(n)), (1, 0): set(range(n))}
    host._moved_rows = set(range(n))
    host._overview_pixel_span = 1000
    host.setTransform(QtGui.QTransform.fromScale(2, 2))
    host.centerOn(QtCore.QPointF(98, 59))


def test_detail_scheduler_does_not_materialize_dense_100k_viewport(canvas, monkeypatch):
    from prism import lod
    host, model = canvas
    dense(host, model, 100000)
    monkeypatch.setattr(host, 'visible_cards', Mock(side_effect=AssertionError('full facade list')))
    pixmap = QtGui.QPixmap(512, 512)
    monkeypatch.setattr(lod, 'display_pixmap', lambda *args: pixmap)
    assert host.queue_visible_details() is False
    assert all(card is None for card in host.cards.records)
    assert not hasattr(host, '_details_cards')
    assert len(host._details_state['rows']) <= 256
    assert host._detail_quality['total'] is None
    host._display_timer.stop()


def test_direct_renderer_threshold_stops_at_128_without_facades(canvas, monkeypatch):
    host, model = canvas
    dense(host, model, 100000)
    lookup = Mock(wraps=host._card_rect)
    monkeypatch.setattr(host, '_card_rect', lookup)
    rect = QtCore.QRectF(20, 20, 156, 78)
    assert not host._few_visible_rows(rect)
    assert lookup.call_count == 128
    assert not host._few_visible_rows(rect)
    assert lookup.call_count == 128
    assert all(card is None for card in host.cards.records)


def test_ambiguous_sparse_hit_fanout_uses_exact_tiled_backend(canvas, monkeypatch):
    host, model = canvas
    dense(host, model, 100000)
    lookup = Mock(wraps=host._card_rect)
    monkeypatch.setattr(host, '_card_rect', lookup)
    assert not host._few_visible_rows(QtCore.QRectF(0, 0, 1, 1))
    assert lookup.call_count <= 512
    assert all(card is None for card in host.cards.records)


def test_tile_iterator_creates_only_rows_consumed_by_a_slice(canvas, monkeypatch):
    host, model = canvas
    dense(host, model, 100000)
    monkeypatch.setattr(host, 'visible_cards', Mock(side_effect=AssertionError('eager tile list')))
    operations = iter(host._tile_draw_operations(QtCore.QRectF(20, 20, 156, 78)))
    painted = []
    for _ in range(32):
        card, image = next(operations)
        if card is not None:
            painted.append(card.row)
    assert len(painted) == 32 and len(set(painted)) == 32
    assert host.cards.resident_count() == 32


def test_failed_first_image_does_not_starve_other_visible_rows(canvas, monkeypatch):
    from prism import lod
    host, model = canvas
    dense(host, model, 1000, unique=True)
    model.items[0]._display_decode_failed = True
    requested = set()
    pixmap = QtGui.QPixmap(512, 512)
    def display(item, *args):
        requested.add(item.save_id)
        return None if item.save_id == 0 else pixmap
    monkeypatch.setattr(lod, 'display_pixmap', display)
    for _ in range(100):
        host.queue_visible_details()
        host._display_timer.stop()
        if host._details_state['scan_done']:
            break
    assert host._details_state['scan_done']
    assert requested == set(range(1000))
    assert not host.queue_visible_details()  # failure is never reported as ready
    host._display_timer.stop()


def test_ready_viewport_finishes_and_repeated_checks_allocate_no_facades(canvas, monkeypatch):
    from prism import lod
    host, model = canvas
    dense(host, model, 1000, unique=True)
    pixmap = QtGui.QPixmap(512, 512)
    monkeypatch.setattr(lod, 'display_pixmap', lambda *args: pixmap)
    for _ in range(100):
        ready = host.queue_visible_details()
        host._display_timer.stop()
        if ready:
            break
    assert ready
    assert host.queue_visible_details()
    assert all(card is None for card in host.cards.records)
    host._display_timer.stop()
