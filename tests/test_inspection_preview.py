"""Temporary inspection adjustments must end when the selection ends."""
from unittest.mock import Mock

import pytest
from PyQt6 import QtCore, QtGui

from prism import fileio, lod
from prism.asset_records import ImageRecord
from prism.item_lifecycle import native_pixmap


def image_item(view, tmp_path, name='a.png', virtual=False):
    image = QtGui.QImage(160, 90, QtGui.QImage.Format.Format_RGB32)
    image.fill(QtGui.QColor(50, 70, 90))
    path = tmp_path/name
    assert image.save(str(path))
    item = fileio._load_image_item(str(path))
    if virtual:
        record = ImageRecord({'data': {'filename': str(path), 'imageWidth': 160, 'imageHeight': 90}}, None)
        record._source_path = str(path)
        record._preview_pixmap = item._preview_pixmap
        item = record
    view.scene.addItem(item)
    item.setSelected(True)
    view._detail_panel._on_selection_changed()
    return item


@pytest.mark.parametrize('virtual', [False, True])
@pytest.mark.parametrize('leave', ['other', 'empty', 'collection'])
def test_leaving_image_restores_immediately(view, tmp_path, monkeypatch, virtual, leave):
    item = image_item(view, tmp_path, virtual=virtual)
    panel = view._detail_panel
    original = item.original_bytes()
    size, crop = QtCore.QSize(item._image_size), QtCore.QRectF(item.crop)
    panel._exposure_slider.setValue(10)
    panel._false_color_box.setChecked(True)
    assert item._preview_adjusted
    # Restoring cannot read the original again or wait for a decode.
    monkeypatch.setattr(item, 'pixmap', Mock(side_effect=AssertionError('synchronous restore decode')))
    if leave == 'other':
        view.scene.clearSelection()
        image_item(view, tmp_path, name='b.png')
    elif leave == 'empty':
        view.scene.clearSelection()
        panel._on_selection_changed()
    else:
        panel.show_collection('Other canvas', 0)
    assert not item._preview_adjusted
    assert getattr(item, '_display_source_pixmap', None) is None
    assert item.original_bytes() == original
    assert item._image_size == size and item.crop == crop
    assert not native_pixmap(item).isNull() or item._preview_pixmap is not None


def test_single_image_histogram_is_visible_and_clears(view, tmp_path):
    image_item(view, tmp_path)
    panel = view._detail_panel
    assert not panel._hist_widget.isHidden()
    assert not panel._lbl_hist.isHidden()
    assert panel._hist_widget._stats['total'] > 0
    view.scene.clearSelection()
    panel._on_selection_changed()
    assert panel._hist_widget.isHidden()
    assert panel._hist_widget._stats is None


def test_reset_preserves_cached_sharp_pixels_and_statistics(view, tmp_path):
    item = image_item(view, tmp_path)
    panel = view._detail_panel
    sharp = QtGui.QPixmap(160, 90)
    sharp.fill(QtGui.QColor(50, 70, 90))
    lod.cache().store(item, 192, sharp, notify=False)
    before = panel._hist_widget._stats['mean']
    panel._exposure_slider.setValue(10)
    assert panel._hist_widget._stats['mean'] > before
    panel._reset_hdr_controls()
    assert not item._preview_adjusted
    assert panel._hist_widget._stats['mean'] == before
    assert lod.cache().get(item, 192).cacheKey() == sharp.cacheKey()


def test_hdr_adjustment_also_restores_without_redecoding(view, tmp_path, monkeypatch):
    item = image_item(view, tmp_path)
    panel = view._detail_panel
    baseline = native_pixmap(item)
    monkeypatch.setattr(panel, '_hdr_source_available', lambda item: True)
    load = Mock(return_value=(QtGui.QImage(160, 90, QtGui.QImage.Format.Format_RGB32), None))
    monkeypatch.setattr('prism.widgets.detail_panel.load_image', load)
    panel._exposure_slider.setValue(10)
    assert load.call_count == 1
    view.scene.clearSelection()
    panel._on_selection_changed()
    assert load.call_count == 1
    assert not item._preview_adjusted
    assert native_pixmap(item).cacheKey() == baseline.cacheKey()


def test_metadata_refresh_does_not_end_active_inspection(view, tmp_path):
    item = image_item(view, tmp_path)
    panel = view._detail_panel
    panel._exposure_slider.setValue(10)
    session = panel._inspection_session
    panel._refresh_item(item)
    assert panel._inspection_session is session and item._preview_adjusted
    view.scene.clearSelection()
    panel._on_selection_changed()
    assert panel._inspection_session is None


def test_false_colour_does_not_apply_exposure_twice(view, tmp_path):
    from prism.fileio.image import adjust_display_image, false_color_preview
    item = image_item(view, tmp_path)
    panel = view._detail_panel
    source = item.pixmap().toImage()
    expected = false_color_preview(adjust_display_image(source, 1.0))
    panel._exposure_slider.setValue(10)
    panel._false_color_box.setChecked(True)
    assert item.pixmap().toImage().convertToFormat(expected.format()) == expected


def test_direct_selection_replacement_without_empty_event_restores(view, tmp_path):
    item = image_item(view, tmp_path)
    panel = view._detail_panel
    other = fileio._load_image_item(str(tmp_path/'a.png'))
    view.scene.addItem(other)
    panel._exposure_slider.setValue(10)
    assert item._preview_adjusted
    # Public/search selections can replace their inspector item directly.
    panel._refresh_item(other)
    assert not item._preview_adjusted
    assert panel._item is other and panel._exposure_slider.value() == 0
