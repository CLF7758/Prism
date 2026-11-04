"""Read layer for opening a project: catalog counts plus one page at a time.

:mod:`prism.project_catalog_index` already builds a rebuildable, scalar-only
catalog. This module turns it into the two reads an open path actually needs —
"how many items per canvas and kind" and "the summaries of this page" — so a
100k-item project never has to ``fetchall`` every detail JSON just to draw a
sidebar.

**This is a read layer, not a writable scene.** Nothing here is wired into the
real open path yet, so a project opened through this module is still read-only:
do not save from it, do not treat the summaries as items, and do not enable
partial-scene saving on its behalf. The source JSON and the original bytes stay
in the project; the cache holds small scalar projections only.

Rules the callers can rely on:

* ``counts()``/``total()`` answer from the cache's ``counts``/``catalog_state``
  tables. Neither ever runs ``COUNT(*)`` against the source project.
* When the catalog is missing, incomplete or stale, ``counts()``/``total()``
  return ``None`` and ``page()``/``details()`` raise
  :class:`prism.project_catalog_index.CatalogNotReady`. None of them may return
  ``0`` or ``()`` and let an unusable catalog look like an empty project.
* Every connection is a ``mode=ro`` URI, every read is one bounded batch
  (<= 1024 records), and cancellation is checked around that batch.
* ``page()`` is keyset paging. There is no ``OFFSET`` walk to degrade on the
  tenth page of a big canvas: pass the last id you received as ``after_id``.
* Filters are checked before they reach the catalog. A ``canvas_ids`` that is
  a plain string is ONE canvas id (not a sequence of characters), and a
  ``kind``/``text``/``min_rating`` of the wrong type raises instead of
  quietly matching nothing.
* ``known_canvas_ids`` -- the canvases that still exist in the project -- is
  handed to the index layer unchanged, so the two bucket sentinels
  (``UNASSIGNED_CANVAS``/``UNCLASSIFIED_CANVAS``) answer the scene's question
  instead of the catalog's narrower one. The hand-off is probed, not
  assumed: an index layer that does not know the parameter yet still answers,
  and ``scope.last_known_canvas_forwarded`` says which of the two happened.
* ``iter_query()`` counts what it read. ``scope.last_read_stats`` (and the
  module level ``catalog_read_counters()``) always carry the number of
  catalog pages and rows a read cost, so "one page, not the whole library"
  is an assertable fact and not a promise.
* That count point keeps the three numbers the C2 criteria compare:
  ``rows`` (rows taken out of the catalog), ``returned`` (rows actually
  handed to the caller) and ``limit`` (the limit as it was given).
  ``limit_respected`` is ``returned <= limit`` -- the verdict of the third
  criterion, reported per read instead of being re-derived by every checker.
"""
from contextlib import closing
import collections
import inspect
import json
import os
import sqlite3
import sys
import threading

from prism.project_catalog_index import (VERSION, CatalogNotReady,
                                        PKG3_UNKNOWN, ProjectCatalogIndex,
                                        RANGE_SNAPSHOT_EVENT, _fingerprint,
                                        build_range_snapshot,
                                        classify_scope_mode, page_scope_fields,
                                        pkg3_restore, pkg3_span,
                                        record_pkg3, stable_fingerprint)


#: Hard ceiling for one round trip, matching the catalog's build batches.
MAX_BATCH = 1024

#: What a page fetch returns when the caller does not ask for a size.
DEFAULT_PAGE_LIMIT = 128


# ── C2: what a catalog read actually cost ──────────────────────────────
#
# `performance_trace.record()` is a no-op when no trace is active, so the
# count point cannot live in the trace alone. There is a counter that is
# always alive plus an injectable observer, so a test without a trace can
# still assert "this read touched one page, not the whole library".
#
# The event name is fixed: ``catalog_rows_read`` with ``rows``, ``pages``
# and ``scope``. Observation entry points:
#
# * ``scope.last_read_stats`` -- the stats of the most recent ``iter_query()``
#   on that instance (a plain dict, readable at any time).
# * ``catalog_read_counters()`` -- counter snapshot, always available.
# * ``reset_catalog_read_counters()`` -- clear the counters (tests).
# * ``add_catalog_read_observer(cb)`` / ``remove_catalog_read_observer(cb)``
#   -- ``cb(event, **fields)``, called synchronously on the production path.
# * the trace file (``Prism-operations-<pid>.jsonl``), same event name, only
#   while a trace is active.
#
# The three numbers the C2 criteria compare are carried by the same event:
# ``rows`` (rows read out of the catalog), ``returned`` (rows handed to the
# caller) and ``limit`` (the limit as it was given). ``limit_respected`` is
# the verdict of the third criterion, ``returned <= limit``, so a checker
# never has to re-derive it.

_CATALOG_READ_LOCK = threading.Lock()
_CATALOG_READ_COUNTERS = collections.Counter()
_CATALOG_READ_OBSERVERS: list = []


def catalog_read_counters() -> dict:
    """C2 counter snapshot.

    Returns a dict with ``catalog_rows`` (cumulative rows read out of the
    catalog), ``catalog_returned`` (cumulative rows handed to the caller),
    ``catalog_pages`` (cumulative pages read), ``iter_query_count`` (calls)
    and ``catalog_rows_read`` (events). Unlike ``performance_trace.record()``
    this answers even when no trace is active.
    """
    with _CATALOG_READ_LOCK:
        return dict(_CATALOG_READ_COUNTERS)


def reset_catalog_read_counters() -> None:
    """Clear the C2 counters (tests only; the production path never calls it)."""
    with _CATALOG_READ_LOCK:
        _CATALOG_READ_COUNTERS.clear()


