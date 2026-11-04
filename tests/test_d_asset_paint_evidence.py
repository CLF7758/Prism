"""D9+10 renderer-side evidence; only synthetic models, offscreen Qt devices."""
import json
import time
from types import SimpleNamespace
from unittest.mock import Mock
import pytest
from PyQt6 import QtCore, QtGui, QtWidgets
from prism import lod
from prism import asset_paint_evidence as proof
from prism.widgets.asset_canvas import AssetCanvas


class Evidence:
    active = True
    diagnostic = True
    id_budget = 10000
    query_id = 'query-d'

    def __init__(self):
        self.events = []
        self.source_revision = 7

    def fields(self):
        return dict(schema_version=2, presentation_schema_version=1,
                    session_id='session-d', query_id=self.query_id, scope='synthetic',
                    source_revision=self.source_revision, cancelled=False, final=False)

    def record(self, event, **fields):
        json.dumps(fields)  # Evidence must be trace-serializable.
        self.events.append((event, fields))

    def last(self, event):
        return next(fields for name, fields in reversed(self.events) if name == event)


class Item(QtCore.QObject):
    def __init__(self, **fields):
        super().__init__()
        self.__dict__.update(fields)


class Model(QtCore.QAbstractListModel):
    def __init__(self, items, evidence):
        super().__init__()
        self.items = items
        self.presentation_evidence = evidence

    def rowCount(self, parent=QtCore.QModelIndex()):
        return 0 if parent.isValid() else len(self.items)

    def data(self, index, role=QtCore.Qt.ItemDataRole.DisplayRole):
        return str(index.row()) if role == QtCore.Qt.ItemDataRole.DisplayRole else None


class Delegate(QtWidgets.QStyledItemDelegate):
    def __init__(self, host):
        super().__init__(host)
        self.host = host
        self.calls = 0

    def paint(self, painter, option, index):
        self.calls += 1
        lod.draw_image(painter, self.host.model().items[index.row()],
                       QtCore.QRectF(option.rect), self.host)


@pytest.fixture
def canvas(qapp):
    owned = []

    def build(count=4, evidence=None):
        owner = QtWidgets.QWidget()
        owner.mode, owner.sort_key = 'all', 'name'
        owner.view = SimpleNamespace(scene=SimpleNamespace(project_id='synthetic-d'),
                                     on_can_undo_changed=lambda _: None,
                                     on_can_redo_changed=lambda _: None)
        host = AssetCanvas(owner)
        host.resize(500, 320)
        host._composites.MIN_ITEMS = 100000
        ev = evidence if evidence is not None else Evidence()
        store = lod.cache()
        store.entries.clear()
        store.pixel_revisions.clear()
        store.bytes = 0
        items = []
        for row in range(count):
            pm = QtGui.QPixmap(256, 256)
            pm.fill(QtGui.QColor(40+row%100, 130, 220))
            item = Item(_image_size=QtCore.QSize(512, 512), crop=QtCore.QRectF(),
                                   _canvas_id='synthetic', _display_source_revision=1,
                                   _preview_pixmap=pm, opacity=lambda: 1., flip=lambda: 1.)
            lod.cache().store(item, 512, pm, notify=False)
            items.append(item)
        host.setModel(Model(items, ev))
        host.setItemDelegate(Delegate(host))
        host.rebuild()
        host._tiles.revision = host._tiles.content_revision()
        owned.append((owner, host))
        return host, ev

    yield build
    for owner, host in owned:
        host._tiles.reset()
        host._composites.reset()
        host._display_timer.stop()
        owner.close()


def output(host):
    image = QtGui.QImage(host.viewport().size(), QtGui.QImage.Format.Format_ARGB32_Premultiplied)
    image.fill(QtCore.Qt.GlobalColor.transparent)
    return image


def open_frame(host, clip=None):
    return proof.begin_frame(host, QtGui.QPaintEvent(clip if clip is not None else host.viewport().rect()))


def draw_card(host, painter, row, target=None):
    card = host.cards[row]
    painter.save()
    if target is None:
        target = card.sceneBoundingRect()
    with proof.delegate_scope(host, card, painter):
        lod.draw_image(painter, host.model().items[row], QtCore.QRectF(target), host)
    painter.restore()


