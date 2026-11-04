"""A-3: the catalog's derived indexes are exact, verifiable and honest.

The catalog cache owns two derived indexes:

* ``counts_by_scope`` pre-aggregates the per ``(canvas_id, kind, untagged,
  deleted)`` counters, so the sidebar numbers are a table read instead of a
  grouped scan over every catalog row;
* ``assets_fts`` is the FTS5 trigram index of the catalog rows. Its consumer
  side lives in :mod:`prism.search_candidates`; this file proves that the
  index is usable, that it is refused when it does not describe the catalog,
  and that it answers exactly what the ``instr()`` scan answers.

Nothing here trusts the derived data: every number is compared with the
grouped scan over the same rows.
"""
from contextlib import closing
import json
import sqlite3

import pytest

from prism import search_candidates as candidates
from prism.project_catalog_index import (COUNTS_MARKER_KEY, COUNTS_TABLE,
                                         CatalogNotReady, ProjectCatalogIndex,
                                         UNASSIGNED_CANVAS, UNCLASSIFIED_CANVAS,
                                         _variable_limit)


# ── fixtures ───────────────────────────────────────────────────

RECORDS = [
    (1, 'pixmap', dict(canvasId='A', title='雨夜 unloaded', notes='', filename='night.png')),
    (2, 'pixmap', dict(canvasId='A', title='100%_literal', notes='', filename='pct.png')),
    (3, 'pixmap', dict(canvasId='A', title='ÄPFEL', notes='', filename='apple.png')),
    (4, 'pixmap', dict(canvasId='A', title='Σίσυφος', notes='', filename='summer.png')),
    (5, 'pixmap', dict(canvasId='A', title='İSTANBUL', notes='', filename='city.png')),
    (6, 'pixmap', dict(canvasId='A', title='istanbul', notes='', filename='plain.png')),
    (7, 'pixmap', dict(canvasId='A', title='100AA_literal', notes='', filename='aa.png')),
    (8, 'pixmap', dict(canvasId='A', title='木纹贴图', notes='精选木 特写',
                       filename='wood.png', tags=['材质'])),
    (9, 'pixmap', dict(canvasId='A', title='标注文字', notes='', filename='note.png')),
    (10, 'text', dict(canvasId='A', title='needle annotation', notes='')),
    (11, 'pixmap', dict(canvasId=None, title='未归类', notes='', filename='loose.png')),
    (12, 'pixmap', dict(canvasId='B', title='deleted one', notes='', filename='gone.png',
                        deletedAt='2026-01-01T00:00:00')),
    (13, 'pixmap', dict(canvasId='B', title='   ', notes='', filename='blank.png',
                        tags=['  '])),
    (14, 'pixmap', dict(canvasId='B', title='kept', notes='', filename='kept.png',
                        tags=['keep'])),
]


def make_index(tmp_path, records=RECORDS, name='catalog'):
    """A catalog index over a throwaway source project."""
    source = tmp_path / (name + '-source.prism')
    with sqlite3.connect(source) as db:
        db.executescript('CREATE TABLE items(id INTEGER PRIMARY KEY,type TEXT,x,y,z,'
                         'scale,rotation,flip,data);'
                         'CREATE TABLE sqlar(item_id INTEGER PRIMARY KEY,data BLOB);')
        for key, kind, extra in records:
            db.execute('INSERT INTO items VALUES(?,?,?,?,?,?,?,?,?)',
                       (key, kind, 0, 0, 0, 1, 0, 0, json.dumps(extra, ensure_ascii=False)))
    return ProjectCatalogIndex(source, tmp_path / (name + '.sqlite'))


def raw_canvas_counts(index, *, kind=None, untagged_only=False):
    """The grouped scan over the catalog rows, written out independently."""
    conditions, params = ['deleted_at IS NULL'], []
    if kind is not None:
        conditions.append('kind=?')
        params.append(kind)
    else:
        conditions.append("kind NOT IN ('text','path')")
    if untagged_only:
        conditions.append('prism_untagged(tags)=1')
    with closing(index._cache(readonly=True)) as cache:
        index._ready(cache)
        rows = cache.execute('SELECT canvas_id,COUNT(*) FROM assets WHERE '
                              + ' AND '.join(conditions) + ' GROUP BY canvas_id',
                              params).fetchall()
    return {canvas_id: int(total) for canvas_id, total in rows}


