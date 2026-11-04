"""Display-only directory assets. None are registered in the editable scene.

The directory rows reach the browser through `CatalogRows`, a lazy
sequence: it reads the catalog one bounded page at a time and only keeps
the pages the consumer actually asks for. A 100k-row project used to be
materialised as 100k dicts before the first row could be shown.
"""
from collections import OrderedDict
import collections
import dataclasses
import inspect
import itertools
import json
import sys
import threading
import time
import weakref
import tempfile
import zlib

from PyQt6 import QtCore
from prism.asset_records import ImageRecord
from prism.item_lifecycle import is_deleted
from prism.project_catalog_index import CatalogNotReady
from prism.project_open_scope import OpenScopeCancelled
from prism.presentation_evidence import QueryEvidence, fingerprint
from prism.qt_thread_safety import JobRegistry, gui_signal_carrier


class CatalogAssetProxy(ImageRecord):
    _catalog_proxy = True

    # Immutable defaults are shared until a visible record actually changes
    # them. Do not initialize editing/Qt/decode state for all 100k rows.
    _scale, _rotation, _flip, _z, _opacity = 1, 0, 1, 0, 1
    _visible, _selected, _disposed = True, False, False
    _graphics = None
    _sequence = 0
    _source_blob = _source_path = None
    _source_pinned = False
    _grayscale = _preview_adjusted = _display_decode_failed = False
    _grayscale_cache = _preview_pixmap = _overview_pixmap = None
    _overview_payload = _display_thumbnail = _derived_pixmap = None
    _display_source_revision = 0
    _display_decode_request = _perceptual_hash = None
    _last_painted = 0
    color_group = dominant_color = active_mode = None
    crop_mode = False
    viewport_scale = 1
    _flags = (ImageRecord.GraphicsItemFlag.ItemIsMovable
              | ImageRecord.GraphicsItemFlag.ItemIsSelectable
              | ImageRecord.GraphicsItemFlag.ItemSendsGeometryChanges)

    @staticmethod
    def row_signature(row):
        # Only immutable scalar values; do not retain another row dictionary.
        # Include deletion/availability as well as every projected display field.
        return tuple(row.get(key) for key in (
            'id', 'kind', 'canvas_id', 'title', 'filename', 'width', 'height',
            'tags', 'rating', 'notes', 'group_id', 'deleted_at',
            'has_preview', 'has_original'))

    def matches_catalog_row(self, row, source, scene, reader):
        return (self._scene_ref() is scene
                and self._catalog_project_id == scene.project_id
                and self._catalog_reader() is reader
                and self._catalog_source_path == str(source)
                and self._catalog_row_signature == self.row_signature(row))

    def __init__(self, row, source, scene, reader, *, parsed_tags=None):
        extra = dict(title=row.get('title') or '', filename=row.get('filename') or '',
                     canvasId=row.get('canvas_id'), imageWidth=max(1, row.get('width') or 1),
                     imageHeight=max(1, row.get('height') or 1),
                     tags=(tuple(json.loads(row.get('tags') or '[]')) if parsed_tags is None
                           else tuple(parsed_tags)), rating=row.get('rating') or 0,
                     notes=row.get('notes') or '', groupId=row.get('group_id'),
                     deletedAt=row.get('deleted_at'), lastUsed=row.get('last_used', ''))
        # Display-only projection: ordinary editable ImageRecord initialization
        # creates many per-instance defaults and is intentionally not used here.
        self.save_id = row['id']
        self.filename = extra['filename']
        self._extra_data = extra
        self._prism_source = (str(source), row['id'])
        self._source_format = self._format_from_filename(self.filename)
        self.load_metadata_from_data(extra)
        if self._deleted_at is not None:
            self._visible = False
            self._flags = self._flags & ~self.GraphicsItemFlag.ItemIsSelectable
        self._catalog_row_signature = self.row_signature(row)
        self._catalog_source_path = str(source)
        self.TYPE = row.get('kind') or 'pixmap'
        self._catalog_project_id = scene.project_id
        self._catalog_display_ready = False
        self._catalog_display_failed = False
        self._catalog_reader = weakref.ref(reader)
        self._scene_ref = weakref.ref(scene)  # Decoder lifetime token only: no scene.addItem.

    def load_metadata_from_data(self, data):
        # Passive display rows do not need independent mutable tag/category
        # lists. Share the immutable tag tuple with the owned projection.
        self._last_used = data.get('lastUsed', '')
        self._group_id = data.get('groupId')
        self._group_note = data.get('groupNote', '')
        self._categories = ()
        self._tags = data['tags']
        self._rating = max(0, min(5, int(data.get('rating', 0) or 0)))
        self._notes = data.get('notes', '')
        self._title = data.get('title', '')
        self._canvas_id = data.get('canvasId', 'default-canvas')
        self._deleted_at = data.get('deletedAt')

    @property
    def _position(self):
        position = self.__dict__.get('_catalog_position')
        return position if position is not None else QtCore.QPointF()

    @_position.setter
    def _position(self, value):
        self._catalog_position = value

    @property
    def _image_size(self):
        size = self.__dict__.get('_catalog_image_size')
        return size if size is not None else QtCore.QSize(
            self._extra_data['imageWidth'], self._extra_data['imageHeight'])

    @_image_size.setter
    def _image_size(self, value):
        self._catalog_image_size = value

    @property
    def _crop(self):
        crop = self.__dict__.get('_catalog_crop')
        return crop if crop is not None else QtCore.QRectF(
            0, 0, self._extra_data['imageWidth'], self._extra_data['imageHeight'])

    @_crop.setter
    def _crop(self, value):
        self._catalog_crop = value

    def request_catalog_display(self, owner):
        reader = self._catalog_reader()
        if reader is not None:
            reader.request(self, owner)

    def graphics_item(self):
        raise RuntimeError('Directory proxies must be resolved by ID before editing')

    def get_save_data(self, *args, **kwargs):
        raise RuntimeError('Directory proxies are display-only, never save them as canonical assets')

    def setSelected(self, value):
        raise RuntimeError('Directory proxies must not be selected in the editable scene')

    def update(self, *args):
        from prism.lod import cache
        store = cache()
        if store is not None:
            store.changed.emit(self)

    def prepareGeometryChange(self):
        pass


class _Signals(QtCore.QObject):
    result = QtCore.pyqtSignal(object)


# ── A-4: what one browser read cost ───────────────────────────────────
#
# `project_open_scope` publishes the *catalog* side of the count point
# (`scope.last_read_stats`, `catalog_read_counters()`, the trace event
# `catalog_rows_read`): how many rows the catalog handed out. The browser
# adds the fact only it knows: how many rows were **materialised** as
# Python dicts, and how many pages it had to ask for.
#
# The counters are always alive (no trace needed) plus an injectable
# observer:
#
# * `catalog_browser_counters()` -- snapshot
# * `reset_catalog_browser_counters()` -- clear (tests)
# * `add_catalog_browser_read_observer(cb)` / `remove_catalog_browser_read_observer(cb)`
#   -- `cb(event, **fields)`
# * trace event `catalog_browser_rows_read` / `catalog_browser_empty`
#
# The empty path records one event too: "nothing matched" is a fact that
# has to be observable, not a silent state.

_BROWSER_LOCK = threading.Lock()
_BROWSER_COUNTERS = collections.Counter()
_BROWSER_OBSERVERS: list = []


def catalog_browser_counters() -> dict:
    """Snapshot of the browser-side read counters.

    Keys: ``catalog_browser_reads`` (reads started),
    ``catalog_browser_rows_read`` (rows read from the catalog),
    ``catalog_browser_pages_read`` (catalog pages read),
    ``catalog_browser_delivered`` (rows handed to the consumer) and
    ``catalog_browser_empty`` (reads that matched nothing).
    """
    with _BROWSER_LOCK:
        return dict(_BROWSER_COUNTERS)


def reset_catalog_browser_counters() -> None:
    """Clear the browser-side counters (tests only)."""
    with _BROWSER_LOCK:
        _BROWSER_COUNTERS.clear()