def make_resource(host, rows=(0,), width=512):
    image = QtGui.QImage(width, width, QtGui.QImage.Format.Format_ARGB32_Premultiplied)
    image.fill(QtCore.Qt.GlobalColor.transparent)
    painter = QtGui.QPainter(image)
    with proof.resource_scope(host, image):
        for row in rows:
            draw_card(host, painter, row, QtCore.QRectF(row*20, 0, 20, 20))
    painter.end()
    proof.generated(host, image, complete=False)
    return image


def test_scene_paint_counts_visible_content_not_model_rows(canvas, qapp):
    host, ev = canvas(40)
    host.parent().show()
    host.show()
    qapp.processEvents()
    host.viewport().repaint()
    qapp.processEvents()
    frame = ev.last('asset_paint_frame')
    assert frame['model_rows'] == 40
    assert 0 < frame['viewport_drawn'] < 40
    assert frame['paint_calls'] >= frame['viewport_drawn']
    assert frame['coverage_scope'] == 'paint_clip'
    assert frame['capture_id'] == host._asset_paint_capture_id
    assert frame['display_confirmed'] is False
    assert frame['schema_version'] == 2


def test_resource_generation_and_install_never_add_viewport_drawn(canvas):
    host, ev = canvas()
    frame = open_frame(host)
    image = make_resource(host, (0, 1))
    host._tiles._install((0, 0, 0), image)
    assert frame.drawn == 0
    proof.finish_frame(host, frame)
    assert ev.last('asset_resource_generated')['viewport_drawn'] == 0
    assert ev.last('asset_cache_installed')['viewport_drawn'] == 0
    assert ev.last('asset_paint_frame')['viewport_drawn'] == 0


def test_incremental_buffer_only_replays_generated_regions(canvas):
    host, ev = canvas(300)
    key = (-5, 0, 0)
    assert not host._tiles._step_build(key)
    image = host._tiles._jobs[key][0]
    resource = image._asset_paint_resource
    assert 0 < resource.generated <= 128
    assert not resource.complete
    frame = open_frame(host)
    image_out = output(host)
    painter = QtGui.QPainter(image_out)
    painter.drawImage(QtCore.QRectF(0, 0, 512, 512), image)
    proof.observe_image(host, painter, QtCore.QRectF(0, 0, 512, 512), image, category='incremental')
    painter.end()
    assert frame.drawn == resource.generated
    assert frame.drawn < len(host.cards)
    assert all(area.boundingRect().width() < 512 for area, _ in frame.entries)
    proof.finish_frame(host, frame)
    assert ev.last('asset_paint_frame')['categories']['incremental'] == resource.generated


def test_partial_buffer_clip_outside_generated_content_is_zero(canvas):
    host, ev = canvas()
    image = make_resource(host, (0,))
    frame = open_frame(host, QtCore.QRect(100, 100, 30, 30))
    out = output(host)
    painter = QtGui.QPainter(out)
    proof.observe_image(host, painter, QtCore.QRectF(0, 0, 512, 512), image, category='incremental')
    painter.end()
    proof.finish_frame(host, frame)
    assert ev.last('asset_paint_frame')['viewport_drawn'] == 0
    assert not ev.last('asset_visibility_snapshot')['current_viewport_exact']


def test_partial_and_disjoint_clips_are_not_bounding_box_proof(canvas):
    host, ev = canvas()
    region = QtGui.QRegion(QtCore.QRect(0, 0, 20, 20)) | QtGui.QRegion(QtCore.QRect(80, 0, 20, 20))
    frame = open_frame(host, region)
    out = output(host)
    painter = QtGui.QPainter(out)
    draw_card(host, painter, 0, QtCore.QRectF(40, 0, 20, 20))
    painter.end()
    proof.finish_frame(host, frame)
    result = ev.last('asset_paint_frame')
    assert result['viewport_drawn'] == 0
    assert len(result['effective_clip']) == 2
    assert not result['full_viewport_repaint']
    snapshot = ev.last('asset_visibility_snapshot')
    assert not snapshot['current_viewport_exact']


