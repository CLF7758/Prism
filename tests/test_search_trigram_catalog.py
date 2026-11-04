"""D-1: the catalog search reaches the trigram virtual table and stays exact.

The production path is ``prism.project_search.search_catalog_assets``. It reads
the project asset catalog through ``ProjectOpenScope``; this file proves that

* the trigram index of the catalog cache is really used when it is verified,
* the answers are the same with and without that index,
* Chinese text, Unicode case folding and literal ``%``/``_`` survive the
  trigram projection,
* more than one page of same-kind matches is complete and not falsely empty,
* a cancelled scan raises instead of publishing half an answer.
"""
import json
import sqlite3
from types import SimpleNamespace

import pytest

from prism import search_candidates as candidates
from prism.project_open_scope import OpenScopeCancelled, ProjectOpenScope
from prism.project_search import search_catalog_assets


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
]


def scope_for(tmp_path, records, *, trigram=True):
    """A ready catalog scope for a small synthetic project.

    ``trigram=False`` builds the catalog without filling the trigram index -
    the state of every cache that was built before the index existed.
    """
    source = tmp_path / 'catalog-source.prism'
    with sqlite3.connect(source) as db:
        db.executescript("CREATE TABLE items(id INTEGER PRIMARY KEY,type TEXT,x,y,z,scale,rotation,flip,data);"
                         "CREATE TABLE sqlar(item_id INTEGER PRIMARY KEY,data BLOB);")
        for key, kind, extra in records:
            db.execute('INSERT INTO items VALUES(?,?,?,?,?,?,?,?,?)',
                       (key, kind, 0, 0, 0, 1, 0, 0, json.dumps(extra, ensure_ascii=False)))
    scope = ProjectOpenScope(source, tmp_path / 'catalog.sqlite')
    assert scope.build(trigram=trigram).complete
    return scope


def index_catalog(scope):
    """Build the trigram index the way its owner has to build it."""
    connection = sqlite3.connect(scope.cache_path)
    try:
        candidates.create_trigram_index(connection)
        rows = connection.execute('SELECT id,title,filename,notes,tags FROM assets').fetchall()
        candidates.populate_trigram_index(
            connection, candidates.CATALOG_FTS_TABLE,
            ((row[0], candidates.catalog_haystack(
                row[1], row[2], row[3], candidates.catalog_tag_names(row[4])))
             for row in rows))
        candidates.write_trigram_marker(connection, candidates.build_state(connection))
        connection.commit()
    finally:
        connection.close()
    return scope


def traced(monkeypatch):
    """Record the SQL the production path runs against the catalog cache."""
    statements = []
    original = candidates.open_readonly_cache

    def opening(path):
        connection = original(path)
        connection.set_trace_callback(statements.append)
        return connection

    monkeypatch.setattr('prism.project_search.open_readonly_cache', opening)
    return statements


def scene_and_service():
    return SimpleNamespace(), SimpleNamespace(nodes=[])


def service_with_page(canvas_id, title):
    """A resource service whose page node carries ``title`` in its path."""
    node = {'id': 'page-'+canvas_id, 'nodeType': 'page', 'pageId': canvas_id,
            'title': title, 'parentId': None}
    return SimpleNamespace(), SimpleNamespace(nodes=[node])


def keyset_ids(scope, query, limit=256):
    """Walk the scope's keyset pages by hand and collect every id."""
    after, out = None, []
    while True:
        rows = scope.query(text=query, limit=limit, after_id=after)
        if not rows:
            break
        out.extend(str(row['id']) for row in rows)
        after = rows[-1]['id']
        if len(rows) < limit:
            break
    return out


def ids(scope, query, **kwargs):
    scene, service = scene_and_service()
    return [hit['item_id'] for hit in
            search_catalog_assets(scope, scene, service, query, **kwargs)]


def test_chinese_query_matches_chinese_rows_with_and_without_the_index(tmp_path):
    scope = scope_for(tmp_path, RECORDS)
    # '雨夜' is two characters: no trigram anchor, so the exact scan answers.
    assert ids(scope, '雨夜') == ['1']
    # '精选木' is three characters: the trigram index may answer it.
    assert ids(scope, '精选木') == ['8']
    index_catalog(scope)
    assert ids(scope, '雨夜') == ['1']
    assert ids(scope, '精选木') == ['8']