def add_catalog_browser_read_observer(callback) -> None:
    """Register a browser-read observer: ``callback(event, **fields)``."""
    if callback not in _BROWSER_OBSERVERS:
        _BROWSER_OBSERVERS.append(callback)


def remove_catalog_browser_read_observer(callback) -> None:
    """Unregister a browser-read observer."""
    try:
        _BROWSER_OBSERVERS.remove(callback)
    except ValueError:
        pass


def _record_browser_read(event: str, *, rows: int = 0, pages: int = 0,
                         **fields) -> None:
    """Record one browser-read event: the counter always moves.

    A failing trace or observer must never change the counter, and never
    change what the caller gets back.
    """
    with _BROWSER_LOCK:
        _BROWSER_COUNTERS[event] += 1
        _BROWSER_COUNTERS[event + '_rows'] += int(rows or 0)
        _BROWSER_COUNTERS[event + '_pages'] += int(pages or 0)
    module = sys.modules.get('prism.performance_trace')
    if module is not None:
        try:
            module.record(event, rows=rows, pages=pages, **fields)
        except Exception:
            pass
    for callback in list(_BROWSER_OBSERVERS):
        try:
            callback(event, rows=rows, pages=pages, **fields)
        except Exception:
            pass


def _count_delivered(rows: int) -> None:
    """Legacy count of logical sequence accesses, not final GUI consumption."""
    with _BROWSER_LOCK:
        _BROWSER_COUNTERS['catalog_browser_delivered'] += int(rows or 0)


# ── A-4: the lazy row sequence ────────────────────────────────────────

#: One catalog page. `ProjectOpenScope.iter_query` reads the cache in
#: pages of this size, so one page is one bounded SQLite round trip.
DEFAULT_PAGE_SIZE = 512

#: How many pages the producer may run ahead of the consumer.
READ_AHEAD_PAGES = 2

#: How many pages stay resident. 8 * 512 = 4096 rows -- a few MB of
#: dicts, instead of the 100k dicts the old `list()` call kept alive.
MAX_CACHED_PAGES = 8

#: How long a waiting thread blocks before it re-checks the cancel flag.
PRODUCER_WAIT = 0.2

#: Threads in this module's own pool (thumbnail queue).
BROWSER_POOL_THREADS = 4

_POOL = None
_POOL_LOCK = threading.Lock()


def catalog_browser_pool():
    """The thread pool this module uses for its own background work.

    The thumbnail queue used to run on the global pool, where it competed
    with tiles, layout and decoding: a busy global pool left the directory
    cards without their previews for a long time. A dedicated pool keeps
    the directory traffic away from that.

    The pool is created on first use, never at import time: a
    `QThreadPool` must not be built before the application exists.
    """
    global _POOL
    with _POOL_LOCK:
        if _POOL is None:
            pool = QtCore.QThreadPool()
            pool.setMaxThreadCount(BROWSER_POOL_THREADS)
            pool.setExpiryTimeout(30000)
            _POOL = pool
        return _POOL


def _normalize_filters(filters):
    """The filters the scope accepts, with the string canvas id guarded.

    `canvas_ids` is a **sequence** of ids. A plain string is ONE id: the
    index layer would split it into characters and match nothing without
    saying so (`project_catalog_index._predicate` does `tuple(canvas_ids)`).
    The scope layer already wraps a string into a one-element tuple, but
    this side never hands a string down, so the guard cannot be bypassed
    from here.
    """
    clean = dict(filters or {})
    canvas_ids = clean.get('canvas_ids')
    if isinstance(canvas_ids, str):
        clean['canvas_ids'] = (canvas_ids,)
    return clean


# ── 能力探测：scope 层支不支持 `max_rows` ─────────────────────────────
#
# `ProjectOpenScope.iter_query()` 正在加可选 kwarg `max_rows`（总量上限）。
# 落地顺序与本文件无关：**探测一次并缓存**，不支持就不传。
#
# 探测必须看**显式参数**，不能看 `**filters`：scope 层的 `iter_query`
# 用 `**filters` 收未知关键字，并在第一次推进生成器时抛
# `TypeError('iter_query() got unexpected filter arguments: max_rows')`。
# 那时已经进了生成器，错误会从读行的那一路冒出来 —— 所以宁可靠
# `inspect.signature` 判断，不靠试错。
#
# 缓存键是**底层函数对象**：A-2 的文件被临时回退时，函数对象变了，
# 缓存自然失效，不会拿着旧结论去传一个不存在的关键字。

_MAX_ROWS_SUPPORT: dict = {}


def _scope_supports_max_rows(scope) -> bool:
    """Does this scope's `iter_query` take an explicit `max_rows`?

    Probed once per scope implementation and cached. Any failure to
    inspect means "not supported" -- the caller then keeps the
    producer-side cap, which needs nothing from the scope layer.
    """
    try:
        method = scope.iter_query
    except Exception:
        return False
    key = getattr(method, '__func__', method)
    try:
        cached = _MAX_ROWS_SUPPORT.get(key)
    except TypeError:                      # unhashable mock
        cached = None
        key = None
    if cached is not None:
        return cached
    try:
        parameters = inspect.signature(method).parameters
        supported = 'max_rows' in parameters
    except (TypeError, ValueError):
        supported = False
    if key is not None:
        _MAX_ROWS_SUPPORT[key] = supported
    return supported


