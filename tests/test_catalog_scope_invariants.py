"""Resident guards for the catalog-scope invariants (A-5, round 2).

First round's lesson, in one sentence: **performance numbers can be optimised,
but a drift in the *definition* (口径) is worse than being slow.** Nothing in
this file measures anything -- it pins the definitions:

======  ====================================================================
I-1     the catalog's canvas ids live inside the project's pages
I-2     record count == catalog ``assets`` rows == sum of the per-canvas counts
I-3     records / unique source images / decoded bytes are three separate numbers
I-4     ``max_rows`` is a total cap, ``limit`` is a page size
I-5     a string where a set belongs is rejected or handled -- never silently empty
I-6     the partial (scene) definition and the catalog definition never swap
G-1     the full open path really creates the catalog scope instance
G-2     the consumer really takes ``view._open_scope`` as its scope provider
======  ====================================================================

Every guard can be falsified
---------------------------
A guard that cannot fail is not a guard. Each invariant therefore carries a
**sentinel** test that breaks the thing on purpose (a corrupted catalog row, a
patched implementation, an injected bad answer) and asserts that the guard
notices. The sentinels are part of the suite on purpose: they keep the guards
from decaying into tautologies. The one-off falsification drill log (broken ->
red -> restored -> green, one line per invariant) is in ``coordination/W-A5.md``.

Read-only discipline
--------------------
The synthetic fixture is built in ``tmp_path`` and the catalog cache beside it
is the only thing ever written. The 51.81 GiB reference project is touched by
exactly one check, through ``mode=ro`` URIs only, and that check can be
skipped with ``PRISM_A5_SKIP_REAL=1``. The application is never asked to open
it: no GUI, no document load, no write path near it.

Scope note: the overlay arithmetic of ``ProjectAssetCatalog`` (how a session's
live objects replace disk rows) belongs to A-1. The guards that compare the
*sidebar* number therefore use a session **without live rows**, so that what
is measured is *which definition is used* -- not the overlay's arithmetic.
"""
from contextlib import closing
import inspect
import json
import os
from pathlib import Path
import shutil
import sqlite3

import pytest

from prism.export_service import catalog_ids, catalog_ids_for_canvas
from prism.project_catalog import ProjectAssetCatalog
from prism.project_catalog_index import ProjectCatalogIndex
from prism.project_open_scope import ProjectOpenScope


CANVAS_A = 'canvas-a'
CANVAS_B = 'canvas-b'
DEFAULT_CANVAS = 'default-canvas'
PAGE_IDS = (DEFAULT_CANVAS, CANVAS_A, CANVAS_B)

#: The synthetic project: ``(canvas_id, filename, tagged)`` per record.
#:
#: 8 records over **5 distinct source file names**, so the record count and the
#: unique-source count are two different numbers -- without that gap I-3
#: could not tell a swap apart (and the guard would be a tautology).
#:
#: The first record carries no ``canvasId`` at all: the catalog falls back to
#: ``default-canvas``, which is a real page of the fixture.
LAYOUT = (
    (None, 'd1.png', False),
    (CANVAS_A, 'a0.png', True),
    (CANVAS_A, 'a1.png', True),
    (CANVAS_A, 'shared.png', False),
    (CANVAS_A, 'shared.png', False),
    (CANVAS_B, 'b0.png', True),
    (CANVAS_B, 'shared.png', False),
    (CANVAS_B, 'shared.png', False),
)

#: The number of records the catalog must report.
RECORD_COUNT = len(LAYOUT)

#: The number of distinct source images those records point at.
UNIQUE_SOURCE_COUNT = len({filename for _canvas, filename, _tagged in LAYOUT})

#: The reference project and its catalog cache (read-only, never written).
REAL_PROJECT = Path(os.environ.get(
    'PRISM_A5_REAL_PROJECT', r'E:\Prism-stress-100k\Prism-100k-v1.prism'))
REAL_CATALOG = Path(os.environ.get(
    'PRISM_A5_REAL_CATALOG',
    str(Path(os.environ.get('APPDATA', '')) / 'Prism' / 'catalog'
        / 'Prism-100k-v1.prism.sqlite')))


# ── the fixture ──────────────────────────────────────────────────────────