@pytest.mark.parametrize('transform', ['rotate', 'shear', 'projective'])
def test_non_axis_transform_is_unknown_not_exact_bbox(canvas, transform):
    host, ev = canvas()
    frame = open_frame(host)
    out = output(host)
    painter = QtGui.QPainter(out)
    if transform == 'rotate':
        painter.rotate(17)
    elif transform == 'shear':
        painter.shear(.2, 0)
    else:
        painter.setTransform(QtGui.QTransform(1, 0, .001, 0, 1, 0, 0, 0, 1))
    draw_card(host, painter, 0, QtCore.QRectF(5, 5, 40, 40))
    painter.end()
    proof.finish_frame(host, frame)
    result = ev.last('asset_paint_frame')
    assert result['viewport_drawn'] == result['content_ready'] == 0
    assert result['unknown'] > 0


def test_flip_crop_clip_actual_pixmap_and_required_pixels(canvas):
    host, ev = canvas()
    item = host.model().items[0]
    item.crop = QtCore.QRectF(128, 64, 128, 128)
    item.flip = lambda: -1
    frame = open_frame(host)
    out = output(host)
    painter = QtGui.QPainter(out)
    painter.setClipRect(QtCore.QRectF(0, 0, 20, 40))
    draw_card(host, painter, 0, QtCore.QRectF(0, 0, 40, 40))
    painter.end()
    proof.finish_frame(host, frame)
    regions = ev.last('asset_visibility_snapshot')['diagnostic_regions']
    assert len(regions) == 1
    entry = regions[0]
    assert entry['pixmap_size'] == [64, 64]  # The shared LOD resize is actually used.
    assert entry['source_crop'] == [16, 8, 16, 16]
    assert entry['required_crop_pixels'] == [40, 40]
    assert entry['transform'][0] < 0
    assert not entry['ready']
    assert max(point[0] for polygon in entry['effective_clip'] for point in polygon) == 20


def test_missing_pm_bool_and_placeholder_are_compatible(canvas, monkeypatch):
    host, ev = canvas()
    monkeypatch.setattr(lod, 'display_pixmap', lambda *a: None)
    frame = open_frame(host)
    out = output(host)
    painter = QtGui.QPainter(out)
    with proof.delegate_scope(host, host.cards[0], painter):
        assert lod.draw_image(painter, host.model().items[0], QtCore.QRectF(0, 0, 20, 20), host) is False
    painter.end()
    proof.finish_frame(host, frame)
    result = ev.last('asset_paint_frame')
    assert result['placeholder'] == 1
    assert result['viewport_drawn'] == result['content_ready'] == 0


def test_uninstrumented_delegate_is_unknown_not_content(canvas):
    host, ev = canvas()
    frame = open_frame(host)
    out = output(host)
    painter = QtGui.QPainter(out)
    with proof.delegate_scope(host, host.cards[0], painter):
        painter.drawText(QtCore.QPointF(5, 20), 'Loading preview')
    painter.end()
    proof.finish_frame(host, frame)
    assert ev.last('asset_paint_frame')['unknown'] == 1
    assert ev.last('asset_paint_frame')['viewport_drawn'] == 0


@pytest.mark.parametrize('changed', ['query', 'source', 'item_revision', 'crop', 'row_identity'])
def test_current_metadata_does_not_make_old_resource_ready(canvas, changed):
    host, ev = canvas()
    image = make_resource(host)
    if changed == 'query':
        ev.query_id = 'new-query'
    elif changed == 'source':
        ev.source_revision += 1
    elif changed == 'item_revision':
        host.model().items[0]._display_source_revision += 1
    elif changed == 'crop':
        host.model().items[0].crop = QtCore.QRectF(0, 0, 256, 256)
    else:
        host.model().items[0]._canvas_identity = ('replacement', 33)
    frame = open_frame(host)
    out = output(host)
    painter = QtGui.QPainter(out)
    proof.observe_image(host, painter, QtCore.QRectF(0, 0, 512, 512), image, category='tile')
    painter.end()
    proof.finish_frame(host, frame)
    result = ev.last('asset_paint_frame')
    assert result['viewport_drawn'] == 1
    assert result['content_ready'] == 0
    assert result['fallback'] == 1
    assert ev.last('asset_visibility_snapshot')['diagnostic_regions'][0]['stale'] is True


def test_pixel_revision_survives_metadata_change_and_lod_resize(canvas):
    host, ev = canvas()
    item = host.model().items[0]
    item._display_source_revision = 2
    frame = open_frame(host)
    out = output(host)
    painter = QtGui.QPainter(out)
    draw_card(host, painter, 0, QtCore.QRectF(0, 0, 40, 40))
    painter.end()
    proof.finish_frame(host, frame)
    entry = ev.last('asset_visibility_snapshot')['diagnostic_regions'][0]
    assert entry['actual_source_revision'] == 1
    assert entry['expected_item_revision'] == 2
    assert not entry['ready']