def raw_buckets(index):
    """Every ``(canvas, kind, untagged, deleted)`` bucket of the catalog rows."""
    with closing(index._cache(readonly=True)) as cache:
        index._ready(cache)
        rows = cache.execute(
            'SELECT canvas_id,kind,prism_untagged(tags),deleted_at IS NOT NULL,COUNT(*) '
            'FROM assets GROUP BY canvas_id,kind,prism_untagged(tags),'
            'deleted_at IS NOT NULL').fetchall()
    return {(canvas, kind, int(untagged), int(deleted)): int(total)
            for canvas, kind, untagged, deleted, total in rows}


def aggregate_buckets(index):
    """Every bucket of the pre-aggregate, read straight from the table."""
    with closing(index._cache(readonly=True)) as cache:
        index._ready(cache)
        rows = cache.execute('SELECT canvas_id,kind,untagged,deleted,total FROM '
                              + COUNTS_TABLE).fetchall()
    return {(canvas, kind, int(untagged), int(deleted)): int(total)
            for canvas, kind, untagged, deleted, total in rows}


def trigram_ids(index, query):
    """The ids the trigram candidate path returns, or ``None`` without anchor.

    This is the production shortlist: the trigram index rejects rows, the
    exact word filter still decides, exactly like
    :func:`prism.project_search.search_catalog_assets` does it.
    """
    connection = sqlite3.connect(index.cache)
    try:
        if candidates.catalog_trigram_plan(query) is None:
            return None
        found = []
        for row in candidates.iter_catalog_candidate_rows(
                connection, query, table='assets', fts_table=candidates.CATALOG_FTS_TABLE):
            haystack = candidates.catalog_haystack(
                row['title'], row['filename'], row['notes'],
                candidates.catalog_tag_names(row['tags']))
            if all(word in haystack for word in candidates.query_words(query)):
                found.append(row['id'])
        return found
    finally:
        connection.close()


def exact_ids(index, query):
    """The ids the ``instr()`` scan returns."""
    return [row['id'] for row in index.query(text=query, limit=1024)]


# ── the pre-aggregate ──────────────────────────────────────────

def test_canvas_counts_answers_the_same_with_and_without_the_pre_aggregate(tmp_path):
    index = make_index(tmp_path)
    assert index.build().complete
    for kind in (None, 'pixmap', 'text'):
        for untagged_only in (False, True):
            expected = raw_canvas_counts(index, kind=kind, untagged_only=untagged_only)
            assert index.canvas_counts(kind=kind, untagged_only=untagged_only) == expected
    # The soft-deleted row is excluded, the null canvas is kept as its own key.
    counts = index.canvas_counts()
    assert counts == {'A': 9, None: 1, 'B': 2}
    assert index.canvas_counts(untagged_only=True) == {'A': 8, None: 1, 'B': 1}


def test_every_pre_aggregate_bucket_is_the_exact_total_of_its_rows(tmp_path):
    index = make_index(tmp_path)
    assert index.build().complete
    expected = raw_buckets(index)
    actual = aggregate_buckets(index)
    assert actual == expected
    # Nothing is truncated: the buckets add up to every catalog row.
    with closing(index._cache(readonly=True)) as cache:
        index._ready(cache)
        rows = cache.execute('SELECT count(*) FROM assets').fetchone()[0]
    assert sum(actual.values()) == rows


def test_the_pre_aggregate_does_not_scan_the_catalog_rows(tmp_path):
    index = make_index(tmp_path)
    assert index.build().complete
    with closing(index._cache(readonly=True)) as cache:
        index._ready(cache)
        plan = cache.execute(
            'EXPLAIN QUERY PLAN SELECT canvas_id,SUM(total) FROM ' + COUNTS_TABLE +
            " WHERE deleted=0 AND kind NOT IN ('text','path') GROUP BY canvas_id"
        ).fetchall()
    assert not any('assets' in row[3] for row in plan), plan