def build_project(path):
    """A micro ``.prism``: three pages, eight records, five source images.

    The blobs are **not** valid images. Nothing in the catalog path decodes
    them, and I-3 asserts exactly that.
    """
    with closing(sqlite3.connect(path)) as connection:
        connection.executescript('''
            CREATE TABLE items(id INTEGER PRIMARY KEY,type TEXT,x,y,z,scale,
                               rotation,flip,data);
            CREATE TABLE sqlar(name TEXT,item_id INTEGER PRIMARY KEY,mode INTEGER,
                               mtime INTEGER,sz INTEGER,data BLOB);
            CREATE TABLE workspace_pages(id TEXT,kind TEXT,title TEXT);
        ''')
        for key, (canvas, filename, tagged) in enumerate(LAYOUT, start=1):
            extra = {'title': 'Asset %d' % key, 'imageWidth': 16, 'imageHeight': 16,
                     'filename': filename, 'tags': ['keep'] if tagged else [],
                     'rating': 0, 'notes': ''}
            if canvas is not None:
                extra['canvasId'] = canvas
            connection.execute('INSERT INTO items VALUES(?,?,?,?,?,?,?,?,?)',
                               (key, 'pixmap', key, 0, 0, 1, 0, 0,
                                json.dumps(extra)))
            blob = bytes([key % 251]) * (40 + key)
            connection.execute('INSERT INTO sqlar VALUES(?,?,0,0,?,?)',
                               (filename, key, len(blob), blob))
        for page_id in PAGE_IDS:
            connection.execute('INSERT INTO workspace_pages VALUES(?,?,?)',
                               (page_id, 'canvas', page_id))
        connection.commit()


@pytest.fixture(scope='module')
def project(tmp_path_factory):
    """The synthetic project, built once for the whole module."""
    source = tmp_path_factory.mktemp('invariants') / 'invariants.prism'
    build_project(source)
    return source


@pytest.fixture(scope='module')
def scope(project, tmp_path_factory):
    """The catalog scope over the synthetic project, built once."""
    instance = ProjectOpenScope(project, tmp_path_factory.mktemp('cache')
                                / 'catalog.sqlite')
    assert instance.build(batch_size=4).complete
    return instance


@pytest.fixture
def writable_scope(scope, tmp_path):
    """A **copy** of the catalog cache that a test may corrupt.

    The sentinels that break the catalog on purpose work on this copy: the
    shared cache stays clean, and the source project is never written.
    """
    copy = tmp_path / 'catalog-copy.sqlite'
    shutil.copyfile(scope.cache_path, copy)
    for suffix in ('-wal', '-shm'):
        sidecar = Path(str(scope.cache_path) + suffix)
        if sidecar.exists():
            shutil.copyfile(sidecar, Path(str(copy) + suffix))
    return ProjectOpenScope(scope.project_path, copy)


class _FakeItem:
    """A scene item with the attributes the counters read."""

    def __init__(self, save_id, canvas_id, tags=()):
        self.save_id = save_id
        self.TYPE = 'pixmap'
        self._canvas_id = canvas_id
        self._tags = list(tags)
        self._rating = 0
        self._title = 'Fake %s' % save_id
        self._notes = ''
        self.filename = 'fake-%s.png' % save_id
        self._deleted_at = None

    def isVisible(self):
        return True


class _FakeScene:
    """A scene stand-in: no Qt, no editable state, only the counters' inputs."""

    def __init__(self, items=(), pages=PAGE_IDS, partial=None):
        self.project_id = 'fake-project'
        self._items = list(items)
        self.workspace_pages = [dict(id=page_id, kind='canvas', title=page_id)
                                for page_id in pages]
        self._partial_project_source = partial
        self._partial_project_session = None

    def iter_assets(self):
        return iter(self._items)

    def items(self):
        return iter(self._items)


@pytest.fixture
def scene_items():
    """Three live assets: the scene definition answers 3, the catalog 8."""
    return [_FakeItem(101, CANVAS_A), _FakeItem(102, CANVAS_A),
            _FakeItem(103, CANVAS_B)]


@pytest.fixture
def catalog(scope):
    """The catalog service with a **ready** directory and an idle session."""
    scene = _FakeScene()
    return ProjectAssetCatalog(scene, None, scope_provider=lambda: scope)


@pytest.fixture
def paged_catalog(scope):
    """A partially loaded session **with** a ready directory."""
    scene = _FakeScene(partial='/somewhere/project.prism')
    return ProjectAssetCatalog(scene, None, scope_provider=lambda: scope)


@pytest.fixture
def partial_catalog(scene_items):
    """A partially loaded session **without** a directory: the counts are unknown."""
    scene = _FakeScene(scene_items, partial='/somewhere/project.prism')
    return ProjectAssetCatalog(scene, None, scope_provider=lambda: None)


@pytest.fixture
def full_catalog(scene_items):
    """A fully loaded session **without** a directory: the scene is the truth."""
    scene = _FakeScene(scene_items)
    return ProjectAssetCatalog(scene, None, scope_provider=lambda: None)


# ── the checkers the guards share ─────────────────────────────────────────
#
# The sentinels call the very same checker as the guard, so a sentinel that
# passes means the guard can really go red.

def _assert_one_number(numbers):
    """Every named number must be the same integer; ``None`` is a violation.

    ``None`` is a violation on purpose: a count that cannot be produced is
    "unknown", and "unknown" must never be silently rendered as a number.
    """
    unavailable = sorted(name for name, value in numbers.items()
                         if not isinstance(value, int) or isinstance(value, bool))
    assert not unavailable, (
        'count口径 is unavailable (it must be an int, never None): %s'
        % ', '.join(unavailable))
    distinct = sorted(set(numbers.values()))
    assert len(distinct) == 1, (
        'count口径 drifted, the numbers disagree: %r' % numbers)


