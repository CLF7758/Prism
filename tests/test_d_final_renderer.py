"""Final renderer regressions using real Qt draws on offscreen QImages."""
import uuid

import pytest
from PyQt6 import QtCore, QtGui

from prism import asset_paint_evidence as proof, lod
from prism.presentation_evidence import QueryEvidence
from tests.test_d_asset_paint_evidence import canvas, output, open_frame, make_resource
from tests.test_d_paint_closeout import shared, setup, last, close, delegate_draw


def colour(host, row, value):
    item = host.model().items[row]
    item._preview_pixmap.fill(QtGui.QColor(value))
    lod.cache().store(item, 512, item._preview_pixmap, notify=False)


def overlap(host, offset=0.):
    host.cards[1]._x = offset
    colour(host, 0, 'red')
    colour(host, 1, 'green')


def test_opaque_overlap_final_unique_is_one_but_operations_and_lifetime_are_two(canvas):
    host, ev, events = shared(canvas)
    overlap(host)
    out = output(host)
    frame = open_frame(host)
    painter = QtGui.QPainter(out)
    delegate_draw(host, painter, 0)
    delegate_draw(host, painter, 1)
    close(host, frame, painter)
    assert out.pixelColor(10, 10).name() == '#008000'
    snap = last(events, 'asset_visibility_snapshot')
    paint = last(events, 'asset_paint_frame')
    assert paint['viewport_drawn'] == snap['frame_counts']['viewport_drawn'] == 2
    assert snap['ever_drawn_unique_ids'] == 2 and snap['ever_drawn_unique_ids_complete']
    assert snap['viewport_drawn'] == snap['unique_viewport_drawn'] == 1
    assert snap['diagnostic_ids'] == [1]
    assert snap['content_ready'] == snap['current_revision_coverage'] == 1
    assert snap['current_viewport_exact']


@pytest.mark.parametrize('mode', ['synchronous', 'incremental'])
def test_actual_tile_background_final_viewport_unique_excludes_erased_row(canvas, monkeypatch, mode):
    host, ev, events = shared(canvas)
    host.setBackgroundBrush(QtGui.QColor('black'))
    tiles = host._tiles
    key = (0, 0, 0)

    def render(image, target, source):
        painter = QtGui.QPainter(image)
        with proof.resource_scope(host, image):
            delegate_draw(host, painter, 1)
        painter.end()
        proof.generated(host, image, complete=True)

    if mode == 'incremental':
        def job(image, target, source):
            render(image, target, source)
            yield
        monkeypatch.setattr(tiles, '_job_factory', job)
        assert tiles._begin_job(key)
        assert not tiles._advance_job(key)
        assert tiles._advance_job(key)
    else:
        monkeypatch.setattr(tiles, 'render', render)
        tiles._build(key)
    image = tiles.tiles[key][0]
    assert image._asset_paint_resource.background is not None
    assert ev.snapshot('drawn')['logical_rows'] == 0
    out = output(host)
    frame = open_frame(host)
    painter = QtGui.QPainter(out)
    delegate_draw(host, painter, 0)
    target = QtCore.QRectF(0, 0, 512, 512)
    painter.drawImage(target, image)
    proof.observe_image(host, painter, target, image)
    close(host, frame, painter)
    assert out.pixelColor(10, 10).name() == '#000000'
    snap = last(events, 'asset_visibility_snapshot')
    assert snap['viewport_drawn'] == snap['content_ready'] == snap['current_revision_coverage'] == 1
    assert snap['diagnostic_ids'] == [1] and snap['current_viewport_exact']
    assert snap['frame_counts']['viewport_drawn'] == snap['ever_drawn_unique_ids'] == 2


def test_partial_opaque_overlap_keeps_remaining_contribution_not_full_ready(canvas):
    host, ev, events = shared(canvas)
    overlap(host, 10.)
    out = output(host)
    frame = open_frame(host)
    painter = QtGui.QPainter(out)
    delegate_draw(host, painter, 0)
    delegate_draw(host, painter, 1)
    close(host, frame, painter)
    assert out.pixelColor(5, 10).name() == '#ff0000'
    assert out.pixelColor(15, 10).name() == '#008000'
    snap = last(events, 'asset_visibility_snapshot')
    assert snap['viewport_drawn'] == 2 and snap['diagnostic_ids'] == [0, 1]
    assert snap['content_ready'] == snap['current_revision_coverage'] == 1
    assert snap['current_viewport_exact']