def test_grouped_counts_answers_the_same_with_and_without_the_pre_aggregate(tmp_path):
    index = make_index(tmp_path)
    assert index.build().complete
    with closing(index._cache(readonly=True)) as cache:
        index._ready(cache)
        expected = tuple(
            (cid, bool(deleted), bool(untagged), int(total))
            for cid, deleted, untagged, total in cache.execute(
                "SELECT canvas_id,deleted_at IS NOT NULL,prism_untagged(tags),COUNT(*) "
                "FROM assets WHERE kind NOT IN ('text','path') "
                "GROUP BY canvas_id,deleted_at IS NOT NULL,prism_untagged(tags)").fetchall())
    assert index.grouped_counts() == expected


def test_count_uses_the_pre_aggregate_and_stays_exact(tmp_path):
    index = make_index(tmp_path)
    assert index.build().complete
    assert index.count() == sum(raw_canvas_counts(index).values()) == 12
    assert index.count(canvas_ids=('A',)) == 9
    assert index.count(canvas_ids=(None,)) == 1
    assert index.count(canvas_ids=()) == 0
    assert index.count(untagged_only=True) == 10
    assert index.count(kind='text') == 1
    # include_deleted selects the trash range, matching query and the UI.
    with closing(index._cache(readonly=True)) as cache:
        deleted = cache.execute("SELECT COUNT(*) FROM assets WHERE deleted_at IS NOT NULL "
                                "AND kind NOT IN ('text','path')").fetchone()[0]
    assert index.count(include_deleted=True) == deleted == 1
    assert index.count() + index.count(include_deleted=True) == 13
    # A text filter needs the rows themselves, so it keeps the exact scan.
    assert index.count(text='wood') == 1
    # The 'needle' row is a text annotation, not an asset.
    assert index.count(text='needle') == 0
    with closing(index._cache(readonly=True)) as cache:
        index._ready(cache)
        plan = cache.execute('EXPLAIN QUERY PLAN SELECT COALESCE(SUM(total),0) FROM '
                              + COUNTS_TABLE + " WHERE deleted=0").fetchall()
    assert not any('assets' in row[3] for row in plan), plan


def test_an_old_cache_without_the_pre_aggregate_still_answers_exactly(tmp_path):
    index = make_index(tmp_path)
    assert index.build().complete
    expected = index.canvas_counts()
    # Drop the derived tables: that is what a cache from before this change
    # looks like. The read path has to fall back to the grouped scan.
    connection = sqlite3.connect(index.cache)
    try:
        connection.execute('DROP TABLE ' + COUNTS_TABLE)
        connection.execute('DELETE FROM catalog_state WHERE key=?', (COUNTS_MARKER_KEY,))
        connection.commit()
    finally:
        connection.close()
    assert index.canvas_counts() == expected
    assert index.count() == sum(expected.values())


def test_the_cache_internal_migration_never_reads_the_source(tmp_path, monkeypatch):
    index = make_index(tmp_path)
    assert index.build().complete
    connection = sqlite3.connect(index.cache)
    try:
        connection.execute('DROP TABLE ' + COUNTS_TABLE)
        connection.execute('DELETE FROM catalog_state WHERE key=?', (COUNTS_MARKER_KEY,))
        connection.execute('DELETE FROM "%s"' % candidates.CATALOG_FTS_TABLE)
        connection.execute('DELETE FROM catalog_state WHERE key=?',
                           (candidates.TRIGRAM_MARKER_KEY,))
        connection.commit()
    finally:
        connection.close()

    def forbidden():
        raise AssertionError('The migration must not read the source project')

    monkeypatch.setattr(index, '_source', forbidden)
    assert index.ensure_indexes() is True
    assert aggregate_buckets(index) == raw_buckets(index)
    with closing(index._cache(readonly=True)) as cache:
        index._ready(cache)
        status = candidates.trigram_index_status(
            cache, table='assets', id_column='id',
            fts_table=candidates.CATALOG_FTS_TABLE)
    assert status.usable, status.reason


# ── the trigram index ──────────────────────────────────────────

def test_the_build_produces_a_trusted_trigram_index(tmp_path):
    index = make_index(tmp_path)
    assert index.build().complete
    with closing(index._cache(readonly=True)) as cache:
        index._ready(cache)
        status = candidates.trigram_index_status(
            cache, table='assets', id_column='id',
            fts_table=candidates.CATALOG_FTS_TABLE)
    assert status.usable, status.reason
    assert status.rows == len(RECORDS)


