"""D9/10 closeout regressions: actual Qt pixels and bounded side-channel proof."""
import json
from PyQt6 import QtCore, QtGui, QtWidgets
import pytest
from prism import lod, asset_paint_evidence as proof
from prism.presentation_evidence import QueryEvidence
from tests.test_d_asset_paint_evidence import canvas, output, open_frame, draw_card, make_resource


def setup(host):
    # Hidden QWidget layouts do not necessarily resize their viewport yet.
    host.viewport().resize(500, 320)
    host.setAlignment(QtCore.Qt.AlignmentFlag.AlignLeft | QtCore.Qt.AlignmentFlag.AlignTop)
    host.setSceneRect(0, 0, 500, 320)
    host.resetTransform()
    host.horizontalScrollBar().setValue(0)
    host.verticalScrollBar().setValue(0)
    for row, card in enumerate(host.cards):
        card._x, card._y = row*20., 0.
        card._size = QtCore.QSizeF(20, 20)
        host.model().items[row].save_id = 'asset-%d' % row


def shared(canvas, count=2, *, diagnostic=True, budget=1000):
    events = []
    ev = QueryEvidence(scope={'kind': 'closeout'}, source_revision=7,
                       diagnostic=diagnostic, id_budget=budget,
                       observer=lambda event, **fields: events.append((event, fields)))
    host, ev = canvas(count, evidence=ev)
    setup(host)
    return host, ev, events


def last(events, name):
    return next(fields for event, fields in reversed(events) if event == name)


def close(host, frame, painter=None):
    if painter is not None:
        painter.end()
    proof.finish_frame(host, frame)


def delegate_draw(host, painter, row):
    card = host.cards[row]
    painter.save()
    painter.translate(card._x, card._y)
    option = QtWidgets.QStyleOptionViewItem()
    option.rect = card.boundingRect().toAlignedRect()
    with proof.delegate_scope(host, card, painter):
        host.delegate.paint(painter, option, host.model().index(row))
    painter.restore()


@pytest.mark.parametrize('cached', [False, True])
def test_transparent_current_over_old_red_is_not_ready(canvas, cached):
    host, ev, events = shared(canvas, 1)
    item = host.model().items[0]
    old = item._preview_pixmap
    old.fill(QtGui.QColor('red'))
    item._display_source_revision = 2
    new = QtGui.QPixmap(256, 256)
    new.fill(QtCore.Qt.GlobalColor.transparent)
    item._preview_pixmap = new
    lod.cache().store(item, 512, new, notify=False)
    image = make_resource(host) if cached else None
    if cached:
        assert image._asset_paint_resource.generated == 0
        assert ev.snapshot('drawn')['unique_rows'] == 0
    out = output(host)
    frame = open_frame(host)
    painter = QtGui.QPainter(out)
    target = QtCore.QRectF(0, 0, 20, 20)
    painter.drawPixmap(target, old, QtCore.QRectF(old.rect()))
    with proof.delegate_scope(host, host.cards[0], painter):
        proof.observe_pixmap(host, painter, item, target, old, QtCore.QRectF(old.rect()), 20, 1)
    if cached:
        target = QtCore.QRectF(0, 0, 512, 512)
        painter.drawImage(target, image)
        proof.observe_image(host, painter, target, image, category='incremental')
    else:
        delegate_draw(host, painter, 0)
    close(host, frame, painter)
    assert out.pixelColor(10, 10).name() == '#ff0000'
    snap = last(events, 'asset_visibility_snapshot')
    assert snap['content_ready'] == snap['current_revision_coverage'] == 0
    assert last(events, 'asset_paint_frame')['content_ready'] == 0
    assert snap['ever_drawn_unique_ids'] == 1