def add_catalog_read_observer(callback) -> None:
    """Register a C2 observer: ``callback(event, **fields)``."""
    if callback not in _CATALOG_READ_OBSERVERS:
        _CATALOG_READ_OBSERVERS.append(callback)


def remove_catalog_read_observer(callback) -> None:
    """Unregister a C2 observer."""
    try:
        _CATALOG_READ_OBSERVERS.remove(callback)
    except ValueError:
        pass


def _record_catalog_read(event: str, **fields) -> None:
    """Record one C2 event: the counter always moves, trace/observer best effort.

    A failing trace or observer must never change the counter, and must never
    change what the caller gets back.
    """
    module = sys.modules.get('prism.performance_trace')
    if module is not None:
        try:
            module.record(event, **fields)
        except Exception:
            pass
    for callback in list(_CATALOG_READ_OBSERVERS):
        try:
            callback(event, **fields)
        except Exception:
            pass


# ── pkg3: what this scope looked like when a read happened ────────────
#
# 规格 §6 的范围快照在读取侧只需要三个问题：**这次读的是什么范围**、**目录
# 当时是什么状态**、**一共读了多少**。这一段只回答它们，并且遵守两条硬约束：
#
# * **不为记日志多查一次。** `telemetry_state()` 只读 `catalog_state` 一张小表
#   （每次 (源指纹, 缓存指纹) 变化才真正读，之后走缓存），它**不是** `COUNT(*)`、
#   也**不是**遍历十万行。总数直接取建库状态里的 `total` —— 那是建库时就写好
#   的标量。
# * **不知道就写不知道。** 目录没建好时 `expected_total_known=False`，
#   `expected_total` 是 `unknown`，绝不用 0 冒充"这个项目是空的"。

#: 目录状态的口径（与 §6 一致）：missing / building / ready / stale / error。
CATALOG_STATE_VALUES = ('missing', 'building', 'ready', 'stale', 'error')


def _scope_project_name(scope) -> str:
    """The project file name (trace lines never carry the whole path)."""
    try:
        return os.path.basename(str(scope.index.source))
    except Exception:
        return PKG3_UNKNOWN


def _classify_cache_state(state, source_fingerprint):
    """``(catalog_state, reason)`` from the cached build state.

    ``ready`` needs a complete build of *this* source; anything else is
    reported with the reason that stopped it, so a stale cache never looks
    like a fresh one and an incomplete build never looks like "empty".
    """
    if not state:
        return 'missing', 'no_build_state'
    if state.get('invalid'):
        return 'stale', 'build_marked_invalid'
    if state.get('version') != VERSION:
        return 'stale', 'build_version_%s' % (state.get('version'),)
    if state.get('source') != source_fingerprint:
        return 'stale', 'source_fingerprint_changed'
    if not state.get('complete'):
        return 'building', 'build_incomplete_last_id=%s' % (state.get('last_id'),)
    return 'ready', 'complete'


def _range_snapshot_fields(clean, stats, *, project=PKG3_UNKNOWN,
                           state=None, forwarded=None, read_reason=None):
    """The §6 read-side field set, assembled from values that already exist.

    Every value here is something the read produced or the cache already had.
    Nothing is recomputed for the log.
    """
    mode = 'untagged' if clean.get('untagged_only') else 'all'
    effective, reason = classify_scope_mode(
        mode, clean.get('canvas_ids'), search_text=clean.get('text') or '')
    catalog_state, catalog_reason = (state or (PKG3_UNKNOWN, PKG3_UNKNOWN))
    expected_total = stats.get('expected_total')
    known = stats.get('expected_total_known', False)
    return build_range_snapshot(
        clean.get('canvas_ids'),
        stage='range_read',
        project=project,
        effective_mode=effective,
        effective_mode_reason=reason,
        filter_label=stats.get('scope'),
        filters=dict(clean),
        page_size=stats.get('limit'),
        pages_read=stats.get('pages'),
        rows_read=stats.get('rows'),
        rows_returned=stats.get('returned'),
        delivered=stats.get('yielded'),
        complete=stats.get('complete'),
        cancelled=stats.get('cancelled'),
        truncated=stats.get('truncated'),
        limit_respected=stats.get('limit_respected'),
        known_canvas_forwarded=forwarded,
        known_canvases=stats.get('known_canvases'),
        expected_total=expected_total if known else PKG3_UNKNOWN,
        expected_total_known=bool(known),
        catalog_state=catalog_state,
        catalog_state_reason=catalog_reason,
        source_fingerprint=stats.get('source_fingerprint'),
        cache_fingerprint=stats.get('cache_fingerprint'),
        cache_revision=stats.get('cache_revision'),
        read_reason=read_reason)


def _read_scope_fields(read_scope_pages=None, *, search_text='', mode='all'):
    """Convenience wrapper for the callers that only have a page scope."""
    effective, reason = classify_scope_mode(mode, read_scope_pages,
                                            search_text=search_text)
    fields = page_scope_fields(read_scope_pages)
    fields['effective_mode'] = effective
    fields['effective_mode_reason'] = reason
    return fields


def _record_range_snapshot(scope, clean, stats, *, forwarded=None,
                           read_reason=None) -> dict:
    """One §6 read-side snapshot for a finished ``iter_query``.

    The catalog state / fingerprints / expected total come from
    :meth:`ProjectOpenScope.telemetry_state`, which is memoised on the source
    and cache fingerprints -- a repeated read of an unchanged catalog adds two
    ``stat()`` calls and no query. A failing telemetry path returns an empty
    dict and changes nothing about the read that already happened.
    """
    try:
        state = scope.telemetry_state()
    except Exception:
        return {}
    for key, value in state.items():
        stats.setdefault(key, value)
    snapshot = _range_snapshot_fields(clean, stats, project=state.get('project'),
                                      state=(state.get('catalog_state'),
                                             state.get('catalog_state_reason')),
                                      forwarded=forwarded,
                                      read_reason=read_reason)
    record_pkg3(RANGE_SNAPSHOT_EVENT, **snapshot)
    return snapshot


