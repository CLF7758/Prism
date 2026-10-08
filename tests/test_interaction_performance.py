"""Interaction regressions: prevent disk I/O and bulk analysis during gestures."""
from unittest.mock import Mock

from PyQt6 import QtCore, QtGui, QtWidgets

from prism import fileio
from prism.items import PrismPixmapItem
from prism.widgets.color_filter import ColorFilterBar
from prism.widgets.detail_panel import _HistogramWidget


def preview_item(tmp_path, view):
    image = QtGui.QImage(1600, 900, QtGui.QImage.Format.Format_RGB32)
    image.fill(QtGui.QColor('red'))
    path = tmp_path / 'source.jpg'
    assert image.save(str(path))
    item = fileio._load_image_item(str(path))
    view.scene.addItem(item)
    return item


def test_paint_detail_decision_never_checks_disk(view, tmp_path, monkeypatch):
    item = preview_item(tmp_path, view)
    canvas = QtGui.QImage(32, 32, QtGui.QImage.Format.Format_RGB32)
    painter = QtGui.QPainter(canvas)
    try:
        monkeypatch.setattr(item, 'has_original_source', Mock(side_effect=AssertionError('disk lookup')))
        painter.scale(0.01, 0.01)
        assert item._preview_is_detailed_enough(painter, item._preview_pixmap)
        painter.resetTransform()
        assert not item._preview_is_detailed_enough(painter, item._preview_pixmap)
    finally:
        painter.end()


def test_selection_inspector_uses_preview_without_decoding(view, tmp_path, monkeypatch):
    item = preview_item(tmp_path, view)
    with monkeypatch.context() as patch:
        patch.setattr(item, '_decode_source_blob', Mock(side_effect=AssertionError('full decode')))
        item.setSelected(True)
        panel = view._detail_panel
        panel._on_selection_changed()
        assert panel._dims_label.text() == '1600 × 900'
        assert panel._hist_widget._stats['total'] > 0
        assert not item.has_full_pixmap()


def test_notes_refresh_preserves_cursor_and_cached_statistics(view, tmp_path, monkeypatch):
    item = preview_item(tmp_path, view)
    item.notes = 'abcdef'
    item.setSelected(True)
    panel = view._detail_panel
    panel._on_selection_changed()
    cursor = panel._notes_edit.textCursor()
    cursor.setPosition(2)
    panel._notes_edit.setTextCursor(cursor)
    monkeypatch.setattr(panel._hist_widget, 'update_from_pixmap', Mock(side_effect=AssertionError('statistics recomputed')))
    monkeypatch.setattr(panel, '_build_color_distribution', Mock(side_effect=AssertionError('colours recomputed')))
    panel._refresh_item(item)
    assert panel._notes_edit.textCursor().position() == 2


def test_histogram_ignores_transparent_pixels_and_keeps_luminance(qapp):
    image = QtGui.QImage(4, 1, QtGui.QImage.Format.Format_ARGB32)
    for index, color in enumerate(('red', 'green', 'blue', 'transparent')):
        image.setPixelColor(index, 0, QtGui.QColor(color))
    histogram = _HistogramWidget()
    histogram.update_from_pixmap(QtGui.QPixmap.fromImage(image))
    assert histogram._stats['total'] == 3
    # QColor('green') is RGB(0,128,0); red/green/blue luminosities are 76/75/29.
    assert [histogram._bins[0][level] for level in (76, 75, 29)] == [1, 1, 1]
    assert histogram._stats['mean'] == 60
    histogram.deleteLater()


def test_interaction_defers_detail_then_restores_quality(view, tmp_path, qtbot):
    item = preview_item(tmp_path, view)
    canvas = QtGui.QImage(32, 32, QtGui.QImage.Format.Format_RGB32)
    painter = QtGui.QPainter(canvas)
    try:
        view._begin_interaction()
        assert item._preview_is_detailed_enough(painter, item._preview_pixmap)
        qtbot.waitUntil(lambda: not view.scene._interaction_in_progress, timeout=1000)
        assert not item._preview_is_detailed_enough(painter, item._preview_pixmap)
        view._import_in_progress = True
        assert item._preview_is_detailed_enough(painter, item._preview_pixmap)
        view._import_in_progress = False
    finally:
        painter.end()


def test_colour_recount_is_deferred_bounded_and_updates_filter(view, tmp_path, monkeypatch):
    items = [preview_item(tmp_path, view) for _ in range(12)]
    bar = ColorFilterBar(view)
    try:
        bar.recount()
        assert all(item.color_group is None or not item.color_group for item in items)
        assert bar.all_chip._count == 12
        bar.apply_filter('red')
        bar._analyze_pending()
        assert len(bar._analysis_pending) >= 4
        while bar._analysis_pending:
            bar._analyze_pending()
        assert bar.chips['red']._count == 12
        assert all(item.opacity() == 1.0 for item in items)
        # Unloaded items should not materialize originals just to count colours.
        item = items[0]
        item.color_group = None
        item._preview_pixmap = None
        QtWidgets.QGraphicsPixmapItem.setPixmap(item, QtGui.QPixmap())
        with monkeypatch.context() as patch:
            patch.setattr(item, 'original_bytes', Mock(side_effect=AssertionError('original read')))
            bar.recount()
            assert id(item) not in bar._analysis_pending
    finally:
        bar._analysis_timer.stop()
        bar.deleteLater()


def test_colour_analysis_waits_for_gesture_and_handles_removed_items(view, tmp_path):
    item = preview_item(tmp_path, view)
    bar = ColorFilterBar(view)
    try:
        bar.recount()
        view._begin_interaction()
        bar._analyze_pending()
        assert not item.color_group
        view._end_interaction()
        view.scene.removeItem(item)
        bar._analyze_pending()
        assert not bar._analysis_pending
    finally:
        bar._analysis_timer.stop()
        bar.deleteLater()


def test_content_bounds_reuse_and_invalidate_after_move(view, tmp_path, qapp, monkeypatch):
    item = preview_item(tmp_path, view)
    qapp.processEvents()
    original = view.scene.itemsBoundingRect
    counter = Mock(wraps=original)
    monkeypatch.setattr(view.scene, 'itemsBoundingRect', counter)
    view._cached_scene_bounds = None
    first = QtCore.QRectF(view._content_bounds())
    assert view._content_bounds() == first
    assert counter.call_count == 1
    item.moveBy(200, 300)
    qapp.processEvents()
    assert view._content_bounds() != first
    view.scene.removeItem(item)
    assert view._cached_scene_bounds is None


def test_pending_colour_analysis_does_not_decode_discarded_preview(view, tmp_path, monkeypatch):
    item = preview_item(tmp_path, view)
    bar = ColorFilterBar(view)
    try:
        bar.recount()
        item._preview_pixmap = None
        with monkeypatch.context() as patch:
            patch.setattr(item, 'original_bytes', Mock(side_effect=AssertionError('original read')))
            bar._analyze_pending()
            assert not item.color_group
    finally:
        bar._analysis_timer.stop()
        bar.deleteLater()