def test_an_emptied_trigram_table_is_refused(tmp_path):
    index = make_index(tmp_path)
    assert index.build().complete
    connection = sqlite3.connect(index.cache)
    try:
        connection.execute('DELETE FROM "%s"' % candidates.CATALOG_FTS_TABLE)
        connection.commit()
        status = candidates.trigram_index_status(
            connection, table='assets', id_column='id',
            fts_table=candidates.CATALOG_FTS_TABLE)
    finally:
        connection.close()
    assert not status.usable
    assert 'of' in status.reason and 'catalog rows' in status.reason


def test_a_changed_trigram_marker_is_refused(tmp_path):
    index = make_index(tmp_path)
    assert index.build().complete
    connection = sqlite3.connect(index.cache)
    try:
        connection.execute('UPDATE catalog_state SET value=? WHERE key=?',
                           (json.dumps({'total': 999, 'last_id': 999, 'source': {}}),
                            candidates.TRIGRAM_MARKER_KEY))
        connection.commit()
        status = candidates.trigram_index_status(
            connection, table='assets', id_column='id',
            fts_table=candidates.CATALOG_FTS_TABLE)
    finally:
        connection.close()
    assert not status.usable
    assert 'build state' in status.reason


def test_a_changed_trigram_text_is_refused(tmp_path):
    index = make_index(tmp_path)
    assert index.build().complete
    connection = sqlite3.connect(index.cache)
    try:
        connection.execute('UPDATE "%s" SET text=? WHERE rowid=1'
                           % candidates.CATALOG_FTS_TABLE, ('completely different',))
        connection.commit()
        status = candidates.trigram_index_status(
            connection, table='assets', id_column='id',
            fts_table=candidates.CATALOG_FTS_TABLE)
    finally:
        connection.close()
    assert not status.usable
    assert 'does not match' in status.reason


def test_a_rebuild_drops_the_stale_index_and_builds_a_fresh_one(tmp_path):
    index = make_index(tmp_path)
    assert index.build().complete
    with closing(index._cache(readonly=True)) as cache:
        index._ready(cache)
        assert candidates.trigram_index_status(
            cache, table='assets', id_column='id',
            fts_table=candidates.CATALOG_FTS_TABLE).usable
    # A changed source invalidates the cache: the rebuild has to drop the old
    # index instead of leaving a marker that describes a table that was
    # emptied.
    with sqlite3.connect(index.source) as db:
        db.execute('UPDATE items SET x=1 WHERE id=1')
    rebuilt = index.build()
    assert rebuilt.complete and not rebuilt.resumed
    with closing(index._cache(readonly=True)) as cache:
        index._ready(cache)
        status = candidates.trigram_index_status(
            cache, table='assets', id_column='id',
            fts_table=candidates.CATALOG_FTS_TABLE)
    assert status.usable, status.reason


def test_a_build_without_the_trigram_index_leaves_no_usable_index(tmp_path):
    index = make_index(tmp_path)
    assert index.build(trigram=False).complete
    with closing(index._cache(readonly=True)) as cache:
        index._ready(cache)
        status = candidates.trigram_index_status(
            cache, table='assets', id_column='id',
            fts_table=candidates.CATALOG_FTS_TABLE)
    assert not status.usable
    assert 'marker' in status.reason


def test_the_trigram_index_and_the_exact_scan_answer_identically(tmp_path):
    index = make_index(tmp_path)
    assert index.build().complete
    queries = ['雨夜', '精选木', 'äpfel', 'ΣΊΣΥ', 'istanbul', '100%', '100%_literal',
               'needle', 'wood', '木纹', '材质', 'a', 'un', 'night wood', '100AA']
    for query in queries:
        exact = exact_ids(index, query)
        shortlist = trigram_ids(index, query)
        if shortlist is None:
            # No indexable anchor: the exact scan answers, and it is right.
            assert exact == sorted(exact)
            continue
        assert sorted(shortlist) == sorted(exact), query


def test_a_short_query_has_no_trigram_anchor_and_uses_the_exact_scan(tmp_path):
    index = make_index(tmp_path)
    assert index.build().complete
    assert trigram_ids(index, '木纹') is None
    assert trigram_ids(index, 'ab') is None
    assert exact_ids(index, '木纹') == [8]