class CatalogRows:
    """Lazy, replayable, thread-safe view over one directory read.

    The browser treats the payload of `CatalogQuery` as a list: it slices
    ``rows[offset:offset+256]``, asks ``len(rows)`` for the progress
    counter and reads ``row['id']``. This class answers all three exactly
    like the materialised list did, while holding at most
    `MAX_CACHED_PAGES` pages of rows in memory.

    * `len(rows)` is the **hit count** of the read. It comes from
      `ProjectOpenScope.count()`, which answers from the catalog cache
      without reading a single row.
    * The rows are read by a **producer thread** through
      `ProjectOpenScope.iter_query()`, one bounded page at a time, so a
      catalog read never runs on the GUI thread.
    * `rows[a:b]` returns the same rows, in the same order, that the
      materialised list held.
    * The sequence can be **walked again from the beginning**: the
      browser caches it and replays it when the mode changes. A replay
      re-reads only the pages it needs; pages that are still cached are
      served from memory.

    Thread safety: one lock guards the page cache, the cursor and the
    demand counter. The producer holds it only while it swaps a finished
    page in -- the catalog read itself happens outside the lock.
    """

    def __init__(self, scope, filters=None, *, max_rows=None, limit=None,
                 page_size=DEFAULT_PAGE_SIZE, cancel=None, evidence=None):
        self.scope = scope
        self.filters = _normalize_filters(filters)
        self.cancel = cancel
        if evidence is None:
            try:
                state = scope.source_state()
            except Exception:
                state = None
            evidence = QueryEvidence(dict(filters=self.filters),
                source_revision=fingerprint((str(getattr(scope, 'project_path', '')), state)))
            evidence.record('catalog_query_started', complete=False)
        self.presentation_evidence = evidence
        self._read_counts = evidence._counter('read')
        self.page_size = max(1, int(page_size))
        #: The **total** number of rows this read may return.
        #:
        #: `max_rows` is the canonical name (it is the scope layer's own
        #: kwarg); `limit` is the older alias of the very same concept
        #: from the first round. They are not two different limits: when
        #: both are given the **smaller** one wins, because both mean
        #: "at most this many rows".
        #:
        #: The cap is enforced twice on purpose:
        #: 1. **scope layer** -- `scope.iter_query(max_rows=...)`, so the
        #:    catalog stops reading by itself (only when the scope really
        #:    has the kwarg; see `_scope_supports_max_rows`);
        #: 2. **producer layer** -- the producer stops after the cap even
        #:    if the scope ignored it, so the cap holds either way.
        self.max_rows = self._effective_cap(max_rows, limit)
        #: Kept as an alias: the first round exposed `limit` publicly.
        self.limit = self.max_rows
        self._max_rows_forwarded = False
        self._lock = threading.RLock()
        self._ready = threading.Condition(self._lock)
        self._pages = OrderedDict()      # page index -> tuple of rows
        self._cursor = 0                 # next page the producer will read
        self._wanted = -1                # highest page the consumer asked for
        self._pinned = -1                # the page the consumer reads now
        self._exhausted = False
        self._total = 0                  # hit count -- what len() answers
        self._counted_total = 0          # restore a cancelled, cached replay
        self._produced = 0               # rows produced in the current pass
        self._rows_read = 0              # rows read from the catalog (cumulative)
        self._pages_read = 0
        self._delivered = 0
        self._error = None
        self._cancelled = False
        self._closed = False
        self._generation = 0
        self._producer = None
        self._generator = None
        # Replay metadata on disk instead of retaining 100k dicts or rereading
        # the directory after every mode switch. RAM still holds eight pages.
        self._spool = tempfile.TemporaryFile()
        self._spool_pages = {}
        self._replay_wanted = OrderedDict()
        self._spool_bytes = 0
        self._spool_limit = 128 * 1024 * 1024
        self._spool_page_limit = 4096
        self._rows_replayed = 0
        self._disk_replay_row_loads = 0
        self._resume_after = None
        self._can_resume = 'resume_after' in inspect.signature(scope.iter_query).parameters

    # ── preparation (off the GUI thread) ─────────────────────────────

    @staticmethod
    def _effective_cap(max_rows, limit):
        """One cap out of two names: the smaller of the two wins."""
        values = [int(value) for value in (max_rows, limit)
                  if value is not None]
        if not values:
            return None
        return max(0, min(values))

    def prepare(self):
        """Count the hits and start the producer. Call off the GUI thread.

        Raises `CatalogNotReady` when the catalog cannot answer, so the
        caller never receives "no rows" where the truth is "no answer".
        """
        self._max_rows_forwarded = (self.max_rows is not None
                                    and _scope_supports_max_rows(self.scope))
        total = self.scope.count(**self.filters)
        if self.max_rows is not None:
            total = min(total, self.max_rows)
            self.page_size = max(1, min(self.page_size, self.max_rows))
        self._total = self._counted_total = total
        if total <= 0:
            self._record_empty()
            self._exhausted = True
            self.presentation_evidence.record('catalog_rows_read_evidence',
                **self.presentation_evidence.read_fields(), complete=True, final=True)
            return self
        self._start_producer()
        self._wait_for_page(0)
        return self

    def _record_empty(self):
        """An empty answer is never silently "the project is empty".

        A `canvas_ids` push-down that matches nothing is ambiguous: the
        filter may be right, or the directory and the scene may use
        different canvas coordinates (verified on one project, not on all
        of them). Answering "there are no assets" in that case would be a
        lie, so it fails loudly and the caller falls back to the in-memory
        branch. The other filters are coordinate independent, so an empty
        answer there is a real "nothing matched".
        """
        catalog_total = None
        try:
            catalog_total = self.scope.total()
        except Exception:
            catalog_total = None
        label = self._scope_label()
        if 'canvas_ids' in self.filters and 'known_canvas_ids' not in self.filters:
            _record_browser_read('catalog_browser_empty', reason='pushdown_miss',
                                 catalog_total=catalog_total, scope=label)
            raise CatalogNotReady(
                'Directory canvas filter matched no rows; the in-memory '
                'branch is authoritative')
        _record_browser_read('catalog_browser_empty',
                             reason='filter_no_match' if self.filters
                             else 'project_empty',
                             catalog_total=catalog_total, scope=label)

    def _scope_label(self):
        if not self.filters:
            return 'all'
        return '|'.join('%s=%s' % (key, value)
                        for key, value in sorted(self.filters.items()))

    # ── the sequence contract ─────────────────────────────────────────

    def __len__(self):
        return self._total

    def __getitem__(self, key):
        if isinstance(key, slice):
            start, stop, step = key.indices(self._total)
            if step == 1:
                return self._slice(start, stop)
            return [self._row(index)
                    for index in range(start, stop, step)]
        index = int(key)
        if index < 0:
            index += self._total
        if index < 0 or index >= self._total:
            raise IndexError('catalog row index out of range')
        return self._row(index)

    def __iter__(self):
        for index in range(self._total):
            yield self._row(index)

    def __bool__(self):
        return self._total > 0

    def __repr__(self):
        return '<CatalogRows total=%d rows_read=%d>' % (self._total,
                                                        self._rows_read)

    def stats(self):
        """Sampling is side-effect free; delivered means sequence accesses."""
        with self._lock:
            return dict(total=self._total, rows_read=self._rows_read,
                        pages_read=self._pages_read, delivered=self._delivered,
                        cached_pages=len(self._pages), page_size=self.page_size,
                        exhausted=self._exhausted, cancelled=self._cancelled,
                        complete=not self._error, replay_rows=self._rows_replayed,
                        disk_replay_row_loads=self._disk_replay_row_loads,
                        replay_bytes=self._spool_bytes, replay_limit=self._spool_limit)

    def _saved_page(self, number):
        location = self._spool_pages.get(number)
        if location is None or self._spool.closed:
            return None
        self._spool.seek(location[0])
        rows = tuple(json.loads(zlib.decompress(self._spool.read(location[1]))))
        self._disk_replay_row_loads += len(rows)
        self._pages[number] = rows
        self._pinned = number
        self._trim()
        return rows

    def _save_page(self, number, rows):
        if number in self._spool_pages or self._spool.closed:
            return
        payload = zlib.compress(json.dumps(rows, ensure_ascii=True, separators=(',', ':')).encode('utf-8'))
        if (self._spool_bytes + len(payload) > self._spool_limit
                or len(self._spool_pages) >= self._spool_page_limit):
            return  # Correct bounded fallback: an uncached page may be reread.
        self._spool.seek(0, 2)
        offset = self._spool.tell()
        self._spool.write(payload)
        self._spool_pages[number] = (offset, len(payload))
        self._spool_bytes += len(payload)

    def try_slice(self, start, stop):
        """GUI: only take resident rows, never wait or decode a spool page."""
        if not self._lock.acquire(blocking=False):
            return None
        try:
            result = []
            end = min(stop, self._total)
            while start < end:
                number, offset = divmod(start, self.page_size)
                page = self._pages.get(number)
                if page is None:
                    if self._error is not None:
                        raise self._error
                    if number in self._spool_pages and not self._spool.closed:
                        self._replay_wanted[number] = None
                        self._start_producer()
                        self._ready.notify_all()
                        return None
                    if number < self._cursor:
                        self._restart(number)
                    elif self._exhausted or self._cancelled:
                        self._settle()
                        break
                    self._wanted = max(self._wanted, number + READ_AHEAD_PAGES)
                    self._start_producer()
                    self._ready.notify_all()
                    return None
                self._pinned = number
                self._pages.move_to_end(number)
                take = min(end-start, len(page)-offset)
                if take <= 0:
                    break
                result.extend(page[offset:offset+take])
                start += take
            if result:
                self._accessed(result)
            return result
        finally:
            self._lock.release()

    def bind_evidence(self, evidence, cancel=None):
        """A replay gets a new query, but keeps the originating read ID."""
        with self._ready:
            interrupted = self._cancelled or bool(self.cancel and self.cancel())
            evidence.reuse_read(self.presentation_evidence)
            self.presentation_evidence = evidence
            self.cancel = cancel
            if interrupted:
                self._cancelled = False
                self._total = self._counted_total
                # Checking at most eight cached page lengths is bounded. A
                # fully resident projection needs no replacement producer.
                if sum(len(page) for page in self._pages.values()) >= self._total:
                    self._generation += 1
                    self._exhausted = True
                    self._close_generator()
                else:
                    self._restart(0)
            self._closed = False
            self._ready.notify_all()
            evidence.record('catalog_replay_state', phase='bound',
                            counter_basis='read_id_lifetime', **self.stats())

    def _accessed(self, rows, *, emit=True, evidence=None):
        evidence = self.presentation_evidence if evidence is None else evidence
        count = len(rows)
        with self._lock:
            self._delivered += count
        _count_delivered(count)
        evidence.observe('sequence_access', (row['id'] for row in rows))
        if emit:
            evidence.delivery('sequence_access')

    def close(self):
        """Stop the producer. The consumer does not have to call it."""
        with self._ready:
            self._closed = True
            self._cancelled = True
            self._exhausted = True
            self._generation += 1
            self._close_generator()
            self._spool.close()
            self._ready.notify_all()

    def _row(self, index):
        evidence = self.presentation_evidence
        page_no, offset = divmod(index, self.page_size)
        page = self._page(page_no)
        if page is None or offset >= len(page):
            raise IndexError('catalog row index out of range')
        row = page[offset]
        self._accessed((row,), emit=(self._delivered % 256 == 255 or index == self._total-1),
                       evidence=evidence)
        return row

    def _slice(self, start, stop):
        evidence = self.presentation_evidence
        rows = []
        index = start
        while index < stop:
            page_no, offset = divmod(index, self.page_size)
            page = self._page(page_no)
            if not page:
                break
            take = min(stop - index, len(page) - offset)
            rows.extend(page[offset:offset + take])
            index += take
        if rows:
            self._accessed(rows, evidence=evidence)
        return rows

    # ── the producer ─────────────────────────────────────────────────

    def _start_producer(self):
        with self._lock:
            producer = self._producer
            if producer is not None and producer.is_alive():
                return
            self._producer = threading.Thread(
                target=self._produce, name='prism-catalog-rows', daemon=True)
            self._producer.start()

    def _produce(self):
        while True:
            with self._lock:
                if self._closed or self._error is not None or self._cancelled:
                    return
                if self._replay_wanted:
                    number, _unused = self._replay_wanted.popitem(last=False)
                    try:
                        self._saved_page(number)
                    except Exception as error:
                        self._error = error
                    self._ready.notify_all()
                    continue
                if self._exhausted:
                    return
                if self._cursor > self._wanted:
                    # Nothing to read yet: wait until the consumer asks.
                    self._ready.wait(PRODUCER_WAIT)
                    continue
                cursor = self._cursor
                generation = self._generation
            try:
                with self._lock:
                    rows = (self._pages.get(cursor) if self._can_resume else None)
                    if rows is None and self._can_resume:
                        rows = self._saved_page(cursor)
                    if rows is not None:
                        # Replay can be sparse when the disk budget fills. A
                        # generator opened before this cached page would lag
                        # behind it; reopen after its last ID at the next gap.
                        self._close_generator()
                replayed = rows is not None
                if not replayed:
                    rows = self._pull()
            except OpenScopeCancelled:
                with self._ready:
                    if generation != self._generation:
                        continue  # Cancellation belonged to an interrupted pass.
                    self._cancelled = True
                    self._exhausted = True
                    self._close_generator()
                    self._settle()
                    self._ready.notify_all()
                return
            except Exception as error:
                with self._ready:
                    if generation != self._generation:
                        continue  # A failed old pass cannot poison the new query.
                    self._error = error
                    self._exhausted = True
                    self._close_generator()
                    self._ready.notify_all()
                return
            with self._ready:
                if self._closed:
                    return
                if generation != self._generation:
                    continue            # a replay started while reading
                if rows is None:
                    self._exhausted = True
                    self._settle()
                else:
                    if replayed:
                        self._rows_replayed += len(rows)
                    else:
                        self._rows_read += len(rows)
                        self._pages_read += 1
                        self._save_page(cursor, rows)
                    if rows:
                        self._resume_after = rows[-1]['id']
                    self._produced += len(rows)
                    if cursor not in self._pages:
                        self._pages[cursor] = rows
                        self._trim()
                    else:
                        self._pages.move_to_end(cursor)
                    self._cursor = cursor + 1
                    if self.max_rows is not None and self._produced >= self.max_rows:
                        # The caller asked for at most this many rows: stop
                        # here instead of asking the catalog for one more
                        # page. The elements that take part in the caller's
                        # sort are exactly these.
                        self._exhausted = True
                        self._close_generator()
                self._ready.notify_all()

    def _close_generator(self):
        """Close the read early so its count point is published.

        `ProjectOpenScope.iter_query()` publishes `last_read_stats` and
        the C2 counters when the iteration ends. A read that stops at the
        caller's limit has to close the generator, otherwise the numbers
        of the stopped read stay unpublished.
        """
        generator, self._generator = self._generator, None
        if generator is None:
            return
        try:
            generator.close()
        except Exception:
            pass

    def _pull(self):
        """Read one page of rows from the directory (outside the lock).

        `max_rows` is forwarded to the scope **only** when the scope
        really has that kwarg (`_scope_supports_max_rows`). The fallback
        below exists for a scope that refuses it after all: the cap is
        enforced by the producer anyway, so the read stays correct and
        bounded either way.

        The refusal can arrive in two places -- when the generator is
        built (a plain function that validates its arguments) or when it
        is first advanced (a generator function that validates them on
        the first `next()`). Both are covered.
        """
        evidence = self.presentation_evidence  # Never rebase a late read callback.
        rows = None
        generator = None
        for attempt in (0, 1):
            try:
                with self._lock:
                    generator = self._generator
                    if generator is None:
                        generator = self._new_generator()
                        self._generator = generator
                rows = tuple(itertools.islice(generator, self.page_size))
                break
            except TypeError as error:
                if attempt or 'max_rows' not in str(error):
                    raise
                # The scope refused `max_rows`: drop it and read on without.
                with self._lock:
                    self._max_rows_forwarded = False
                    if self._generator is generator:
                        self._generator = None
        if not rows:
            evidence.record('catalog_rows_read_evidence', **evidence.read_fields(),
                            complete=True, final=True)
            return None
        self._read_counts.observe(row['id'] for row in rows)
        evidence.read_pulled(len(rows))
        evidence.record('catalog_rows_read_evidence', **evidence.read_fields(),
                        complete=False, final=False)
        _record_browser_read('catalog_browser_rows_read', rows=len(rows),
                             pages=1, scope=self._scope_label())
        return rows

    def _new_generator(self):
        """A fresh directory read, with `max_rows` only if it is supported."""
        kwargs = dict(self.filters)
        if self._max_rows_forwarded:
            kwargs['max_rows'] = max(0, self.max_rows-self._produced)
        cancel = self.cancel  # A rebound replay cannot revive this old pass.
        if self._can_resume:
            kwargs['resume_after'] = self._resume_after
        generator = self.scope.iter_query(limit=self.page_size,
                                         cancel=lambda: bool(cancel and cancel()), **kwargs)
        return generator

    def _settle(self):
        """The hit count is what the read could actually deliver.

        The count comes from a separate `COUNT` query, so a catalog that
        changed underneath could make it too large. Squeezing it to the
        rows that really exist keeps `len(rows)` and the number of
        deliverable rows in step -- the browser walks the rows until
        `offset == len(rows)`, and a count that stayed too large would
        keep it walking forever.
        """
        if self._produced < self._total:
            self._total = self._produced

    def _trim(self):
        """Keep the page cache bounded; the page being read is never dropped."""
        while len(self._pages) > MAX_CACHED_PAGES:
            for index in list(self._pages):
                if index != self._pinned:
                    self._pages.pop(index)
                    break
            else:
                break

    def _wait_for_page(self, index):
        """Block until page `index` is resident (called off the GUI thread)."""
        with self._ready:
            while (index not in self._pages and not self._exhausted
                   and self._error is None and not self._cancelled):
                self._wanted = max(self._wanted, index)
                self._ready.notify_all()
                self._ready.wait(PRODUCER_WAIT)

    def _page(self, page_no):
        """Return page `page_no`, reading it if needed. Blocks the caller."""
        with self._ready:
            while True:
                page = self._pages.get(page_no)
                if page is None:
                    page = self._saved_page(page_no)
                if page is not None:
                    self._pages.move_to_end(page_no)
                    self._pinned = page_no
                    return page
                if self._error is not None:
                    raise self._error
                if page_no < self._cursor:
                    # The page was read in this pass but has been evicted:
                    # the consumer is replaying the sequence, so the
                    # producer starts a new pass from the first page.
                    self._restart(page_no)
                    continue
                if self._exhausted or self._cancelled:
                    # Nothing left to read: the read ends here.
                    self._settle()
                    return None
                self._wanted = max(self._wanted, page_no + READ_AHEAD_PAGES)
                self._ready.notify_all()
                self._start_producer()
                self._ready.wait(PRODUCER_WAIT)

    def _restart(self, page_no):
        """Start a new pass over the directory (the sequence is replayed).

        The pages that are still cached stay valid -- the catalog answers
        the same rows in the same order -- so the new pass only has to
        read what the consumer asks for again.
        """
        self._generation += 1
        self._cursor = 0
        self._produced = 0
        self._resume_after = None
        self._close_generator()
        self._exhausted = False
        self._wanted = page_no
        self._ready.notify_all()


