"""Independent D regressions: real catalog installation and real Qt pixels.

No production patches: provenance is installed by CatalogDisplayReader from
actual SQLite display_summaries, not manufactured credentials. Expected
unknown/stale assertions remain strict even when they expose a defect.
"""
import base64
import json
import sqlite3
import weakref
from types import SimpleNamespace

import pytest
from PyQt6 import QtCore, QtGui, QtWidgets

from prism import lod
from prism.asset_records import ImageRecord
from prism.catalog_browser import CatalogAssetProxy, CatalogDisplayReader, _DisplayQuery
from prism.project_catalog_index import CatalogNotReady
from tests.test_catalog_keyset_overlay import scope_for
from tests.test_d_asset_paint_evidence import canvas, open_frame, output, draw_card
from prism import asset_paint_evidence as proof


def thumbnail(color='red', width=64, height=32):
    image = QtGui.QImage(width, height, QtGui.QImage.Format.Format_RGB32)
    image.fill(QtGui.QColor(color))
    buffer = QtCore.QBuffer()
    assert buffer.open(QtCore.QIODevice.OpenModeFlag.WriteOnly)
    assert image.save(buffer, 'PNG')
    return [width, height, base64.b64encode(bytes(buffer.data())).decode('ascii')]


def overview(color='red', width=8, height=4):
    image = QtGui.QImage(width, height, QtGui.QImage.Format.Format_RGBA8888)
    image.fill(QtGui.QColor(color))
    raw = image.constBits().asstring(image.sizeInBytes())
    return [width, height, base64.b64encode(raw).decode('ascii')]


def assert_pixels(pixmap, color='red', width=None):
    assert pixmap is not None and not pixmap.isNull()
    assert pixmap.toImage().pixelColor(0, 0) == QtGui.QColor(color)
    if width is not None:
        assert pixmap.width() == width


def record(th=None, ov=None):
    return ImageRecord({'save_id': 999, 'data': {
        'imageWidth': 1024, 'imageHeight': 512,
        'displayThumbnail': th, 'displayOverview': ov}}, None)


class Owner(QtWidgets.QWidget):
    def __init__(self):
        super().__init__()
        self._model = SimpleNamespace(items=[])
        self._item_rows = {}
        self.batches = []

    def model(self):
        return self._model

    def needs_display(self, item):
        return item in self._model.items

    def catalog_rows_changed(self, rows):
        self.batches.append(tuple(rows))


@pytest.fixture
def isolated_lod(qapp, monkeypatch):
    store = lod.DisplayCache(qapp)
    monkeypatch.setattr(qapp, '_prism_lod_cache', store, raising=False)
    # A fallback test must not trigger original decoding in the background.
    monkeypatch.setattr('prism.image_decode.request_display', lambda *a, **kw: None)
    monkeypatch.setattr('prism.source_store.read_archive',
                        lambda *a, **kw: pytest.fail('Unexpected original read'))
    yield store
    store.entries.clear()
    store.overviews.clear()
    store.pixel_revisions.clear()
    store.bytes = store.overview_bytes = 0


@pytest.fixture
def pending_catalog(isolated_lod, qtbot, tmp_path):
    th, ov = thumbnail(), overview()
    scope = scope_for(tmp_path, [(1, 'pixmap', dict(
        title='verified red', canvasId='A', imageWidth=1024, imageHeight=512,
        displayThumbnail=th, displayOverview=ov))])
    scene = QtCore.QObject()
    scene.project_id = 'pixel-provenance-fixture'
    owner = Owner()
    qtbot.addWidget(owner)
    owner.show()
    reader = CatalogDisplayReader(scope, owner)
    # Match the browser's complete scalar projection; summaries() omits size.
    row = list(scope.iter_query())[0]
    assert (row['width'], row['height']) == (1024, 512)
    item = CatalogAssetProxy(row, scope.project_path, scene, reader)
    assert item._image_size == QtCore.QSize(1024, 512)
    owner._model.items = [item]
    owner._item_rows[id(item)] = 0
    bundle = SimpleNamespace(scope=scope, scene=scene, owner=owner,
                             reader=reader, item=item, thumbnail=th, overview=ov)
    yield bundle
    reader.timer.stop()
    owner.close()


@pytest.fixture
def catalog(pending_catalog, qtbot):
    bundle = pending_catalog
    bundle.reader.request(bundle.item, bundle.owner)
    qtbot.waitUntil(lambda: bundle.item._catalog_display_ready, timeout=10000)
    assert not bundle.item._catalog_display_failed
    assert bundle.owner.batches == [(0,)]
    assert bundle.item._catalog_thumbnail_provenance == (tuple(bundle.thumbnail), 0)
    assert bundle.item._catalog_overview_provenance == (tuple(bundle.overview), 0)
    return bundle