def _assert_canvas_ids_inside_pages(catalog_ids, page_ids):
    outside = sorted(set(catalog_ids) - set(page_ids))
    assert not outside, (
        'catalog canvas ids the project does not have: %r' % outside)


def _assert_record_count(scope, expected):
    actual = scope.count()
    assert actual == expected, (
        'record count drifted: %r != %r' % (actual, expected))


def _assert_capped(rows, cap):
    assert len(rows) <= cap, (
        'the read returned more rows than the cap: %d > %d' % (len(rows), cap))


def _assert_page_size(rows, expected_total):
    assert len(rows) == expected_total, (
        'limit became a total cap: %d rows instead of %d'
        % (len(rows), expected_total))


def _assert_selects_the_canvas(rows, canvas_id, expected):
    """``rows`` are catalog rows (dicts)."""
    assert rows, ('a string canvas id selected nothing -- the silent empty answer')
    assert {row['canvas_id'] for row in rows} == {canvas_id}
    assert len(rows) == expected


def _assert_result_canvas(rows, canvas_id, expected):
    """``rows`` are ``AssetResult`` objects (the scene-side catalog service)."""
    assert rows, ('a string canvas scope selected nothing -- the silent empty answer')
    assert {row.canvas_id for row in rows} == {canvas_id}
    assert len(rows) == expected


# ── read-only helpers ────────────────────────────────────────────────────

def _read_rows(path, sql, parameters=()):
    """Read from a database through a **read-only** URI. Never writes."""
    uri = Path(path).as_uri() + '?mode=ro'
    connection = sqlite3.connect(uri, uri=True)
    try:
        connection.row_factory = sqlite3.Row
        return [dict(row) for row in connection.execute(sql, parameters)]
    finally:
        connection.close()


def _catalog_rows(scope):
    return _read_rows(scope.cache_path, 'SELECT COUNT(*) AS n FROM assets')[0]['n']


def _catalog_canvas_ids(scope):
    return {row['canvas_id'] for row in
            _read_rows(scope.cache_path, 'SELECT DISTINCT canvas_id FROM assets')}


def _catalog_filenames(scope):
    return {row['filename'] for row in
            _read_rows(scope.cache_path, 'SELECT DISTINCT filename FROM assets')}


def _project_page_ids(project):
    return {row['id'] for row in
            _read_rows(project, 'SELECT id FROM workspace_pages')}


def _insert_catalog_row(scope, row_id, canvas_id):
    """Write one row into the **cache** (a tmp copy, never a real project)."""
    connection = sqlite3.connect(scope.cache_path)
    try:
        connection.execute(
            'INSERT INTO assets(id,kind,canvas_id,title,group_id,width,height,'
            'has_preview,has_original,tags,rating,notes,filename,deleted_at) '
            'VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
            (row_id, 'pixmap', canvas_id, 'Injected', None, 16, 16, 0, 0,
             '[]', 0, '', 'injected-%s.png' % row_id, None))
        connection.commit()
    finally:
        connection.close()


# ══════════════════════════════════════════════════════════════════════════
# I-1: the catalog's canvas ids live inside the project's pages
#
# Two coordinate systems meet here: the catalog stores the ``canvasId`` of
# every record, the project stores its pages in ``workspace_pages``. If they
# ever stop agreeing, "open one page" and "open everything" answer different
# questions -- the failure mode this project already paid for once.
# ══════════════════════════════════════════════════════════════════════════

def test_catalog_canvas_ids_are_a_subset_of_the_project_pages(scope, project):
    """I-1: every catalog canvas id exists in the source project."""
    catalog_ids = _catalog_canvas_ids(scope)
    page_ids = _project_page_ids(project)
    assert catalog_ids, 'the fixture must hold catalog rows'
    assert page_ids, 'the fixture must hold project pages'
    _assert_canvas_ids_inside_pages(catalog_ids, page_ids)
    # The same set, read through the scope API the sidebar uses.
    _assert_canvas_ids_inside_pages(set(scope.canvas_counts()), page_ids)


def test_a_catalog_canvas_id_outside_the_project_is_detected(writable_scope, project):
    """I-1 sentinel: a canvas id the project does not have must be caught.

    Broken on purpose: one catalog row on a canvas that does not exist. The
    same checker the guard above uses must go red.
    """
    page_ids = _project_page_ids(project)
    _assert_canvas_ids_inside_pages(_catalog_canvas_ids(writable_scope), page_ids)
    _insert_catalog_row(writable_scope, 9001, 'ghost-canvas')
    assert 'ghost-canvas' in _catalog_canvas_ids(writable_scope), (
        'the injected row did not reach the catalog -- the sentinel is vacuous')
    with pytest.raises(AssertionError):
        _assert_canvas_ids_inside_pages(_catalog_canvas_ids(writable_scope),
                                        page_ids)