# ── filter guards ─────────────────────────────────────────────────────
#
# `ProjectCatalogIndex._predicate` builds the WHERE clause from these
# values without checking their types, and a wrong type is not an error:
# it is a query that matches nothing. The guards below turn "silently
# empty" into "loudly wrong" without changing the values that always
# worked.

#: Filters ``query()`` and ``iter_query()`` accept.
FILTER_NAMES = ('kind', 'canvas_ids', 'untagged_only', 'min_rating', 'text',
                'include_deleted', 'recent_only', 'recent_limit', 'tag_sets')


def _as_bool(name, value):
    """``None``/``bool``/``0``/``1`` -> bool; anything else is a caller bug."""
    if value is None:
        return False
    if isinstance(value, bool):
        return value
    if isinstance(value, int) and value in (0, 1):
        return bool(value)
    raise TypeError('%s must be a bool, got %r' % (name, type(value).__name__))


def _as_kind(kind):
    """A kind filter is one kind name, or nothing at all."""
    if kind is None:
        return None
    if not isinstance(kind, str):
        raise TypeError('kind must be a string or None, got %r'
                        % type(kind).__name__)
    return kind


def _as_canvas_ids(canvas_ids):
    """Normalise ``canvas_ids``; a bare string is ONE canvas id.

    The catalog does ``tuple(canvas_ids)``, so a string used to be split
    into single characters: ``canvas_id IN ('s','t','r',...)`` matched
    nothing and the read answered "empty" without an error. There is no
    reading of this API in which a string means several canvas ids, so the
    string is wrapped into a one-element tuple instead.
    """
    if canvas_ids is None:
        return None
    if isinstance(canvas_ids, str):
        _record_catalog_read('canvas_ids_string_coerced', canvas_id=canvas_ids)
        return (canvas_ids,)
    try:
        ids = tuple(canvas_ids)
    except TypeError:
        raise TypeError('canvas_ids must be a string or an iterable of ids, '
                        'got %r' % type(canvas_ids).__name__) from None
    for value in ids:
        if value is not None and not isinstance(value, str):
            raise TypeError('canvas_ids entries must be strings or None, '
                            'got %r' % type(value).__name__)
    return ids


def _as_known_canvas_ids(value):
    """Normalise the canvases that still exist in the project.

    ``None`` means "the caller does not know", and the bucket keeps the
    index layer's own, narrower meaning. A plain string is ONE canvas id:
    the index layer iterates the value, so a string would arrive as its
    characters and the bucket would answer about canvases that do not
    exist.
    """
    if value is None:
        return None
    if isinstance(value, str):
        _record_catalog_read('known_canvas_ids_string_coerced',
                             canvas_id=value)
        return (value,)
    try:
        ids = tuple(value)
    except TypeError:
        raise TypeError('known_canvas_ids must be None, a string, or an '
                        'iterable of canvas ids, got %r'
                        % type(value).__name__) from None
    for item in ids:
        if item is not None and not isinstance(item, str):
            raise TypeError('known_canvas_ids entries must be strings or None, '
                            'got %r' % type(item).__name__)
    return ids


def _as_min_rating(value):
    """A rating floor is a whole, non-negative number."""
    if value is None:
        return 0
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        raise TypeError('min_rating must be a number, got %r'
                        % type(value).__name__)
    try:
        rating = int(value)
    except (TypeError, ValueError):
        raise ValueError('min_rating must be a whole number, got %r'
                         % (value,)) from None
    if isinstance(value, float) and value != rating:
        raise ValueError('min_rating must be a whole number, got %r' % (value,))
    if rating < 0:
        raise ValueError('min_rating must not be negative, got %r' % (rating,))
    return rating


def _as_text(text):
    """Search text is a string; ``None`` means "no text filter"."""
    if text is None:
        return ''
    if not isinstance(text, str):
        raise TypeError('text must be a string, got %r' % type(text).__name__)
    return text


def _as_limit(limit):
    """One page holds between one and :data:`MAX_BATCH` records."""
    if isinstance(limit, bool) or not isinstance(limit, int):
        raise TypeError('limit must be an integer, got %r'
                        % type(limit).__name__)
    if not 1 <= limit <= MAX_BATCH:
        raise ValueError('limit must be between 1 and %d' % MAX_BATCH)
    return limit


def _as_after_id(after_id):
    """The keyset cursor is an item id, or nothing at all."""
    if after_id is None:
        return None
    if isinstance(after_id, bool) or not isinstance(after_id, int):
        raise TypeError('after_id must be an integer or None, got %r'
                        % type(after_id).__name__)
    return after_id


def _as_max_rows(max_rows):
    """The total cap: ``None`` (no cap) or a positive whole number.

    The cap is **not** the page size. ``limit`` stays the page size, because
    ``export_service.catalog_ids()`` needs the whole set; ``max_rows`` is the
    opt-in ceiling a caller puts on how many rows it wants to receive.
    """
    if max_rows is None:
        return None
    if isinstance(max_rows, bool) or not isinstance(max_rows, int):
        raise TypeError('max_rows must be an integer or None, got %r'
                        % type(max_rows).__name__)
    if max_rows < 1:
        raise ValueError('max_rows must be at least 1, got %r' % (max_rows,))
    return max_rows


def _untagged_bucket(tags):
    """The catalog's untagged rule, tolerant of a malformed tags column.

    ``prism_untagged_safe()`` in the catalog module treats a tags column that
    is not a list as *tagged*, so one malformed row never breaks a count.
    The fallback grouping in :meth:`ProjectOpenScope._summaries_grouped_fallback`
    has to agree with it, so the rule is repeated here rather than imported
    from a private symbol that may be renamed.
    """
    try:
        names = json.loads(tags) if isinstance(tags, str) and tags else tags
        if names is None:
            return False
        if not isinstance(names, list):
            return False
        return not any(str(name).strip() for name in names)
    except (TypeError, ValueError):
        return False