def test_ancestor_and_child_fallback_with_source_crop(canvas):
    host, ev = canvas()
    tiles = host._tiles
    image = make_resource(host)
    tiles._install((-1, 0, 0), image)
    frame = open_frame(host)
    out = output(host)
    painter = QtGui.QPainter(out)
    assert tiles._fallback(painter, (0, 0, 0), QtCore.QRectF(0, 0, 256, 256))
    assert frame.drawn == 1 and frame.ready == 0
    assert frame.entries[0][1]['resource_crop'] == [0, 0, 256, 256]
    painter.end()
    proof.finish_frame(host, frame)
    assert ev.last('asset_paint_frame')['categories']['ancestor'] == 1
    tiles.reset()
    tiles.revision = tiles.content_revision()
    tiles._install((1, 0, 0), image)
    frame = open_frame(host)
    painter = QtGui.QPainter(out)
    assert tiles._fallback(painter, (0, 0, 0), QtCore.QRectF(0, 0, 256, 256))
    painter.end()
    proof.finish_frame(host, frame)
    assert ev.last('asset_paint_frame')['categories']['child'] == 1


def test_tile_old_image_and_current_fade_are_separate(canvas, monkeypatch):
    host, ev = canvas()
    tiles = host._tiles
    image = make_resource(host)
    tiles._install((0, 0, 0), image)
    tiles.stale.add((0, 0, 0))
    new = make_resource(host)
    tiles._install((0, 0, 0), new)
    host.resetTransform()
    host.setSceneRect(0, 0, 500, 300)
    host.horizontalScrollBar().setValue(0)
    host.verticalScrollBar().setValue(0)
    monkeypatch.setattr(tiles, 'keys', lambda rect, level: [(0, 0, 0)])
    monkeypatch.setattr(tiles, '_start_cover', lambda *a: None)
    monkeypatch.setattr(tiles, '_fallback', lambda *a: False)
    monkeypatch.setattr(tiles, 'content_revision', lambda: tiles.revision)
    current, born, old, revision = tiles.tiles[(0, 0, 0)]
    tiles.tiles[(0, 0, 0)] = (current, time.monotonic()-.08, old, revision)
    frame = open_frame(host)
    out = output(host)
    painter = QtGui.QPainter(out)
    tiles.paint(painter)
    painter.end()
    proof.finish_frame(host, frame)
    result = ev.last('asset_paint_frame')
    assert result['categories']['old_tile'] == 1
    assert result['categories']['tile'] == 1
    assert result['content_ready'] == 0
    regions = ev.last('asset_visibility_snapshot')['diagnostic_regions']
    assert next(e for e in regions if e['category'] == 'old_tile')['stale']
    assert 0 < next(e for e in regions if e['category'] == 'tile')['opacity'] < 1
    assert len(tiles.tiles[(0, 0, 0)]) == 4


def test_parent_build_from_children_is_generation_not_viewport(canvas):
    host, ev = canvas()
    tiles = host._tiles
    for y in range(2):
        for x in range(2):
            tiles._install((1, x, y), make_resource(host))
    frame = open_frame(host)
    tiles._build((0, 0, 0))
    assert frame.drawn == 0
    proof.finish_frame(host, frame)
    assert ev.last('asset_cache_installed')['viewport_drawn'] == 0
    assert tiles.tiles[(0, 0, 0)][0]._asset_paint_resource.complete


def test_group_build_and_group_paint_separate_no_detail_claim(canvas, qapp):
    host, ev = canvas(4)
    host.parent().show()
    host.show()
    qapp.processEvents()
    group = host._composites
    group.groups = [(QtCore.QRectF(0, 0, 300, 300), list(host.cards))]
    group.row_group = {row: 0 for row in range(4)}
    group._content_key = group._content_key_for_view()
    host.scale(.1, .1)
    group.paint(None, QtCore.QRectF(0, 0, 300, 300))
    group.camera_changed = 0
    ev.events.clear()
    for _ in range(20):
        group._work()
        if not group.pending:
            break
    assert group.cache
    assert ev.last('asset_resource_generated')['viewport_drawn'] == 0
    assert ev.last('asset_cache_installed')['viewport_drawn'] == 0
    frame = open_frame(host)
    out = output(host)
    painter = QtGui.QPainter(out)
    group.paint(painter, QtCore.QRectF(0, 0, 300, 300))
    painter.end()
    proof.finish_frame(host, frame)
    result = ev.last('asset_paint_frame')
    assert result['categories']['group'] > 0
    assert result['viewport_drawn'] > 0
    assert result['content_ready'] == 0