@pytest.mark.parametrize('alpha_kind', ['partial', 'unknown'])
@pytest.mark.parametrize('offset,expected', [(0., 0), (10., 1)])
def test_uncertain_later_alpha_cannot_overstate_visible_lower_bound(canvas, monkeypatch, alpha_kind, offset, expected):
    host, ev, events = shared(canvas)
    overlap(host, offset)
    image = QtGui.QImage(256, 256, QtGui.QImage.Format.Format_ARGB32_Premultiplied)
    image.fill(QtCore.Qt.GlobalColor.transparent)
    painter = QtGui.QPainter(image)
    painter.fillRect(0, 0, 128, 256, QtGui.QColor('green'))
    painter.end()
    item = host.model().items[1]
    item._preview_pixmap = QtGui.QPixmap.fromImage(image)
    lod.cache().store(item, 512, item._preview_pixmap, notify=False)
    if alpha_kind == 'unknown':
        monkeypatch.setattr(proof, 'MAX_ALPHA_PIXELS', 0)
    out = output(host)
    frame = open_frame(host)
    painter = QtGui.QPainter(out)
    delegate_draw(host, painter, 0)
    delegate_draw(host, painter, 1)
    close(host, frame, painter)
    assert out.pixelColor(int(offset+2), 10).name() == '#008000'
    snap = last(events, 'asset_visibility_snapshot')
    assert snap['viewport_drawn'] == expected
    assert snap['incomplete'] and snap['coverage_basis'] == 'lower_bound'
    assert not snap['ever_drawn_unique_ids_complete']
    assert snap['frame_counts']['viewport_drawn'] == snap['ever_drawn_unique_ids'] == 1


@pytest.mark.parametrize('kind', ['fallback', 'placeholder'])
@pytest.mark.parametrize('offset,expected', [(0., 0), (10., 1)])
def test_current_fallback_and_placeholder_counts_have_final_viewport_scope(canvas, kind, offset, expected):
    host, ev, events = shared(canvas)
    overlap(host, offset)
    out = output(host)
    frame = open_frame(host)
    painter = QtGui.QPainter(out)
    if kind == 'fallback':
        host.model().items[0]._display_source_revision = 2
        delegate_draw(host, painter, 0)
    else:
        target = QtCore.QRectF(0, 0, 20, 20)
        with proof.delegate_scope(host, host.cards[0], painter):
            painter.fillRect(target, QtGui.QColor('gray'))
            proof.observe_missing(host, painter, target)
    delegate_draw(host, painter, 1)
    close(host, frame, painter)
    assert out.pixelColor(int(offset+2), 10).name() == '#008000'
    snap = last(events, 'asset_visibility_snapshot')
    assert snap[kind] == expected
    assert snap['frame_counts'][kind] == 1
    assert snap['current_viewport_exact']


def test_lifetime_incomplete_propagates_between_two_views_of_shared_evidence(canvas):
    events = []
    ev = QueryEvidence(source_revision=7, diagnostic=True,
                       observer=lambda event, **fields: events.append((event, fields)))
    first, _ = canvas(1, evidence=ev)
    setup(first)
    second, _ = canvas(1, evidence=ev)
    setup(second)
    image = QtGui.QImage(256, 256, QtGui.QImage.Format.Format_ARGB32_Premultiplied)
    image.fill(QtCore.Qt.GlobalColor.transparent)
    painter = QtGui.QPainter(image)
    painter.fillRect(0, 0, 128, 256, QtGui.QColor('green'))
    painter.end()
    first.model().items[0]._preview_pixmap = QtGui.QPixmap.fromImage(image)
    lod.cache().store(first.model().items[0], 512, first.model().items[0]._preview_pixmap, notify=False)
    frame = open_frame(first)
    out = output(first)
    painter = QtGui.QPainter(out)
    delegate_draw(first, painter, 0)
    close(first, frame, painter)
    assert not last(events, 'asset_paint_frame')['ever_drawn_unique_ids_complete']
    frame = open_frame(second)
    out = output(second)
    painter = QtGui.QPainter(out)
    delegate_draw(second, painter, 0)
    close(second, frame, painter)
    snap = last(events, 'asset_visibility_snapshot')
    assert snap['viewport_drawn'] == 1 and snap['current_viewport_exact']
    assert snap['ever_drawn_unique_ids'] == 1
    assert not snap['ever_drawn_unique_ids_complete']
    assert snap['ever_drawn_unique_ids_incomplete'] and snap['ever_drawn_unique_ids_basis'] == 'lower_bound'
    assert getattr(ev, '_asset_drawn_id_state')['incomplete']