def test_the_real_project_catalog_canvas_ids_are_inside_the_project_pages():
    """I-1 on the 51.81 GiB reference project: read-only, and a strict subset.

    The reference project is opened through ``mode=ro`` URIs only -- it is
    never opened for writing, never opened as an application document, and
    never scanned in full: only ``workspace_pages.id`` and the catalog's
    ``canvas_id`` column are read.

    Skipped with ``PRISM_A5_SKIP_REAL=1`` or when the reference files are not
    on this machine.
    """
    if os.environ.get('PRISM_A5_SKIP_REAL') == '1':
        pytest.skip('PRISM_A5_SKIP_REAL=1')
    if not REAL_PROJECT.exists() or not REAL_CATALOG.exists():
        pytest.skip('the reference project or its catalog cache is not on this machine')
    catalog_ids = {row['canvas_id'] for row in _read_rows(
        REAL_CATALOG, 'SELECT DISTINCT canvas_id FROM assets')}
    page_ids = {row['id'] for row in _read_rows(
        REAL_PROJECT, 'SELECT id FROM workspace_pages')}
    assert catalog_ids, 'the reference catalog must hold rows'
    assert page_ids, 'the reference project must hold pages'
    _assert_canvas_ids_inside_pages(catalog_ids, page_ids)


# ══════════════════════════════════════════════════════════════════════════
# I-2: record count == catalog rows == sum of the per-canvas counts
#
# The sidebar's "all assets" number, the catalog's row count and the
# per-canvas breakdown are three readings of one question. If they drift, the
# sidebar lies about the size of the project.
# ══════════════════════════════════════════════════════════════════════════

def test_the_record_count_equals_the_catalog_rows_and_the_canvas_breakdown(scope):
    """I-2: 记录数 == 目录 assets 行数 == 各画布分项计数之和."""
    breakdown = scope.canvas_counts()
    _assert_one_number({'catalog assets rows': _catalog_rows(scope),
                         'catalog total': scope.total(),
                         'sum of the per-canvas counts': sum(breakdown.values())})
    # The breakdown really is per canvas, and it really covers every canvas.
    assert set(breakdown) == set(_catalog_canvas_ids(scope))


def test_the_sidebar_total_equals_the_catalog_total(scope, catalog):
    """I-2: the sidebar's "all assets" number is the catalog's number."""
    snapshot = catalog.count_snapshot()
    _assert_one_number({'catalog assets rows': _catalog_rows(scope),
                         'sidebar total': snapshot['total'],
                         'sum of the per-canvas counts':
                             sum(scope.canvas_counts().values())})


def test_a_catalog_row_the_counters_do_not_see_is_detected(writable_scope):
    """I-2 sentinel: a catalog row the counters do not see must be caught."""
    _assert_one_number({'catalog assets rows': _catalog_rows(writable_scope),
                         'catalog total': writable_scope.total(),
                         'sum of the per-canvas counts':
                             sum(writable_scope.canvas_counts().values())})
    _insert_catalog_row(writable_scope, 9002, CANVAS_A)
    assert _catalog_rows(writable_scope) == RECORD_COUNT + 1, (
        'the injected row did not reach the catalog -- the sentinel is vacuous')
    with pytest.raises(AssertionError):
        _assert_one_number({'catalog assets rows': _catalog_rows(writable_scope),
                             'catalog total': writable_scope.total(),
                             'sum of the per-canvas counts':
                                 sum(writable_scope.canvas_counts().values())})


def test_an_undercounted_directory_aggregate_is_detected(scope, catalog,
                                                          monkeypatch):
    """I-2 sentinel: a directory aggregate that under-counts must be caught.

    The broken implementation drops one bucket from the aggregate -- the shape
    of "the aggregate and the rows disagree".
    """
    groups = scope.grouped_counts()
    assert len(groups) > 1, 'the fixture must hold more than one bucket'
    dropped = groups[0][3]
    original = ProjectOpenScope.grouped_counts

    def short(self, **kwargs):
        return tuple(original(self, **kwargs)[1:])

    monkeypatch.setattr(ProjectOpenScope, 'grouped_counts', short)
    snapshot = catalog.count_snapshot()
    assert isinstance(snapshot.get('total'), int), (
        'the catalog count path must be available for this sentinel: %r' % snapshot)
    assert snapshot['total'] == RECORD_COUNT - dropped, (
        'the injected aggregate did not under-count: %r' % snapshot)
    with pytest.raises(AssertionError):
        _assert_one_number({'catalog assets rows': _catalog_rows(scope),
                             'sidebar total': snapshot['total']})


# ══════════════════════════════════════════════════════════════════════════
# I-3: records / unique source images / decoded bytes are three numbers
#
# 100 000 records built from ~2 700 distinct pictures is exactly the shape
# that turns "100k assets" into "100k images to decode". The three numbers
# must never be substituted for one another, and a number nobody measured
# must never be invented.
# ══════════════════════════════════════════════════════════════════════════