def _normalize_filters(filters):
    """Validate and normalise the filter kwargs before they reach the catalog."""
    return dict(
        kind=_as_kind(filters.get('kind')),
        canvas_ids=_as_canvas_ids(filters.get('canvas_ids')),
        untagged_only=_as_bool('untagged_only', filters.get('untagged_only')),
        min_rating=_as_min_rating(filters.get('min_rating', 0)),
        text=_as_text(filters.get('text', '')),
        include_deleted=_as_bool('include_deleted',
                                 filters.get('include_deleted')),
        recent_only=_as_bool('recent_only', filters.get('recent_only')),
        recent_limit=_as_max_rows(filters.get('recent_limit')),
        tag_sets=_as_canvas_ids(filters.get('tag_sets')),
    )


# ── the known-canvas hand-off ───────────────────────────────────────
#
# The two bucket sentinels (``UNASSIGNED_CANVAS`` /
# ``UNCLASSIFIED_CANVAS``) are not canvas ids: which rows they contain
# depends on which canvases still exist in the project. The catalog can
# only answer that with the set of known canvases, so the scope layer
# hands the set over unchanged.
#
# The parameter is optional on both sides -- the index layer grew it
# after this layer -- so the hand-off is *probed*, never assumed. An index
# layer that cannot take it still answers, with the bucket's own narrower
# meaning, and the scope says so instead of leaving the caller with a
# silently different number.

#: The keyword the index layer uses to learn which canvases still exist.
KNOWN_CANVAS_PARAM = 'known_canvas_ids'


def _index_takes_known_canvas_ids(func):
    """Whether an index-layer callable accepts ``known_canvas_ids``.

    ``True``/``False`` when the signature can be read, ``None`` when it
    cannot (a C-level callable, an exotic stub).
    """
    try:
        parameters = inspect.signature(func).parameters
    except (TypeError, ValueError):
        return None
    for name, parameter in parameters.items():
        if name == KNOWN_CANVAS_PARAM:
            return True
        if parameter.kind is inspect.Parameter.VAR_KEYWORD:
            return True
    return False


def _forward_known_canvas_ids(func, kwargs, known_canvas_ids):
    """Call an index-layer callable with the known canvases, if it can.

    Returns ``(result, forwarded)``. ``forwarded`` is ``True`` when the
    index layer took the parameter, ``False`` when it could not (the
    answer is then the index layer's own, narrower one) and ``None`` when
    there was nothing to hand over.
    """
    if known_canvas_ids is None:
        return func(**kwargs), None
    accepted = _index_takes_known_canvas_ids(func)
    if accepted is False:
        return func(**kwargs), False
    try:
        return func(**kwargs, known_canvas_ids=known_canvas_ids), True
    except TypeError as exc:
        if accepted or KNOWN_CANVAS_PARAM not in str(exc):
            raise
        # The signature could not be read and the call did not bind: the
        # index layer does not know the parameter yet. Answer anyway.
        return func(**kwargs), False


def _filter_label(clean):
    """A short, stable label for the filter set (the ``scope`` trace field)."""
    parts = []
    if clean['kind'] is not None:
        parts.append('kind=%s' % clean['kind'])
    if clean['canvas_ids'] is not None:
        ids = ['null' if value is None else value
               for value in clean['canvas_ids']]
        parts.append('canvas=%s' % ','.join(ids))
    if clean['untagged_only']:
        parts.append('untagged')
    if clean['min_rating']:
        parts.append('rating>=%s' % clean['min_rating'])
    if clean['text']:
        parts.append('text=%s' % clean['text'][:40])
    if clean['include_deleted']:
        parts.append('deleted')
    return '|'.join(parts) or 'all'


class OpenScopeCancelled(RuntimeError):
    """A cancelled read is not an empty page; the caller has to retry."""