class CatalogQuery(QtCore.QRunnable):
    """Scalar projection only. No detail JSON, originals, Qt items or GUI calls.

    `run()` does not materialise the whole hit set any more: it hands the
    consumer a `CatalogRows` sequence that reads the directory one
    bounded page at a time and keeps only the pages the consumer asks
    for. The payload shape is unchanged -- `(token, rows, error)` -- and
    `rows` still answers `len()`, slicing and `row['id']` exactly like the
    list it replaces.

    `max_rows` is a **total** cap, not a page size: the read stops after
    `max_rows` rows, so the elements that take part in the caller's sort
    are at most `max_rows`. The page size stays `page_size`.

    `max_rows` and `limit` are **the same concept** (a total cap), not
    two different limits: `max_rows` is the canonical name because it is
    the scope layer's own kwarg, `limit` is the older alias. When both
    are given the smaller one wins.

    The cap is forwarded to `scope.iter_query(max_rows=...)` **only when
    the scope really has that kwarg** (probed once, cached). Whether or
    not the scope honours it, the producer stops at the cap, so the read
    is bounded either way and the code never depends on the scope layer's
    version.
    """
    def __init__(self, scope, token, cancelled, callback, *, filters=None,
                 max_rows=None, limit=None, page_size=DEFAULT_PAGE_SIZE,
                 evidence=None):
        super().__init__()
        self.evidence = evidence
        self.scope, self.token, self.cancelled = scope, token, cancelled
        self.filters = dict(filters or {})
        self.max_rows = CatalogRows._effective_cap(max_rows, limit)
        self.limit = self.max_rows
        self.page_size = page_size
        # The carrier has no owner on purpose: it must outlive the browser
        # that queued the work.  The browser keeps the job alive until it
        # finished, and that is where the carrier is released -- on the GUI
        # thread, never on a worker.
        self.signals = gui_signal_carrier(_Signals(),
                                          QtCore.QCoreApplication.instance())
        self.signals.result.connect(callback)

    def run(self):
        try:
            rows = CatalogRows(self.scope, self.filters, max_rows=self.max_rows,
                               page_size=self.page_size, cancel=self.cancelled,
                               evidence=self.evidence)
            rows.prepare()
            result = self.token, rows, None
        except Exception as error:
            result = self.token, None, str(error)
        if not self.cancelled():
            try:
                self.signals.result.emit(result)
            except RuntimeError:
                pass