def test_the_catalog_counts_records_not_unique_sources(scope):
    """I-3: the catalog counts records; the source-image count is smaller."""
    _assert_record_count(scope, RECORD_COUNT)
    sources = _catalog_filenames(scope)
    assert sources, 'the fixture must hold source names'
    assert RECORD_COUNT > len(sources), (
        'the fixture must repeat source images, otherwise the record count and '
        'the source count are the same number and a swap cannot be seen')
    assert RECORD_COUNT != len(sources)


def test_a_source_count_answered_as_the_record_count_is_detected(scope, monkeypatch):
    """I-3 sentinel: 有人把源图数当记录数返回 must be caught."""
    sources = _catalog_filenames(scope)
    monkeypatch.setattr(ProjectOpenScope, 'count',
                        lambda self, **kwargs: len(sources))
    assert scope.count() == len(sources), (
        'the injected implementation did not take over -- the sentinel is vacuous')
    with pytest.raises(AssertionError):
        _assert_record_count(scope, RECORD_COUNT)


def _deny_source_blob_reads(monkeypatch):
    """Deny every read of the source blob column; report what was denied."""
    denied = []
    original = ProjectCatalogIndex._source

    def guarded(self):
        connection = original(self)

        def authorizer(action, arg1, arg2, db_name, trigger_name):
            if action == sqlite3.SQLITE_READ and arg1 == 'sqlar' and arg2 == 'data':
                denied.append((arg1, arg2))
                return sqlite3.SQLITE_DENY
            return sqlite3.SQLITE_OK

        connection.set_authorizer(authorizer)
        return connection

    monkeypatch.setattr(ProjectCatalogIndex, '_source', guarded)
    return denied


def test_the_catalog_never_reads_the_source_bytes(project, tmp_path, monkeypatch):
    """I-3: 没有解码就绝不能报解码字节数.

    The catalog build is allowed to ask *whether* a record has an original
    (``has_original``), but it must never read the bytes themselves: a layer
    that never reads the bytes cannot report a decoded-byte number.

    The check is an SQLite authorizer that denies reads of ``sqlar.data``.
    The build must still succeed.
    """
    denied = _deny_source_blob_reads(monkeypatch)
    instance = ProjectOpenScope(project, tmp_path / 'denied.sqlite')
    result = instance.build(batch_size=4)
    assert result.complete, 'the build must work without the blob column'
    assert result.total_records == RECORD_COUNT
    assert denied == [], ('the catalog build read the source blob column: %r' % denied)


def test_the_blob_denial_is_effective(project, tmp_path, monkeypatch):
    """I-3 sentinel: the denial must really deny, or the guard is vacuous."""
    denied = _deny_source_blob_reads(monkeypatch)
    instance = ProjectOpenScope(project, tmp_path / 'denied.sqlite')
    connection = instance.index._source()
    try:
        with pytest.raises(sqlite3.DatabaseError):
            connection.execute('SELECT data FROM sqlar LIMIT 1').fetchall()
    finally:
        connection.close()
    assert denied, 'the authorizer did not report the denied read'


def test_the_catalog_has_no_decoded_byte_channel(scope):
    """I-3: the catalog has no measurement channel for decoded bytes.

    Three facts together mean "no decoded-byte number can be invented here":
    the cache carries no byte column, ``has_original`` is a 0/1 flag rather
    than a size, and the read statistics carry no byte figure.
    """
    columns = {row['name'] for row in
               _read_rows(scope.cache_path, 'PRAGMA table_info(assets)')}
    assert columns, 'the catalog cache must have an assets table'
    byte_columns = sorted(name for name in columns
                          if 'byte' in name.lower() or 'size' in name.lower())
    assert not byte_columns, (
        'the catalog cache must not carry a byte figure: %r' % byte_columns)
    flags = {row['has_original'] for row in
             _read_rows(scope.cache_path, 'SELECT DISTINCT has_original FROM assets')}
    assert flags <= {0, 1}, ('has_original is a flag, not a size: %r' % flags)
    list(scope.iter_query(limit=3))
    stats = scope.last_read_stats
    assert stats, 'the read must publish its statistics'
    byte_keys = sorted(key for key in stats if 'byte' in key.lower())
    assert not byte_keys, (
        'the read statistics must not carry a decoded-byte figure: %r' % byte_keys)


# ══════════════════════════════════════════════════════════════════════════
# I-4: ``max_rows`` is a total cap, ``limit`` is a page size
#
# The contract (A-2): ``iter_query(max_rows=N)`` hands at most N rows to the
# caller, while ``limit`` stays the *page* size -- the export path depends on
# the full-volume return.
# ══════════════════════════════════════════════════════════════════════════

def _max_rows_landed():
    return 'max_rows' in inspect.signature(
        ProjectOpenScope.iter_query).parameters