def test_the_production_path_reaches_the_trigram_virtual_table(tmp_path, monkeypatch):
    scope = scope_for(tmp_path, RECORDS)
    index_catalog(scope)
    statements = traced(monkeypatch)
    assert ids(scope, '精选木') == ['8']
    assert any('MATCH' in statement for statement in statements), statements
    assert any('assets_fts' in statement for statement in statements), statements


def test_without_an_index_the_exact_scan_runs_and_answers_the_same(tmp_path, monkeypatch):
    """A cache that was built before the index existed must still answer."""
    scope = scope_for(tmp_path, RECORDS, trigram=False)
    statements = traced(monkeypatch)
    assert ids(scope, '精选木') == ['8']
    assert not any('MATCH' in statement for statement in statements), statements


def test_a_cache_without_the_index_table_at_all_answers_the_same(tmp_path, monkeypatch):
    scope = scope_for(tmp_path, RECORDS, trigram=False)
    connection = sqlite3.connect(scope.cache_path)
    connection.execute('DROP TABLE IF EXISTS assets_fts')
    connection.commit()
    connection.close()
    statements = traced(monkeypatch)
    assert ids(scope, '精选木') == ['8']
    assert ids(scope, '雨夜') == ['1']
    assert not any('MATCH' in statement for statement in statements), statements


def test_index_and_exact_scan_answer_identically(tmp_path):
    scope = scope_for(tmp_path, RECORDS)
    queries = ['雨夜', '精选木', 'äpfel', 'ΣΊΣΥ', 'istanbul', '100%', 'needle', 'wood']
    exact = {query: ids(scope, query) for query in queries}
    index_catalog(scope)
    indexed = {query: ids(scope, query) for query in queries}
    assert indexed == exact


def test_percent_and_underscore_are_literal_not_like_wildcards(tmp_path):
    scope = scope_for(tmp_path, RECORDS)
    index_catalog(scope)
    # '%' alone must not match every row the way a LIKE wildcard would.
    assert ids(scope, '%') == ['2']
    assert ids(scope, '%_') == ['2']
    assert ids(scope, '100%') == ['2']
    assert ids(scope, '100%_literal') == ['2']
    # The row that only shares the non-wildcard part is not a match.
    assert ids(scope, '100AA') == ['7']


def test_like_literal_keeps_wildcards_literal_for_a_like_based_path():
    connection = sqlite3.connect(':memory:')
    connection.execute('CREATE TABLE t(value TEXT)')
    connection.execute('INSERT INTO t VALUES(?)', ('100%_literal',))
    connection.execute('INSERT INTO t VALUES(?)', ('100AA_literal',))
    # LIKE matches the whole value, so the literal pattern is the whole value.
    rows = connection.execute("SELECT value FROM t WHERE value LIKE ? ESCAPE '\\'",
                              (candidates.like_literal('100%_literal'),)).fetchall()
    assert rows == [('100%_literal',)]
    # A pattern that used to be a wildcard match now matches nothing else.
    assert connection.execute("SELECT value FROM t WHERE value LIKE ? ESCAPE '\\'",
                              (candidates.like_literal('%_'),)).fetchall() == []
    # Without the escape the same pattern matches both rows.
    assert len(connection.execute('SELECT value FROM t WHERE value LIKE ?',
                                  ('%_',)).fetchall()) == 2


def test_unicode_case_folding_is_correct_and_turkish_i_stays_distinct(tmp_path):
    scope = scope_for(tmp_path, RECORDS)
    index_catalog(scope)
    assert ids(scope, 'äpfel') == ['3']
    assert ids(scope, 'ÄPFEL') == ['3']
    assert ids(scope, 'σίσυ') == ['4']
    assert ids(scope, 'ΣΊΣΥ') == ['4']
    # 'İ' folds to 'i' + combining dot, so it stays distinct from 'i' and 'ı'.
    assert ids(scope, 'İSTANBUL') == ['5']
    assert ids(scope, 'istanbul') == ['6']
    assert ids(scope, 'ıstanbul') == []


def test_more_than_one_page_of_same_kind_matches_is_complete(tmp_path):
    records = [(key, 'pixmap', dict(canvasId='A', title='砖墙 Needle %d' % key))
               for key in range(-3, 1500)]
    scope = scope_for(tmp_path, records)
    index_catalog(scope)
    hits = ids(scope, '砖墙 needle')
    assert len(hits) == 1503
    assert set(hits) == {str(key) for key in range(-3, 1500)}
    # The keyset paging of the scope keeps every row as well.
    assert len(list(scope.iter_query(text='needle', limit=256))) == 1503
    assert scope.count(text='needle') == 1503