class _DisplayQuery(QtCore.QRunnable):
    def __init__(self, scope, requests, callback):
        super().__init__()
        self.scope, self.requests = scope, requests
        self.revisions = {item.save_id: item._display_source_revision
                          for item, _owner in requests}
        # The carrier has no owner on purpose; see ``CatalogQuery``.
        self.signals = gui_signal_carrier(_Signals(),
                                          QtCore.QCoreApplication.instance())
        self.signals.result.connect(callback)

    def run(self):
        try:
            source_state = self.scope.source_state()
            rows = self.scope.display_summaries([item.save_id for item, owner in self.requests])
            if source_state != self.scope.source_state():
                raise RuntimeError('Source changed while display metadata was read')
            rows = [dict(row, _read_source_state=source_state,
                         _read_item_revision=self.revisions.get(row['id']))
                    for row in rows]
            error = None
        except Exception as failure:
            rows, error = (), str(failure)
        try:
            self.signals.result.emit((self.requests, rows, error))
        except RuntimeError:
            pass


class CatalogDisplayReader(QtCore.QObject):
    """Bounded visible metadata queue and 8MB compressed-thumbnail retention."""
    def __init__(self, scope, parent):
        super().__init__(parent)
        self.scope = scope
        self.pending = OrderedDict()
        self.running = None
        # The job stays alive until its result was delivered; see
        # ``_dispatch``.
        self._background_jobs = JobRegistry()
        self.thumbnails = OrderedDict()
        self.thumbnail_bytes = 0
        self.thumbnail_budget = 8 * 1024 * 1024
        from prism.resource_budget import shared_cache_budget
        shared_cache_budget().register(self, category='catalog_thumbnail',
                                       usage='thumbnail_bytes', priority=25)
        self.timer = QtCore.QTimer(self)
        self.timer.setSingleShot(True)
        self.timer.timeout.connect(self._dispatch)

    def _trim_cache_to(self, target):
        while self.thumbnails and self.thumbnail_bytes > target:
            _, (item_ref, cost) = self.thumbnails.popitem(last=False)
            item = item_ref()
            if item is not None:
                item._display_thumbnail = None
            self.thumbnail_bytes -= cost

    def request(self, item, owner):
        if item._catalog_display_ready or item._catalog_display_failed:
            return
        self.pending[id(item)] = item, weakref.ref(owner)
        while len(self.pending) > 256:
            self.pending.popitem(last=False)
        if self.running is None and not self.timer.isActive():
            self.timer.start(0)

    def _dispatch(self):
        if self.running is not None:
            return
        requests = []
        while self.pending and len(requests) < 32:
            _, (item, owner_ref) = self.pending.popitem(last=False)
            owner = owner_ref()
            if (owner is not None and not is_deleted(owner) and owner.isVisible()
                    and not item._catalog_display_ready and not item._catalog_display_failed
                    and owner.needs_display(item)):
                requests.append((item, owner_ref))
        if not requests:
            return
        self.running = _DisplayQuery(self.scope, requests, self._ready)
        # The job stays alive until its result was delivered: the pool
        # deletes the runnable in the worker thread, and that deletion must
        # not release the last reference to a Qt object.
        self._background_jobs.start(catalog_browser_pool(), self.running)

    @QtCore.pyqtSlot(object)
    def _ready(self, result):
        requests, rows, error = result
        self._background_jobs.release(self.running)
        self.running = None
        found = {row['id']: row for row in rows}
        from prism.lod import cache
        changed_rows = {}
        try:
            current_source_state = self.scope.source_state()
        except Exception:
            current_source_state = None
        for item, owner_ref in requests:
            owner = owner_ref()
            if owner is None or is_deleted(owner) or not owner.isVisible() or not owner.needs_display(item):
                continue
            row = found.get(item.save_id)
            if error or row is None:
                item._catalog_display_failed = True
            else:
                # The worker's validation is not sufficient: queued delivery
                # can arrive after the asset or archive has changed. Never
                # certify old pixels using the revision observed at delivery.
                try:
                    stale = (row.get('_read_item_revision') != item._display_source_revision
                             or current_source_state is None
                             or row.get('_read_source_state') != current_source_state)
                except Exception:
                    stale = True
                if stale:
                    continue
                item._overview_payload = row['overview']
                item._display_thumbnail = row['thumbnail']
                # display_summaries validates the source catalog before and after
                # its bounded read. These immutable proxies cannot be edited.
                # Keep the exact saved payload and its install revision; a later
                # arbitrary thumbnail or source revision must not inherit it.
                revision = item._display_source_revision
                for name, payload in (('thumbnail', row['thumbnail']),
                                      ('overview', row['overview'])):
                    setattr(item, '_catalog_' + name + '_provenance',
                            (tuple(payload), revision) if isinstance(payload, list)
                            and len(payload) == 3 else None)
                if row['crop'] is not None:
                    item._crop = QtCore.QRectF(*row['crop'])
                item._grayscale = row['grayscale']
                item._flip = row['flip']
                item._opacity = row['opacity']
                item._catalog_display_ready = True
                thumbnail = row['thumbnail']
                if thumbnail:
                    cost = len(thumbnail[2]) if isinstance(thumbnail, list) and len(thumbnail) == 3 else 0
                    self.thumbnails[id(item)] = weakref.ref(item), cost
                    self.thumbnail_bytes += cost
                # Geometry may change when the visible crop arrives. Build the
                # ID-to-row map once per model replacement, not per thumbnail.
                model = owner.model()
                if getattr(owner, '_catalog_geometry_items', None) is not model.items:
                    owner._catalog_geometry_items = model.items
                    # AssetCanvas already builds the exact identity map during
                    # model installation. Do not enumerate 100k rows a second
                    # time for the first visible thumbnail batch.
                    owner._catalog_geometry_rows = getattr(owner, '_item_rows', None)
                    if owner._catalog_geometry_rows is None:
                        owner._catalog_geometry_rows = {id(value): index
                                                        for index, value in enumerate(model.items)}
                index = owner._catalog_geometry_rows.get(id(item))
                if index is not None:
                    entry = changed_rows.setdefault(id(owner), [owner, model, model.items, set()])
                    entry[3].add(index)
            cache().changed.emit(item)
            wake = getattr(owner, 'wake_display_queue', None)
            if wake:
                wake()
        # A sparse batch can span almost the entire library. Sending min..max
        # incorrectly turns two changed cards into a 100k-row geometry scan.
        for owner, model, items, indices in changed_rows.values():
            if (is_deleted(owner) or owner.model() is not model
                    or model.items is not items):
                continue  # A synchronous preview callback replaced the model.
            self._notify_sparse_rows(owner, model, indices)
        self._trim_cache_to(self.thumbnail_budget)
        from prism.resource_budget import shared_cache_budget
        shared_cache_budget().changed(self)
        if self.pending:
            self.timer.start(0)

    @staticmethod
    def _notify_sparse_rows(owner, model, indices):
        """One batch hook, preserving exact rows and selection/model identity.

        AssetCanvas.catalog_rows_changed(rows) updates only these card records
        and schedules at most one coalesced geometry layout. Legacy owners
        receive separate contiguous ranges, never a fabricated enclosing span.
        """
        rows = tuple(sorted(set(indices)))
        if not rows:
            return
        notify = getattr(owner, 'catalog_rows_changed', None)
        if notify is not None:
            notify(rows)
            return
        first = last = rows[0]
        for row in rows[1:]:
            if row == last+1:
                last = row
                continue
            model.dataChanged.emit(model.index(first), model.index(last))
            first = last = row
        model.dataChanged.emit(model.index(first), model.index(last))


