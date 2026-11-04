"""Contract checks for a future repository candidate adapter; no UI wiring."""
from contextlib import closing
import random
import sqlite3
from types import SimpleNamespace

import pytest

from prism import search_candidates as candidates
from prism.project_search import plain_text, search_project


def populated(rows, *, fts=False):
    connection = sqlite3.connect(':memory:')
    connection.execute('CREATE TABLE search_text(result_id TEXT UNIQUE, haystack TEXT)')
    if fts:
        connection.execute("CREATE VIRTUAL TABLE search_fts USING fts5(text,tokenize='trigram')")
    for index, text in enumerate(rows, 1):
        normalized = candidates.normalize_haystack(text)
        connection.execute('INSERT INTO search_text(rowid,result_id,haystack) VALUES(?,?,?)',
                           (index, f'result:{index}', normalized))
        if fts:
            connection.execute('INSERT INTO search_fts(rowid,text) VALUES(?,?)',
                               (index, candidates.trigram_projection(normalized)))
    connection.commit()
    return connection


def literal_ids(rows, query):
    words = tuple(dict.fromkeys(query.casefold().split()))
    return [f'result:{index}' for index, text in enumerate(rows, 1)
            if all(word in text.casefold() for word in words)]


def test_unicode61_and_two_character_trigram_are_not_substring_replacements():
    if not candidates.trigram_available():
        pytest.skip('Linked SQLite lacks FTS5 trigram; fallback tests still run')
    with closing(sqlite3.connect(':memory:')) as connection:
        connection.execute('CREATE VIRTUAL TABLE ordinary USING fts5(text)')
        connection.execute("CREATE VIRTUAL TABLE tri USING fts5(text,tokenize='trigram')")
        for table in ('ordinary', 'tri'):
            connection.execute(f'INSERT INTO {table} VALUES(?)', ('混凝土材质球',))
        assert '材质' in '混凝土材质球'
        assert connection.execute('SELECT rowid FROM ordinary WHERE ordinary MATCH ?', ('材质',)).fetchall() == []
        assert connection.execute('SELECT rowid FROM tri WHERE tri MATCH ?', ('"材质"',)).fetchall() == []
        assert connection.execute('SELECT rowid FROM tri WHERE tri MATCH ?', ('"土材质"',)).fetchall() == [(1,)]


ROWS = ['混凝土材质球 木头 粗糙度', '材质库 diffuse_METAL wood', 'Straße İSTANBUL Σίσυφος',
        'quoted "roughness" a*b c+d 100% _', 'prefix\x00wood suffix', '😀😃😄透明',
        '材质球 Wood', '粗糙 金属 stone', 'other']
QUERIES = ['材', '材质', '土材质', '木头', 'diffuse_metal', '材质 WOOD', 'wood wood',
           'STRASSE', 'straße', 'İSTANBUL', 'ίσυ', '"roughness"', 'a*b', 'c+d',
           '100%', '_', 'wood', 'prefix\x00wood', '😀😃😄', '透明', '" OR *',
           '粗糙度 金属', '', '  ', 'not-found']


@pytest.mark.parametrize('use_fts', [False, True])
@pytest.mark.parametrize('query', QUERIES)
def test_literal_words_and_unicode_match_existing_python_contract(query, use_fts):
    if use_fts and not candidates.trigram_available():
        pytest.skip('Linked SQLite lacks FTS5 trigram')
    with closing(populated(ROWS, fts=use_fts)) as connection:
        assert list(candidates.iter_candidate_ids(connection, query,
                    fts_table='search_fts' if use_fts else None, batch_size=2)) == literal_ids(ROWS, query)


def test_missing_trigram_support_uses_exact_scan_without_touching_fts(monkeypatch):
    monkeypatch.setattr(candidates, 'trigram_available', lambda: False)
    with closing(populated(ROWS)) as connection:
        assert list(candidates.iter_candidate_ids(connection, 'wood', fts_table='not_created')) == literal_ids(ROWS, 'wood')


def test_tag_branch_requires_explicit_callers_contract():
    with closing(populated(ROWS)) as connection:
        # Global project search's tag branch bypasses ordinary text; the
        # caller must still apply its tag predicate after these candidates.
        assert len(list(candidates.iter_candidate_ids(connection, 'absent', text_filter_enabled=False))) == len(ROWS)
        # The asset catalog ANDs tags with text, so it retains this filter.
        assert list(candidates.iter_candidate_ids(connection, 'absent')) == []