def test_a_short_query_has_no_trigram_anchor_and_falls_back_to_the_exact_scan(tmp_path,
                                                                             monkeypatch):
    scope = scope_for(tmp_path, RECORDS)
    index_catalog(scope)
    statements = traced(monkeypatch)
    # '木纹' is two characters: the trigram tokenizer cannot index it.
    assert ids(scope, '木纹') == ['8']
    assert not any('MATCH' in statement for statement in statements), statements


def test_a_stale_index_marker_falls_back_to_the_exact_scan(tmp_path, monkeypatch):
    scope = scope_for(tmp_path, RECORDS)
    index_catalog(scope)
    connection = sqlite3.connect(scope.cache_path)
    connection.execute('UPDATE catalog_state SET value=? WHERE key=?',
                       (json.dumps({'total': 999, 'last_id': 999, 'source': {}}),
                        candidates.TRIGRAM_MARKER_KEY))
    connection.commit()
    connection.close()
    statements = traced(monkeypatch)
    assert ids(scope, '雨夜') == ['1']
    assert not any('MATCH' in statement for statement in statements), statements


def test_an_index_that_does_not_describe_the_catalog_is_refused(tmp_path, monkeypatch):
    scope = scope_for(tmp_path, RECORDS)
    index_catalog(scope)
    connection = sqlite3.connect(scope.cache_path)
    connection.execute('DELETE FROM assets_fts')
    connection.execute("INSERT INTO assets_fts(rowid,text) VALUES(1,'completely different')")
    connection.commit()
    connection.close()
    statements = traced(monkeypatch)
    assert ids(scope, '雨夜') == ['1']
    assert not any('MATCH' in statement for statement in statements), statements


def test_a_cancelled_candidate_scan_raises_instead_of_publishing_half(tmp_path):
    scope = scope_for(tmp_path, [(key, 'pixmap', dict(canvasId='A', title='needle %d' % key))
                                 for key in range(20)])
    index_catalog(scope)
    calls = []

    def cancel():
        calls.append(1)
        return len(calls) > 2

    with pytest.raises(OpenScopeCancelled):
        ids(scope, 'needle', cancel=cancel)


def test_the_candidate_scan_itself_raises_search_cancelled_between_batches(tmp_path):
    scope = scope_for(tmp_path, [(key, 'pixmap', dict(canvasId='A', title='needle %d' % key))
                                 for key in range(20)])
    index_catalog(scope)
    connection = sqlite3.connect(scope.cache_path)
    iterator = candidates.iter_catalog_candidate_rows(
        connection, 'needle', fts_table=candidates.CATALOG_FTS_TABLE, batch_size=1,
        cancel=lambda: cancelled)
    try:
        cancelled = False
        assert next(iterator)['id'] == 0
        cancelled = True
        with pytest.raises(candidates.SearchCancelled):
            next(iterator)
    finally:
        iterator.close()
        connection.close()


def test_text_annotations_are_not_asset_candidates(tmp_path):
    scope = scope_for(tmp_path, RECORDS)
    index_catalog(scope)
    assert '10' not in ids(scope, 'needle')


def test_the_trigram_index_is_refused_without_a_freshness_marker(tmp_path):
    scope = scope_for(tmp_path, RECORDS)
    index_catalog(scope)
    connection = sqlite3.connect(scope.cache_path)
    connection.execute('DELETE FROM catalog_state WHERE key=?',
                       (candidates.TRIGRAM_MARKER_KEY,))
    connection.commit()
    connection.close()
    status = candidates.cached_trigram_status(scope.cache_path)
    assert not status.usable
    assert 'marker' in status.reason


# ── D-1, third round: the acceptance assertions of the brief ──────────


def test_more_than_1024_same_kind_matches_are_complete_on_both_paths(tmp_path):
    """One canvas, one kind, 1503 matches: neither path may lose one."""
    records = [(key, 'pixmap', dict(canvasId='A', title='墙面纹理 Needle %d' % key))
               for key in range(-3, 1500)]
    scope = scope_for(tmp_path, records)
    index_catalog(scope)
    hits = ids(scope, '墙面纹理 needle')
    assert len(hits) == 1503
    assert set(hits) == {str(key) for key in range(-3, 1500)}
    # The exact scan pages through the same rows, in id order, page by page.
    paged = keyset_ids(scope, 'needle', limit=256)
    assert len(paged) == 1503
    assert paged == sorted(paged, key=int)
    assert set(paged) == set(hits)
    # A second page walk must not repeat or drop a row.
    assert keyset_ids(scope, 'needle', limit=1024) == keyset_ids(scope, 'needle', limit=256)