def test_max_rows_caps_the_total_number_of_rows(scope):
    """I-4: ``max_rows`` is a cap on the total, not on the page."""
    if not _max_rows_landed():
        pytest.skip('A-2 max_rows 未落地')
    rows = list(scope.iter_query(limit=3, max_rows=5))
    assert len(rows) == 5
    stats = scope.last_read_stats
    assert stats['returned'] == 5
    assert stats['max_rows'] == 5
    assert stats['truncated'] is True
    assert stats['limit_respected'] is True
    _assert_capped(rows, 5)


def test_max_rows_above_the_hit_count_answers_the_same_rows(scope):
    """I-4: a cap above the hit count must not change the answer.

    This is also A-1's correctness red line: the result set must be item by
    item the same as the read without a cap.
    """
    if not _max_rows_landed():
        pytest.skip('A-2 max_rows 未落地')
    everything = [row['id'] for row in scope.iter_query(limit=3)]
    capped = [row['id'] for row in scope.iter_query(limit=3, max_rows=RECORD_COUNT)]
    assert len(everything) == RECORD_COUNT
    assert capped == everything


def test_limit_stays_a_page_size_not_a_total_cap(scope):
    """I-4: ``limit`` is the page size; the export path needs all rows."""
    rows = list(scope.iter_query(limit=3))
    _assert_page_size(rows, RECORD_COUNT)
    assert len(catalog_ids(scope)) == RECORD_COUNT, (
        'the export path needs the whole set -- limit must stay a page size')
    assert len(catalog_ids_for_canvas(scope, CANVAS_A)) == 4


def test_an_ignored_max_rows_is_detected(scope, monkeypatch):
    """I-4 sentinel: a cap that is silently ignored must be caught."""
    if not _max_rows_landed():
        pytest.skip('A-2 max_rows 未落地')
    original = ProjectOpenScope.iter_query

    def ignoring(self, **kwargs):
        kwargs.pop('max_rows', None)
        return original(self, **kwargs)

    monkeypatch.setattr(ProjectOpenScope, 'iter_query', ignoring)
    rows = list(scope.iter_query(limit=3, max_rows=5))
    assert len(rows) == RECORD_COUNT, (
        'the injected implementation ignored the cap -- the sentinel is vacuous')
    with pytest.raises(AssertionError):
        _assert_capped(rows, 5)


def test_a_total_cap_on_limit_is_detected(scope, monkeypatch):
    """I-4 sentinel: ``limit`` turned into a total cap must be caught."""
    original = ProjectOpenScope.iter_query

    def capped(self, **kwargs):
        limit = kwargs.get('limit')
        rows = []
        for row in original(self, **kwargs):
            rows.append(row)
            if limit is not None and len(rows) >= limit:
                break
        return iter(rows)

    monkeypatch.setattr(ProjectOpenScope, 'iter_query', capped)
    rows = list(scope.iter_query(limit=3))
    assert len(rows) == 3, (
        'the injected implementation did not cap the total -- the sentinel is vacuous')
    with pytest.raises(AssertionError):
        _assert_page_size(rows, RECORD_COUNT)


# ══════════════════════════════════════════════════════════════════════════
# I-5: a string where a set belongs must never answer "nothing"
#
# The historical bug: ``tuple('stress-canvas-0000')`` splits the id into its
# characters, the filter becomes ``canvas_id IN ('s','t','r',...)`` and the
# read answers "zero rows" **without an error** -- the one answer that is
# worse than an exception.
# ══════════════════════════════════════════════════════════════════════════

def test_a_string_canvas_id_is_one_canvas_not_its_characters(scope):
    """I-5: a string canvas id selects that canvas, at both layers."""
    rows = list(scope.iter_query(canvas_ids=CANVAS_A, limit=100))
    _assert_selects_the_canvas(rows, CANVAS_A, 4)
    assert scope.last_read_stats['filters']['canvas_ids'] == (CANVAS_A,)
    assert len(scope.query(canvas_ids=CANVAS_A, limit=100)) == 4
    assert scope.count(canvas_ids=CANVAS_A) == 4
    # The catalog layer has its own guard.
    where, params = ProjectCatalogIndex._predicate(canvas_ids=CANVAS_A)
    assert 'canvas_id IN' in where
    assert params == [CANVAS_A], 'one id, not one parameter per character'
    _assert_selects_the_canvas(scope.index.query(canvas_ids=CANVAS_A),
                                CANVAS_A, 4)


def test_a_string_canvas_id_split_into_characters_is_detected(scope, monkeypatch):
    """I-5 sentinel: the historical ``tuple()`` split must be caught."""
    monkeypatch.setattr('prism.project_open_scope._as_canvas_ids',
                        lambda canvas_ids: tuple(canvas_ids))
    rows = list(scope.iter_query(canvas_ids=CANVAS_A, limit=100))
    assert rows == [], (
        'the injected implementation did not split the string -- the sentinel '
        'is vacuous')
    with pytest.raises(AssertionError):
        _assert_selects_the_canvas(rows, CANVAS_A, 4)


