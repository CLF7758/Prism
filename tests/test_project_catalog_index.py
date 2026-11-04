"""Read-only catalog checkpoints must never masquerade as a full project."""
from dataclasses import asdict
import hashlib
import json
import sqlite3
import threading
import time

import pytest

import prism.project_catalog_index as catalog_module
from prism.project_catalog_index import (CatalogBuildCancelled, CatalogNotReady,
                                         ProjectCatalogIndex)


@pytest.fixture
def catalog(tmp_path):
    source = tmp_path/'source # project.prism'
    with sqlite3.connect(source) as connection:
        connection.executescript('''
            CREATE TABLE items(id INTEGER PRIMARY KEY,type TEXT,x,y,z,scale,rotation,flip,data);
            CREATE TABLE sqlar(item_id INTEGER PRIMARY KEY,data BLOB);
        ''')
        for key in (-4, 1, 2, 3, 4, 5, 6):
            extra = dict(canvasId='A' if key <= 3 else 'B', title=f'Asset {key}',
                         imageWidth=2048, imageHeight=1024, tags=['keep'],
                         futureField={'nested': ['must survive']},
                         displayThumbnail='UNIQUE_PREVIEW_'+str(key)+'x'*10000)
            connection.execute('INSERT INTO items VALUES(?,?,?,?,?,?,?,?,?)',
                               (key, 'pixmap', key, 4, 0, 1, 30, 1, json.dumps(extra)))
            if key != 2:  # Missing originals remain visible in the catalog.
                connection.execute('INSERT INTO sqlar VALUES(?,?)', (key, bytes([key % 256])*20))
    return ProjectCatalogIndex(source, tmp_path/'cache.sqlite')


def test_exact_counts_reuse_stable_rows_and_invalidate_on_cache_writes(catalog):
    catalog.build()
    with sqlite3.connect(catalog.cache) as connection:
        connection.execute('UPDATE assets SET last_used=1 WHERE id=1')
    assert catalog.count(recent_only=True) == 1
    assert catalog.last_count_stats['route'] == 'exact'
    assert catalog.count(recent_only=True) == 1
    assert catalog.last_count_stats['route'] == 'exact_cached'
    # A different predicate must not inherit the previous answer.
    assert catalog.count(recent_only=True, canvas_ids=('B',)) == 0
    with sqlite3.connect(catalog.cache) as connection:
        connection.execute('UPDATE assets SET last_used=1 WHERE id=4')
    assert catalog.count(recent_only=True) == 2
    assert catalog.last_count_stats['route'] == 'exact'
    assert catalog.count(recent_only=True, canvas_ids=('B',)) == 1
    # Readiness is still checked on a would-be cache hit.
    with sqlite3.connect(catalog.source) as connection:
        connection.execute("UPDATE items SET x=999 WHERE id=1")
    with pytest.raises(CatalogNotReady):
        catalog.count(recent_only=True)


def test_recent_count_has_a_covering_index_without_wide_asset_reads(catalog):
    catalog.build()
    predicate, params = catalog._predicate(recent_only=True)
    with sqlite3.connect(catalog.cache) as connection:
        plan = connection.execute('EXPLAIN QUERY PLAN SELECT COUNT(*) FROM assets WHERE '+predicate,
                                  params).fetchall()
    assert any('COVERING INDEX assets_recent_metadata' in row[3] for row in plan), plan


def test_old_ready_cache_upgrades_index_without_reprocessing_project_rows(catalog):
    catalog.build()
    original = hashlib.sha256(catalog.source.read_bytes()).hexdigest()
    with sqlite3.connect(catalog.cache) as connection:
        connection.execute('DROP INDEX assets_recent_metadata')
    assert catalog.needs_index_upgrade()
    upgraded = catalog.build()
    assert upgraded.complete and upgraded.processed_records == 0
    assert not catalog.needs_index_upgrade()
    assert hashlib.sha256(catalog.source.read_bytes()).hexdigest() == original


def test_projection_is_bounded_and_canonical_details_keep_unknown_fields(catalog):
    original = hashlib.sha256(catalog.source.read_bytes()).hexdigest()
    result = catalog.build(batch_size=2)
    assert result.complete and result.total_records == 7
    first = catalog.page('A', limit=2)
    second = catalog.page('A', after_id=first[-1]['id'], limit=2)
    assert [r['id'] for r in first+second] == [-4, 1, 2, 3]
    assert 'displayThumbnail' not in first[0] and 'data' not in first[0]
    assert len(json.dumps(first+second)) < 2000
    assert second[0]['has_original'] == 0
    details = catalog.details([3, -4, 3])
    assert [r['id'] for r in details] == [3, -4]
    assert json.loads(details[0]['data'])['futureField']['nested'] == ['must survive']
    assert hashlib.sha256(catalog.source.read_bytes()).hexdigest() == original
    assert catalog.build().processed_records == 0