def test_summaries_answers_a_whole_overlay_in_one_call(tmp_path):
    index = make_index(tmp_path)
    assert index.build().complete
    with closing(index._cache(readonly=True)) as cache:
        index._ready(cache)
        ids = [row[0] for row in cache.execute('SELECT id FROM assets ORDER BY id')]
    # One call, not one call per 1024 ids: the ids are chunked inside.
    rows = index.summaries(ids)
    assert [row['id'] for row in rows] == sorted(ids)
    assert index.summaries([]) == ()
    with pytest.raises(ValueError, match='bounded batches'):
        index.summaries(range(index.MAX_SUMMARY_IDS + 1))


def raw_text_count(index, text):
    """The exact text count over the catalog rows, written out independently."""
    with closing(index._cache(readonly=True)) as cache:
        index._ready(cache)
        conditions = ['deleted_at IS NULL', "kind NOT IN ('text','path')"]
        params = []
        for word in text.split():
            word = word.casefold()
            conditions.append('(instr(prism_casefold(title),?)>0 OR '
                              'instr(prism_casefold(notes),?)>0 OR '
                              'instr(prism_casefold(filename),?)>0 OR '
                              'instr(prism_tag_text(tags),?)>0)')
            params.extend([word] * 4)
        return cache.execute('SELECT COUNT(*) FROM assets WHERE '
                             + ' AND '.join(conditions), params).fetchone()[0]


def test_count_with_text_uses_the_trigram_index_and_answers_the_same(tmp_path):
    index = make_index(tmp_path)
    assert index.build().complete
    for query in ('wood', 'needle', '材质', 'a', '100%', '100%_literal', '素材'):
        assert index.count(text=query) == raw_text_count(index, query), query
    # The anchored query took the trigram route, the short one the exact scan.
    index.count(text='wood')
    assert index.last_count_stats['route'] == 'trigram'
    assert index.last_count_stats['candidates'] >= index.last_count_stats['hits']
    assert 0 <= index.last_count_stats['selectivity'] <= 1
    index.count(text='a')
    assert index.last_count_stats['route'] == 'exact'


def test_count_with_text_falls_back_to_the_exact_scan_without_a_trusted_index(tmp_path):
    index = make_index(tmp_path)
    assert index.build().complete
    expected = raw_text_count(index, 'wood')
    connection = sqlite3.connect(index.cache)
    try:
        connection.execute('DELETE FROM "%s"' % candidates.CATALOG_FTS_TABLE)
        connection.commit()
    finally:
        connection.close()
    # A broken index must not answer with a short list.
    assert index.count(text='wood') == expected
    assert index.last_count_stats['route'] == 'exact'


# ── the unassigned / unclassified bucket ────────────────────────

BUCKET_RECORDS = [
    (1, 'pixmap', dict(canvasId='A', title='on a known canvas', notes='')),
    (2, 'pixmap', dict(canvasId='B', title='on another known canvas', notes='')),
    (3, 'pixmap', dict(canvasId='C', title='on a canvas that is gone', notes='')),
    (4, 'pixmap', dict(canvasId=None, title='without a canvas', notes='')),
    (5, 'pixmap', dict(canvasId='default-canvas', title='on the default canvas', notes='')),
]


def effective_canvas(canvas_id, known):
    """The memory side of the bucket: the rule of ``project_catalog``."""
    if canvas_id is None:
        return UNASSIGNED_CANVAS
    if canvas_id in known or canvas_id == 'default-canvas':
        return canvas_id
    return UNASSIGNED_CANVAS


def test_the_unassigned_bucket_can_be_pushed_down(tmp_path):
    index = make_index(tmp_path, BUCKET_RECORDS, name='bucket')
    assert index.build().complete
    known = {'A', 'B'}
    rows = index.query(canvas_ids=(UNASSIGNED_CANVAS,), known_canvas_ids=known, limit=1024)
    expected = [row['id'] for row in index.query(limit=1024)
                if effective_canvas(row['canvas_id'], known) == UNASSIGNED_CANVAS]
    assert sorted(row['id'] for row in rows) == expected
    assert sorted(row['id'] for row in rows) == [3, 4]


def test_the_unclassified_bucket_can_be_pushed_down(tmp_path):
    index = make_index(tmp_path, BUCKET_RECORDS, name='bucket')
    assert index.build().complete
    known = {'A', 'B'}
    rows = index.query(canvas_ids=(UNCLASSIFIED_CANVAS,), known_canvas_ids=known, limit=1024)
    expected = [row['id'] for row in index.query(limit=1024)
                if effective_canvas(row['canvas_id'], known)
                in ('default-canvas', UNASSIGNED_CANVAS)]
    assert sorted(row['id'] for row in rows) == expected
    assert sorted(row['id'] for row in rows) == [3, 4, 5]


