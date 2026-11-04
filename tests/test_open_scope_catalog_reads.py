"""C2: what a catalog read costs, and the guards in front of the filters.

Three things are pinned here:

* the C2 count point -- ``scope.last_read_stats`` and the module level
  ``catalog_read_counters()`` -- must show that a filtered read touched one
  page more than it hit, never the whole library;
* a ``canvas_ids`` that is a plain string is ONE canvas id. The catalog layer
  splits a string into characters, so the guard in ``project_open_scope`` is
  the only thing standing between a caller mistake and a silent "no rows";
* the filters really reach the catalog, and a wrong type fails loudly
  instead of quietly matching nothing.

Nothing here touches the real 100k project: every project is built in
``tmp_path``.
"""
from contextlib import closing
import json
import sqlite3

import pytest

from prism.project_catalog_index import (UNCLASSIFIED_CANVAS, CatalogNotReady,
                                         ProjectCatalogIndex)
from prism.project_open_scope import (FILTER_NAMES, OpenScopeCancelled,
                                      ProjectOpenScope,
                                      add_catalog_read_observer,
                                      catalog_read_counters,
                                      remove_catalog_read_observer)


CANVAS_A, CANVAS_B, CANVAS_C = 'canvas-a', 'canvas-b', 'canvas-c'

#: The three filters the C2 criterion talks about.
TIERS = (('one canvas', dict(canvas_ids=(CANVAS_A,))),
         ('unclassified', dict(canvas_ids=(None,))),
         ('untagged', dict(untagged_only=True)))


def build_project(path):
    """A small library with all three tiers, plus one deliberately wide canvas.

    * ``canvas-a``: 70 items, the first 20 without tags -> 70 hits, 20 untagged
    * ``canvas-b``: 30 items, all tagged
    * no canvas at all: 25 items -> the unclassified bucket
    * ``canvas-c``: 250 items -> a filter that matches more than one page
    """
    layout = []
    for index in range(70):
        layout.append(dict(canvasId=CANVAS_A, kind='pixmap',
                           tags=[] if index < 20 else ['keep'],
                           rating=index % 3, title='Asset %d' % index))
    for index in range(30):
        layout.append(dict(canvasId=CANVAS_B, kind='pixmap', tags=['keep'],
                           rating=1, title='B asset %d' % index))
    for index in range(25):
        layout.append(dict(canvasId=None, kind='pixmap', tags=['keep'],
                           rating=2, title='Loose %d' % index))
    for index in range(250):
        layout.append(dict(canvasId=CANVAS_C, kind='image', tags=['keep'],
                           rating=5, title='Wide %d' % index))
    with closing(sqlite3.connect(path)) as connection:
        connection.executescript('''
            CREATE TABLE items(id INTEGER PRIMARY KEY,type TEXT,x,y,z,scale,rotation,flip,data);
            CREATE TABLE sqlar(item_id INTEGER PRIMARY KEY,data BLOB);
            CREATE TABLE workspace_pages(id TEXT,kind TEXT,title TEXT);
        ''')
        for key, extra in enumerate(layout, start=1):
            extra = dict(extra, imageWidth=2048, imageHeight=1024)
            connection.execute('INSERT INTO items VALUES(?,?,?,?,?,?,?,?,?)',
                               (key, extra['kind'], key, 4, 0, 1, 30, 1,
                                json.dumps(extra)))
            connection.execute('INSERT INTO sqlar VALUES(?,?)',
                               (key, bytes([key % 256])*20))
        connection.commit()
    return layout


@pytest.fixture
def project(tmp_path):
    source = tmp_path/'reads.prism'
    build_project(source)
    return source


@pytest.fixture
def scope(project, tmp_path):
    instance = ProjectOpenScope(project, tmp_path/'reads-cache.sqlite')
    assert instance.build(batch_size=64).complete
    return instance


class SpyIndex:
    """Records every ``query`` the scope hands to the catalog."""

    def __init__(self, inner):
        self.inner = inner
        self.calls = []

    def __getattr__(self, name):
        return getattr(self.inner, name)

    def query(self, **kwargs):
        self.calls.append(kwargs)
        return self.inner.query(**kwargs)


# ── C2: the count point ────────────────────────────────────────────────