def test_deleted_aggregate_count_matches_deleted_query(catalog):
    with sqlite3.connect(catalog.source) as db:
        raw = json.loads(db.execute('SELECT data FROM items WHERE id=1').fetchone()[0])
        raw['deletedAt'] = '2026-11-02T00:00:00Z'
        db.execute('UPDATE items SET data=? WHERE id=1', (json.dumps(raw),))
    catalog.build()
    assert catalog.count(include_deleted=True) == 1
    assert [row['id'] for row in catalog.query(include_deleted=True)] == [1]
    assert catalog.count() == 6


def test_scope_candidate_queries_use_untagged_and_recent_indexes(catalog):
    catalog.build()
    with catalog._cache(readonly=True) as db:
        for filters, index in (({'untagged_only': True}, 'assets_untagged'),
                               ({'recent_only': True, 'recent_limit': 50}, 'assets_recent')):
            where, parameters = catalog._predicate(**filters)
            plan = db.execute('EXPLAIN QUERY PLAN SELECT id FROM assets WHERE ' + where,
                              parameters).fetchall()
            assert any(index in row[3] for row in plan), plan


def test_cancel_resume_and_counts_are_atomic(catalog):
    totals = []
    result = catalog.build(batch_size=2, cancel=lambda: bool(totals), on_batch=totals.append)
    assert not result.complete and result.total_records == 2
    with pytest.raises(CatalogNotReady):
        catalog.page('A')
    with pytest.raises(CatalogNotReady):
        catalog.details([-4])
    result = catalog.build(batch_size=2)
    assert result.resumed and result.complete
    assert result.processed_records == 5 and result.total_records == 7
    with sqlite3.connect(catalog.cache) as connection:
        counts = dict(connection.execute('SELECT scope,total FROM counts'))
    assert counts == {json.dumps(['A', 'pixmap']): 4, json.dumps(['B', 'pixmap']): 3}


def test_source_change_invalidates_queries_and_rebuilds(catalog):
    catalog.build()
    with sqlite3.connect(catalog.source) as connection:
        connection.execute('DELETE FROM items WHERE id=1')
    with pytest.raises(CatalogNotReady):
        catalog.page('A')
    result = catalog.build()
    assert result.complete and not result.resumed and result.total_records == 6
    assert [r['id'] for r in catalog.page('A')] == [-4, 2, 3]


def test_write_during_build_rejects_mixed_versions(catalog):
    def mutate(_total):
        with sqlite3.connect(catalog.source) as connection:
            connection.execute('UPDATE items SET x=99 WHERE id=5')
    with pytest.raises(CatalogNotReady, match='changed during') as caught:
        catalog.build(batch_size=2, on_batch=mutate)
    assert caught.value.source_before != caught.value.source_after or (
        caught.value.data_versions[0] != caught.value.data_versions[1])
    with pytest.raises(CatalogNotReady):
        catalog.page('A')
    assert catalog.build().total_records == 7


def test_malformed_metadata_cannot_mark_partial_index_complete(catalog):
    with sqlite3.connect(catalog.source) as connection:
        connection.execute("UPDATE items SET data='broken' WHERE id=3")
    with pytest.raises(json.JSONDecodeError):
        catalog.build(batch_size=2)
    with pytest.raises(CatalogNotReady):
        catalog.page('A')
    with sqlite3.connect(catalog.cache) as connection:
        state = json.loads(connection.execute(
            "SELECT value FROM catalog_state WHERE key='build'").fetchone()[0])
    assert not state['complete'] and state['total'] == 2


def test_refuses_source_as_cache_and_unrelated_destination(catalog, tmp_path):
    with pytest.raises(ValueError):
        ProjectCatalogIndex(catalog.source, catalog.source)
    unrelated = tmp_path/'unrelated.sqlite'
    with sqlite3.connect(unrelated) as connection:
        connection.execute('CREATE TABLE important(data)')
    before = unrelated.read_bytes()
    with pytest.raises(ValueError, match='not a Prism catalog'):
        ProjectCatalogIndex(catalog.source, unrelated).build()
    assert unrelated.read_bytes() == before