def test_budget_overflow_and_production_have_no_fake_unique_success(canvas):
    ev = Evidence()
    ev.id_budget = 1
    host, ev = canvas(evidence=ev)
    frame = open_frame(host)
    out = output(host)
    painter = QtGui.QPainter(out)
    draw_card(host, painter, 0, QtCore.QRectF(0, 0, 20, 20))
    draw_card(host, painter, 1, QtCore.QRectF(20, 0, 20, 20))
    painter.end()
    proof.finish_frame(host, frame)
    snapshot = ev.last('asset_visibility_snapshot')
    assert snapshot['incomplete'] and len(snapshot['diagnostic_ids']) <= 1
    assert snapshot['content_ready'] == 0
    ev.diagnostic = False
    frame = open_frame(host, QtCore.QRect(0, 0, 20, 20))
    painter = QtGui.QPainter(out)
    draw_card(host, painter, 0, QtCore.QRectF(0, 0, 20, 20))
    painter.end()
    proof.finish_frame(host, frame)
    snapshot = ev.last('asset_visibility_snapshot')
    assert snapshot['viewport_drawn'] is None
    assert 'diagnostic_ids' not in snapshot and 'diagnostic_regions' not in snapshot
    assert ev.last('asset_paint_frame')['viewport_candidates'] is None


def test_cumulative_regions_repaint_replaces_not_accumulates_old_pixels(canvas):
    host, ev = canvas()
    card_rect = QtCore.QRect(0, 0, 20, 20)
    out = output(host)
    frame = open_frame(host)
    painter = QtGui.QPainter(out)
    draw_card(host, painter, 0, QtCore.QRectF(card_rect))
    painter.end()
    proof.finish_frame(host, frame)
    assert ev.last('asset_visibility_snapshot')['unique_viewport_drawn'] == 1
    frame = open_frame(host, card_rect)
    proof.observe_missing(host, QtGui.QPainter(), QtCore.QRectF())  # Inactive painter remains harmless.
    painter = QtGui.QPainter(out)
    proof.observe_missing(host, painter, QtCore.QRectF(card_rect))
    painter.end()
    proof.finish_frame(host, frame)
    assert ev.last('asset_visibility_snapshot')['unique_viewport_drawn'] == 0
    assert not ev.last('asset_paint_frame')['full_viewport_repaint']


def test_inactive_missing_and_broken_evidence_never_break_renderer(canvas, monkeypatch):
    host, ev = canvas()
    ev.active = False
    frame = open_frame(host)
    assert frame is None
    make_resource(host)
    assert not ev.events
    ev.active = True
    ev.fields = Mock(side_effect=RuntimeError('broken diagnostic observer'))
    assert open_frame(host) is None
    host._render_tile(output(host), QtCore.QRectF(0, 0, 300, 200), QtCore.QRectF(0, 0, 300, 200))
    host.model().presentation_evidence = None
    assert open_frame(host) is None
    monkeypatch.setattr(host, '_observe_asset_pixmap', Mock(side_effect=RuntimeError('observer failed')))
    out = output(host)
    painter = QtGui.QPainter(out)
    assert lod.draw_image(painter, host.model().items[0], QtCore.QRectF(0, 0, 20, 20), host) is True
    painter.end()
    assert out.pixelColor(10, 10).alpha() > 0


def test_query_changed_or_cancelled_midframe_cannot_publish_valid_result(canvas):
    host, ev = canvas()
    frame = open_frame(host)
    ev.query_id = 'superseded'
    proof.finish_frame(host, frame)
    assert not ev.events
    frame = open_frame(host)
    ev.active = False
    proof.finish_frame(host, frame)
    assert not ev.events


