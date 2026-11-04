"""Quality markers consume completed Qt drawing, not installed cache levels."""
import pytest
from PyQt6 import QtCore, QtGui
from prism import lod, asset_paint_evidence as proof
from prism.project_catalog_index import PaintRollup
import prism.widgets.asset_canvas as module
from tests.test_d_asset_paint_evidence import canvas, output, open_frame
from tests.test_d_paint_closeout import shared, delegate_draw


@pytest.mark.parametrize('kind', ['ready', 'stale', 'missing', 'unknown_source', 'normal'])
def test_markers_follow_completed_viewport_proof(canvas, monkeypatch, kind):
    host, ev, events = shared(canvas, 1, diagnostic=kind != 'normal')
    host.setCurrentIndex(host.model().index(0))
    item = host.model().items[0]
    if kind == 'stale':
        item._display_source_revision += 1
    if kind == 'unknown_source':
        lod.cache().remember_pixels(item, item._preview_pixmap, None)
    calls = []
    monkeypatch.setattr(module, '_PKG3_MARKER_API', (PaintRollup, lambda _: {},
        lambda event, **fields: calls.append((event, fields)), 'assets.paint_frame'))
    frame = open_frame(host)
    image = output(host)
    painter = QtGui.QPainter(image)
    if kind != 'missing':
        delegate_draw(host, painter, 0)
    painter.end()
    quality = proof.finish_frame(host, frame)
    host._record_paint_frame(content=bool(frame.drawn), cards=1, quality=quality)
    ready = [fields for event, fields in calls if event == module.MARKER_VIEWPORT_QUALITY_READY]
    if kind == 'ready':
        assert quality['detailed'] == quality['covered'] == quality['total'] == 1
        assert ready and ready[0]['capture_id'] == frame.frame_id
        assert any(event == module.MARKER_VIEWPORT_QUALITY_PAINTED for event, _ in calls)
        assert any(event == module.MARKER_FOCUS_QUALITY_READY for event, _ in calls)
    else:
        assert not ready
        assert not any(event.endswith('quality_painted') for event, _ in calls)
        if kind == 'normal':
            unknown = next(fields for event, fields in calls
                           if event == module.MARKER_VIEWPORT_QUALITY_UNKNOWN)
            assert unknown['painted'] is True  # Drawn does not imply clear.


def test_tracking_unknown_preview_is_bounded_without_freezing_none(canvas):
    host, ev, events = shared(canvas, 1)
    item = host.model().items[0]
    store = lod.cache()
    pm = item._preview_pixmap
    key = (id(item), pm.cacheKey())
    item._source_path = 'not-read-by-this-test.png'
    store.remember_pixels(item, pm, None)
    store.track_preview(item, notify=False)
    assert key not in store.pixel_revisions
    assert lod.pixmap_source_revision(item, pm) is None
    assert store.get(item, 192) is not None  # Still budgeted and usable.
    store.store(item, 192, pm, notify=False, source_revision=1)
    store.track_preview(item, notify=False)
    assert lod.pixmap_source_revision(item, pm) == 1
    store.remember_pixels(item, pm, None)
    assert key not in store.pixel_revisions  # Unknown input revokes old proof.


def test_partial_repaint_does_not_claim_new_whole_view_paint(canvas, monkeypatch):
    host, ev, events = shared(canvas, 1)
    frame = open_frame(host)
    image = output(host)
    painter = QtGui.QPainter(image)
    delegate_draw(host, painter, 0)
    painter.end()
    assert proof.finish_frame(host, frame)['detailed'] == 1
    partial = open_frame(host, QtCore.QRect(100, 100, 10, 10))
    quality = proof.finish_frame(host, partial)
    assert quality['detailed'] == 1 and not quality['full_viewport_repaint']
    calls = []
    monkeypatch.setattr(module, '_PKG3_MARKER_API', (PaintRollup, lambda _: {},
        lambda event, **fields: calls.append((event, fields)), 'assets.paint_frame'))
    host._record_paint_frame(content=True, cards=1, quality=quality)
    assert any(event == module.MARKER_VIEWPORT_QUALITY_READY for event, _ in calls)
    assert not any(event.endswith('quality_painted') for event, _ in calls)