def verified_result(bundle):
    """Use the production query and real scope, delaying only GUI installation."""
    results = []
    job = _DisplayQuery(bundle.scope, [(bundle.item, weakref.ref(bundle.owner))], results.append)
    job.run()
    assert len(results) == 1 and results[0][2] is None
    assert results[0][1][0]['thumbnail'] == bundle.thumbnail
    return results[0]


def mutate_archive(bundle):
    with sqlite3.connect(bundle.scope.project_path) as db:
        extra = json.loads(db.execute('SELECT data FROM items WHERE id=1').fetchone()[0])
        extra['displayThumbnail'] = thumbnail('blue')
        extra['displayOverview'] = overview('blue')
        extra['title'] = 'changed source ' + 'x' * 10000
        db.execute('UPDATE items SET data=? WHERE id=1', (json.dumps(extra),))


def test_real_catalog_reader_installs_only_saved_payload_provenance(catalog):
    item = catalog.item
    pixmap = lod.thumbnail_pixmap(item, 64)
    assert_pixels(pixmap, width=64)
    assert lod.pixmap_source_revision(item, pixmap) == 0
    assert lod.thumbnail_pixmap(item, 64).cacheKey() == pixmap.cacheKey()
    assert item._graphics is None and item._source_blob is None
    print('CATALOG_RGB', json.dumps(dict(actual_source_revision=0, width=pixmap.width(),
                                        original_width=item._image_size.width())))


@pytest.mark.parametrize('kind', ['thumbnail', 'overview'])
def test_catalog_without_reader_credential_is_unknown(pending_catalog, kind):
    item = pending_catalog.item
    if kind == 'thumbnail':
        item._display_thumbnail = pending_catalog.thumbnail
        pixmap = lod.thumbnail_pixmap(item, 64)
    else:
        lod.restore_overview(item, pending_catalog.overview)
        pixmap = item._overview_pixmap
    assert_pixels(pixmap)
    assert lod.pixmap_source_revision(item, pixmap) is None


@pytest.mark.parametrize('kind', ['thumbnail', 'overview'])
def test_non_catalog_does_not_inherit_copied_catalog_credential(catalog, kind):
    item = record(catalog.thumbnail, catalog.overview)
    # Even an authentic credential copied off the installed proxy is insufficient.
    setattr(item, '_catalog_' + kind + '_provenance',
            getattr(catalog.item, '_catalog_' + kind + '_provenance'))
    if kind == 'thumbnail':
        pixmap = lod.thumbnail_pixmap(item, 64)
    else:
        lod.restore_overview(item, item._overview_payload)
        pixmap = item._overview_pixmap
    assert_pixels(pixmap)
    assert lod.pixmap_source_revision(item, pixmap) is None