@pytest.mark.parametrize('unknown', [False, True])
def test_mixed_or_uninspected_alpha_is_unknown_not_rectangular_proof(canvas, monkeypatch, unknown):
    host, ev, events = shared(canvas, 1)
    item = host.model().items[0]
    image = QtGui.QImage(256, 256, QtGui.QImage.Format.Format_ARGB32_Premultiplied)
    image.fill(QtCore.Qt.GlobalColor.transparent)
    painter = QtGui.QPainter(image)
    painter.fillRect(0, 0, 128, 256, QtGui.QColor('green'))
    painter.end()
    pm = QtGui.QPixmap.fromImage(image)
    item._preview_pixmap = pm
    lod.cache().store(item, 512, pm, notify=False)
    if unknown:
        monkeypatch.setattr(proof, 'MAX_ALPHA_PIXELS', 0)
    frame = open_frame(host)
    out = output(host)
    painter = QtGui.QPainter(out)
    delegate_draw(host, painter, 0)
    close(host, frame, painter)
    snap = last(events, 'asset_visibility_snapshot')
    assert out.pixelColor(2, 10).alpha() > 0 and out.pixelColor(18, 10).alpha() == 0
    assert snap['content_ready'] == 0 and snap['incomplete']
    assert not snap['current_viewport_exact'] and snap['coverage_basis'] == 'lower_bound'
    assert all(not entry['occludes'] for entry in snap['diagnostic_regions'])
    if unknown:
        assert not snap['ever_drawn_unique_ids_complete']


@pytest.mark.parametrize('mode', ['incremental', 'synchronous'])
def test_actual_tile_background_fill_erases_earlier_row(canvas, monkeypatch, mode):
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
        partial = tiles._jobs[key][0]
        assert partial.pixelColor(10, 10).alpha() == 0
        assert partial._asset_paint_resource.background is None
        assert tiles._advance_job(key)
    else:
        monkeypatch.setattr(tiles, 'render', render)
        tiles._build(key)
    image = tiles.tiles[key][0]
    assert image._asset_paint_resource.background is not None
    assert ev.snapshot('drawn')['unique_rows'] == 0
    frame = open_frame(host)
    out = output(host)
    painter = QtGui.QPainter(out)
    delegate_draw(host, painter, 0)
    painter.drawImage(QtCore.QRectF(0, 0, 512, 512), image)
    proof.observe_image(host, painter, QtCore.QRectF(0, 0, 512, 512), image)
    close(host, frame, painter)
    assert out.pixelColor(10, 10).name() == '#000000'
    snap = last(events, 'asset_visibility_snapshot')
    assert snap['content_ready'] == snap['current_revision_coverage'] == 1
    assert snap['current_viewport_exact']
    assert not next(e for e in snap['diagnostic_regions'] if e['row'] == 0)['current_viewport_row_ready']
    assert snap['diagnostic_regions'][1]['kind'] == 'occlusion'


@pytest.mark.parametrize('transparent_background', [False, True])
def test_partial_or_transparent_background_does_not_erase_whole_buffer(canvas, transparent_background):
    host, ev, events = shared(canvas)
    image = make_resource(host, (1,))
    if transparent_background:
        proof.observe_background(host, image, QtGui.QColor(0, 0, 0, 0))
    assert image._asset_paint_resource.background is None
    frame = open_frame(host)
    out = output(host)
    painter = QtGui.QPainter(out)
    delegate_draw(host, painter, 0)
    before = out.pixelColor(10, 10)
    painter.drawImage(QtCore.QRectF(0, 0, 512, 512), image)
    proof.observe_image(host, painter, QtCore.QRectF(0, 0, 512, 512), image, category='incremental')
    close(host, frame, painter)
    assert out.pixelColor(10, 10) == before
    assert last(events, 'asset_visibility_snapshot')['content_ready'] == 2