def test_all_candidates_are_consumed_without_pre_ranking_top_n():
    with closing(populated(['wood']*257)) as connection:
        result = list(candidates.iter_candidate_ids(connection, 'wood', batch_size=3))
        assert result == [f'result:{index}' for index in range(1, 258)]


def test_cancellation_is_not_an_empty_success_and_cursor_can_be_closed():
    with closing(populated(['wood']*20)) as connection:
        cancel = False
        iterator = candidates.iter_candidate_ids(connection, 'wood', batch_size=1, cancel=lambda: cancel)
        assert next(iterator) == 'result:1'
        cancel = True
        with pytest.raises(candidates.SearchCancelled):
            next(iterator)
        connection.execute('DROP TABLE search_text')


@pytest.mark.parametrize('keyword,value', [('table', 'search_text; DROP TABLE search_text'),
                                        ('fts_table', 'search_fts WHERE 1=1'),
                                        ('batch_size', 0), ('batch_size', 1025)])
def test_sql_identifiers_and_batch_size_are_checked(keyword, value):
    with closing(populated(ROWS)) as connection:
        with pytest.raises(ValueError):
            list(candidates.iter_candidate_ids(connection, 'wood', **{keyword: value}))
        assert connection.execute('SELECT count(*) FROM search_text').fetchone() == (len(ROWS),)


def test_read_adapter_does_not_build_or_mutate_snapshot():
    with closing(populated(ROWS)) as connection:
        statements = []
        connection.set_trace_callback(statements.append)
        list(candidates.iter_candidate_ids(connection, '材质'))
        assert statements and all(statement.startswith('SELECT ') for statement in statements)


def test_long_queries_preserve_all_words_without_sql_expression_limit():
    query = ' '.join(f'w{index}' for index in range(1200))
    rows = [query, ' '.join(f'w{index}' for index in range(1199))]
    with closing(populated(rows, fts=candidates.trigram_available())) as connection:
        assert list(candidates.iter_candidate_ids(connection, query,
                    fts_table='search_fts' if candidates.trigram_available() else None)) == ['result:1']


def test_random_substrings_do_not_lose_candidates():
    if not candidates.trigram_available():
        pytest.skip('Linked SQLite lacks FTS5 trigram')
    rng = random.Random(20261021)
    alphabet = '木头材质球金属AbCße\u0301_"*%\x00😀'
    rows = [''.join(rng.choice(alphabet) for _ in range(60)) for _ in range(250)]
    queries = [rows[rng.randrange(len(rows))][offset:offset+rng.randrange(1, 10)]
               for offset in range(40)]
    with closing(populated(rows, fts=True)) as connection:
        for query in queries:
            assert list(candidates.iter_candidate_ids(connection, query, fts_table='search_fts')) == literal_ids(rows, query)


def test_document_candidates_preserve_original_ranking_occurrences_and_versions():
    pages = [dict(id='late-exact', kind='document', title='材质', content='<p>wood wood</p>', tags=[]),
             dict(id='early-body', kind='document', title='A', content='<p>材质 材质</p>', tags=[]),
             dict(id='unrelated', kind='document', title='B', content='<p>metal</p>', tags=[])]
    nodes = [dict(id='node:'+page['id'], nodeType='page', pageId=page['id'],
                  title=page['title'], parentId=None) for page in pages]
    scene = SimpleNamespace(workspace_pages=pages, material_records=[], workspace_nodes=nodes,
                            tag_names=[], items=lambda: [])
    service = SimpleNamespace(nodes=nodes)
    # Same complete title/path/body haystack as this fixture's search_project
    # add() call. No HTML parser or offsets are reimplemented in the adapter.
    rows = [' '.join([page['title'], page['title'], plain_text(page['content'])]) for page in pages]
    with closing(populated(rows, fts=candidates.trigram_available())) as connection:
        for query in ('材质', 'wood', '材质 材质'):
            ids = set(candidates.iter_candidate_ids(connection, query,
                      fts_table='search_fts' if candidates.trigram_available() else None))
            selected = [page for index, page in enumerate(pages, 1) if f'result:{index}' in ids]
            assert search_project(scene, service, query, page_records=selected) == search_project(scene, service, query)