def test_a_bucket_beside_a_concrete_canvas_is_the_union(tmp_path):
    index = make_index(tmp_path, BUCKET_RECORDS, name='bucket')
    assert index.build().complete
    known = {'A', 'B'}
    rows = index.query(canvas_ids=('A', UNASSIGNED_CANVAS), known_canvas_ids=known, limit=1024)
    expected = [row['id'] for row in index.query(limit=1024)
                if effective_canvas(row['canvas_id'], known) in ('A', UNASSIGNED_CANVAS)]
    assert sorted(row['id'] for row in rows) == expected


def test_the_bucket_without_the_known_canvas_ids_does_not_raise(tmp_path):
    index = make_index(tmp_path, BUCKET_RECORDS, name='bucket')
    assert index.build().complete
    # Without the known canvases the catalog can only say which rows belong
    # to no canvas at all. It must not raise: the caller gets the catalog's
    # own answer, not an exception.
    rows = index.query(canvas_ids=(UNASSIGNED_CANVAS,), limit=1024)
    assert sorted(row['id'] for row in rows) == [4]
    rows = index.query(canvas_ids=(UNCLASSIFIED_CANVAS,), limit=1024)
    assert sorted(row['id'] for row in rows) == [4, 5]
    assert index.count(canvas_ids=(UNASSIGNED_CANVAS,)) == 1
    assert index.count(canvas_ids=(UNCLASSIFIED_CANVAS,)) == 2


# ── the overlay reconciliation ──────────────────────────────────

def test_summaries_grouped_returns_the_scalar_rows_in_input_order(tmp_path):
    index = make_index(tmp_path)
    assert index.build().complete
    with closing(index._cache(readonly=True)) as cache:
        index._ready(cache)
        ids = [row[0] for row in cache.execute('SELECT id FROM assets ORDER BY id')]
    # The input order is the contract, and an unknown id is not returned.
    asked = [ids[3], 99999, ids[0], ids[1]]
    rows = index.summaries_grouped(asked)
    assert [row['id'] for row in rows] == [ids[3], ids[0], ids[1]]
    assert set(rows[0]) == set(index.SUMMARY_FIELDS)
    # The rows are the ones the row summary returns.
    by_id = {row['id']: row for row in index.summaries(ids)}
    for row in rows:
        assert row['kind'] == by_id[row['id']]['kind']
        assert row['canvas_id'] == by_id[row['id']]['canvas_id']
        assert row['tags'] == by_id[row['id']]['tags']
        assert row['deleted_at'] == by_id[row['id']]['deleted_at']
    assert index.summaries_grouped([]) == ()


def test_summaries_buckets_answers_with_counters_not_rows(tmp_path):
    index = make_index(tmp_path)
    assert index.build().complete
    ids = [row['id'] for row in index.query(limit=1024)]
    grouped = index.summaries_buckets(ids)
    from_rows = {}
    for row in index.summaries(ids):
        if row['kind'] in ('text', 'path'):
            continue
        names = json.loads(row['tags'] or '[]')
        key = (row['canvas_id'], row['deleted_at'] is not None,
               not any(str(name).strip() for name in names))
        from_rows[key] = from_rows.get(key, 0) + 1
    assert dict(((row[0], row[1], row[2]), row[3]) for row in grouped) == from_rows


def test_summaries_buckets_of_every_row_answers_from_the_pre_aggregate(tmp_path):
    """The fast path must give the numbers the row scan gives.

    A caller that covers every catalog row - a fully loaded project - makes
    the buckets of the covered rows the buckets of the whole catalog, which
    the pre-aggregate already holds. The two paths must not drift.
    """
    index = make_index(tmp_path)
    assert index.build().complete
    with closing(index._cache(readonly=True)) as cache:
        index._ready(cache)
        ids = [row[0] for row in cache.execute('SELECT id FROM assets ORDER BY id')]
    with closing(index._cache(readonly=True)) as cache:
        index._ready(cache)
        # The fast path is live when every row is covered ...
        assert index._buckets_of_every_row(cache, ids) is not None
        # ... and it stays out of the way when the caller covers less.
        assert index._buckets_of_every_row(cache, ids[:-1]) is None
    fast = index.summaries_buckets(ids)
    from_rows = {}
    for row in index.summaries(ids):
        if row['kind'] in ('text', 'path'):
            continue
        names = json.loads(row['tags'] or '[]')
        key = (row['canvas_id'], row['deleted_at'] is not None,
               not any(str(name).strip() for name in names))
        from_rows[key] = from_rows.get(key, 0) + 1
    assert dict(((row[0], row[1], row[2]), row[3]) for row in fast) == from_rows