def test_the_index_layer_string_split_is_detected(scope, monkeypatch):
    """I-5 sentinel: the catalog layer's own guard must be caught too."""
    monkeypatch.setattr(ProjectCatalogIndex, '_canvas_id_tuple',
                        staticmethod(lambda canvas_ids: tuple(canvas_ids)))
    rows = scope.index.query(canvas_ids=CANVAS_A)
    assert rows == (), (
        'the injected implementation did not split the string -- the sentinel '
        'is vacuous')
    with pytest.raises(AssertionError):
        _assert_selects_the_canvas(rows, CANVAS_A, 4)


def test_a_canvas_scope_set_selects_that_canvas(catalog):
    """I-5: the scene-side catalog service takes a set of canvas ids."""
    rows = catalog.query(canvas_scope=[CANVAS_A])
    _assert_result_canvas(rows, CANVAS_A, 4)


def test_a_string_canvas_scope_is_not_silently_empty(catalog):
    """I-5: the scene-side catalog service must not answer "nothing".

    All three layers have to agree on the string rule: the scope layer
    (``project_open_scope._as_canvas_ids``), the catalog layer
    (``project_catalog_index._canvas_id_tuple``) and the scene-side service
    (``project_catalog._as_canvas_ids``). This case was written as an ``xfail``
    while the third layer was still missing the guard; it is a plain
    assertion again now that the third layer agrees.
    """
    rows = catalog.query(canvas_scope=CANVAS_A)
    _assert_result_canvas(rows, CANVAS_A, 4)


# ══════════════════════════════════════════════════════════════════════════
# I-6: the partial (scene) definition and the catalog definition never swap
#
# Under a partial load the two definitions are *supposed* to differ. What
# must never happen is that one is returned where the other belongs: the
# scene count is not a whole-project answer, and the catalog count is not a
# session answer.
# ══════════════════════════════════════════════════════════════════════════

def test_a_partial_session_without_a_catalog_reports_unknown_not_a_scene_count(
        partial_catalog, scene_items):
    """I-6: a loaded page is not a whole-project answer -- it is "unknown"."""
    snapshot = partial_catalog.count_snapshot()
    assert snapshot['total'] is None, (
        'a partial session without a catalog must answer "unknown", not %r'
        % snapshot['total'])
    assert partial_catalog.total_count() is None
    assert partial_catalog.canvas_counts() is None
    assert partial_catalog.untagged_count() is None
    # The scene definition would have answered 3 -- the difference is real.
    assert partial_catalog._scene_count_snapshot()['total'] == len(scene_items)


def test_a_full_session_without_a_catalog_uses_the_scene_count(full_catalog,
                                                               scene_items):
    """I-6: with no catalog and a full load, the scene *is* the project."""
    snapshot = full_catalog.count_snapshot()
    assert snapshot['total'] == len(scene_items)
    assert full_catalog.total_count() == len(scene_items)


def test_the_catalog_count_is_used_when_the_catalog_is_ready(paged_catalog,
                                                             scope):
    """I-6: in a partial session the catalog definition answers, not the scene."""
    snapshot = paged_catalog.count_snapshot()
    assert snapshot['total'] == RECORD_COUNT, (
        'a partial session with a ready catalog must answer the catalog number')
    # The scene definition would have answered 0 for this idle session.
    assert paged_catalog._scene_count_snapshot()['total'] == 0
    assert snapshot['total'] != paged_catalog._scene_count_snapshot()['total']


def test_a_scene_count_answered_where_the_catalog_count_belongs_is_detected(
        catalog, partial_catalog, scene_items, monkeypatch):
    """I-6 sentinel: a scene count answered as a catalog count must be caught.

    The injected implementation ignores the partial-load guard and always
    answers the scene count -- the exact swap the invariant forbids.
    """
    monkeypatch.setattr(
        ProjectAssetCatalog, '_counts_with_scope',
        lambda self, scope: self._scene_count_snapshot())
    assert partial_catalog.count_snapshot()['total'] == len(scene_items), (
        'the injected implementation did not take over -- the sentinel is vacuous')
    assert partial_catalog.count_snapshot()['total'] is not None
    # The other direction: the catalog number must not be answered as the
    # scene number of a session that has no live rows.
    assert catalog.count_snapshot()['total'] == 0, (
        'the injected implementation did not take over -- the sentinel is vacuous')
    assert catalog.count_snapshot()['total'] != RECORD_COUNT