# ── AI-06: the query identity, the result cache and the publish contract ────
#
# Three contracts live here because they belong to the *query*, not to one
# widget and not to one owner:
#
# * `QueryKey`      - the stable identity of a query result.  It replaces the
#                     raw tuple that used to be built inline in the browser
#                     and sliced by index (`key[2:11]`, `key[11:13]`), which
#                     made every new field a silent wire change.
# * `CatalogResultCache` - the reuse layer.  It keeps *completed* results and
#                     *in-flight prefix checkpoints* apart, and it is bounded
#                     by **bytes**, not by a row count.
# * `ResultPublication` - what the browser tells the rest of the application
#                     after a model publication: which query produced which
#                     rows, how much work this attempt really did, and whether
#                     the result was reused.
#
# Ownership: AI-06 owns this file.  AI-03 consumes `ResultPublication` for
# the count contract, AI-07 consumes the query digest for the geometry key.
# They read; they do not write here.


#: Bumped whenever the meaning of a query key field changes.  A key built by
#: an older schema never compares equal to one built by a newer schema, so a
#: stale result can never be mistaken for a current one.
QUERY_KEY_SCHEMA_VERSION = 1

#: The default budget for *additional* retained results.
#:
#: This is a **byte** budget on purpose.  The previous layer bounded the
#: reuse cache by a row count (131072, later raised to 262144), which is not a
#: memory bound: one row is a proxy object with its own copy of the title,
#: filename, notes and tag list, so the same row count costs very different
#: amounts from project to project.  Raising the row count to make "all" and
#: "untagged" fit together hid the problem instead of solving it.
#:
#: The budget covers the *additional* retention only: the result the model
#: currently publishes is owned by the model, not by this cache, so it is
#: pinned and never evicted to satisfy the budget.  What the budget governs is
#: how many *other* finished results may be kept alive for instant reuse.
DEFAULT_RESULT_CACHE_BUDGET_BYTES = 256 * 1024 * 1024

#: How many finished results may be retained, whatever the byte budget says.
#: A bound on the number of entries keeps a pathological project from
#: filling the budget with many tiny results.
DEFAULT_RESULT_CACHE_MAX_ENTRIES = 8

#: How many in-flight prefix checkpoints may be kept.
DEFAULT_RESULT_CACHE_MAX_PENDING = 4


@dataclasses.dataclass(frozen=True)
class CatalogLayoutToken:
    """Publication identity without retaining rows or their proxy objects.

    The browser validates source/metadata/open versions and hashes stable row
    identities, in order, together with their geometry inputs. The canvas
    separately validates its geometry revision, grid, viewport and pack schema.
    """

    project: str
    open_epoch: str
    query_digest: str
    source_revision: str
    overlay_revision: str
    metadata_epoch: int
    ordered_geometry_digest: str
    row_count: int


@dataclasses.dataclass(frozen=True)
class QueryKey:
    """The stable identity of one query result.

    Everything that can change *which rows belong to the result* or *in which
    order they are published* is a field here.  The key is deliberately built
    from values, never from object addresses:

    * ``project``         - the project identity, not the file path alone.
    * ``source_revision`` - the logical revision of the source the directory
      describes (the catalog's own source state).  A file mtime is not a
      revision: two saves inside one second, or a restore that keeps the
      mtime, would both hide a change.
    * ``overlay_revision``- the dirty overlay revision of the live scene.
      Tag/rating/ownership edits that are not saved yet change the answer
      without touching the source.
    * ``metadata_epoch``  - bumped whenever the catalog metadata is rebuilt.
    * ``scope_pages``/``scope_all`` - the range the query is limited to.
    * ``mode``            - all / untagged / unclassified / recent / random /
      visual.
    * ``search``/``search_kind`` - the search text and the domain it searches.
    * ``min_rating``/``active_tags``/``saved_rule`` - the tag and rating rules.
    * ``random_seed``/``visual_revision`` - the two results whose content
      depends on something other than the data: a random shuffle and a
      visual search both produce a *different* result for the same rows.

    The three identities below answer three different questions, and the
    reuse layer needs all three:

    * :meth:`source_identity` - which directory rows the read must deliver.
      Two queries with the same source identity can share one physical read.
    * :meth:`match_identity` - which of those rows survive the filter.
      Same match identity => same candidate set, only the order may differ.
    * :meth:`result_identity` - the published order.
    """

    project: str = ''
    source_revision: str = ''
    overlay_revision: str = ''
    metadata_epoch: int = 0
    scope_pages: tuple = ()
    scope_all: bool = True
    mode: str = 'all'
    search: str = ''
    search_kind: str = 'all'
    min_rating: int = 0
    active_tags: tuple = ()
    saved_rule: str = ''
    sort_key: str = 'default'
    random_seed: object = None
    visual_revision: tuple = ()
    schema_version: int = QUERY_KEY_SCHEMA_VERSION

    def identity(self):
        """The full identity.  Every field that changes the result is here."""
        return (self.schema_version, self.project, self.source_revision,
                self.overlay_revision, self.metadata_epoch, self.scope_pages,
                self.scope_all, self.mode, self.search, self.search_kind,
                self.min_rating, self.active_tags, self.saved_rule,
                self.sort_key, self.random_seed, self.visual_revision)

    def source_identity(self):
        """Which directory rows the physical read must deliver."""
        return (self.schema_version, self.project, self.source_revision,
                self.overlay_revision, self.scope_pages, self.scope_all)

    def match_identity(self):
        """Which delivered rows survive the filter (the candidate set)."""
        return self.source_identity() + (
            self.mode, self.search, self.search_kind, self.min_rating,
            self.active_tags, self.saved_rule)

    def result_identity(self):
        """The published order of the candidate set."""
        return self.match_identity() + (self.sort_key, self.random_seed,
                                         self.visual_revision)

    def digest(self):
        """A short, stable hash of the full identity.

        The hash is taken over the key object itself, so a key and its
        digest always agree: ``digest() == fingerprint(key)``.  A consumer
        that keeps the key around can recompute the digest without knowing
        which fields the key is made of.
        """
        return fingerprint(self)

    def match_digest(self):
        return fingerprint(self.match_identity())


