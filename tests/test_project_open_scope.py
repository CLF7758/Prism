"""The open scope must answer from the catalog, never from a source scan."""
from collections import Counter
from contextlib import closing
import json
import sqlite3
import time

import pytest

from prism.project_catalog_index import CatalogNotReady, ProjectCatalogIndex
from prism.project_open_scope import (DEFAULT_PAGE_LIMIT, MAX_BATCH,
                                      OpenScopeCancelled, ProjectOpenScope)


CANVAS_A, CANVAS_B = 'canvas-a', 'canvas-b'
PIXMAP, IMAGE = 'pixmap', 'image'

#: canvas -> ((kind, count), ...): 125 items across two canvases.
LAYOUT = ((CANVAS_A, ((PIXMAP, 50), (IMAGE, 20))),
          (CANVAS_B, ((PIXMAP, 55),)))


def build_project(path):
    """A small .prism: two canvases, 125 items, unknown metadata fields."""
    ids, by_kind, key = {}, {}, 1
    with closing(sqlite3.connect(path)) as connection:
        connection.executescript('''
            CREATE TABLE items(id INTEGER PRIMARY KEY,type TEXT,x,y,z,scale,rotation,flip,data);
            CREATE TABLE sqlar(item_id INTEGER PRIMARY KEY,data BLOB);
            CREATE TABLE workspace_pages(id TEXT,kind TEXT,title TEXT);
        ''')
        for canvas, kinds in LAYOUT:
            connection.execute('INSERT INTO workspace_pages VALUES(?,?,?)',
                               (canvas, 'canvas', canvas))
            ids[canvas] = []
            for kind, count in kinds:
                by_kind[canvas, kind] = []
                for _ in range(count):
                    extra = dict(canvasId=canvas, title=f'Asset {key}',
                                 imageWidth=2048, imageHeight=1024,
                                 tags=['keep'],
                                 futureField={'nested': ['must survive']},
                                 displayThumbnail='UNIQUE_PREVIEW_'+str(key)+'x'*200)
                    connection.execute('INSERT INTO items VALUES(?,?,?,?,?,?,?,?,?)',
                                       (key, kind, key, 4, 0, 1, 30, 1, json.dumps(extra)))
                    connection.execute('INSERT INTO sqlar VALUES(?,?)',
                                       (key, bytes([key % 256])*20))
                    ids[canvas].append(key)
                    by_kind[canvas, kind].append(key)
                    key += 1
        connection.commit()
    return dict(ids=ids, by_kind=by_kind,
                counts={scope: len(rows) for scope, rows in by_kind.items()},
                total=key-1)


@pytest.fixture
def project(tmp_path):
    source = tmp_path/'open scope.prism'
    return source, build_project(source)


@pytest.fixture
def scope(project, tmp_path):
    source, expected = project
    instance = ProjectOpenScope(source, tmp_path/'open-scope-cache.sqlite')
    assert instance.build(batch_size=32).complete
    return instance, expected


def test_counts_match_the_project_without_scanning_the_source(project, tmp_path, monkeypatch):
    source, expected = project
    # Ground truth read straight from the project file, decoded in Python.
    with closing(sqlite3.connect(source)) as direct:
        observed = Counter((json.loads(payload)['canvasId'], kind)
                           for kind, payload in direct.execute('SELECT type,data FROM items'))
        total = direct.execute('SELECT COUNT(*) FROM items').fetchone()[0]
    assert dict(observed) == expected['counts']
    assert total == expected['total'] == 125

    executed = []
    original = ProjectCatalogIndex._source

    class Recording:
        """Proxy that logs every statement the scope sends to the project."""
        def __init__(self, connection):
            self._connection = connection
        def __getattr__(self, name):
            return getattr(self._connection, name)
        def execute(self, sql, *parameters):
            executed.append(' '.join(sql.split()))
            return self._connection.execute(sql, *parameters)

    monkeypatch.setattr(ProjectCatalogIndex, '_source',
                        lambda self: Recording(original(self)))
    scope = ProjectOpenScope(source, tmp_path/'cache.sqlite')
    assert scope.build(batch_size=32).complete
    assert not [sql for sql in executed if 'count(' in sql.casefold()], executed

    executed.clear()
    assert scope.counts() == expected['counts']
    assert scope.total() == expected['total']
    assert executed == [], 'counts must come from the cache, not the project'