class ProjectOpenScope:
    """Catalog-backed read-only scope for one project.

    ``project_path`` is the ``.prism`` file; ``cache_path`` is the catalog
    cache that :meth:`build` fills. The catalog keeps the schema, the build
    checkpoints and the source fingerprint, so this layer asks it whether a
    cache can be trusted instead of re-deriving that rule.

    Not thread safe: one call owns its connections, but callers should still
    keep a single reader per cache and let the GUI decide when to refresh.
    """

    #: Stats of the most recent :meth:`iter_query` on this instance. Always a
    #: plain dict (or ``None`` before the first read), readable at any time.
    last_read_stats = None

    #: Whether the last call that carried ``known_canvas_ids`` actually handed
    #: it to the index layer (``True``), could not (``False`` -- the answer is
    #: then the index layer's own, narrower one) or was never asked to
    #: (``None``).
    last_known_canvas_forwarded = None

    def __init__(self, project_path, cache_path):
        self.index = ProjectCatalogIndex(project_path, cache_path)
        self.last_read_stats = None
        self.last_known_canvas_forwarded = None
        self._telemetry_cache = None

    # ── identity ────────────────────────────────────────────────

    @property
    def project_path(self):
        return self.index.source

    @property
    def cache_path(self):
        return self.index.cache

    def source_state(self):
        """Stat-only fingerprint; compare two snapshots to spot a stale cache.

        No connection is opened and no catalog table is read, so this is cheap
        enough to poll before deciding whether to rebuild.
        """
        return dict(version=VERSION,
                    source=_fingerprint(self.index.source),
                    cache=_fingerprint(self.index.cache))

    def telemetry_state(self):
        """Catalog state for the §6 snapshot: cached, cheap, never a re-count.

        The answer is memoised on ``(source, cache)`` fingerprints, so an
        unchanged catalog costs two ``stat()`` calls and nothing else. The
        one connection it may open reads the single ``catalog_state`` row --
        a fixed-size table, not ``COUNT(*)`` and not a walk over the rows.
        ``expected_total`` comes from that same build state, so "the catalog
        could not answer" stays ``unknown`` instead of collapsing to 0.
        """
        source_fingerprint = _fingerprint(self.index.source)
        cache_fingerprint = _fingerprint(self.index.cache)
        memo_key = (source_fingerprint, cache_fingerprint)
        if self._telemetry_cache is not None and self._telemetry_cache[0] == memo_key:
            return dict(self._telemetry_cache[1])
        if cache_fingerprint is None:
            snapshot = dict(catalog_state='missing',
                            catalog_state_reason='cache_file_absent',
                            source_fingerprint=source_fingerprint,
                            cache_fingerprint=PKG3_UNKNOWN,
                            cache_revision=PKG3_UNKNOWN,
                            expected_total=PKG3_UNKNOWN,
                            expected_total_known=False,
                            project=_scope_project_name(self))
            self._telemetry_cache = (memo_key, snapshot)
            return dict(snapshot)
        state = None
        try:
            # Navigation asks for this status on the GUI thread. A cache
            # builder may hold the schema lock; status must not wait through
            # SQLite's default five-second busy timeout.
            with closing(self.index._cache(readonly=True, timeout=0.0)) as cache:
                state = self.index._state(cache)
        except (CatalogNotReady, sqlite3.Error, OSError, ValueError) as error:
            snapshot = dict(catalog_state='error',
                            catalog_state_reason=type(error).__name__,
                            source_fingerprint=source_fingerprint,
                            cache_fingerprint=cache_fingerprint,
                            cache_revision=PKG3_UNKNOWN,
                            expected_total=PKG3_UNKNOWN,
                            expected_total_known=False,
                            project=_scope_project_name(self))
            # A transient lock is not an immutable state of these file stamps.
            # Retry the next request rather than retaining an error forever.
            return dict(snapshot)
        catalog_state, reason = _classify_cache_state(state, source_fingerprint)
        known = bool(catalog_state == 'ready')
        snapshot = dict(catalog_state=catalog_state,
                        catalog_state_reason=reason,
                        source_fingerprint=source_fingerprint,
                        cache_fingerprint=cache_fingerprint,
                        # The build state *is* the cache revision: version,
                        # source fingerprint, completeness, last row and total.
                        cache_revision=(stable_fingerprint(
                            [state.get('version'), state.get('source'),
                             bool(state.get('complete')), state.get('last_id'),
                             state.get('total')], prefix='cache')
                            if state else PKG3_UNKNOWN),
                        cache_revision_source='catalog_state.build',
                        expected_total=(int(state.get('total') or 0) if known
                                        else PKG3_UNKNOWN),
                        expected_total_known=known,
                        project=_scope_project_name(self))
        self._telemetry_cache = (memo_key, snapshot)
        return dict(snapshot)

    @property
    def ready(self):
        """True only for a complete cache of this exact source file."""
        return self._cache_state() is not None

    def build(self, **options):
        """Delegate to the catalog builder.

        Backgrounding, time budgets and progress reporting stay the caller's
        job: a build is the one operation here that reads the whole source.
        """
        return self.index.build(**options)

    # ── counts ──────────────────────────────────────────────────

    def counts(self):
        """``{(canvas_id, kind): count}`` from the cache, or ``None``."""
        snapshot = self._snapshot()
        return None if snapshot is None else snapshot['counts']

    def total(self):
        """Cached item total, or ``None``. Never a ``COUNT(*)`` on the source."""
        snapshot = self._snapshot()
        return None if snapshot is None else snapshot['total']

    # ── bounded reads ───────────────────────────────────────────

    def page(self, canvas_id, *, kind=None, after_id=None,
             limit=DEFAULT_PAGE_LIMIT, cancel=None):
        """One keyset page of summaries for ``canvas_id``.

        Rows are ordered by id; feed the last ``id`` of a page back as
        ``after_id`` to continue, and stop on an empty tuple. Each row keeps
        the catalog's scalar columns only — ``id``, ``kind``, ``canvas_id``,
        ``title``, ``group_id``, ``width``, ``height``, ``has_preview``,
        ``has_original`` — so a page stays cheap regardless of item size.
        """
        if not 1 <= limit <= MAX_BATCH:
            raise ValueError('limit must be between 1 and 1024')
        self._check_cancel(cancel)
        rows = self.index.page(canvas_id, kind=kind, after_id=after_id, limit=limit)
        # A cancelled page is dropped rather than delivered half-read.
        self._check_cancel(cancel)
        return rows

    def details(self, item_ids, *, cancel=None):
        """Original ``items`` rows for exactly these ids, in the given order.

        Unknown metadata fields survive untouched. At most 1024 ids per call:
        the whole point of the scope is that a page's details arrive bounded,
        not that a caller can page in every row at once.
        """
        ids = tuple(dict.fromkeys(item_ids))
        if len(ids) > MAX_BATCH:
            raise ValueError('Details must be fetched in bounded batches')
        self._check_cancel(cancel)
        rows = self.index.details(ids)
        self._check_cancel(cancel)
        return rows

    # ── aggregate queries (unified-range, scene-independent) ────

    def query(self, *, kind=None, canvas_ids=None, untagged_only=False,
              min_rating=0, text='', include_deleted=False, limit=1024,
              cancel=None, after_id=None, known_canvas_ids=None,
              trace_context=None, recent_only=False, recent_limit=None, tag_sets=None):
        """Bounded aggregate query over the catalog projection.

        Backs "全部/未标签/未分类/默认/搜索" so a partially loaded page still
        yields whole-project truth without touching the editable scene. Text is
        a case-insensitive substring over title/notes/filename/tags; full
        ``tags.parse_query`` semantics remain in `project_catalog.query`.

        The filter values are checked before they reach the catalog: a wrong
        type is a caller bug, and a caller bug must not answer "no rows".

        ``known_canvas_ids`` is the set of canvases that still exist in the
        project. It is what lets the two bucket sentinels
        (``UNASSIGNED_CANVAS``/``UNCLASSIFIED_CANVAS``) answer the scene's
        question instead of the catalog's narrower one, and it is handed over
        unchanged. It is optional on both sides: an index layer that does not
        know the parameter yet still answers, with its own narrower meaning,
        and ``scope.last_known_canvas_forwarded`` says which happened.

        ``trace_context`` is the pkg3 trace hand-off for a worker thread: the
        caller captures it where it *creates* the job and passes it here, so
        the read-side §6 snapshot joins the same trace as the GUI side. When it
        is absent (or the trace API is not there yet) the read simply logs
        without a parent span -- nothing else changes.
        """
        with pkg3_restore(trace_context):
            self._check_cancel(cancel)
            clean = _normalize_filters(dict(kind=kind, canvas_ids=canvas_ids,
                                            untagged_only=untagged_only,
                                            min_rating=min_rating, text=text,
                                            include_deleted=include_deleted,
                                            recent_only=recent_only, recent_limit=recent_limit, tag_sets=tag_sets))
            page_limit = _as_limit(limit)
            cursor = _as_after_id(after_id)
            known = _as_known_canvas_ids(known_canvas_ids)
            rows, forwarded = _forward_known_canvas_ids(
                self.index.query,
                dict(kind=clean['kind'], canvas_ids=clean['canvas_ids'],
                     untagged_only=clean['untagged_only'],
                     min_rating=clean['min_rating'], text=clean['text'],
                     include_deleted=clean['include_deleted'],
                     **({'recent_only': True, 'recent_limit': clean['recent_limit']}
                        if clean['recent_only'] else {}),
                     **({'tag_sets': clean['tag_sets']} if clean['tag_sets'] is not None else {}),
                     limit=page_limit, after_id=cursor),
                known)
            self.last_known_canvas_forwarded = forwarded
            self._check_cancel(cancel)
            return rows

    def iter_query(self, *, cancel=None, limit=DEFAULT_PAGE_LIMIT, max_rows=None,
                   known_canvas_ids=None, trace_context=None, resume_after=None, **filters):
        """Yield every matching summary in bounded, cancellable ID-keyset pages.

        ``limit`` is the *page* size, not a cap on the number of results: the
        iteration runs until the catalog has nothing left. What a read cost is
        published in ``scope.last_read_stats`` (``pages``/``rows``/``yielded``/
        ``returned``/``sorted_elements``/``limit``/``max_rows``/
        ``limit_respected``/``truncated``) and in the module level
        ``catalog_read_counters()``, so "one page, not the whole library" is
        an assertable fact.

        ``max_rows`` is the **total** cap: at most that many rows are handed
        to the caller and the read stops there without asking for one more
        page. It is a separate, opt-in parameter, so the callers that need
        the whole set (``export_service.catalog_ids()``) keep working
        unchanged. It is a named argument on purpose: it never reaches the
        catalog as a filter.

        The count point keeps the numbers the C2 criteria compare:

        * ``rows`` -- how many catalog rows this read took out of the cache;
        * ``returned`` -- how many rows were actually handed to the caller;
        * ``limit`` -- the page size, and ``max_rows`` -- the total cap
          (``None`` when there is none).

        ``limit_respected`` is the verdict of the third criterion: with a cap
        it is ``returned <= max_rows``, without one it is
        ``returned <= limit``. One field, one meaning.

        ``complete`` says whether the read delivered everything it was asked
        for: without a cap that is "the result set was read to its end"; with
        a cap it is "the cap did not cut the result set". ``truncated`` says
        the other half: the cap stopped this read. When the cap is reached
        exactly, whether the result set had more rows is **unknown** -- the
        read deliberately does not ask for one more page.

        The filters are the ones :meth:`query` accepts, and they are validated
        before they reach the catalog.

        ``known_canvas_ids`` is the hand-off of :meth:`query`: the canvases
        that still exist in the project, passed on to every page so the
        bucket sentinels answer the scene's question. It is a named argument
        for the same reason ``max_rows`` is: it is context, not a filter, and
        it must never reach the catalog as one.

        ``trace_context`` is the pkg3 hand-off for the worker thread that walks
        this generator: the caller captures it where it *creates* the job (see
        ``performance_trace.capture_context``) and passes it here, so the §6
        read-side snapshot lands in the same trace as the GUI side. Absent (or
        the trace API not being there yet) it simply logs without a parent.
        """
        unknown = sorted(set(filters) - set(FILTER_NAMES))
        if unknown:
            if unknown == ['after_id']:
                raise TypeError('iter_query() runs its own keyset paging; '
                                'after_id is not accepted')
            raise TypeError('iter_query() got unexpected filter arguments: %s'
                            ' (accepted: %s)'
                            % (', '.join(unknown), ', '.join(FILTER_NAMES)))
        page_limit = _as_limit(limit)
        cap = _as_max_rows(max_rows)
        clean = _normalize_filters(filters)
        known = _as_known_canvas_ids(known_canvas_ids)
        with pkg3_restore(trace_context) as trace_restored:
            stats = dict(scope=_filter_label(clean), filters=dict(clean),
                         limit=page_limit, max_rows=cap, pages=0, rows=0,
                         yielded=0, returned=0, sorted_elements=0, max_page=0,
                         known_canvases=0 if known is None else len(known),
                         limit_respected=True, truncated=False, complete=False,
                         cancelled=False, trace_context_restored=trace_restored)
            self.last_read_stats = stats
            after = _as_after_id(resume_after)
            page_counter = 0
            row_counter = 0
            yield_counter = 0
            complete = False
            truncated = False
            cancelled = False
            try:
                while True:
                    # Never ask for more rows than the cap still allows: the read
                    # must not spend a round trip on rows it would have to drop.
                    want = page_limit if cap is None else min(page_limit,
                                                              cap - yield_counter)
                    if want <= 0:
                        truncated = True
                        break
                    rows = self.query(after_id=after, limit=want, cancel=cancel,
                                      known_canvas_ids=known, **clean)
                    page_counter += 1
                    row_counter += len(rows)
                    stats['pages'] = page_counter
                    stats['rows'] = row_counter
                    stats['max_page'] = max(stats['max_page'], len(rows))
                    if not rows:
                        complete = True
                        break
                    for row in rows:
                        self._check_cancel(cancel)
                        yield_counter += 1
                        yield row
                        if cap is not None and yield_counter >= cap:
                            truncated = True
                            break
                    stats['yielded'] = yield_counter
                    stats['returned'] = yield_counter
                    stats['sorted_elements'] = yield_counter
                    if truncated:
                        break
                    after = rows[-1]['id']
                    if len(rows) < want:
                        complete = True
                        break
            except OpenScopeCancelled:
                cancelled = True
                raise
            finally:
                stats['pages'] = page_counter
                stats['rows'] = row_counter
                stats['yielded'] = yield_counter
                stats['returned'] = yield_counter
                stats['sorted_elements'] = yield_counter
                stats['page_limit_respected'] = stats['max_page'] <= page_limit
                stats['result_limit_respected'] = cap is None or yield_counter <= cap
                stats['limit_respected'] = stats['page_limit_respected'] and stats['result_limit_respected']
                stats['truncated'] = truncated
                stats['complete'] = complete
                stats['cancelled'] = cancelled
                self.last_read_stats = stats
                with _CATALOG_READ_LOCK:
                    _CATALOG_READ_COUNTERS['iter_query_count'] += 1
                    _CATALOG_READ_COUNTERS['catalog_rows'] += row_counter
                    _CATALOG_READ_COUNTERS['catalog_returned'] += yield_counter
                    _CATALOG_READ_COUNTERS['catalog_pages'] += page_counter
                    if truncated:
                        _CATALOG_READ_COUNTERS['catalog_capped'] += 1
                _record_catalog_read('catalog_rows_read', rows=row_counter,
                                     pages=page_counter, scope=stats['scope'],
                                     yielded=yield_counter, returned=yield_counter,
                                     limit=page_limit, max_rows=cap,
                                     max_page=stats['max_page'],
                                     page_limit_respected=stats['page_limit_respected'],
                                     result_limit_respected=stats['result_limit_respected'],
                                     known_canvases=stats['known_canvases'],
                                     resume_after=resume_after,
                                     limit_respected=stats['limit_respected'],
                                     truncated=truncated, complete=complete,
                                     cancelled=cancelled)
                _record_range_snapshot(
                    self, clean, stats,
                    forwarded=self.last_known_canvas_forwarded,
                    read_reason=('cancelled' if cancelled else
                                 'truncated' if truncated else
                                 f'keyset_continuation_after:{resume_after}' if resume_after is not None
                                 else 'complete'))

    def summaries(self, item_ids, *, cancel=None):
        self._check_cancel(cancel)
        rows = self.index.summaries(item_ids)
        self._check_cancel(cancel)
        return rows

    def summaries_grouped(self, item_ids, *, cancel=None):
        """The batch summary API, in one round trip.

        The catalog side owns this API (``ProjectCatalogIndex.summaries_grouped``,
        A-3): one call, one connection, the scalar summary rows of the given
        ids in input order. An id the catalog does not know is not returned.

        This layer only forwards it, and it **probes the catalog first**: while
        the catalog side has not landed (or answers with another name), the
        call falls back to the row-per-id :meth:`summaries`. So the chain
        catalog -> scope -> consumer cannot break on the order the two sides
        land in.

        The fallback returns the **narrower** rows the old
        ``ProjectCatalogIndex.summaries()`` gives (``id``, ``kind``,
        ``canvas_id``, ``tags``, ``deleted_at`` -- no ``rating``, ``notes`` or
        ``filename``), in the same input order. A consumer that needs the
        extra columns has to check that the catalog has the batch API.
        """
        self._check_cancel(cancel)
        grouped = getattr(self.index, 'summaries_grouped', None)
        if callable(grouped):
            rows = grouped(item_ids)
        else:
            rows = self._summaries_grouped_fallback(item_ids)
        self._check_cancel(cancel)
        return rows

    def summaries_buckets(self, item_ids, *, cancel=None):
        """The counter buckets of the given catalog rows, in one round trip.

        Same buckets as :meth:`grouped_counts`, restricted to the given ids:
        one entry per ``(canvas_id, deleted, untagged)`` with the number of the
        given asset rows in it. This is the API the counter path wants -- it
        answers a counter question with a counter instead of one row per
        asset.

        Forwarded from the catalog side (``ProjectCatalogIndex.summaries_buckets``,
        A-3) with the same capability probe as :meth:`summaries_grouped`: while
        the catalog side has no such method, the buckets are computed here
        from the row-per-id :meth:`summaries`, with the catalog's own bucket
        rule (asset rows only, and a malformed tags column counts as tagged).
        """
        self._check_cancel(cancel)
        buckets = getattr(self.index, 'summaries_buckets', None)
        if callable(buckets):
            rows = buckets(item_ids)
        else:
            rows = self._summaries_buckets_fallback(item_ids)
        self._check_cancel(cancel)
        return rows

    def _summaries_grouped_fallback(self, item_ids):
        """The batch rows, taken row by row while the catalog has no API."""
        wanted = tuple(dict.fromkeys(item_ids))
        by_id = {row['id']: row for row in self.index.summaries(wanted)}
        return tuple(by_id[key] for key in wanted if key in by_id)

    def _summaries_buckets_fallback(self, item_ids):
        """The same buckets, computed row by row while the catalog has no API.

        The bucket rule is the catalog's own: one entry per
        ``(canvas_id, deleted, untagged)``, asset rows only
        (``kind NOT IN ('text','path')``), and a malformed tags column counts
        as *tagged* -- the same tolerance ``prism_untagged_safe()`` has.
        """
        totals = collections.Counter()
        for row in self.index.summaries(item_ids):
            if row.get('kind') in ('text', 'path'):
                continue
            key = (row.get('canvas_id'), bool(row.get('deleted_at')),
                   _untagged_bucket(row.get('tags')))
            totals[key] += 1
        return tuple((canvas, deleted, untagged, total)
                     for (canvas, deleted, untagged), total
                     in sorted(totals.items(),
                               key=lambda item: (str(item[0][0]), item[0][1],
                                                 item[0][2])))

    def display_summaries(self, item_ids, *, cancel=None):
        self._check_cancel(cancel)
        rows = self.index.display_summaries(item_ids)
        self._check_cancel(cancel)
        return rows

    def card_rows(self, item_ids, *, cancel=None):
        self._check_cancel(cancel)
        rows = self.index.card_rows(item_ids)
        self._check_cancel(cancel)
        return rows

    def tag_groups(self, *, cancel=None, include_deleted=False):
        self._check_cancel(cancel)
        rows = self.index.tag_groups(cancel=cancel, **({'include_deleted': True} if include_deleted else {}))
        self._check_cancel(cancel)
        return rows

    def grouped_counts(self, *, cancel=None):
        self._check_cancel(cancel)
        rows = self.index.grouped_counts()
        self._check_cancel(cancel)
        return rows

    def count(self, *, kind=None, canvas_ids=None, untagged_only=False,
              min_rating=0, text='', include_deleted=False, cancel=None,
              known_canvas_ids=None, recent_only=False, recent_limit=None, tag_sets=None):
        """Matching-row count for the same predicate as :meth:`query`.

        The filters are validated the same way, so a wrong type cannot answer
        "zero matches" without saying why.

        ``known_canvas_ids`` is the hand-off described in :meth:`query`: the
        canvases that still exist in the project, passed on unchanged so the
        bucket sentinels can answer the scene's question.
        """
        self._check_cancel(cancel)
        clean = _normalize_filters(dict(kind=kind, canvas_ids=canvas_ids,
                                        untagged_only=untagged_only,
                                        min_rating=min_rating, text=text,
                                        include_deleted=include_deleted,
                                        recent_only=recent_only, recent_limit=recent_limit, tag_sets=tag_sets))
        known = _as_known_canvas_ids(known_canvas_ids)
        total, forwarded = _forward_known_canvas_ids(
            self.index.count,
            dict(kind=clean['kind'], canvas_ids=clean['canvas_ids'],
                 untagged_only=clean['untagged_only'],
                 min_rating=clean['min_rating'], text=clean['text'],
                 include_deleted=clean['include_deleted'],
                 **({'recent_only': True, 'recent_limit': clean['recent_limit']}
                    if clean['recent_only'] else {}),
                 **({'tag_sets': clean['tag_sets']} if clean['tag_sets'] is not None else {})),
            known)
        self.last_known_canvas_forwarded = forwarded
        self._check_cancel(cancel)
        return total

    def canvas_counts(self, *, kind=None, untagged_only=False, cancel=None):
        """Active per-canvas counts from the cache (scene-independent)."""
        self._check_cancel(cancel)
        clean = _normalize_filters(dict(kind=kind, untagged_only=untagged_only))
        counts = self.index.canvas_counts(kind=clean['kind'],
                                          untagged_only=clean['untagged_only'])
        self._check_cancel(cancel)
        return counts

    # ── internals ───────────────────────────────────────────────

    def _cache_state(self):
        """Trusted current-catalog status, memoised by source/cache/WAL stamps."""
        snapshot = self.telemetry_state()
        return snapshot if snapshot['catalog_state'] == 'ready' else None

    def _snapshot(self):
        """Counts and total from one read-only connection, so they agree."""
        try:
            with closing(self.index._cache(readonly=True)) as cache:
                state = self.index._ready(cache)
                rows = cache.execute('SELECT scope,total FROM counts').fetchall()
        except (CatalogNotReady, sqlite3.Error, OSError, ValueError):
            return None
        counts = {}
        try:
            for scope, total in rows:
                canvas_id, kind = json.loads(scope)
                counts[(canvas_id, kind)] = int(total)
            return dict(counts=counts, total=int(state['total']))
        except (KeyError, TypeError, ValueError):
            # Built by this version but internally inconsistent. Reporting zero
            # here would claim the project is empty, which is the one wrong answer.
            raise CatalogNotReady('Catalog counts are unreadable') from None

    @staticmethod
    def _check_cancel(cancel):
        if cancel is not None and cancel():
            raise OpenScopeCancelled('Open scope read cancelled')


__all__ = ['MAX_BATCH', 'DEFAULT_PAGE_LIMIT', 'FILTER_NAMES', 'KNOWN_CANVAS_PARAM',
           'CatalogNotReady', 'OpenScopeCancelled', 'ProjectOpenScope',
           'catalog_read_counters', 'reset_catalog_read_counters',
           'add_catalog_read_observer', 'remove_catalog_read_observer']