@pytest.mark.parametrize('label,filters', TIERS)
def test_a_filtered_read_costs_one_page_more_than_it_hits(scope, label, filters):
    """C2: 累计取行数 <= 命中数 + 1 页；limit=100 时参与排序的元素数 <= 100.

    These three tiers match at most 100 rows each, so the third criterion
    ("the read returned no more rows than the caller asked for") holds here
    too -- see ``test_the_total_returned_rows_must_not_exceed_the_limit`` for
    the tier that does not.
    """
    hits = scope.count(**filters)
    assert hits is not None and hits > 0, label

    rows = list(scope.iter_query(limit=100, **filters))
    stats = scope.last_read_stats

    assert len(rows) == hits, label
    assert stats['rows'] <= hits + 100, (label, stats)
    assert stats['sorted_elements'] <= 100, (label, stats)
    assert stats['returned'] == hits, (label, stats)
    assert stats['returned'] <= stats['limit'], (label, stats)
    assert stats['limit_respected'] is True, (label, stats)
    assert stats['pages'] == -(-hits // 100), (label, stats)
    assert stats['max_page'] <= 100, (label, stats)
    assert stats['complete'] is True, (label, stats)
    assert stats['cancelled'] is False, (label, stats)


def test_the_count_point_is_visible_without_an_active_trace(scope):
    """`performance_trace.record()` is a no-op without a trace; the counters are not."""
    before = catalog_read_counters()
    events = []
    observer = lambda event, **fields: events.append((event, fields))
    add_catalog_read_observer(observer)
    try:
        list(scope.iter_query(canvas_ids=(CANVAS_A,), limit=100))
    finally:
        remove_catalog_read_observer(observer)
    after = catalog_read_counters()

    assert after.get('iter_query_count', 0) == before.get('iter_query_count', 0) + 1
    assert after.get('catalog_rows', 0) == before.get('catalog_rows', 0) + 70
    assert after.get('catalog_returned', 0) == before.get('catalog_returned', 0) + 70
    assert after.get('catalog_pages', 0) == before.get('catalog_pages', 0) + 1
    assert events and events[0][0] == 'catalog_rows_read'
    assert events[0][1]['rows'] == 70 and events[0][1]['pages'] == 1
    assert events[0][1]['scope'] == 'canvas=canvas-a'
    assert events[0][1]['returned'] == 70 and events[0][1]['limit'] == 100
    assert events[0][1]['limit_respected'] is True


def test_the_trace_event_carries_the_same_numbers(scope, monkeypatch):
    """The trace event name and fields are the contract the acceptance group reads."""
    import prism.performance_trace as trace
    seen = []
    monkeypatch.setattr(trace, 'record',
                        lambda kind, **fields: seen.append((kind, fields)))
    list(scope.iter_query(canvas_ids=(CANVAS_A,), limit=100))
    kinds = [kind for kind, _fields in seen]
    assert 'catalog_rows_read' in kinds
    fields = dict(seen)['catalog_rows_read']
    assert fields['rows'] == 70 and fields['pages'] == 1
    assert fields['scope'] == 'canvas=canvas-a'
    assert fields['returned'] == 70 and fields['limit'] == 100
    assert fields['limit_respected'] is True


def test_a_broken_observer_cannot_break_a_read(scope):
    def boom(event, **fields):
        raise RuntimeError('observer exploded')
    add_catalog_read_observer(boom)
    try:
        rows = list(scope.iter_query(canvas_ids=(CANVAS_A,), limit=100))
    finally:
        remove_catalog_read_observer(boom)
    assert len(rows) == 70


def test_last_read_stats_is_empty_before_the_first_read(scope):
    assert scope.last_read_stats is None


def test_a_cancelled_iteration_still_reports_what_it_read(scope):
    cancelled = False
    rows = scope.iter_query(canvas_ids=(CANVAS_A,), limit=10,
                            cancel=lambda: cancelled)
    assert next(rows)['id'] == 1
    cancelled = True
    with pytest.raises(OpenScopeCancelled):
        next(rows)
    stats = scope.last_read_stats
    assert stats['cancelled'] is True
    assert stats['complete'] is False
    assert stats['rows'] == 10, 'the page was read even though the read stopped'
    assert stats['yielded'] == 1


def test_the_counter_sees_the_sort_that_is_not_limited_yet(scope):
    """C2-2: 参与排序的元素数 <= limit. **Green since the total cap landed.**

    ``iter_query(limit=100, max_rows=100)`` hands the caller 100 rows and
    stops; the scope never materialises the other 150 hits, so the caller
    sorts 100 elements, not 250.

    This is the rewrite the §2.6 contract asked for: the assertion is now
    the criterion itself, and the numbers are the ones that were written down
    before the implementation started.
    """
    hits = scope.count(canvas_ids=(CANVAS_C,))
    assert hits == 250
    rows = list(scope.iter_query(canvas_ids=(CANVAS_C,), limit=100,
                                 max_rows=100))
    stats = scope.last_read_stats
    assert len(rows) == 100
    # The §2.6 numbers, verbatim.
    assert stats['returned'] == 100
    assert stats['sorted_elements'] == 100
    assert stats['limit_respected'] is True
    assert stats['complete'] is False, 'the cap cut the result set'
    assert stats['truncated'] is True
    assert stats['rows'] <= 100 + 100
    # One page, one round trip: the cap stops the read where it is.
    assert stats['pages'] == 1
    assert stats['max_page'] == 100


def test_uncapped_read_distinguishes_page_limit_from_result_limit(scope):
    """An unbounded result may span several correctly bounded pages."""
    rows = list(scope.iter_query(canvas_ids=(CANVAS_C,), limit=100))
    stats = scope.last_read_stats
    assert len(rows) == 250
    assert stats['limit'] == 100
    assert stats['max_rows'] is None
    assert stats['returned'] == 250
    assert stats['sorted_elements'] == 250
    assert stats['rows'] == 250
    assert stats['pages'] == 3
    assert stats['limit_respected'] is True
    assert stats['page_limit_respected'] is True
    assert stats['result_limit_respected'] is True
    assert stats['max_page'] <= 100
    assert stats['truncated'] is False
    assert stats['complete'] is True


# ── C2-3: the total number of rows a read returns vs the limit ─────────

def test_the_count_point_carries_the_limit_and_the_returned_rows(scope):
    """The comparison dimension the third criterion needs is in the count point.

    ``limit`` is the argument as it was given, ``returned`` is the number of
    rows that actually reached the caller, and ``limit_respected`` is the
    verdict -- so a checker never has to re-derive it.
    """
    rows = list(scope.iter_query(canvas_ids=(CANVAS_A,), limit=100))
    stats = scope.last_read_stats
    assert stats['limit'] == 100
    assert stats['returned'] == len(rows) == 70
    assert stats['limit_respected'] is (stats['returned'] <= stats['limit'])
    assert stats['limit_respected'] is True


def test_the_total_returned_rows_must_not_exceed_the_limit(scope):
    """C2-3: 总返回行数 <= 上限. **Green since the total cap landed.**

    With ``max_rows`` the third criterion is ``returned <= max_rows``; without
    it, ``returned <= limit``. One field (``limit_respected``) carries that
    verdict, so there is no second field with the opposite meaning.
    """
    hits = scope.count(canvas_ids=(CANVAS_C,))
    assert hits == 250
    rows = list(scope.iter_query(canvas_ids=(CANVAS_C,), limit=100,
                                 max_rows=100))
    stats = scope.last_read_stats
    # Both numbers of the comparison are in the count point.
    assert stats['limit'] == 100
    assert stats['max_rows'] == 100
    assert stats['returned'] == 100
    # C2-1 is green in the very same read.
    assert stats['rows'] <= hits + 100
    # C2-3: the read returned no more rows than the caller asked for.
    assert stats['returned'] <= stats['max_rows']
    assert stats['limit_respected'] is True


def test_max_rows_never_reaches_the_catalog_query(scope):
    """`max_rows` is a named argument, not a filter.

    It is consumed by the scope layer. If it leaked into ``**filters`` it
    would travel down to ``index.query()`` and raise there -- a crash in the
    middle of a read instead of a cap.
    """
    spy = SpyIndex(scope.index)
    scope.index = spy
    list(scope.iter_query(canvas_ids=(CANVAS_A,), limit=100, max_rows=10))
    assert spy.calls
    first = spy.calls[0]
    assert 'max_rows' not in first
    assert set(first) == {'kind', 'canvas_ids', 'untagged_only', 'min_rating',
                           'text', 'include_deleted', 'limit', 'after_id'}
    assert first['limit'] == 10, 'the page shrinks to what the cap still needs'


def test_max_rows_caps_the_total_not_the_page(scope):
    """The cap is a total: 250 hits with a 100-row page are three pages."""
    hits = scope.count(canvas_ids=(CANVAS_C,))
    assert hits == 250
    rows = list(scope.iter_query(canvas_ids=(CANVAS_C,), limit=100,
                                 max_rows=250))
    stats = scope.last_read_stats
    assert len(rows) == 250
    assert stats['returned'] == 250
    assert stats['rows'] == 250
    assert stats['pages'] == 3
    assert stats['limit_respected'] is True
    assert stats['truncated'] is True
    assert stats['complete'] is False


def test_a_cap_beyond_the_hits_reads_everything(scope):
    """A cap that is not reached does not truncate anything."""
    rows = list(scope.iter_query(canvas_ids=(CANVAS_C,), limit=100,
                                 max_rows=1000))
    stats = scope.last_read_stats
    assert len(rows) == 250
    assert stats['returned'] == 250
    assert stats['truncated'] is False
    assert stats['complete'] is True
    assert stats['limit_respected'] is True


def test_no_cap_leaves_the_read_exactly_as_it_was(scope):
    """`max_rows=None` is the old behaviour, byte for byte.

    `export_service.catalog_ids()` depends on it: it needs every id of the
    project, and it passes `limit` as the page size.
    """
    without = list(scope.iter_query(canvas_ids=(CANVAS_C,), limit=100))
    stats = scope.last_read_stats
    assert len(without) == 250
    assert stats['max_rows'] is None
    assert stats['returned'] == 250
    assert stats['truncated'] is False
    assert stats['limit_respected'] is True, '100 limits each page, not the 250-row result'


@pytest.mark.parametrize('bad', [0, -1, '100', 10.5, True])
def test_a_bad_max_rows_is_rejected(scope, bad):
    with pytest.raises((TypeError, ValueError)):
        list(scope.iter_query(canvas_ids=(CANVAS_A,), limit=100, max_rows=bad))


def test_the_browser_path_does_respect_the_limit(scope):
    """A-4's consumer caps the read, and the count point says so.

    `catalog_browser.CatalogRows` takes ``limit`` as a **total** cap: it
    reads at most that many rows and closes the generator, so the scope
    publishes a count point with ``complete`` false and ``returned`` at most
    the limit. This is the end-to-end shape the third criterion wants.
    """
    from prism.catalog_browser import CatalogRows

    rows = CatalogRows(scope, {'canvas_ids': (CANVAS_C,)}, limit=100)
    rows.prepare()
    delivered = rows[0:100]
    assert len(delivered) == 100
    stats = scope.last_read_stats
    assert stats['returned'] == 100
    assert stats['limit_respected'] is True
    assert stats['complete'] is False, 'the read was cut short on purpose'
    assert stats['rows'] <= 100 + 100


# ── the string canvas id ───────────────────────────────────────────────

def test_a_string_canvas_id_is_one_canvas_not_its_characters(scope):
    rows = list(scope.iter_query(canvas_ids=CANVAS_A, limit=100))
    assert len(rows) == 70
    assert {row['canvas_id'] for row in rows} == {CANVAS_A}
    assert scope.last_read_stats['filters']['canvas_ids'] == (CANVAS_A,)


def test_query_has_the_same_string_guard(scope):
    assert len(scope.query(canvas_ids=CANVAS_A, limit=100)) == 70
    assert scope.count(canvas_ids=CANVAS_A) == 70


def test_the_index_layer_also_treats_a_string_as_one_canvas_id(scope):
    """The second line of defence: the catalog layer no longer splits a string.

    This used to be the *hole* the scope layer had to cover. A-3 closed it
    at 2026-10-22 15:18:02 with `ProjectCatalogIndex._canvas_id_tuple()`, so
    the catalog now takes a plain string as ONE canvas id as well.

    The pit this covers: `tuple('stress-canvas-0000')` splits the id into its
    characters and turns the filter into ``canvas_id IN ('s','t','r',...)`` --
    zero rows and no error, the one answer that is worse than an exception.

    So the silent-wrong-answer pit is now closed in **two** places:

    * the scope layer (A-2) -- `prism/project_open_scope.py` `_as_canvas_ids`
      normalises the string into a one-element tuple before the filter leaves;
    * the catalog layer (A-3) -- `prism/project_catalog_index.py`
      `_canvas_id_tuple()` does the same on its own side.

    Either layer alone is enough; together they make the pit unreachable.
    """
    where, params = ProjectCatalogIndex._predicate(canvas_ids=CANVAS_A)
    assert 'canvas_id IN' in where
    assert params == [CANVAS_A], 'one id, not one parameter per character'
    rows = scope.index.query(canvas_ids=CANVAS_A)
    assert len(rows) == 70, 'the canvas rows, not an empty answer'
    assert {row['canvas_id'] for row in rows} == {CANVAS_A}


def test_both_layers_agree_on_the_rows_a_string_selects(scope):
    """The scope path and the direct catalog path answer the same thing."""
    through_scope = list(scope.iter_query(canvas_ids=CANVAS_A, limit=100))
    through_index = scope.index.query(canvas_ids=CANVAS_A)
    assert [row['id'] for row in through_scope] == [row['id'] for row in through_index]
    assert len(through_scope) == 70


def test_the_guard_reaches_the_catalog_as_one_id(scope):
    spy = SpyIndex(scope.index)
    scope.index = spy
    list(scope.iter_query(canvas_ids=CANVAS_A, limit=100))
    assert spy.calls
    assert spy.calls[0]['canvas_ids'] == (CANVAS_A,)


# ── the filters really reach the catalog ───────────────────────────────

def test_the_filters_reach_the_index_layer(scope):
    spy = SpyIndex(scope.index)
    scope.index = spy
    list(scope.iter_query(kind='pixmap', canvas_ids=(CANVAS_A,),
                          untagged_only=False, min_rating=2, text='Asset',
                          include_deleted=False, limit=100))
    assert spy.calls, 'the scope must talk to the catalog'
    first = spy.calls[0]
    assert first['kind'] == 'pixmap'
    assert first['canvas_ids'] == (CANVAS_A,)
    assert first['untagged_only'] is False
    assert first['min_rating'] == 2
    assert first['text'] == 'Asset'
    assert first['include_deleted'] is False
    assert first['limit'] == 100
    assert 'after_id' in first
    # The filters were applied by the catalog, not by Python afterwards.
    expected = scope.index.count(text='Asset', canvas_ids=(CANVAS_A,),
                                  min_rating=2)
    rows = list(scope.iter_query(text='Asset', canvas_ids=(CANVAS_A,),
                                 min_rating=2, limit=100))
    assert len(rows) == expected == 23


def test_the_catalog_splits_the_text_into_words(scope):
    """`text` is a word list: every word has to match somewhere in the row.

    'Asset 1' is two words, so 'Asset 41' matches as well ('asset' and '1').
    """
    rows = list(scope.iter_query(text='Asset 1', canvas_ids=(CANVAS_A,),
                                 limit=100))
    assert len(rows) == 16


def test_every_filter_is_reachable_through_iter_query(scope):
    rows = list(scope.iter_query(untagged_only=True, limit=100))
    assert len(rows) == 20
    rows = list(scope.iter_query(canvas_ids=(None,), limit=100))
    assert len(rows) == 25
    rows = list(scope.iter_query(kind='image', limit=100))
    assert len(rows) == 250


# ── the batch summary API (A-3's index side, forwarded here) ───────────

class IndexWithoutGrouped:
    """A catalog that does not have the batch APIs yet.

    A-3 owns ``ProjectCatalogIndex.summaries_grouped`` /
    ``summaries_buckets``. This proxy hides them, so the scope layer's
    capability probe takes the fallback path -- the situation where A-3's side
    has not landed (or answers with another name) must not crash the scope
    layer.
    """

    def __init__(self, inner):
        self._inner = inner

    def __getattr__(self, name):
        if name in ('summaries_grouped', 'summaries_buckets',
                    'summaries_uncovered'):
            raise AttributeError(name)
        return getattr(self._inner, name)


def test_summaries_grouped_forwards_to_the_catalog(scope):
    """The batch API answers the summary rows in one round trip."""
    rows = scope.summaries_grouped([1, 2, 3])
    assert len(rows) == 3
    assert [row['id'] for row in rows] == [1, 2, 3], 'input order'
    for row in rows:
        assert 'canvas_id' in row and 'kind' in row


def test_summaries_grouped_falls_back_when_the_catalog_has_no_api(scope):
    """No catalog API -> the same rows, taken one at a time."""
    expected = scope.summaries_grouped([3, 1, 2])
    scope.index = IndexWithoutGrouped(scope.index)
    fallback = scope.summaries_grouped([3, 1, 2])
    assert [row['id'] for row in fallback] == [row['id'] for row in expected]
    assert [row['id'] for row in fallback] == [3, 1, 2], 'input order'


def test_summaries_buckets_forwards_to_the_catalog(scope):
    """The counter buckets: one entry per (canvas_id, deleted, untagged)."""
    rows = scope.summaries_buckets([1, 2, 3])
    assert rows
    for canvas, deleted, untagged, total in rows:
        assert isinstance(canvas, str)
        assert isinstance(deleted, bool)
        assert isinstance(untagged, bool)
        assert isinstance(total, int) and total > 0
    assert sum(row[3] for row in rows) == 3


def test_summaries_buckets_falls_back_when_the_catalog_has_no_api(scope):
    """No catalog API -> the same buckets, computed row by row."""
    expected = scope.summaries_buckets([1, 2, 3])
    scope.index = IndexWithoutGrouped(scope.index)
    fallback = scope.summaries_buckets([1, 2, 3])
    assert sorted(fallback) == sorted(expected)
    assert sum(row[3] for row in fallback) == 3


def test_summaries_buckets_count_only_asset_rows(scope):
    """Brush lines and text annotations are not assets."""
    scope.index = IndexWithoutGrouped(scope.index)
    assert scope.summaries_buckets([]) == ()


# ── a wrong type must not answer "no rows" ─────────────────────────────

@pytest.mark.parametrize('kwargs', [
    dict(kind=123),
    dict(kind=['pixmap']),
    dict(text=123),
    dict(text=['Asset']),
    dict(min_rating=1.5),
    dict(min_rating=-1),
    dict(min_rating='high'),
    dict(untagged_only='yes'),
    dict(include_deleted='no'),
    dict(canvas_ids=(1, 2)),
    dict(canvas_ids=object()),
    dict(typo_filter='x'),
])
def test_a_wrong_filter_type_fails_loudly(scope, kwargs):
    with pytest.raises((TypeError, ValueError)):
        list(scope.iter_query(**kwargs))


@pytest.mark.parametrize('kwargs', [
    dict(kind=123),
    dict(canvas_ids='canvas-a' if False else 7),
    dict(min_rating=1.5),
])
def test_query_rejects_the_same_wrong_types(scope, kwargs):
    with pytest.raises((TypeError, ValueError)):
        scope.query(**kwargs)


def test_count_rejects_a_wrong_type_too(scope):
    with pytest.raises(TypeError):
        scope.count(canvas_ids=7)
    with pytest.raises(TypeError):
        scope.canvas_counts(kind=7)


def test_a_bad_limit_is_rejected_before_the_catalog_is_touched(scope):
    for bad in (0, -1, 1025, '100', 10.5):
        with pytest.raises((TypeError, ValueError)):
            list(scope.iter_query(limit=bad, canvas_ids=(CANVAS_A,)))


def test_iter_query_rejects_its_own_paging_cursor(scope):
    with pytest.raises(TypeError):
        list(scope.iter_query(after_id=1))


# ── E: an unusable catalog stays honest ────────────────────────────────

def test_an_unusable_catalog_is_none_not_zero(project, tmp_path):
    scope = ProjectOpenScope(project, tmp_path/'absent.sqlite')
    assert scope.ready is False
    assert scope.counts() is None and scope.total() is None
    with pytest.raises(CatalogNotReady):
        scope.page(CANVAS_A)
    with pytest.raises(CatalogNotReady):
        scope.details([1])
    with pytest.raises(CatalogNotReady):
        list(scope.iter_query(canvas_ids=(CANVAS_A,)))


def test_a_stale_catalog_is_none_not_zero(scope):
    with closing(sqlite3.connect(scope.project_path)) as connection:
        connection.execute('DELETE FROM items WHERE id=1')
        connection.commit()
    assert scope.ready is False
    assert scope.counts() is None and scope.total() is None
    with pytest.raises(CatalogNotReady):
        list(scope.iter_query())


# ── the known-canvas hand-off ──────────────────────────────────────────
#
# ``UNASSIGNED_CANVAS`` / ``UNCLASSIFIED_CANVAS`` are not canvas ids: which
# rows they contain depends on which canvases still exist in the project.
# The catalog can only answer that with the set of known canvases, so the
# scope hands the set over unchanged -- and says whether the hand-off
# happened, because an index layer that cannot take it answers a different
# (narrower) question.

class RecordingIndex:
    """Records the keyword arguments the scope hands to the catalog."""

    def __init__(self, inner):
        self.inner = inner
        self.calls = []

    def __getattr__(self, name):
        return getattr(self.inner, name)

    def query(self, **kwargs):
        self.calls.append(('query', kwargs))
        return self.inner.query(**kwargs)

    def count(self, **kwargs):
        self.calls.append(('count', kwargs))
        return self.inner.count(**kwargs)


class IndexWithoutKnownCanvases:
    """An index layer from before the known-canvas parameter existed."""

    def __init__(self, inner):
        self.inner = inner
        self.calls = []

    def __getattr__(self, name):
        return getattr(self.inner, name)

    def query(self, *, kind=None, canvas_ids=None, untagged_only=False,
              min_rating=0, text='', include_deleted=False, limit=1024,
              after_id=None):
        self.calls.append(('query', dict(kind=kind, canvas_ids=canvas_ids)))
        return self.inner.query(kind=kind, canvas_ids=canvas_ids,
                                 untagged_only=untagged_only,
                                 min_rating=min_rating, text=text,
                                 include_deleted=include_deleted,
                                 limit=limit, after_id=after_id)

    def count(self, *, kind=None, canvas_ids=None, untagged_only=False,
              min_rating=0, text='', include_deleted=False):
        self.calls.append(('count', dict(kind=kind, canvas_ids=canvas_ids)))
        return self.inner.count(kind=kind, canvas_ids=canvas_ids,
                                untagged_only=untagged_only,
                                min_rating=min_rating, text=text,
                                include_deleted=include_deleted)


class IndexWithUnreadableSignature(IndexWithoutKnownCanvases):
    """The same old index, with a signature that cannot be read."""

    def __getattr__(self, name):
        return getattr(self.inner, name)


IndexWithUnreadableSignature.query.__signature__ = 'not a signature'
IndexWithUnreadableSignature.count.__signature__ = 'not a signature'


def test_the_scope_hands_the_known_canvases_to_the_index_layer(scope):
    """The set reaches the catalog unchanged -- that is the whole point."""
    scope.index = RecordingIndex(scope.index)
    known = (CANVAS_A, CANVAS_B)
    scope.count(canvas_ids=(UNCLASSIFIED_CANVAS,), known_canvas_ids=known)
    scope.query(canvas_ids=(UNCLASSIFIED_CANVAS,), known_canvas_ids=known)
    assert [name for name, _ in scope.index.calls] == ['count', 'query']
    for _, kwargs in scope.index.calls:
        assert kwargs['known_canvas_ids'] == known, kwargs
    assert scope.last_known_canvas_forwarded is True


def test_without_the_known_canvases_nothing_is_handed_over(scope):
    """The default is None: the index call is exactly the call of before."""
    scope.index = RecordingIndex(scope.index)
    scope.count(canvas_ids=(UNCLASSIFIED_CANVAS,))
    scope.query(canvas_ids=(UNCLASSIFIED_CANVAS,))
    for _, kwargs in scope.index.calls:
        assert 'known_canvas_ids' not in kwargs, kwargs
    assert scope.last_known_canvas_forwarded is None


def test_the_known_canvases_change_the_bucket_answer(scope):
    """A canvas that is gone from the project belongs to the bucket.

    ``canvas-c`` is not in the known set, so its 250 rows are unclassified
    rows as far as the catalog is concerned. Without the set the bucket can
    only see the rows that have no canvas at all (25).
    """
    without = scope.count(canvas_ids=(UNCLASSIFIED_CANVAS,))
    with_known = scope.count(canvas_ids=(UNCLASSIFIED_CANVAS,),
                             known_canvas_ids=(CANVAS_A, CANVAS_B))
    assert without == 25, without
    assert with_known == 25 + 250, with_known


def test_two_thousand_known_canvases_bind_two_thousand_and_one_parameters(scope):
    """2,000 known canvases: the bucket binds 2,001 parameters.

    The set travels as SQL parameters -- one per known canvas, plus the
    bucket sentinel itself -- so "the hand-off happened in full" is a
    number, not a promise: 2,001 = 1 bucket + 2,000 known.
    """
    known = tuple('stress-canvas-%04d' % index for index in range(2000))
    scope.index = RecordingIndex(scope.index)
    scope.count(canvas_ids=(UNCLASSIFIED_CANVAS,), known_canvas_ids=known)
    name, kwargs = scope.index.calls[-1]
    assert name == 'count'
    assert kwargs['known_canvas_ids'] == known

    where, params = scope.index.inner._predicate(
        canvas_ids=kwargs['canvas_ids'],
        known_canvas_ids=kwargs['known_canvas_ids'])
    assert where, 'the bucket must reach the catalog as a predicate'
    assert len(params) == 2001, len(params)
    assert len([value for value in params if value in known]) == 2000
    assert [value for value in params if value not in known] == [UNCLASSIFIED_CANVAS]


def test_the_whole_known_set_reaches_the_counted_rows(scope):
    """With 2,000 known canvases every row of the library is a bucket row."""
    known = tuple('stress-canvas-%04d' % index for index in range(2000))
    total = scope.count(canvas_ids=(UNCLASSIFIED_CANVAS,),
                        known_canvas_ids=known)
    assert total == 375, total


def test_an_index_layer_without_the_parameter_still_answers(scope):
    """An older index layer: the answer comes, and says it is the narrow one."""
    scope.index = IndexWithoutKnownCanvases(scope.index)
    total = scope.count(canvas_ids=(UNCLASSIFIED_CANVAS,),
                        known_canvas_ids=(CANVAS_A, CANVAS_B))
    assert total == 25, 'without the hand-off the bucket is the narrow one'
    assert scope.last_known_canvas_forwarded is False
    assert scope.index.calls, 'the index layer was called'

    rows = scope.query(canvas_ids=(UNCLASSIFIED_CANVAS,),
                       known_canvas_ids=(CANVAS_A, CANVAS_B))
    assert len(rows) == 25
    assert scope.last_known_canvas_forwarded is False


def test_an_index_layer_with_an_unreadable_signature_still_answers(scope):
    """The signature cannot be read: the call is retried without the keyword."""
    scope.index = IndexWithUnreadableSignature(scope.index)
    total = scope.count(canvas_ids=(UNCLASSIFIED_CANVAS,),
                        known_canvas_ids=(CANVAS_A, CANVAS_B))
    assert total == 25
    assert scope.last_known_canvas_forwarded is False
    assert len(scope.index.calls) == 1, 'the index layer ran exactly once'


def test_a_string_is_one_known_canvas(scope):
    """A plain string is ONE canvas id, not a sequence of characters."""
    scope.index = RecordingIndex(scope.index)
    scope.count(canvas_ids=(UNCLASSIFIED_CANVAS,), known_canvas_ids=CANVAS_A)
    _, kwargs = scope.index.calls[-1]
    assert kwargs['known_canvas_ids'] == (CANVAS_A,), kwargs


def test_a_wrong_known_canvas_type_fails_loudly(scope):
    """A wrong type must not answer "no rows" without saying why."""
    with pytest.raises(TypeError):
        scope.count(canvas_ids=(UNCLASSIFIED_CANVAS,), known_canvas_ids=7)
    with pytest.raises(TypeError):
        scope.count(canvas_ids=(UNCLASSIFIED_CANVAS,), known_canvas_ids=[1, 2])
    with pytest.raises(TypeError):
        list(scope.iter_query(canvas_ids=(UNCLASSIFIED_CANVAS,),
                              known_canvas_ids=7))


def test_iter_query_hands_the_known_canvases_to_every_page(scope):
    """The set is context, not a filter: it never becomes one."""
    known = tuple('stress-canvas-%04d' % index for index in range(2000))
    scope.index = RecordingIndex(scope.index)
    rows = list(scope.iter_query(canvas_ids=(UNCLASSIFIED_CANVAS,), limit=100,
                                 known_canvas_ids=known))
    assert rows, 'the bucket still has rows'
    assert scope.index.calls, 'the index layer was called'
    for _, kwargs in scope.index.calls:
        assert kwargs['known_canvas_ids'] == known, kwargs
    stats = scope.last_read_stats
    assert stats['known_canvases'] == 2000, stats
    assert 'known_canvas_ids' not in stats['filters'], stats
    assert 'known_canvas_ids' not in FILTER_NAMES
