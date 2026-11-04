"""Temporary inspection adjustments must end when the selection ends."""
from unittest.mock import Mock

import pytest
from PyQt6 import QtCore, QtGui

from prism import fileio, lod
from prism.asset_records import ImageRecord
from prism.item_lifecycle import native_pixmap


def wait_preview(qtbot, panel):
    qtbot.waitUntil(lambda: panel._hdr_running is None and panel._hdr_pending is None
                   and not panel._hdr_updates.isActive(), timeout=5000)


def test_slow_adjustment_keeps_ui_alive_and_merges_latest_parameters(view, qtbot, tmp_path, monkeypatch):
    import threading
    from prism import inspection_preview, image_decode
    item = image_item(view, tmp_path)
    panel = view._detail_panel
    original = item.original_bytes()
    entered, release = threading.Event(), threading.Event()
    calls, decoded, ticks = [], [], []
    render = inspection_preview.render_inspection
    decode = image_decode.decode
    def slow(snapshot, options, **kwargs):
        calls.append((threading.get_ident(),options['exposure']))
        if len(calls)==1:
            entered.set()
            assert release.wait(5)
        return render(snapshot, options, **kwargs)
    def counted(*args, **kwargs):
        decoded.append(1)
        return decode(*args, **kwargs)
    monkeypatch.setattr(inspection_preview,'render_inspection',slow)
    monkeypatch.setattr(image_decode,'decode',counted)
    try:
        panel._exposure_slider.setValue(10)
        qtbot.waitUntil(entered.is_set, timeout=5000)
        QtCore.QTimer.singleShot(0,lambda:ticks.append(1))
        panel._exposure_slider.setValue(20)
        panel._exposure_slider.setValue(30)
        qtbot.waitUntil(lambda: bool(ticks))
    finally:
        release.set()
    wait_preview(qtbot,panel)
    assert [value for _,value in calls] == [1.,3.]
    assert all(thread != threading.get_ident() for thread,_ in calls)
    assert len(decoded)==1
    from prism.fileio.image import adjust_display_image
    expected=adjust_display_image(QtGui.QImage.fromData(original),3.)
    assert native_pixmap(item).toImage().convertToFormat(expected.format()) == expected
    assert item.original_bytes()==original


def test_late_adjustment_cannot_colour_a_new_selection(view, qtbot, tmp_path, monkeypatch):
    import threading
    from prism import inspection_preview
    first = image_item(view,tmp_path)
    panel = view._detail_panel
    entered,release=threading.Event(),threading.Event()
    render=inspection_preview.render_inspection
    def slow(*args,**kwargs):
        entered.set()
        assert release.wait(5)
        return render(*args,**kwargs)
    monkeypatch.setattr(inspection_preview,'render_inspection',slow)
    try:
        panel._exposure_slider.setValue(10)
        qtbot.waitUntil(entered.is_set,timeout=5000)
        second=image_item(view,tmp_path,name='second.png')
        panel._refresh_item(second)
    finally:
        release.set()
    wait_preview(qtbot,panel)
    assert panel._item is second
    assert not first._preview_adjusted and not second._preview_adjusted
    assert panel._inspection_session is None


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


def test_destroyed_inspector_ignores_running_adjustment(view, qtbot, tmp_path, monkeypatch):
    import threading
    from PyQt6 import sip
    from prism import inspection_preview
    from prism.widgets.detail_panel import DetailPanel
    item = image_item(view, tmp_path)
    panel = DetailPanel(view)
    panel._refresh_item(item)
    registry = panel._hdr_jobs
    entered, release = threading.Event(), threading.Event()
    render = inspection_preview.render_inspection
    def slow(*args, **kwargs):
        entered.set()
        assert release.wait(5)
        return render(*args, **kwargs)
    monkeypatch.setattr(inspection_preview, 'render_inspection', slow)
    try:
        panel._exposure_slider.setValue(10)
        qtbot.waitUntil(entered.is_set, timeout=5000)
        panel.deleteLater()
        QtCore.QCoreApplication.sendPostedEvents(None, QtCore.QEvent.Type.DeferredDelete)
        assert sip.isdeleted(panel)
    finally:
        release.set()
    qtbot.waitUntil(lambda: not registry._jobs, timeout=5000)
    assert not item._preview_adjusted