@pytest.mark.parametrize('variant', ['fractional', 'letterbox', 'crop_flip_dpr_scene'])
def test_renderer_derived_complete_content_geometry(canvas, monkeypatch, variant):
    host, ev, events = shared(canvas, 1)
    item = host.model().items[0]
    card = host.cards[0]
    item._image_size = QtCore.QSize(512, 341 if variant == 'fractional' else 256)
    card._size = QtCore.QSizeF(156, 156*341/512) if variant == 'fractional' else QtCore.QSizeF(20, 20)
    dpr = 1.
    if variant == 'crop_flip_dpr_scene':
        item.crop = QtCore.QRectF(64, 32, 128, 64)
        item.flip = lambda: -1
        dpr = 2.
        monkeypatch.setattr(host.viewport(), 'devicePixelRatioF', lambda: dpr)
        host.scale(1.5, 1.5)
        host._layer.setTransform(QtGui.QTransform.fromTranslate(7, 11))
        # Supply real sufficient crop pixels at the demanded LOD; do not make
        # the geometry test accidentally exercise low-resolution fallback.
        lod.cache().store(item, 64, item._preview_pixmap, notify=False)
    out = QtGui.QImage(int(host.viewport().width()*dpr), int(host.viewport().height()*dpr),
                       QtGui.QImage.Format.Format_ARGB32_Premultiplied)
    out.setDevicePixelRatio(dpr)
    out.fill(QtCore.Qt.GlobalColor.transparent)
    frame = open_frame(host)
    painter = QtGui.QPainter(out)
    painter.setWorldTransform(host._layer.sceneTransform()*host.viewportTransform())
    delegate_draw(host, painter, 0)
    close(host, frame, painter)
    snap = last(events, 'asset_visibility_snapshot')
    assert snap['content_ready'] == snap['current_revision_coverage'] == 1
    assert snap['current_viewport_exact']
    entry = snap['diagnostic_regions'][0]
    expected = proof.content_target(card, item)
    assert entry['target'] == pytest.approx(proof.rect_values(expected))
    if variant == 'fractional':
        assert expected.y() == pytest.approx(0.05078125)
    if variant == 'crop_flip_dpr_scene':
        assert entry['transform'][0] < 0
        assert entry['required_crop_pixels'] == pytest.approx([60, 30])


def test_ready_sliver_cannot_define_needed_geometry(canvas):
    host, ev, events = shared(canvas, 1)
    out = output(host)
    frame = open_frame(host)
    painter = QtGui.QPainter(out)
    draw_card(host, painter, 0, QtCore.QRectF(0, 0, 10, 20))
    close(host, frame, painter)
    assert last(events, 'asset_visibility_snapshot')['content_ready'] == 0


def test_query_unique_ids_accumulate_deduplicate_and_keep_resource_original_id(canvas):
    host, ev, events = shared(canvas)
    image = make_resource(host, (0,))
    assert image._asset_paint_resource.entries[0][1]['original_asset_id'] == 'asset:asset-0'
    assert ev.snapshot('drawn')['unique_rows'] == 0
    out = output(host)
    for row, total in ((0, 1), (1, 2), (0, 2)):
        frame = open_frame(host)
        painter = QtGui.QPainter(out)
        delegate_draw(host, painter, row)
        close(host, frame, painter)
        snap = last(events, 'asset_visibility_snapshot')
        paint = last(events, 'asset_paint_frame')
        assert snap['ever_drawn_unique_ids'] == paint['ever_drawn_unique_ids'] == total
        assert snap['ever_drawn_unique_ids_complete']
        assert snap['viewport_drawn'] == 1
        assert snap['frame_id'] == paint['frame_id'] and snap['final'] is paint['final'] is False
    # Original row now refers to another asset; cached pixels still belong to asset-0.
    host.model().items.reverse()
    frame = open_frame(host)
    painter = QtGui.QPainter(out)
    painter.drawImage(QtCore.QRectF(0, 0, 512, 512), image)
    proof.observe_image(host, painter, QtCore.QRectF(0, 0, 512, 512), image)
    close(host, frame, painter)
    assert ev.snapshot('drawn')['logical_rows'] == 4
    assert ev.snapshot('drawn')['unique_rows'] == 2
    assert last(events, 'asset_visibility_snapshot')['diagnostic_regions'][0]['original_asset_id'] == 'asset:asset-0'
    replacement = QueryEvidence(scope={'next': True}, source_revision=7, diagnostic=True,
                                observer=lambda event, **fields: events.append((event, fields)))
    host.model().presentation_evidence = replacement
    frame = open_frame(host)
    painter = QtGui.QPainter(out)
    delegate_draw(host, painter, 0)
    close(host, frame, painter)
    assert last(events, 'asset_visibility_snapshot')['ever_drawn_unique_ids'] == 1