def test_cache_initialization_failure_closes_connection_and_releases_lock(catalog,
                                                                          monkeypatch):
    original = catalog._ensure_schema

    def fail_with_write_lock(connection):
        connection.execute('BEGIN EXCLUSIVE')
        raise RuntimeError('schema fault')

    monkeypatch.setattr(catalog, '_ensure_schema', fail_with_write_lock)
    with pytest.raises(RuntimeError, match='schema fault'):
        catalog.build()
    monkeypatch.setattr(catalog, '_ensure_schema', original)
    assert catalog.build().complete


def test_zero_time_budget_leaves_resumable_incomplete_state(catalog):
    result = catalog.build(max_seconds=0)
    assert not result.complete and result.total_records == 0
    assert asdict(catalog.build())['complete']
    with pytest.raises(ValueError):
        catalog.page('A', limit=100000)
    with pytest.raises(ValueError):
        catalog.details(range(2000))


def test_another_builder_is_rejected_but_lock_recovers(catalog):
    attempts = []
    def other_builder(_total):
        started = time.perf_counter()
        with pytest.raises(sqlite3.OperationalError, match='locked'):
            catalog.build(lock_wait_seconds=1)
        assert time.perf_counter() - started < .25
        attempts.append(True)
    assert catalog.build(batch_size=2, on_batch=other_builder).complete
    assert attempts
    assert catalog.build().complete


def _hold_catalog_writer(catalog):
    entered = threading.Event()
    release = threading.Event()
    failures = []

    def hold(_total):
        entered.set()
        if not release.wait(5):
            raise RuntimeError('test did not release catalog writer')

    def run():
        try:
            catalog.build(batch_size=2, on_batch=hold)
        except Exception as error:
            failures.append(error)

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    assert entered.wait(2)
    return release, thread, failures


def test_default_writer_lock_stays_fail_fast_across_threads(catalog):
    release, thread, failures = _hold_catalog_writer(catalog)
    try:
        started = time.perf_counter()
        with pytest.raises(sqlite3.OperationalError) as caught:
            catalog.build()
        assert time.perf_counter() - started < .25
        assert caught.value.sqlite_errorcode & 0xff == sqlite3.SQLITE_BUSY
    finally:
        release.set()
        thread.join(2)
    assert not thread.is_alive() and not failures


def test_writer_wait_succeeds_after_previous_builder_releases(catalog):
    release, thread, failures = _hold_catalog_writer(catalog)
    timer = threading.Timer(.15, release.set)
    timer.start()
    try:
        result = catalog.build(lock_wait_seconds=1, lock_poll_seconds=.01)
    finally:
        release.set()
        timer.cancel()
        thread.join(2)
    assert not failures and result.complete
    assert .1 <= result.lock_wait_seconds < 1


def test_writer_wait_has_total_deadline(catalog):
    release, thread, failures = _hold_catalog_writer(catalog)
    try:
        started = time.perf_counter()
        with pytest.raises(sqlite3.OperationalError):
            catalog.build(lock_wait_seconds=.12, lock_poll_seconds=.01)
        elapsed = time.perf_counter() - started
    finally:
        release.set()
        thread.join(2)
    assert not failures
    assert .1 <= elapsed < .5


def test_build_time_budget_includes_writer_wait(catalog):
    release, thread, failures = _hold_catalog_writer(catalog)
    try:
        started = time.perf_counter()
        with pytest.raises(sqlite3.OperationalError):
            catalog.build(lock_wait_seconds=1, max_seconds=.1,
                          lock_poll_seconds=.01)
        elapsed = time.perf_counter() - started
    finally:
        release.set()
        thread.join(2)
    assert not failures
    assert .08 <= elapsed < .4


def test_writer_wait_is_cancellable(catalog):
    release, thread, failures = _hold_catalog_writer(catalog)
    cancelled = threading.Event()
    timer = threading.Timer(.08, cancelled.set)
    timer.start()
    try:
        started = time.perf_counter()
        with pytest.raises(CatalogBuildCancelled):
            catalog.build(cancel=cancelled.is_set, lock_wait_seconds=2,
                          lock_poll_seconds=.01)
        elapsed = time.perf_counter() - started
    finally:
        release.set()
        timer.cancel()
        thread.join(2)
    assert not failures
    assert elapsed < .5


def test_writer_wait_parameters_are_finite(catalog):
    for value in (-1, float('nan'), float('inf'), None, True):
        with pytest.raises(ValueError):
            catalog.build(lock_wait_seconds=value)
    with pytest.raises(ValueError):
        catalog.build(lock_poll_seconds=0)