def test_view_id_is_stable_for_host_distinct_across_hosts_and_paired(canvas):
    host, ev, events = shared(canvas, 1)
    second, _ = canvas(1, evidence=ev)
    setup(second)
    ids = []
    captures = []
    identity = ev.fields()
    for view in (host, host, second):
        frame = open_frame(view)
        out = output(view)
        painter = QtGui.QPainter(out)
        delegate_draw(view, painter, 0)
        close(view, frame, painter)
        paint = last(events, 'asset_paint_frame')
        snap = last(events, 'asset_visibility_snapshot')
        assert paint['view_id'] == snap['view_id'] == frame.view_id
        assert paint['frame_id'] == snap['frame_id']
        assert uuid.UUID(paint['view_id']).hex == paint['view_id']
        assert all(snap[key] == identity[key] for key in ('session_id', 'query_id', 'source_revision'))
        ids.append(paint['view_id'])
        captures.append(paint['frame_id'])
    assert ids[0] == ids[1] and ids[0] != ids[2]
    assert len(set(captures)) == 3
    image = make_resource(host)
    proof.installed(host, image)
    assert last(events, 'asset_resource_generated')['view_id'] == ids[0]
    assert last(events, 'asset_cache_installed')['view_id'] == ids[0]
    replacement = QueryEvidence(source_revision=8, diagnostic=True,
                                observer=lambda event, **fields: events.append((event, fields)))
    host.model().presentation_evidence = replacement
    frame = open_frame(host)
    close(host, frame)
    assert last(events, 'asset_visibility_snapshot')['view_id'] == ids[0]


@pytest.mark.parametrize('limit', ['storage', 'operations', 'time'])
def test_unprocessed_later_occlusion_cannot_enter_visible_lower_bound(canvas, monkeypatch, limit):
    host, ev, events = shared(canvas)
    overlap(host)
    frame = open_frame(host)
    if limit == 'storage':
        frame.budget = 1
    out = output(host)
    painter = QtGui.QPainter(out)
    delegate_draw(host, painter, 0)
    delegate_draw(host, painter, 1)
    painter.end()
    if limit == 'operations':
        monkeypatch.setattr(proof, 'FINISH_OPERATIONS', 12)
    elif limit == 'time':
        monkeypatch.setattr(proof, 'FINISH_SECONDS', 0.)
    close(host, frame)
    assert out.pixelColor(10, 10).name() == '#008000'
    snap = last(events, 'asset_visibility_snapshot')
    assert snap['viewport_drawn'] == 0
    assert snap['incomplete'] and snap['coverage_basis'] == 'lower_bound'
    assert snap['closeout_operations'] <= snap['closeout_operation_budget']
    assert snap['frame_counts']['viewport_drawn'] == snap['ever_drawn_unique_ids'] == 2
    assert snap['ever_drawn_unique_ids_complete']


def test_unknown_later_geometry_cannot_leave_earlier_visible_lower_bound(canvas):
    host, ev, events = shared(canvas)
    overlap(host)
    out = output(host)
    frame = open_frame(host)
    painter = QtGui.QPainter(out)
    delegate_draw(host, painter, 0)
    painter.rotate(5.)
    delegate_draw(host, painter, 1)
    close(host, frame, painter)
    assert out.pixelColor(5, 10).name() == '#008000'
    snap = last(events, 'asset_visibility_snapshot')
    assert snap['viewport_drawn'] == 0 and snap['incomplete']
    assert snap['ever_drawn_unique_ids'] == 1 and not snap['ever_drawn_unique_ids_complete']