@pytest.mark.parametrize('diagnostic,budget', [(True, 1), (True, 0), (False, 1)])
def test_query_unique_id_budget_and_production_no_id_sets(canvas, diagnostic, budget):
    host, ev, events = shared(canvas, diagnostic=diagnostic, budget=budget)
    out = output(host)
    frame = open_frame(host)
    painter = QtGui.QPainter(out)
    delegate_draw(host, painter, 0)
    delegate_draw(host, painter, 1)
    close(host, frame, painter)
    snap = last(events, 'asset_visibility_snapshot')
    counts = ev.snapshot('drawn')
    assert counts['logical_rows'] == 2
    assert not snap['ever_drawn_unique_ids_complete']
    if diagnostic:
        assert snap['ever_drawn_unique_ids'] == budget
        assert snap['ever_drawn_unique_ids_budget_exhausted']
        assert snap['ever_drawn_unique_ids_basis'] == 'lower_bound'
        assert snap['incomplete'] and not snap['current_viewport_exact']
    else:
        assert snap['ever_drawn_unique_ids'] is None
        assert ev._counts['drawn'].ids is None
        assert not frame.entries and 'diagnostic_ids' not in snap
        assert all('original_asset_id' not in fields for _, fields in events)


@pytest.mark.parametrize('limit', ['operations', 'time', 'storage'])
def test_closeout_budget_exhaustion_is_explicit_lower_bound(canvas, monkeypatch, limit):
    host, ev, events = shared(canvas, 8)
    frame = open_frame(host)
    out = output(host)
    painter = QtGui.QPainter(out)
    for row in range(8):
        delegate_draw(host, painter, row)
    painter.end()
    if limit == 'operations':
        monkeypatch.setattr(proof, 'FINISH_OPERATIONS', 12)
    elif limit == 'time':
        monkeypatch.setattr(proof, 'FINISH_SECONDS', 0.)
    else:
        frame.budget = 1
    proof.finish_frame(host, frame)
    snap = last(events, 'asset_visibility_snapshot')
    assert snap['incomplete'] and not snap['current_viewport_exact']
    assert snap['coverage_basis'] == 'lower_bound'
    assert snap['closeout_operations'] <= snap['closeout_operation_budget']
    if limit != 'storage':
        assert snap['closeout_budget_exhausted']
        assert last(events, 'asset_paint_frame')['incomplete']
    assert snap['ever_drawn_unique_ids'] == 8  # Closeout is not lifetime collection.
    assert snap['ever_drawn_unique_ids_complete']