def estimate_result_bytes(items, sample=64):
    """Estimate the retained bytes of a result's item list.

    A full recursive ``sys.getsizeof`` walk over 100k proxies is itself a
    GUI stall, so the estimate samples the list and extrapolates.  The sample
    is deterministic (a fixed stride, not a random one), so two measurements
    of the same list are comparable.

    The estimate covers the object header, the instance dictionary, the
    owned extra dictionary and the tag list - the parts a directory proxy
    adds on top of a bare row.  It is an estimate, and it is reported as
    one.
    """
    if not items:
        return 0
    total = len(items)
    stride = max(1, total // max(1, int(sample)))
    probe = items[::stride][:max(1, int(sample))]
    if not probe:
        return 0
    per_item = 0.0
    for item in probe:
        size = sys.getsizeof(item)
        state = getattr(item, '__dict__', None)
        if isinstance(state, dict):
            size += sys.getsizeof(state)
            for value in state.values():
                size += sys.getsizeof(value)
        extra = getattr(item, '_extra_data', None)
        if isinstance(extra, dict):
            size += sys.getsizeof(extra)
            for value in extra.values():
                size += sys.getsizeof(value)
        names = getattr(item, '_tags', None)
        if isinstance(names, (list, tuple)):
            size += sys.getsizeof(names)
            for value in names:
                size += sys.getsizeof(str(value))
        per_item += size
    return int((per_item / len(probe)) * total)


class CatalogResultIds:
    """Read-only ordered ID view over the cache's own stable item list."""
    __slots__ = ('_items',)

    def __init__(self, items):
        self._items = items

    def __len__(self):
        return len(self._items)

    @staticmethod
    def _identifier(item):
        if isinstance(item, dict):
            return str(item.get('result_id') or '')
        value = getattr(item, 'save_id', None)
        if value is None:
            value = getattr(item, 'original_save_id', None)
        return '' if value is None else str(value)

    def __iter__(self):
        return (self._identifier(item) for item in self._items)

    def __getitem__(self, index):
        if isinstance(index, slice):
            return tuple(self._identifier(item) for item in self._items[index])
        return self._identifier(self._items[index])

    def __eq__(self, other):
        try:
            return len(self) == len(other) and all(a == b for a, b in zip(self, other))
        except TypeError:
            return NotImplemented


class CatalogResultCache:
    """The reuse layer: completed results and in-flight checkpoints, apart.

    Two stores, because the two things have different lifetimes:

    * ``pending``  - *prefix checkpoints*.  A query that was cancelled or
      superseded half way through has a partially filtered candidate list.
      That list is not a result: it must never be published as one, and it
      must never be counted as a finished query.
    * ``completed`` - *finished results*.  Each entry carries the published
      order as lightweight asset ids, the version information needed to
      prove it is still current, and - while the byte budget allows - the
      resident items themselves, so a hot switch republishes without
      rebuilding a single proxy.

    The budget is a **byte** budget over the completed entries.  The entry
    that the model currently publishes is pinned: its items are owned by the
    model, not by this cache, so evicting it here would free nothing.  What
    the budget governs is the *additional* retention.
    """

    def __init__(self, *, budget_bytes=DEFAULT_RESULT_CACHE_BUDGET_BYTES,
                 max_entries=DEFAULT_RESULT_CACHE_MAX_ENTRIES,
                 max_pending=DEFAULT_RESULT_CACHE_MAX_PENDING):
        self.budget_bytes = int(budget_bytes)
        self.max_entries = int(max_entries)
        self.max_pending = int(max_pending)
        self._completed = OrderedDict()   # digest -> entry
        self._pending = OrderedDict()     # digest -> checkpoint dict
        self._pinned = None
        self._lock = threading.RLock()
        self.hits = 0
        self.misses = 0
        self.evictions = 0
        self.released_bytes = 0
        self.invalidations = 0

    # ── completed results ──────────────────────────────────────────────

    def get(self, digest):
        """Return the finished entry for ``digest``, or ``None``."""
        with self._lock:
            entry = self._completed.get(digest)
            if entry is None:
                self.misses += 1
                return None
            self.hits += 1
            self._completed.move_to_end(digest)
            return entry

    def put(self, digest, entry):
        """Store a finished result and enforce the byte budget."""
        with self._lock:
            previous = self._completed.pop(digest, None)
            if previous is not None:
                self.released_bytes += int(previous.get('bytes') or 0)
            entry = dict(entry)
            entry.setdefault('bytes', 0)
            entry.setdefault('created', time.monotonic())
            entry.setdefault('items', None)
            entry.setdefault('ids', ())
            self._completed[digest] = entry
            self._completed.move_to_end(digest)
            self._enforce_budget()
            return entry

    def pin(self, digest):
        """Mark the digest the model currently publishes.

        The pinned entry is never evicted to satisfy the budget: its items
        belong to the model.  Everything else is the cache's own overhead
        and is what the budget actually governs.
        """
        with self._lock:
            self._pinned = digest
            if digest in self._completed:
                self._completed.move_to_end(digest)

    def _enforce_budget(self):
        with self._lock:
            while (self._overhead_bytes() > self.budget_bytes
                   or len(self._completed) > self.max_entries
                   or len(self._pending) > self.max_pending):
                # Checkpoints are optional resume accelerators, never active
                # installations. Drop their retained proxies before old results.
                if self._pending:
                    _digest, entry = self._pending.popitem(last=False)
                else:
                    victim = next((key for key in self._completed if key != self._pinned), None)
                    if victim is None:
                        return
                    entry = self._completed.pop(victim)
                self.released_bytes += int(entry.get('bytes') or 0)
                self.evictions += 1

    def _overhead_bytes(self):
        total = 0
        for digest, entry in self._completed.items():
            if digest == self._pinned:
                continue
            total += int(entry.get('bytes') or 0)
        total += sum(int(entry.get('bytes') or 0) for entry in self._pending.values())
        return total

    # ── in-flight checkpoints ──────────────────────────────────────────

    def put_pending(self, digest, checkpoint):
        """Store an **immutable snapshot** of an in-flight prefix.

        The snapshot is what makes a resume safe.  The live pending keeps
        growing while the install runs (``items`` gets appended to, ``offset``
        advances).  If the store kept a reference to the live list, a resume
        would append into the *stored* list while the stored ``offset`` stayed
        behind - and the next resume of the same query would reprocess that
        range and append it a second time, publishing the same logical asset
        twice.

        So the stored checkpoint owns copies: ``items`` becomes a tuple and
        ``overlay`` is copied.  A stored checkpoint can therefore never be
        changed by the install that produced it, by a resume, or by an
        unrelated later query.
        """
        with self._lock:
            from prism.catalog_install import checkpoint_install
            snapshot = checkpoint_install(checkpoint)
            # Sampling is an estimated retention policy, not measured RSS.
            estimate = estimate_result_bytes(snapshot['items'])
            estimate += sys.getsizeof(snapshot['items']) + sys.getsizeof(snapshot['overlay'])
            for values in (snapshot['overlay'].values(), snapshot.get('new') or ()):
                count = len(values)
                sample = tuple(itertools.islice(iter(values), 64))
                if sample:
                    estimate += int(estimate_result_bytes(sample) * count / len(sample))
            snapshot['bytes'] = estimate
            snapshot['bytes_basis'] = 'sampled_python_retention_estimate'
            self._pending[digest] = snapshot
            self._pending.move_to_end(digest)
            self._enforce_budget()

    def get_pending(self, digest):
        """Return the stored snapshot, or ``None``.

        The caller builds a **live** pending from it: the candidate list has
        to be a fresh mutable list, because the resume appends to it.
        """
        with self._lock:
            checkpoint = self._pending.get(digest)
            if checkpoint is not None:
                self._pending.move_to_end(digest)
            if checkpoint is None:
                return None
            from prism.catalog_install import checkpoint_install
            return checkpoint_install(checkpoint)

    def has_pending(self, digest):
        with self._lock:
            return digest in self._pending

    def pending_digests(self):
        with self._lock:
            return list(self._pending)

    def drop_pending(self, digest):
        with self._lock:
            return self._pending.pop(digest, None)

    def clear_pending(self, reason='cleared'):
        """Drop every in-flight prefix checkpoint.

        A checkpoint belongs to the row stream it was built from.  When the
        stream is replaced, the checkpoints are worthless - but they still
        hold the candidate list and the row object, so they must go.
        """
        with self._lock:
            count = len(self._pending)
            self._pending.clear()
            return count

    def pending_view(self):
        """The in-flight checkpoints, keyed by query digest.

        A read-only snapshot: it exists so a caller can ask "is there a
        checkpoint for this query?" without reaching into the store.
        """
        with self._lock:
            from prism.catalog_install import checkpoint_install
            return {key: checkpoint_install(value) for key, value in self._pending.items()}

    # ── invalidation ───────────────────────────────────────────────────

    def invalidate(self, reason, *, predicate=None):
        """Drop finished results that no longer describe the truth.

        ``predicate`` receives the entry and returns True when it must go.
        Returns the list of dropped digests, so the caller can report *why*
        the result was dropped instead of silently losing it.
        """
        with self._lock:
            dropped = []
            for digest in list(self._completed):
                entry = self._completed[digest]
                if predicate is None or predicate(entry):
                    self._completed.pop(digest, None)
                    self.released_bytes += int(entry.get('bytes') or 0)
                    dropped.append(digest)
            for digest in list(self._pending):
                if predicate is None or predicate(self._pending[digest]):
                    self._pending.pop(digest, None)
            if dropped:
                self.invalidations += len(dropped)
            return dropped

    def clear(self, reason='cleared'):
        with self._lock:
            released = sum(int(entry.get('bytes') or 0)
                           for entry in self._completed.values())
            self._completed.clear()
            self._pending.clear()
            self.released_bytes += released
            self.invalidations += 1
            return released

    def stats(self):
        with self._lock:
            return dict(entries=len(self._completed),
                        pending=len(self._pending),
                        pending_bytes_estimated=sum(int(e.get('bytes') or 0) for e in self._pending.values()),
                        bytes=sum(int(entry.get('bytes') or 0)
                                  for entry in self._completed.values()),
                        overhead_bytes=self._overhead_bytes(),
                        budget_bytes=self.budget_bytes,
                        overhead_within_budget=(self._overhead_bytes() <= self.budget_bytes),
                        pinned=self._pinned,
                        hits=self.hits, misses=self.misses,
                        evictions=self.evictions,
                        released_bytes=self.released_bytes,
                        invalidations=self.invalidations)


@dataclasses.dataclass(frozen=True)
class ResultPublication:
    """What one model publication really did.

    This is the contract the rest of the application reads.  It answers the
    questions the acceptance criteria ask for:

    * *which* query produced the rows (:attr:`query_digest` and the three
      identities),
    * *how much* work this attempt did (:attr:`evaluated_rows`,
      :attr:`proxies_created`, :attr:`read_rows`),
    * whether the result was *computed* or *reused*
      (:attr:`result_reused`, :attr:`read_reused`),
    * and whether the publication is final (:attr:`complete`).

    Every counter is **this attempt**, never a cumulative total: the
    acceptance criteria forbid passing a lifetime counter off as the work
    of one query.

    The **identity** fields (:attr:`publication_id`, :attr:`query_generation`,
    :attr:`publication_seq`, :attr:`open_epoch`, :attr:`session_id`) are the
    business identity of the publication, not the log's identity:

    * a new query request registers a new :attr:`query_generation` when it is
      created, so an older request that finishes later can be recognised as
      older and must not overwrite the newer one;
    * :attr:`publication_seq` is monotonic **inside** one generation;
    * :attr:`publication_id` is the deterministic composite of the two plus
      session, project and open epoch.  The **same value** goes to the
      synchronous observer and to the trace event, so a consumer can
      deduplicate a redelivery;
    * deduplication by ``query_digest`` alone is **not** enough: the same
      query can be published again (``all -> untagged -> all``), and the
      digest repeats across generations.
    """

    query_digest: str = ''
    match_digest: str = ''
    source_digest: str = ''
    project: str = ''
    source_revision: str = ''
    overlay_revision: str = ''
    mode: str = ''
    published_rows: int = 0
    evaluated_rows: int = 0
    proxies_created: int = 0
    read_rows: int = 0
    candidate_rows: int = 0
    result_reused: bool = False
    read_reused: bool = False
    pushdown_filters: tuple = ()
    complete: bool = False
    final: bool = False
    reason: str = ''
    #: The **business** identity of this publication.
    #:
    #: ``publication_id`` is a deterministic composite of session, project,
    #: open epoch, query generation and the sequence inside that generation.
    #: It is not a timestamp and not a trace sequence: the wall clock does not
    #: order publications, and a trace sequence belongs to the log, not to the
    #: product.  The observer and the trace event carry the same value, so a
    #: consumer can deduplicate a redelivery.
    publication_id: str = ''
    #: The generation of the query request this publication answers.  A new
    #: request registers a new generation when it is created; an older request
    #: that finishes later may not overwrite a newer one.
    query_generation: int = 0
    #: Monotonic within one query generation.
    publication_seq: int = 0
    #: The open epoch of the project this publication belongs to.  The same
    #: project reopened is a different epoch, and an old callback must not be
    #: applied to it.
    open_epoch: str = ''
    #: The session this publication was produced in.
    session_id: str = ''

    def fields(self):
        data = dataclasses.asdict(self)
        data['published_rows_unit'] = 'model_rows'
        data['evaluated_rows_unit'] = 'candidate_rows'
        data['evaluated_rows_cumulative'] = False
        data['proxies_created_unit'] = 'proxies'
        data['proxies_created_cumulative'] = False
        data['read_rows_unit'] = 'catalog_rows'
        data['read_rows_cumulative'] = False
        data['counter_basis'] = 'this_publication_attempt'
        return data


_RESULT_PUBLICATION_OBSERVERS = []
_RESULT_PUBLICATION_LOCK = threading.Lock()


def add_result_publication_observer(callback):
    """Register ``callback(event, **fields)`` for every publication."""
    with _RESULT_PUBLICATION_LOCK:
        if callback not in _RESULT_PUBLICATION_OBSERVERS:
            _RESULT_PUBLICATION_OBSERVERS.append(callback)


def remove_result_publication_observer(callback):
    with _RESULT_PUBLICATION_LOCK:
        try:
            _RESULT_PUBLICATION_OBSERVERS.remove(callback)
        except ValueError:
            pass


def publish_result(publication):
    """Announce one publication to the observers and to the trace.

    The trace event is ``catalog_result_published``.  It is a **new** event
    name: the analyzer that ships today does not know it, so the numbers in
    it are readable from the raw log but are not counted by the current
    analyzer.  That is stated in the delivery report rather than silently
    assumed to be part of the existing accounting.
    """
    fields = publication.fields()
    with _RESULT_PUBLICATION_LOCK:
        callbacks = list(_RESULT_PUBLICATION_OBSERVERS)
    for callback in callbacks:
        try:
            callback('catalog_result_published', **dict(fields))
        except Exception:
            pass
    try:
        module = sys.modules.get('prism.performance_trace')
        if module is not None:
            module.record('catalog_result_published', priority='terminal',
                          **dict(fields))
    except Exception:
        pass
    return fields