def test_more_than_1024_matches_are_not_falsely_empty(tmp_path):
    """A missing index must look like a full answer, never like no answer."""
    records = [(key, 'pixmap', dict(canvasId='A', title='墙面纹理 Needle %d' % key))
               for key in range(-3, 1500)]
    scope = scope_for(tmp_path, records)
    without_index = ids(scope, '墙面纹理 needle')
    index_catalog(scope)
    with_index = ids(scope, '墙面纹理 needle')
    assert len(without_index) == len(with_index) == 1503
    assert without_index == with_index


def test_the_indexed_path_and_the_exact_path_answer_the_same_on_a_large_corpus(tmp_path):
    records = [(key, 'pixmap', dict(canvasId='A', title='墙面纹理 Needle %d' % key,
                                    notes='备注 %d' % key, filename='shot_%04d.png' % key))
               for key in range(400)]
    scope = scope_for(tmp_path, records)
    queries = ['墙面纹理', 'needle', '墙面纹理 needle', '备注', 'shot_0001',
               '针', 'nonexistent', '100%', 'wood']
    exact = {query: ids(scope, query) for query in queries}
    index_catalog(scope)
    indexed = {query: ids(scope, query) for query in queries}
    assert indexed == exact
    # The corpus really answers: the queries that match have hits.
    assert len(exact['墙面纹理']) == 400
    assert len(exact['needle']) == 400
    assert exact['nonexistent'] == []


def test_a_partial_index_is_refused_and_the_exact_scan_answers(tmp_path):
    """A half filled index would answer with a short list; it must be refused."""
    scope = scope_for(tmp_path, RECORDS)
    index_catalog(scope)
    connection = sqlite3.connect(scope.cache_path)
    connection.execute('DELETE FROM assets_fts WHERE rowid > 5')
    connection.commit()
    connection.close()
    status = candidates.cached_trigram_status(scope.cache_path)
    assert not status.usable
    assert 'rows' in status.reason
    assert ids(scope, '精选木') == ['8']
    # The annotation row is not an asset candidate, on either path.
    assert '10' not in ids(scope, 'needle')


def test_a_word_that_only_matches_the_resource_tree_path_is_not_a_candidate(tmp_path):
    """The catalog has no path column: a path hit must not become a hit."""
    records = [(1, 'pixmap', dict(canvasId='A', title='brick wall'))]
    scope = scope_for(tmp_path, records)
    index_catalog(scope)
    scene, service = service_with_page('A', 'oakgallery')
    hits = [hit['item_id'] for hit in
            search_catalog_assets(scope, scene, service, 'oak')]
    assert hits == []
    # The exact scan agrees: the catalog columns do not carry the word.
    assert ids(scope, 'oak') == []


def test_the_production_path_filters_short_words_with_the_exact_predicate(tmp_path):
    """A one or two character word cannot be indexed; it must still be exact."""
    scope = scope_for(tmp_path, RECORDS)
    index_catalog(scope)
    sql, parameters = candidates.catalog_candidate_sql('针 needle')
    assert 'prism_casefold' in sql
    assert 'prism_tag_text' in sql
    assert len(parameters) == 5   # the FTS phrase plus one short word, four times
    # The same predicate as the exact scan of the catalog.
    assert 'instr(prism_casefold(d."title"),?)>0' in sql
    assert 'instr(prism_tag_text(d."tags"),?)>0' in sql
    # A query of only indexable words needs no exact predicate at all.
    sql, parameters = candidates.catalog_candidate_sql('needle')
    assert 'prism_casefold' not in sql
    assert len(parameters) == 1


def test_the_candidate_statement_does_not_sort_every_match_in_a_temporary_b_tree(tmp_path):
    """The join shape materialised every match; the subquery shape does not."""
    scope = scope_for(tmp_path, RECORDS)
    index_catalog(scope)
    sql, parameters = candidates.catalog_candidate_sql('精选木')
    connection = sqlite3.connect(scope.cache_path)
    try:
        steps = [row[3] for row in
                 connection.execute('EXPLAIN QUERY PLAN '+sql, parameters)]
    finally:
        connection.close()
    assert not any('TEMP B-TREE' in step for step in steps), steps