@pytest.mark.parametrize('kind', ['thumbnail', 'overview'])
@pytest.mark.parametrize('mutation', ['replace_bytes', 'inplace_bytes', 'dimensions'])
def test_changed_payload_without_reinstallation_is_unknown(catalog, kind, mutation):
    item = catalog.item
    attr = '_display_thumbnail' if kind == 'thumbnail' else '_overview_payload'
    old = getattr(item, attr)
    make = thumbnail if kind == 'thumbnail' else overview
    replacement = make('blue', old[0] // 2, old[1] // 2) if mutation == 'dimensions' else make('blue')
    if mutation == 'inplace_bytes':
        old[2] = replacement[2]
    else:
        setattr(item, attr, replacement)
    value = getattr(item, attr)
    if kind == 'thumbnail':
        pixmap = lod.thumbnail_pixmap(item, 64)
    else:
        lod.restore_overview(item, value)
        pixmap = item._overview_pixmap
    assert_pixels(pixmap, 'blue')
    assert lod.pixmap_source_revision(item, pixmap) is None


@pytest.mark.parametrize('kind', ['thumbnail', 'overview'])
def test_equal_payload_copy_preserves_verified_revision(catalog, kind):
    item = catalog.item
    if kind == 'thumbnail':
        item._display_thumbnail = list(item._display_thumbnail)
        pixmap = lod.thumbnail_pixmap(item, 64)
    else:
        item._overview_payload = list(item._overview_payload)
        lod.restore_overview(item, item._overview_payload)
        pixmap = item._overview_pixmap
    assert_pixels(pixmap)
    assert lod.pixmap_source_revision(item, pixmap) == 0


@pytest.mark.parametrize('kind', ['thumbnail', 'overview'])
def test_source_replacement_cannot_relabel_old_saved_payload(catalog, kind):
    item = catalog.item
    old_value = list(catalog.thumbnail if kind == 'thumbnail' else catalog.overview)
    old_pm = lod.thumbnail_pixmap(item, 64)
    assert lod.pixmap_source_revision(item, old_pm) == 0
    item.set_source_blob(base64.b64decode(thumbnail('blue')[2]), 'png')
    assert item._display_source_revision == 1
    if kind == 'thumbnail':
        item._display_thumbnail = old_value
        pixmap = lod.thumbnail_pixmap(item, 64)
    else:
        item._overview_payload = old_value
        lod.restore_overview(item, old_value)
        pixmap = item._overview_pixmap
    assert_pixels(pixmap)
    assert lod.pixmap_source_revision(item, pixmap) is None
    assert lod.pixmap_source_revision(item, old_pm) == 0


@pytest.mark.parametrize('input_revision', [None, 0, 3])
@pytest.mark.parametrize('route', ['higher_level', 'preview', 'native'])
def test_internal_downsample_inherits_actual_input_not_current_metadata(
        isolated_lod, input_revision, route):
    item = record()
    item._display_source_revision = 7
    source = QtGui.QPixmap(512, 256)
    source.fill(QtGui.QColor('red'))
    if route == 'higher_level':
        isolated_lod.store(item, 512, source, notify=False, source_revision=input_revision)
    else:
        isolated_lod.remember_pixels(item, source, input_revision)
        setattr(item, '_preview_pixmap' if route == 'preview' else '_derived_pixmap', source)
    pixmap = lod.display_pixmap(item, 40)
    assert_pixels(pixmap, width=64)
    assert lod.pixmap_source_revision(item, pixmap) == input_revision
    assert lod.pixmap_source_revision(item, source) == input_revision
    assert lod.pixmap_source_revision(item, pixmap) != 7


@pytest.mark.parametrize('input_revision', [None, 0, 3])
@pytest.mark.parametrize('route', ['preview', 'native'])
def test_internal_overview_inherits_input_revision(isolated_lod, input_revision, route):
    item = record()
    item._display_source_revision = 7
    source = QtGui.QPixmap(512, 256)
    source.fill(QtGui.QColor('red'))
    isolated_lod.remember_pixels(item, source, input_revision)
    setattr(item, '_preview_pixmap' if route == 'preview' else '_derived_pixmap', source)
    pixmap = lod.overview_pixmap(item)
    assert_pixels(pixmap, width=8)
    assert lod.pixmap_source_revision(item, pixmap) == input_revision
    assert item._overview_revision == 7  # metadata bookkeeping is not pixel provenance.


def test_real_catalog_overview_restoration_and_cache_share_do_not_cross_items(catalog):
    item = catalog.item
    first = lod.overview_pixmap(item)
    assert_pixels(first, width=8)
    assert lod.pixmap_source_revision(item, first) == 0
    other = record(ov=list(catalog.overview))
    second = lod.overview_pixmap(other)
    assert_pixels(second, width=8)
    assert second.cacheKey() == first.cacheKey()
    assert lod.pixmap_source_revision(other, second) is None
    assert lod.pixmap_source_revision(item, first) == 0
    item._overview_pixmap = None
    restored = lod.overview_pixmap(item)
    assert restored.cacheKey() == first.cacheKey()
    assert lod.pixmap_source_revision(item, restored) == 0


@pytest.mark.parametrize('existing_revision', [None, -1])
def test_reader_install_does_not_bless_arbitrary_preexisting_pixels(pending_catalog, existing_revision):
    bundle = pending_catalog
    arbitrary = QtGui.QPixmap(512, 256)
    arbitrary.fill(QtGui.QColor('blue'))
    bundle.item._preview_pixmap = arbitrary
    lod.cache().remember_pixels(bundle.item, arbitrary, existing_revision)
    bundle.reader._ready(verified_result(bundle))
    assert lod.pixmap_source_revision(bundle.item, arbitrary) == existing_revision
    pixmap = lod.display_pixmap(bundle.item, 40)
    assert_pixels(pixmap, 'blue', width=64)
    assert lod.pixmap_source_revision(bundle.item, pixmap) == existing_revision


@pytest.mark.parametrize('change_at', ['before_read', 'during_read'])
def test_real_catalog_validation_rejects_changed_archive(pending_catalog, monkeypatch, change_at):
    bundle = pending_catalog
    if change_at == 'before_read':
        mutate_archive(bundle)
    else:
        original_ready = bundle.scope.index._ready
        calls = []
        def checked(connection):
            calls.append(1)
            if len(calls) == 2:
                mutate_archive(bundle)
            return original_ready(connection)
        monkeypatch.setattr(bundle.scope.index, '_ready', checked)
    results = []
    _DisplayQuery(bundle.scope, [(bundle.item, weakref.ref(bundle.owner))], results.append).run()
    assert results[0][2] is not None
    bundle.reader._ready(results[0])
    assert bundle.item._catalog_display_failed
    assert not bundle.item._catalog_display_ready
    assert getattr(bundle.item, '_catalog_thumbnail_provenance', None) is None
    assert getattr(bundle.item, '_catalog_overview_provenance', None) is None


@pytest.mark.parametrize('kind', ['thumbnail', 'overview'])
def test_late_reader_result_cannot_stamp_old_pixels_with_new_item_revision(pending_catalog, kind):
    bundle = pending_catalog
    result = verified_result(bundle)
    bundle.item.set_source_blob(base64.b64decode(thumbnail('blue')[2]), 'png')
    assert bundle.item._display_source_revision == 1
    bundle.reader._ready(result)
    if kind == 'thumbnail':
        pixmap = lod.thumbnail_pixmap(bundle.item, 64)
    else:
        lod.restore_overview(bundle.item, bundle.item._overview_payload)
        pixmap = bundle.item._overview_pixmap
    if pixmap is None:  # Rejecting a stale result is also safe.
        return
    assert_pixels(pixmap, 'red')
    actual = lod.pixmap_source_revision(bundle.item, pixmap)
    assert actual is None, ('Old verified red pixels were stamped at new blue source revision: '
                            f'actual={actual}, current={bundle.item._display_source_revision}, kind={kind}')


def test_archive_changed_after_verified_read_before_install_is_not_current(pending_catalog):
    bundle = pending_catalog
    result = verified_result(bundle)
    before = bundle.scope.source_state()
    mutate_archive(bundle)
    assert before != bundle.scope.source_state()
    with pytest.raises(CatalogNotReady):
        bundle.scope.display_summaries([1])
    bundle.reader._ready(result)
    pixmap = lod.thumbnail_pixmap(bundle.item, 64)
    if pixmap is None:
        return
    assert_pixels(pixmap, 'red')
    actual = lod.pixmap_source_revision(bundle.item, pixmap)
    assert actual is None, ('Stale red archive result certified after archive changed to blue: '
                            f'actual={actual}, current={bundle.item._display_source_revision}')


@pytest.mark.parametrize('width,expected_ready', [(32, True), (128, False)])
def test_actual_catalog_rgb_paint_current_is_not_resolution_ready(catalog, canvas, width, expected_ready):
    host, evidence = canvas(1)
    item = catalog.item
    host.model().items[0] = item
    card = host.cards[0]
    card._x = card._y = 0.0
    card._size = QtCore.QSizeF(width, width / 2)
    host.viewport().resize(500, 320)
    host.setAlignment(QtCore.Qt.AlignmentFlag.AlignLeft | QtCore.Qt.AlignmentFlag.AlignTop)
    host.setSceneRect(0, 0, 500, 320)
    host.resetTransform()
    item._preview_pixmap = lod.thumbnail_pixmap(item, 64)
    assert lod.pixmap_source_revision(item, item._preview_pixmap) == 0
    image = output(host)
    frame = open_frame(host)
    painter = QtGui.QPainter(image)
    try:
        draw_card(host, painter, 0, QtCore.QRectF(0, 0, width, width / 2))
    finally:
        painter.end()
    proof.finish_frame(host, frame)
    assert image.pixelColor(width // 2, width // 4) == QtGui.QColor('red')
    entry = evidence.last('asset_visibility_snapshot')['diagnostic_regions'][0]
    assert entry['actual_source_revision'] == entry['expected_item_revision'] == 0
    assert entry['revision_current'] is True
    assert entry['ready'] is expected_ready
    assert evidence.last('asset_paint_frame')['current_revision_coverage'] == 1
    assert evidence.last('asset_paint_frame')['content_ready'] == int(expected_ready)
    print('CATALOG_PAINT', json.dumps({key: entry[key] for key in
          ('actual_source_revision', 'expected_item_revision', 'pixmap_size',
           'required_crop_pixels', 'revision_current', 'ready')}))