# ══════════════════════════════════════════════════════════════════════════
# G-1 / G-2: the two links that decide which口径 the sidebar uses
#
# The chain that makes the catalog口径 reachable on the **default** (full)
# open path is three links long:
#
#   link 1  view.open_from_file()  ->  view._catalog_scope_instance(filename)
#            ->  view._open_scope = scope
#   link 2  media_library builds the catalog service with
#            scope_provider=lambda: getattr(self.view, '_open_scope', None)
#   link 3  ProjectAssetCatalog._catalog_scope() -> ready -> catalog口径
#
# Link 3 is pinned by ``tests/test_view_catalog_scope_wiring.py``. Links 1
# and 2 were **not**: that file calls the helper directly and hand-rolls its
# own provider, so deleting ``view.py``'s call or clearing the provider in
# ``media_library.py`` leaves all four of its cases green. These guards close
# that hole -- they are the difference between "4 passed" and "the wiring is
# really there".
#
# Both guards are **behaviour level**: they drive the production code paths
# (``view.open_from_file`` and the panel's real catalog service). Neither
# touches the 51.81 GiB reference project: the fixture is a tmp ``.prism``
# and the reader is replaced by a no-op.
# ══════════════════════════════════════════════════════════════════════════

def _assert_open_path_creates_the_scope(calls, filename):
    """The full open path must create the catalog scope for this project."""
    assert calls, ('the full open path never created a catalog scope instance '
                   '-- the catalog口径 is unreachable on the default path')
    assert list(calls) == [filename], (
        'the scope instance was created for the wrong project: %r' % (calls,))


def _assert_scope_provider_reads_the_view(panel):
    """The consumer's provider must read ``view._open_scope`` -- live."""
    provider = getattr(panel.catalog, '_scope_provider', None)
    assert provider is not None, (
        'the catalog service was built without a scope provider -- the '
        'catalog口径 can never be reached')
    from types import SimpleNamespace
    sentinel = SimpleNamespace(ready=True)
    panel.view._open_scope = sentinel
    assert provider() is sentinel, (
        'the provider does not return view._open_scope')
    panel.view._open_scope = None
    assert provider() is None, (
        'the provider caches the scope it saw earlier instead of reading the '
        'view every time')


def test_the_full_open_path_creates_the_catalog_scope_instance(main_window,
                                                                project,
                                                                monkeypatch):
    """G-1: 链接 1 —— the full open path must really call the helper."""
    view = main_window.view
    # The on-demand branch owns its own scope instance; this guard is about
    # the default (full) branch.
    monkeypatch.setenv('PRISM_PAGED_OPEN', '0')
    calls = []
    monkeypatch.setattr(view, '_catalog_scope_instance',
                        lambda filename: calls.append(filename))
    # The reader is replaced: nothing is read, nothing is written.
    monkeypatch.setattr(view, '_start_project_reader',
                        lambda filename, scope: None)
    view.open_from_file(str(project))
    _assert_open_path_creates_the_scope(calls, str(project))


def test_a_full_open_path_without_the_scope_instance_is_detected(main_window,
                                                                  project,
                                                                  monkeypatch):
    """G-1 sentinel: 断掉链接 1 (the ``_catalog_scope_instance`` call) 必须变红."""
    view = main_window.view
    monkeypatch.setenv('PRISM_PAGED_OPEN', '0')
    calls = []
    monkeypatch.setattr(view, '_catalog_scope_instance',
                        lambda filename: calls.append(filename))
    monkeypatch.setattr(view, '_start_project_reader',
                        lambda filename, scope: None)

    def broken_open(self, filename):
        """``open_from_file`` with the ``_catalog_scope_instance`` line gone."""
        self._start_project_reader(filename, None)

    monkeypatch.setattr(type(view), 'open_from_file', broken_open)
    view.open_from_file(str(project))
    assert calls == [], (
        'the injected breakage did not take over -- the sentinel is vacuous')
    with pytest.raises(AssertionError):
        _assert_open_path_creates_the_scope(calls, str(project))


def test_the_media_library_takes_the_view_scope_as_its_scope_provider(main_window):
    """G-2: 链接 2 —— the consumer must use ``view._open_scope`` as provider."""
    panel = main_window.view.category_panel
    assert hasattr(panel, 'catalog'), 'the media library has no catalog service'
    _assert_scope_provider_reads_the_view(panel)


def test_a_media_library_without_the_view_wiring_is_detected(main_window,
                                                             monkeypatch):
    """G-2 sentinel: 断掉链接 2 (no provider at all) 必须变红."""
    panel = main_window.view.category_panel
    monkeypatch.setattr(panel.catalog, '_scope_provider', None)
    assert getattr(panel.catalog, '_scope_provider', None) is None, (
        'the injected breakage did not take over -- the sentinel is vacuous')
    with pytest.raises(AssertionError):
        _assert_scope_provider_reads_the_view(panel)


def test_a_scope_provider_that_caches_the_old_scope_is_detected(main_window,
                                                                 monkeypatch):
    """G-2 sentinel: a provider that freezes the scope must be caught."""
    panel = main_window.view.category_panel
    frozen = object()
    monkeypatch.setattr(panel.catalog, '_scope_provider', lambda: frozen)
    with pytest.raises(AssertionError):
        _assert_scope_provider_reads_the_view(panel)