def test_the_trigram_plan_ands_every_indexable_word(tmp_path):
    plan = candidates.catalog_trigram_plan('wall needle')
    assert plan is not None
    assert plan.fts_expression == '"needle" AND "wall"'
    assert plan.words == ('wall', 'needle')
    # A query of only short words has no indexable anchor at all.
    assert candidates.catalog_trigram_plan('a') is None
    assert candidates.catalog_trigram_plan('ab') is None
    assert candidates.catalog_trigram_plan('') is None


def test_populate_trigram_index_is_idempotent(tmp_path):
    scope = scope_for(tmp_path, RECORDS)
    index_catalog(scope)
    connection = sqlite3.connect(scope.cache_path)
    try:
        before = connection.execute('SELECT count(*) FROM assets_fts').fetchone()[0]
        rows = connection.execute('SELECT id,title,filename,notes,tags FROM assets').fetchall()
        candidates.populate_trigram_index(
            connection, candidates.CATALOG_FTS_TABLE,
            ((row[0], candidates.catalog_haystack(
                row[1], row[2], row[3], candidates.catalog_tag_names(row[4])))
             for row in rows))
        connection.commit()
        after = connection.execute('SELECT count(*) FROM assets_fts').fetchone()[0]
    finally:
        connection.close()
    assert before == after
    assert ids(scope, '精选木') == ['8']


def test_write_trigram_marker_rejects_a_state_that_is_not_a_mapping(tmp_path):
    scope = scope_for(tmp_path, RECORDS)
    connection = sqlite3.connect(scope.cache_path)
    try:
        with pytest.raises(ValueError):
            candidates.write_trigram_marker(connection, None)
    finally:
        connection.close()


def test_a_cancelled_scan_publishes_nothing(tmp_path):
    """The cancelled round must raise, not hand a half answer to the caller."""
    scope = scope_for(tmp_path, [(key, 'pixmap', dict(canvasId='A', title='needle %d' % key))
                                 for key in range(20)])
    index_catalog(scope)
    published = []
    with pytest.raises(OpenScopeCancelled):
        published.extend(ids(scope, 'needle', cancel=lambda: True))
    assert published == []


def test_the_exact_scan_is_used_when_the_query_has_no_indexable_word(tmp_path, monkeypatch):
    scope = scope_for(tmp_path, RECORDS)
    index_catalog(scope)
    statements = traced(monkeypatch)
    assert ids(scope, '木') == ['8']
    assert not any('MATCH' in statement for statement in statements), statements


def test_an_index_with_another_tokenizer_is_refused(tmp_path):
    """A folding tokenizer could merge two words the exact scan keeps apart."""
    scope = scope_for(tmp_path, RECORDS)
    connection = sqlite3.connect(scope.cache_path)
    try:
        connection.execute('DROP TABLE IF EXISTS assets_fts')
        connection.execute("CREATE VIRTUAL TABLE assets_fts USING fts5(text, tokenize='trigram')")
        rows = connection.execute('SELECT id,title,filename,notes,tags FROM assets').fetchall()
        candidates.populate_trigram_index(
            connection, candidates.CATALOG_FTS_TABLE,
            ((row[0], candidates.catalog_haystack(
                row[1], row[2], row[3], candidates.catalog_tag_names(row[4])))
             for row in rows))
        candidates.write_trigram_marker(connection, candidates.build_state(connection))
        connection.commit()
    finally:
        connection.close()
    status = candidates.cached_trigram_status(scope.cache_path)
    assert not status.usable
    assert 'case-sensitive' in status.reason
    assert ids(scope, '精选木') == ['8']


def test_create_trigram_index_replaces_a_table_with_a_foreign_tokenizer(tmp_path):
    """The owner side may find a cache from an older build; it must heal."""
    scope = scope_for(tmp_path, RECORDS)
    connection = sqlite3.connect(scope.cache_path)
    try:
        connection.execute('DROP TABLE IF EXISTS assets_fts')
        connection.execute("CREATE VIRTUAL TABLE assets_fts USING fts5(text, tokenize='trigram')")
        connection.commit()
        candidates.create_trigram_index(connection)
        statement = connection.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='assets_fts'").fetchone()[0]
    finally:
        connection.close()
    assert 'case_sensitive' in statement
    # The table is empty after the replacement, so the consumer still refuses
    # it - an index with no rows would answer with a short list.
    status = candidates.cached_trigram_status(scope.cache_path)
    assert not status.usable
    assert 'rows' in status.reason