def test_null_canvas_count_does_not_duplicate_on_resume(catalog):
    with sqlite3.connect(catalog.source) as connection:
        for key in (-4, 1, 2):
            connection.execute('UPDATE items SET data=? WHERE id=?',
                               (json.dumps({'canvasId': None}), key))
    catalog.build(batch_size=1)
    assert len(catalog.page(None)) == 3
    with sqlite3.connect(catalog.cache) as connection:
        assert connection.execute('SELECT total FROM counts WHERE scope=?',
                                  (json.dumps([None, 'pixmap']),)).fetchone()[0] == 3


def test_page_query_uses_index_without_loading_source_json(catalog, monkeypatch):
    catalog.build()
    def forbidden():
        raise AssertionError('A summary query must not open source metadata')
    monkeypatch.setattr(catalog, '_source', forbidden)
    assert len(catalog.page('A', after_id=-4, kind='pixmap', limit=2)) == 2
    assert b'UNIQUE_PREVIEW_' not in catalog.cache.read_bytes()
    with sqlite3.connect(catalog.cache) as connection:
        plan = connection.execute('EXPLAIN QUERY PLAN SELECT * FROM assets '
                                  'WHERE canvas_id IS ? AND kind=? AND id>? '
                                  'ORDER BY id LIMIT ?', ('A', 'pixmap', -4, 2)).fetchall()
    assert any('SEARCH' in row[3] and 'assets_kind_page' in row[3] for row in plan)


def test_wal_change_is_stale_even_without_main_file_update(catalog):
    connection = sqlite3.connect(catalog.source)
    try:
        assert connection.execute('PRAGMA journal_mode=WAL').fetchone()[0] == 'wal'
        catalog.build()
        before = catalog.source.stat().st_mtime_ns
        connection.execute('UPDATE items SET x=123 WHERE id=4')
        connection.commit()
        assert catalog.source.stat().st_mtime_ns == before
        with pytest.raises(CatalogNotReady):
            catalog.page('A')
    finally:
        connection.close()


def test_pkg3_span_enters_real_context_and_writes_one_lifecycle(monkeypatch):
    calls = []

    class Handle:
        def set(self, **fields):
            calls.append(('set', fields))

    class Manager:
        def __enter__(self):
            calls.append(('enter', None))
            return Handle()
        def __exit__(self, *exc_info):
            calls.append(('exit', exc_info[0]))
            return False

    observed = []
    monkeypatch.setattr(catalog_module, '_pkg3_trace_span',
                        lambda event, **fields: Manager())
    monkeypatch.setattr(catalog_module, '_pkg3_trace_record',
                        lambda *args, **kwargs: calls.append(('duplicate', kwargs)))
    monkeypatch.setattr(catalog_module, '_PKG3_OBSERVERS', [
        lambda event, **fields: observed.append((event, fields))])
    with catalog_module.pkg3_span('catalog.query', rows=1) as span:
        span.set(rows=3)
    assert [kind for kind, _value in calls] == ['enter', 'set', 'exit']
    assert observed == [('catalog.query', {'rows': 3})]


def test_pkg3_restore_covers_whole_block_and_restores_on_error(monkeypatch):
    state = {'value': 'caller'}
    exits = []

    class Manager:
        def __enter__(self):
            state['value'] = 'worker'
        def __exit__(self, *exc_info):
            exits.append(exc_info[0])
            state['value'] = 'caller'
            return False

    monkeypatch.setattr(catalog_module, '_pkg3_restore_context',
                        lambda context: Manager())
    with pytest.raises(ValueError, match='business'):
        with catalog_module.pkg3_restore(object()) as restored:
            assert restored is True
            assert state['value'] == 'worker'
            raise ValueError('business')
    assert state['value'] == 'caller'
    assert exits == [ValueError]


def test_stall_context_uses_frozen_dispatch_and_background_is_not_causal():
    fields = catalog_module.build_stall_context(
        stall_id='stall-1', duration_ms=4000, stage='gui_stall',
        window_start_monotonic=10, window_end_monotonic=14,
        dispatch_intervals=[dict(dispatch_id='short-recovery',
                                 start_monotonic=13.99,
                                 end_monotonic=14.0, depth=0)],
        heartbeat_delay_ms=4000, background_busy=True)
    assert fields['stall_id'] == 'stall-1'
    assert fields['covering_dispatches'] == []
    assert fields['classification'] == 'unclassified'
    assert fields['classification_reason'] == 'background_activity_observed_not_causal'
    assert fields['background_is_causal_evidence'] is False