def test_page_is_keyset_paging(scope):
    scope, expected = scope
    wanted = expected['ids'][CANVAS_A]
    seen, pages, after = [], 0, None
    while True:
        rows = scope.page(CANVAS_A, limit=16, after_id=after)
        if not rows:
            break
        assert 1 <= len(rows) <= 16
        ids = [row['id'] for row in rows]
        assert ids == sorted(ids)
        seen.extend(ids)
        after, pages = ids[-1], pages+1
    assert seen == wanted, 'keyset paging repeats or drops rows'
    assert pages == -(-len(wanted)//16)

    summary = scope.page(CANVAS_B, limit=1)[0]
    assert set(summary) == {'id', 'kind', 'canvas_id', 'title', 'group_id',
                            'width', 'height', 'has_preview', 'has_original'}
    assert summary['canvas_id'] == CANVAS_B
    assert (DEFAULT_PAGE_LIMIT, MAX_BATCH) == (128, 1024)

    tail = scope.page(CANVAS_A, after_id=wanted[9], limit=MAX_BATCH)
    assert [row['id'] for row in tail] == wanted[10:]
    images = scope.page(CANVAS_A, kind=IMAGE, limit=MAX_BATCH)
    assert [row['id'] for row in images] == expected['by_kind'][CANVAS_A, IMAGE]

    for bad in (0, -1, MAX_BATCH+1, 100000):
        with pytest.raises(ValueError):
            scope.page(CANVAS_A, limit=bad)


def test_details_return_only_the_requested_rows_with_unknown_fields(scope):
    scope, _expected = scope
    rows = scope.details([7, 3, 125, 7])
    assert [row['id'] for row in rows] == [7, 3, 125]
    assert set(rows[0]) == {'id', 'type', 'x', 'y', 'z', 'scale', 'rotation',
                            'flip', 'data'}
    payload = json.loads(rows[0]['data'])
    assert payload['futureField']['nested'] == ['must survive']
    assert payload['displayThumbnail'].startswith('UNIQUE_PREVIEW_7')
    assert scope.details([10**9]) == ()
    assert scope.details(()) == ()
    with pytest.raises(ValueError, match='bounded'):
        scope.details(range(MAX_BATCH+1))


def test_an_unusable_catalog_is_none_not_zero(project, tmp_path):
    source, _expected = project
    scope = ProjectOpenScope(source, tmp_path/'absent.sqlite')
    assert scope.ready is False
    assert scope.counts() is None and scope.total() is None
    with pytest.raises(CatalogNotReady):
        scope.page(CANVAS_A)
    with pytest.raises(CatalogNotReady):
        scope.details([1])

    totals = []
    result = scope.build(batch_size=16, cancel=lambda: bool(totals),
                         on_batch=totals.append)
    assert not result.complete and result.total_records == 16
    assert scope.ready is False
    assert scope.counts() is None and scope.total() is None

    broken = tmp_path/'broken.sqlite'
    broken.write_bytes(b'')
    assert ProjectOpenScope(source, broken).ready is False

    unrelated = tmp_path/'unrelated.sqlite'
    with closing(sqlite3.connect(unrelated)) as connection:
        connection.execute('CREATE TABLE important(data)')
    assert ProjectOpenScope(source, unrelated).counts() is None


def test_source_change_makes_the_scope_stale(scope):
    scope, _expected = scope
    before = scope.source_state()
    assert scope.ready is True and scope.counts() is not None

    with closing(sqlite3.connect(scope.project_path)) as connection:
        connection.execute('DELETE FROM items WHERE id=1')
        connection.commit()

    after = scope.source_state()
    assert after['source'] != before['source']
    assert after['version'] == before['version']
    assert scope.ready is False
    assert scope.counts() is None and scope.total() is None
    with pytest.raises(CatalogNotReady):
        scope.page(CANVAS_A)


def test_a_cancelled_read_is_not_an_empty_page(scope):
    scope, _expected = scope
    with pytest.raises(OpenScopeCancelled):
        scope.page(CANVAS_A, cancel=lambda: True)
    with pytest.raises(OpenScopeCancelled):
        scope.details([1], cancel=lambda: True)
    assert scope.page(CANVAS_A, limit=1), 'cancellation must not poison the cache'


def test_repeated_readiness_reuses_state_until_the_cache_changes(scope, monkeypatch):
    scope, _expected = scope
    opened = []
    original = scope.index._cache

    def opening(*args, **kwargs):
        opened.append(True)
        return original(*args, **kwargs)

    monkeypatch.setattr(scope.index, '_cache', opening)
    assert all(scope.ready for _ in range(12))
    assert len(opened) == 1, 'unchanged navigation must not reopen the catalog'
    with closing(sqlite3.connect(scope.cache_path)) as connection:
        state = json.loads(connection.execute(
            "SELECT value FROM catalog_state WHERE key='build'").fetchone()[0])
        state['invalid'] = True
        connection.execute("UPDATE catalog_state SET value=? WHERE key='build'",
                           (json.dumps(state),))
        connection.commit()
    assert not scope.ready, 'a changed catalog must invalidate the ready answer'
    assert len(opened) == 2


def test_source_wal_commit_invalidates_readiness_without_changing_main_file(project, tmp_path):
    source, _expected = project
    with closing(sqlite3.connect(source)) as writer:
        assert writer.execute('PRAGMA journal_mode=WAL').fetchone()[0] == 'wal'
        writer.execute('PRAGMA wal_autocheckpoint=0')
        scope = ProjectOpenScope(source, tmp_path/'wal-source-cache.sqlite')
        assert scope.build(batch_size=32).complete and scope.ready
        before = scope.source_state()['source']
        writer.execute('DELETE FROM items WHERE id=1')
        writer.commit()
        after = scope.source_state()['source']
        assert after['file'] == before['file'], 'this case must exercise WAL-only change'
        assert after['wal'] != before['wal']
        assert not scope.ready


def test_cache_wal_commit_invalidates_a_previously_ready_answer(scope):
    scope, _expected = scope
    with closing(sqlite3.connect(scope.cache_path)) as writer:
        assert writer.execute('PRAGMA journal_mode=WAL').fetchone()[0] == 'wal'
        writer.execute('PRAGMA wal_autocheckpoint=0')
        assert scope.ready
        before = scope.source_state()['cache']
        state = json.loads(writer.execute(
            "SELECT value FROM catalog_state WHERE key='build'").fetchone()[0])
        state['complete'] = False
        writer.execute("UPDATE catalog_state SET value=? WHERE key='build'",
                       (json.dumps(state),))
        writer.commit()
        after = scope.source_state()['cache']
        assert after['file'] == before['file'], 'this case must exercise WAL-only change'
        assert after['wal'] != before['wal']
        assert not scope.ready
        assert scope.telemetry_state()['expected_total_known'] is False


def test_busy_catalog_status_is_bounded_and_recovers_without_a_file_change(scope):
    scope, expected = scope
    before = scope.source_state()
    with closing(sqlite3.connect(scope.cache_path)) as locker:
        locker.execute('BEGIN EXCLUSIVE')
        started = time.perf_counter()
        status = scope.telemetry_state()
        elapsed = time.perf_counter() - started
        assert elapsed < .5, 'a status query must not consume SQLite default 5s wait'
        assert status['catalog_state'] == 'error'
        assert status['expected_total_known'] is False
        assert status['expected_total'] != 0, 'busy is not an empty library'
        locker.rollback()
    assert scope.source_state() == before, 'unlock alone must exercise transient recovery'
    assert scope.ready
    assert scope.telemetry_state()['expected_total'] == expected['total']


def test_missing_and_corrupt_status_never_create_or_mutate_project_files(project, tmp_path):
    source, _expected = project
    source_bytes = source.read_bytes()
    absent = tmp_path/'missing-cache.sqlite'
    corrupt = tmp_path/'broken-cache.sqlite'
    corrupt.write_bytes(b'not a database')
    for path in (absent, corrupt):
        scope = ProjectOpenScope(source, path)
        assert scope.ready is False
        status = scope.telemetry_state()
        assert status['catalog_state'] != 'ready'
        assert status['expected_total_known'] is False
        assert status['expected_total'] != 0
        assert scope.total() is None
    assert not absent.exists()
    assert corrupt.read_bytes() == b'not a database'
    assert source.read_bytes() == source_bytes