def test_summaries_buckets_does_not_take_the_fast_path_on_a_partial_cover(tmp_path):
    """As many ids as rows is not the same as covering every row.

    One row is left out and one id that is not a row is added: the count of
    ids is the same, the coverage is not. The fast path has to stay out of
    the way, or it would answer with the buckets of the whole catalog.
    """
    index = make_index(tmp_path)
    assert index.build().complete
    with closing(index._cache(readonly=True)) as cache:
        index._ready(cache)
        ids = [row[0] for row in cache.execute('SELECT id FROM assets ORDER BY id')]
    partial = [value for value in ids if value != ids[0]] + [999999]
    assert len(partial) == len(ids)
    with closing(index._cache(readonly=True)) as cache:
        index._ready(cache)
        assert index._buckets_of_every_row(cache, partial) is None
    buckets = dict(((row[0], row[1], row[2]), row[3])
                   for row in index.summaries_buckets(partial))
    everything = dict(((row[0], row[1], row[2]), row[3])
                      for row in index.summaries_buckets(ids))
    assert buckets != everything
    assert sum(buckets.values()) == sum(everything.values()) - 1


def test_the_id_chunk_never_exceeds_the_runtime_variable_limit(tmp_path):
    """One statement may not carry more ids than this SQLite accepts."""
    index = make_index(tmp_path)
    assert index.build().complete
    with closing(index._cache(readonly=True)) as cache:
        index._ready(cache)
        limit = _variable_limit(cache)
        assert index._chunk_size(cache) == min(index.SUMMARY_CHUNK, limit)
        assert index._chunk_size(cache) <= limit


def test_summaries_uncovered_is_the_complement_of_the_covered_rows(tmp_path):
    index = make_index(tmp_path)
    assert index.build().complete
    with closing(index._cache(readonly=True)) as cache:
        index._ready(cache)
        ids = [row[0] for row in cache.execute('SELECT id FROM assets ORDER BY id')]
    everything = index.summaries_buckets(ids)
    # Every catalog row is covered: the disk has nothing left to add.
    assert index.summaries_uncovered(ids) == ()
    half = ids[:len(ids) // 2]
    uncovered = index.summaries_uncovered(half)
    covered = index.summaries_buckets(half)
    merged = {}
    for row in list(uncovered) + list(covered):
        key = (row[0], row[1], row[2])
        merged[key] = merged.get(key, 0) + row[3]
    assert merged == {(row[0], row[1], row[2]): row[3] for row in everything}


def test_a_canvas_id_string_is_one_id_not_its_characters(tmp_path):
    index = make_index(tmp_path)
    assert index.build().complete
    # The scope layer already wraps a string; the index may not split it.
    rows = index.query(canvas_ids='A', limit=1024)
    assert rows and all(row['canvas_id'] == 'A' for row in rows)
    assert index.count(canvas_ids='A') == len(rows)


# ── honesty ─────────────────────────────────────────────────────

def test_a_stale_catalog_raises_instead_of_answering_zero(tmp_path):
    index = make_index(tmp_path)
    assert index.build().complete
    with sqlite3.connect(index.source) as db:
        db.execute('UPDATE items SET x=1 WHERE id=1')
    with pytest.raises(CatalogNotReady):
        index.canvas_counts()
    with pytest.raises(CatalogNotReady):
        index.count()
    with pytest.raises(CatalogNotReady):
        index.grouped_counts()


def test_a_missing_catalog_raises_instead_of_answering_zero(tmp_path):
    index = make_index(tmp_path)
    with pytest.raises(CatalogNotReady):
        index.canvas_counts()
    with pytest.raises(CatalogNotReady):
        index.count()