@pytest.mark.parametrize('virtual', [False, True])
@pytest.mark.parametrize('leave', ['other', 'empty', 'collection'])
def test_leaving_image_restores_immediately(view, qtbot, tmp_path, monkeypatch, virtual, leave):
    item = image_item(view, tmp_path, virtual=virtual)
    panel = view._detail_panel
    original = item.original_bytes()
    size, crop = QtCore.QSize(item._image_size), QtCore.QRectF(item.crop)
    panel._exposure_slider.setValue(10)
    wait_preview(qtbot, panel)
    panel._false_color_box.setChecked(True)
    wait_preview(qtbot, panel)
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


def test_reset_preserves_cached_sharp_pixels_and_statistics(view, qtbot, tmp_path):
    item = image_item(view, tmp_path)
    panel = view._detail_panel
    sharp = QtGui.QPixmap(160, 90)
    sharp.fill(QtGui.QColor(50, 70, 90))
    lod.cache().store(item, 192, sharp, notify=False)
    before = panel._hist_widget._stats['mean']
    panel._exposure_slider.setValue(10)
    wait_preview(qtbot, panel)
    assert panel._hist_widget._stats['mean'] > before
    panel._reset_hdr_controls()
    assert not item._preview_adjusted
    assert panel._hist_widget._stats['mean'] == before
    assert lod.cache().get(item, 192).cacheKey() == sharp.cacheKey()


def test_hdr_adjustment_also_restores_without_redecoding(view, qtbot, tmp_path, monkeypatch):
    item = image_item(view, tmp_path)
    panel = view._detail_panel
    baseline = native_pixmap(item)
    monkeypatch.setattr(panel, '_hdr_source_available', lambda item: True)
    load = Mock(return_value=(QtGui.QImage(160, 90, QtGui.QImage.Format.Format_RGB32), None))
    monkeypatch.setattr('prism.widgets.detail_panel.load_image', load)
    panel._exposure_slider.setValue(10)
    wait_preview(qtbot, panel)
    assert load.call_count == 1
    view.scene.clearSelection()
    panel._on_selection_changed()
    assert load.call_count == 1
    assert not item._preview_adjusted
    assert native_pixmap(item).cacheKey() == baseline.cacheKey()


def test_metadata_refresh_does_not_end_active_inspection(view, qtbot, tmp_path):
    item = image_item(view, tmp_path)
    panel = view._detail_panel
    panel._exposure_slider.setValue(10)
    wait_preview(qtbot, panel)
    session = panel._inspection_session
    panel._refresh_item(item)
    assert panel._inspection_session is session and item._preview_adjusted
    view.scene.clearSelection()
    panel._on_selection_changed()
    assert panel._inspection_session is None


def test_false_colour_does_not_apply_exposure_twice(view, qtbot, tmp_path):
    from prism.fileio.image import adjust_display_image, false_color_preview
    item = image_item(view, tmp_path)
    panel = view._detail_panel
    source = item.pixmap().toImage()
    expected = false_color_preview(adjust_display_image(source, 1.0))
    panel._exposure_slider.setValue(10)
    wait_preview(qtbot, panel)
    panel._false_color_box.setChecked(True)
    wait_preview(qtbot, panel)
    assert item.pixmap().toImage().convertToFormat(expected.format()) == expected


def test_direct_selection_replacement_without_empty_event_restores(view, qtbot, tmp_path):
    item = image_item(view, tmp_path)
    panel = view._detail_panel
    other = fileio._load_image_item(str(tmp_path/'a.png'))
    view.scene.addItem(other)
    panel._exposure_slider.setValue(10)
    wait_preview(qtbot, panel)
    assert item._preview_adjusted
    # Public/search selections can replace their inspector item directly.
    panel._refresh_item(other)
    assert not item._preview_adjusted
    assert panel._item is other and panel._exposure_slider.value() == 0