def test_legacy_marker_units_and_cache_quality_never_painted(canvas, monkeypatch):
    host, ev = canvas()
    calls = []
    class Rollup:
        def __init__(self, **kwargs):
            pass
        def count_frame(self, **fields):
            assert fields['quality'] == 'unknown'
        def due(self):
            return True
        def take(self):
            return dict(frames=1, cards=4)
    import prism.widgets.asset_canvas as module
    monkeypatch.setattr(module, '_PKG3_MARKER_API', (Rollup, lambda _: {},
        lambda event, **fields: calls.append((event, fields)), 'assets.paint_frame'))
    host._paint_quality = dict(visible=4, detailed=4, focus_ready=True)
    host._record_paint_frame(content=False, cards=4)
    assert calls[0][1]['cards_units'] == 'model_rows'
    assert not any(event.endswith('quality_painted') or event.endswith('quality_ready') for event, _ in calls)


def test_manifest_budget_is_query_wide_and_overflow_is_unknown(canvas):
    ev = Evidence()
    ev.id_budget = 1
    host, ev = canvas(evidence=ev)
    first = make_resource(host)
    second = make_resource(host, (1,))
    assert len(first._asset_paint_resource.entries) == 1
    assert second._asset_paint_resource.incomplete
    assert len(second._asset_paint_resource.entries) == 0
    frame = open_frame(host)
    out = output(host)
    painter = QtGui.QPainter(out)
    proof.observe_image(host, painter, QtCore.QRectF(0, 0, 512, 512), second)
    painter.end()
    proof.finish_frame(host, frame)
    assert ev.last('asset_paint_frame')['viewport_drawn'] == 0
    assert ev.last('asset_paint_frame')['unknown'] > 0
    assert ev.last('asset_visibility_snapshot')['incomplete']


def test_ready_sliver_is_not_whole_card_and_retained_metadata_is_revalidated(canvas):
    host, ev = canvas()
    card = host.cards[0]
    card._x = card._y = 0
    card._size = QtCore.QSizeF(20, 20)
    out = output(host)
    frame = open_frame(host)
    painter = QtGui.QPainter(out)
    draw_card(host, painter, 0, QtCore.QRectF(0, 0, 10, 20))
    painter.end()
    proof.finish_frame(host, frame)
    assert ev.last('asset_visibility_snapshot')['unique_content_ready'] == 0
    frame = open_frame(host)
    painter = QtGui.QPainter(out)
    draw_card(host, painter, 0, QtCore.QRectF(0, 0, 20, 20))
    painter.end()
    proof.finish_frame(host, frame)
    assert ev.last('asset_visibility_snapshot')['unique_content_ready'] == 1
    host.model().items[0]._display_source_revision = 2
    frame = open_frame(host, QtCore.QRect(80, 0, 10, 10))
    proof.finish_frame(host, frame)
    snapshot = ev.last('asset_visibility_snapshot')
    assert snapshot['unique_content_ready'] == 0
    assert snapshot['diagnostic_regions'][0]['stale']


def test_device_pixel_ratio_required_pixels_and_clip_are_physical(canvas, monkeypatch):
    host, ev = canvas()
    monkeypatch.setattr(host.viewport(), 'devicePixelRatioF', lambda: 2.)
    out = QtGui.QImage(host.viewport().width()*2, host.viewport().height()*2,
                       QtGui.QImage.Format.Format_ARGB32_Premultiplied)
    out.setDevicePixelRatio(2.)
    frame = open_frame(host, QtCore.QRect(0, 0, 10, 10))
    painter = QtGui.QPainter(out)
    draw_card(host, painter, 0, QtCore.QRectF(0, 0, 20, 20))
    painter.end()
    proof.finish_frame(host, frame)
    entry = ev.last('asset_visibility_snapshot')['diagnostic_regions'][0]
    assert entry['required_crop_pixels'] == [40, 40]
    assert max(p[0] for polygon in entry['effective_clip'] for p in polygon) == 20
    assert ev.last('asset_paint_frame')['geometry_units'] == 'device_pixels'


def test_annotation_and_nonstandard_composition_degrade_honestly(canvas):
    host, ev = canvas()
    host.scene().addRect(0, 0, 20, 20, brush=QtGui.QColor('black'))
    frame = open_frame(host)
    image = make_resource(host)
    assert image._asset_paint_resource.incomplete
    out = output(host)
    painter = QtGui.QPainter(out)
    painter.setCompositionMode(QtGui.QPainter.CompositionMode.CompositionMode_DestinationOver)
    draw_card(host, painter, 0, QtCore.QRectF(0, 0, 20, 20))
    painter.end()
    proof.finish_frame(host, frame)
    assert ev.last('asset_paint_frame')['unknown'] > 0
    assert not ev.last('asset_visibility_snapshot')['current_viewport_exact']