def test_regional_closeout_does_not_scan_every_entry_per_row(canvas, monkeypatch):
    monkeypatch.setattr(proof, 'FINISH_SECONDS', 10.)
    host, ev, events = shared(canvas, 100)
    for row, card in enumerate(host.cards):
        card._x, card._y = (row%10)*40., (row//10)*30.
        card._size = QtCore.QSizeF(10, 10)
    frame = open_frame(host)
    out = output(host)
    painter = QtGui.QPainter(out)
    for row in range(100):
        delegate_draw(host, painter, row)
    close(host, frame, painter)
    snap = last(events, 'asset_visibility_snapshot')
    assert snap['current_viewport_exact'] and snap['content_ready'] == 100
    assert snap['closeout_operations'] < 100*100
    assert not snap['closeout_budget_exhausted']
    print('REGIONAL_CLOSEOUT', json.dumps({key: snap[key] for key in
          ('closeout_operations', 'closeout_elapsed_ms', 'content_ready')}))


def test_render_events_explicit_nonterminal_and_paired_after_model_final(canvas):
    host, ev, events = shared(canvas, 1)
    ev.final = True
    image = make_resource(host)
    proof.installed(host, image)
    frame = open_frame(host)
    out = output(host)
    painter = QtGui.QPainter(out)
    delegate_draw(host, painter, 0)
    close(host, frame, painter)
    assert ev.final is True and ev.active
    assert all(fields['final'] is False for _, fields in events)
    assert last(events, 'asset_paint_frame')['frame_id'] == last(events, 'asset_visibility_snapshot')['frame_id']


def test_mixed_alpha_outside_clip_cannot_increment_actual_unique_ids(canvas):
    host, ev, events = shared(canvas, 1)
    item = host.model().items[0]
    image = QtGui.QImage(256, 256, QtGui.QImage.Format.Format_ARGB32_Premultiplied)
    image.fill(QtCore.Qt.GlobalColor.transparent)
    painter = QtGui.QPainter(image)
    painter.fillRect(0, 0, 64, 256, QtGui.QColor('red'))
    painter.end()
    pm = QtGui.QPixmap.fromImage(image)
    item._preview_pixmap = pm
    lod.cache().store(item, 512, pm, notify=False)
    clip = QtCore.QRect(15, 0, 5, 20)
    frame = open_frame(host, clip)
    out = output(host)
    painter = QtGui.QPainter(out)
    painter.setClipRect(clip)
    delegate_draw(host, painter, 0)
    close(host, frame, painter)
    assert out.pixelColor(18, 10).alpha() == 0
    snap = last(events, 'asset_visibility_snapshot')
    assert snap['ever_drawn_unique_ids'] == 0
    assert not snap['ever_drawn_unique_ids_complete']
    assert snap['viewport_drawn'] == last(events, 'asset_paint_frame')['viewport_drawn'] == 0
    assert snap['content_ready'] == 0 and snap['incomplete']


@pytest.mark.parametrize('count', [500, 1000, 2000])
def test_dense_real_draw_closeout_has_bounded_work_not_exact_success_on_exhaustion(canvas, count):
    host, ev, events = shared(canvas, count, budget=10000)
    for row, card in enumerate(host.cards):
        card._x, card._y = float(row%200), float(row//200)
        card._size = QtCore.QSizeF(1, 1)
    frame = open_frame(host)
    out = output(host)
    painter = QtGui.QPainter(out)
    # Large fixture setup legitimately evicts item previews under the shared
    # cache budget. The benchmark owns one real immutable source pixmap.
    pm = QtGui.QPixmap(256, 256)
    pm.fill(QtGui.QColor('green'))
    try:
        for row, card in enumerate(host.cards):
            item = host.model().items[row]
            target = card.sceneBoundingRect()
            painter.drawPixmap(target, pm, QtCore.QRectF(pm.rect()))
            with proof.delegate_scope(host, card, painter):
                proof.observe_pixmap(host, painter, item, target, pm, QtCore.QRectF(pm.rect()), 1, 1)
    finally:
        painter.end()
    close(host, frame)
    snap = last(events, 'asset_visibility_snapshot')
    assert out.pixelColor(0, 0).alpha() == 255
    assert snap['ever_drawn_unique_ids'] == count and snap['ever_drawn_unique_ids_complete']
    assert snap['closeout_operations'] <= proof.FINISH_OPERATIONS
    if snap['closeout_budget_exhausted']:
        assert snap['incomplete'] and not snap['current_viewport_exact']
        assert snap['coverage_basis'] == 'lower_bound'
        assert last(events, 'asset_paint_frame')['incomplete']
    else:
        assert snap['content_ready'] == count and snap['current_viewport_exact']
    print('DENSE_REAL_CLOSEOUT', json.dumps(dict(rows=count, **{key: snap[key] for key in
          ('closeout_operations', 'closeout_elapsed_ms', 'content_ready', 'incomplete',
           'closeout_budget_exhausted')})))