def test_nested_group_thumbnail_in_tile_stays_group_not_detail(canvas):
    host, ev = canvas()
    group_image = make_resource(host)
    tile = QtGui.QImage(512, 512, QtGui.QImage.Format.Format_ARGB32_Premultiplied)
    tile.fill(QtCore.Qt.GlobalColor.transparent)
    painter = QtGui.QPainter(tile)
    with proof.resource_scope(host, tile):
        painter.drawImage(QtCore.QRectF(0, 0, 512, 512), group_image)
        proof.observe_image(host, painter, QtCore.QRectF(0, 0, 512, 512), group_image, category='group')
    painter.end()
    proof.generated(host, tile, complete=True)
    frame = open_frame(host)
    out = output(host)
    painter = QtGui.QPainter(out)
    proof.observe_image(host, painter, QtCore.QRectF(0, 0, 512, 512), tile, category='tile')
    painter.end()
    proof.finish_frame(host, frame)
    assert ev.last('asset_paint_frame')['categories']['group'] == 1
    assert ev.last('asset_paint_frame')['content_ready'] == 0


def test_rotated_view_tile_target_is_unknown_even_with_rect_painter(canvas, monkeypatch):
    host, ev = canvas()
    image = make_resource(host)
    host._tiles._install((-1, 0, 0), image)
    monkeypatch.setattr(host, 'viewportTransform', lambda: QtGui.QTransform().rotate(17))
    frame = open_frame(host)
    out = output(host)
    painter = QtGui.QPainter(out)
    assert host._tiles._fallback(painter, (0, 0, 0), QtCore.QRectF(0, 0, 256, 256))
    painter.end()
    proof.finish_frame(host, frame)
    assert ev.last('asset_paint_frame')['viewport_drawn'] == 0
    assert ev.last('asset_paint_frame')['unknown'] > 0


def test_side_channel_preserves_offscreen_pixels_and_original_metadata(canvas):
    host, ev = canvas()
    image = QtGui.QImage(512, 512, QtGui.QImage.Format.Format_ARGB32_Premultiplied)
    image.fill(host.backgroundBrush().color())
    original_geometry = [host._source_geometry(item) for item in host.model().items]
    original_pixmaps = [item._preview_pixmap.cacheKey() for item in host.model().items]
    host._render_tile(image, QtCore.QRectF(image.rect()), QtCore.QRectF(0, 0, 512, 512))
    ev.active = False
    reference = QtGui.QImage(image.size(), image.format())
    reference.fill(host.backgroundBrush().color())
    host._render_tile(reference, QtCore.QRectF(reference.rect()), QtCore.QRectF(0, 0, 512, 512))
    assert image == reference
    assert original_geometry == [host._source_geometry(item) for item in host.model().items]
    assert original_pixmaps == [item._preview_pixmap.cacheKey() for item in host.model().items]


def test_real_shared_query_evidence_contract(canvas):
    from prism.presentation_evidence import QueryEvidence
    events = []
    def observer(event, **fields):
        events.append((event, fields))
    ev = QueryEvidence(scope={'kind': 'synthetic-d'}, source_revision=7,
                       diagnostic=True, id_budget=1000, observer=observer)
    host, ev = canvas(evidence=ev)
    frame = open_frame(host)
    out = output(host)
    painter = QtGui.QPainter(out)
    draw_card(host, painter, 0, QtCore.QRectF(0, 0, 20, 20))
    painter.end()
    proof.finish_frame(host, frame)
    result = next(fields for event, fields in events if event == 'asset_paint_frame')
    assert result['query_id'] == ev.query_id
    assert result['schema_version'] == 2 and result['presentation_schema_version'] == 1
    assert result['viewport_drawn'] == 1
    assert result['source_revision'] == 7
    assert result['actual_source_revision_histogram'] == {'1': 1}
    assert any(event == 'asset_visibility_snapshot' for event, _ in events)
    before = len(events)
    ev.active = False
    proof.finish_frame(host, open_frame(host))
    assert len(events) == before
