"""Shared accounting/eviction for rebuildable resident caches, never originals.

Callbacks execute only on their registering thread. This first integration is
for GUI-owned CPU caches; disk previews, GPU allocations and dirty records are
separate resources and must not be misreported as covered by this limit.

**Project scope.** A registration may name the project it belongs to. The byte
limit stays process wide, but a project that is closed must not keep charging
the next project's budget: :meth:`CacheBudget.activate_scope` releases the
scope being left. A scope key is a *string* -- the resolved project path --
never an object address, so it outlives the session that created it and can be
handed to a worker thread.

**What this limit covers.** Only the rebuildable CPU caches that report their
own size through the registered ``usage`` callable:

* ``image_display`` -- decoded display pixmaps and overviews,
* ``video_preview`` -- decoded opening frames of videos,
* ``material_encoded`` -- encoded material textures,
* ``catalog_thumbnail`` -- catalog browser thumbnails,
* ``canvas_composites`` -- composited canvas layers.

**What it does not cover.** GPU/driver allocations (they are not Python objects
with a byte count), the original bytes stored inside a ``.prism`` archive
(owned by the file), disk sidecars such as the thumbnail cache directory, the
editable scene with its dirty records and undo stack, and process memory held
outside those caches (Qt, Python, plugins). A bounded budget here therefore
means "the rebuildable caches stay under a byte ceiling", never "process
memory is bounded".

**What the rollup reports besides them.** The resource rollup also states the
resident memory that no cache claims (``resident_unaccounted_bytes``: the
runtime, the browser side of WebEngine and anything that is not in a
rebuildable cache -- not a leak verdict, and not a number to add up), the
bytes the project leases hold on disk (``temp_disk``) and the memory of the
child processes (``webengine_children``). Both of the latter are read from
counters: the lease sizes are recorded when the lease is taken, and the
process enumeration runs in a background thread because it costs tens of
milliseconds.
"""
import logging
import os
import threading
import time
import weakref
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)


#: Scope key of a cache that belongs to no single project.
GLOBAL_SCOPE = None


#: Memo for `project_scope_key`.
#:
#: `Path.resolve()` walks the filesystem: it resolves symlinks, drives and
#: relative segments, so it costs a round of metadata calls every time. The
#: decode path asks for the scope key once per image, which on a 100 000
#: item project meant 100 000 filesystem round trips -- measured at 301 us
#: each, so ~30 s of pure overhead, and the GUI stalled on it 57 times.
#:
#: The key is derived from the path string only, never from an object
#: address, so the answer for one path cannot change within a session and a
#: bounded cache cannot go stale. `None` is not cached and `functools.lru_cache`
#: cannot take it, so the early return stays ahead of the decorator.
_SCOPE_KEY_CACHE = {}
_SCOPE_KEY_CACHE_LIMIT = 64


def project_scope_key(path):
    """A stable scope key for a project file.

    The key is the resolved path as a *string*: two sessions that open the
    same file share one key, and the key stays meaningful after the session
    object is gone. It is never derived from an object address.
    """
    if not path:
        return None
    try:
        cached = _SCOPE_KEY_CACHE.get(path)
    except TypeError:
        # An unhashable path-like cannot be cached; resolve it directly.
        cached = None
        cacheable = False
    else:
        cacheable = True
    if cached is not None:
        return cached
    try:
        resolved = str(Path(path).resolve())
    except (OSError, ValueError):
        resolved = str(path)
    if cacheable:
        if len(_SCOPE_KEY_CACHE) >= _SCOPE_KEY_CACHE_LIMIT:
            _SCOPE_KEY_CACHE.clear()
        _SCOPE_KEY_CACHE[path] = resolved
    return resolved


class ResourceLease:
    """One claim on a temporary source, a half-written file or a decode result.

    A lease names its owner (the project scope), what it is (the ``kind``) and
    which concrete resource it is (the ``resource_id``), so the release point
    is explicit instead of "whatever the object address used to be".

    A resource may be leased by several owners. The release callback runs only
    for the last owner, so one project closing never deletes a file another
    project is still using.
    """

    def __init__(self, scope, kind, resource_id, release=None, size_bytes=None):
        self.scope = scope
        self.kind = str(kind)
        self.resource_id = str(resource_id)
        self._release = release
        self._released = False
        self._lock = threading.Lock()
        #: The bytes this lease holds on disk, when the writer knew them.
        #: ``None`` means "not known" -- never "zero".
        self.size_bytes = None if size_bytes is None else max(0, int(size_bytes))

    @property
    def released(self):
        return self._released

    def release(self):
        """Release the resource once. Later calls do nothing."""
        with self._lock:
            if self._released:
                return False
            self._released = True
        _forget_temp_lease(self.kind, self.resource_id)
        callback = self._release
        if callback is not None:
            try:
                if _release_holder(self.scope.key if self.scope is not None else '',
                                   self.resource_id):
                    callback(self)
            except Exception:
                logger.warning('Could not release %s lease %s',
                               self.kind, self.resource_id, exc_info=True)
        return True


#: resource id -> {scope key: number of leases}. A resource is only deleted
#: when its last owner releases it.
_holders = {}
_holders_lock = threading.Lock()


def _take_holder(scope_key, resource_id):
    with _holders_lock:
        holders = _holders.setdefault(str(resource_id), {})
        holders[scope_key] = holders.get(scope_key, 0)+1


def _release_holder(scope_key, resource_id):
    """True when the last owner let go of the resource."""
    with _holders_lock:
        holders = _holders.get(str(resource_id))
        if not holders:
            return True
        remaining = holders.get(scope_key, 0)-1
        if remaining > 0:
            holders[scope_key] = remaining
            return False
        holders.pop(scope_key, None)
        if not holders:
            _holders.pop(str(resource_id), None)
            return True
        return False


#: The live temporary-file leases, by kind and resource id.
#:
#: The size is recorded when the lease is taken -- the writer knows it --
#: so the rollup reads a counter and never walks the disk.  A resource
#: leased by several owners is counted once and dropped when the last owner
#: lets go, exactly like the holder count above.
_temp_leases = {}
_temp_lease_counts = {}
_temp_lock = threading.Lock()


def _record_temp_lease(kind, resource_id, size_bytes):
    """Remember one live temporary-file lease and the bytes it holds."""
    kind = str(kind)
    resource_id = str(resource_id)
    with _temp_lock:
        key = (kind, resource_id)
        entry = _temp_leases.get(key)
        if entry is None:
            _temp_leases[key] = [size_bytes, 1]
        else:
            entry[1] += 1
            if entry[0] is None and size_bytes is not None:
                entry[0] = size_bytes
        _temp_lease_counts[kind] = _temp_lease_counts.get(kind, 0) + 1


def _forget_temp_lease(kind, resource_id):
    """Forget one released temporary-file lease and the bytes it held."""
    kind = str(kind)
    resource_id = str(resource_id)
    with _temp_lock:
        entry = _temp_leases.get((kind, resource_id))
        if entry is not None:
            entry[1] -= 1
            if entry[1] <= 0:
                _temp_leases.pop((kind, resource_id), None)
        count = _temp_lease_counts.get(kind, 0) - 1
        if count > 0:
            _temp_lease_counts[kind] = count
        else:
            _temp_lease_counts.pop(kind, None)


def temp_disk_usage():
    """The live temporary-file bytes the project leases hold.

    The sizes come from the writers, so this is a counter read: no file is
    stat-ed and no directory is scanned while the rollup is written.
    """
    with _temp_lock:
        entries = tuple((kind, size, holders)
                        for (kind, _rid), (size, holders) in _temp_leases.items())
        counts = dict(_temp_lease_counts)
    total = 0
    by_kind = {}
    without_size = 0
    for kind, size, holders in entries:
        if size is None:
            without_size += holders
            continue
        total += size
        by_kind[kind] = by_kind.get(kind, 0) + size
    return {'bytes': total, 'bytes_by_kind': by_kind,
            'lease_count': sum(counts.values()), 'leases_by_kind': counts,
            'leases_without_size': without_size}


class ProjectScope:
    """The resources of one open project.

    A scope owns three things: a cancellation token for the project's
    background work, the leases on its temporary files, and its share of the
    shared byte budget. The scope is identified by a string key, so it can be
    handed to a worker thread and outlive the objects that created it.
    """

    def __init__(self, key, *, label=None):
        self.key = str(key)
        self.label = label or self.key
        self.cancelled = threading.Event()
        self._leases = {}
        self._counter = 0
        self._lock = threading.RLock()

    # ── cancellation ────────────────────────────────────────────

    def cancel(self):
        """Ask the project's background work to stop."""
        self.cancelled.set()

    def is_cancelled(self):
        return self.cancelled.is_set()

    # ── leases ──────────────────────────────────────────────────

    def lease(self, kind, resource_id, release=None, size_bytes=None):
        """Take a lease on one temporary or intermediate resource.

        The lease is owned by this scope and is released when the scope is
        released, or earlier by an explicit :meth:`ResourceLease.release`.

        ``size_bytes`` is how many bytes the resource holds on disk.  The
        writer knows it, so it is recorded here instead of being measured
        later: the rollup then reads a counter and never walks the disk.
        """
        with self._lock:
            self._counter += 1
            lease = ResourceLease(self, kind, resource_id, release, size_bytes)
            self._leases[(str(kind), str(resource_id), self._counter)] = lease
        _take_holder(self.key, resource_id)
        _record_temp_lease(lease.kind, lease.resource_id, lease.size_bytes)
        return lease

    def leases(self, kind=None):
        """The leases this scope still holds, optionally of one kind only."""
        with self._lock:
            values = tuple(self._leases.values())
        if kind is None:
            return values
        return tuple(lease for lease in values if lease.kind == str(kind))

    def release_leases(self, kind=None):
        """Release the leases this scope holds; returns how many were released."""
        with self._lock:
            leases = tuple(self._leases.values())
            self._leases.clear()
        released = 0
        for lease in leases:
            if kind is not None and lease.kind != str(kind):
                continue
            if lease.release():
                released += 1
        return released

    def __repr__(self):
        return f'<ProjectScope {self.key!r} leases={len(self._leases)}>'


_scopes = {}
_scopes_lock = threading.Lock()
_current_scope = None


def register_scope(scope):
    """Remember a scope so workers can find it by its key."""
    with _scopes_lock:
        _scopes[scope.key] = scope
    return scope


def scope_for(key):
    """The registered scope for a project key, or ``None``."""
    if not key:
        return None
    with _scopes_lock:
        return _scopes.get(project_scope_key(key))


def current_project_scope():
    """The scope of the project that is open right now, or ``None``."""
    with _scopes_lock:
        return _current_scope


def set_current_project_scope(scope):
    """Install the scope of the project that is open right now."""
    global _current_scope
    with _scopes_lock:
        previous = _current_scope
        _current_scope = scope
    return previous


@dataclass
class _Cache:
    owner: object
    category: str
    usage: str
    trim: str
    thread: int
    priority: int
    touched: int
    scope: str = None
    #: True for a cache that belongs to no single project: it holds the bytes
    #: of whichever project is open, so it follows the active scope.
    process_wide: bool = False


def _webengine_children_payload(sample):
    """The measured child-process bytes, or ``None`` when there is no sample.

    The sample is taken by a background thread: the enumeration costs tens
    of milliseconds and the thread that writes the rollup must never pay
    for it.
    """
    try:
        from prism.performance_trace import resource_accounting, unknown_resource
    except Exception:  # pragma: no cover - the trace module is optional
        return None
    if sample is None:
        return None
    if sample.get('bytes') is None:
        return unknown_resource(
            'webengine_children',
            'the background enumeration failed: %s'
            % (sample.get('error') or 'unknown error'))
    accounting = resource_accounting('webengine_children',
                                     owner='prism.performance_trace',
                                     budget=None, unit='bytes', source='measured')
    accounting.observe(sample['bytes'], source='measured')
    payload = dict(accounting.snapshot())
    payload.update(
        child_count=sample.get('count'),
        children=sample.get('children') or [],
        children_truncated=bool(sample.get('children_truncated')),
        children_unreadable=sample.get('children_unreadable'),
        sampled_at=sample.get('sampled_at'),
        sample_age_seconds=round(max(0.0, time.perf_counter() -
                                      sample.get('monotonic', 0.0)), 3),
        sample_source=sample.get('source'),
        enumeration_ms=sample.get('duration_ms'),
        sample_thread=sample.get('thread'),
        scope='the direct child processes of this process',
        note='the working set of every direct child process; the WebEngine '
             'renderers are among them.  The enumeration runs in a background '
             'thread, so the age says how fresh the number is.')
    return payload


def _temp_disk_payload():
    """The live temporary-file bytes, or ``None`` when they cannot be stated.

    The sizes are recorded when the lease is taken, so this is a counter
    read and never a walk of the disk.
    """
    try:
        from prism.performance_trace import resource_accounting
    except Exception:  # pragma: no cover - the trace module is optional
        return None
    usage = temp_disk_usage()
    if usage['lease_count'] and not usage['bytes_by_kind']:
        # Every live lease carries no byte count: the total is unknown, and
        # an unknown is never a zero.
        return None
    accounting = resource_accounting('temp_disk', owner='prism.resource_budget',
                                     budget=None, unit='bytes', source='measured')
    accounting.observe(usage['bytes'], source='measured')
    payload = dict(accounting.snapshot())
    payload.update(
        bytes_by_kind=usage['bytes_by_kind'],
        lease_count=usage['lease_count'],
        leases_by_kind=usage['leases_by_kind'],
        leases_without_size=usage['leases_without_size'],
        bytes_complete=not usage['leases_without_size'],
        note='the temporary files the project leases hold; the writers record '
             'the size when they take the lease, so no file is stat-ed here')
    return payload


class CacheBudget:
    #: The cache classes the rollup reports.
    #:
    #: ``categories`` are the :meth:`snapshot` categories that hold the bytes,
    #: ``owner`` names the module that owns them. Every class carries the
    #: *shared* cache limit as its budget: it bounds the rebuildable CPU
    #: caches, never the process RSS.  The classes outside this limit are in
    #: :attr:`UNMEASURED_CLASSES`.
    RESOURCE_CLASSES = (
        ('cpu_decoded', ('image_display',), 'prism.lod'),
        ('encoded_thumbnail', ('catalog_thumbnail',), 'prism.catalog_browser'),
        ('tile_composite', ('canvas_tiles', 'canvas_composites'),
         'prism.canvas_tiles/canvas_composites'),
        ('material_texture', ('material_encoded',), 'prism.material_cache'),
        ('video_preview', ('video_preview',), 'prism.video_thumbnails'),
        ('editor_attachments', ('attachment_bytes',),
         'prism.fileio.attachments_store'),
    )

    #: The classes the shared cache limit does not own, with the status to
    #: report for each of them.
    #:
    #: ``status`` is ``unknown`` (this machine cannot provide the value) or
    #: ``not_accounted`` (no counter is exposed).  A class that does have a
    #: live counter reports it and falls back to this status only when the
    #: counter has no value yet -- never to a zero, which would read like
    #: "nothing is used".
    #:
    #: * ``gpu`` -- no GPU memory source exists in this build, so it is
    #:   always unknown.
    #: * ``webengine_children`` -- measured from a background sample of the
    #:   child processes; unknown until the first sample exists.
    #: * ``temp_disk`` -- measured from the live temporary-file leases;
    #:   not_accounted when a live lease carries no byte count.
    #: * ``dirty_records`` -- the partial project session keeps a diff base,
    #:   not a dirty set, so no counter is exposed.
    UNMEASURED_CLASSES = (
        ('gpu', 'unknown',
         'no GPU memory source is reachable from this build: the process '
         'memory counter it reads (GetProcessMemoryInfo) carries no GPU '
         'field, the build binds no per-adapter GPU counter (the only CUDA '
         'references pick a compute device and report no memory), so the '
         'value stays unknown instead of being reported as zero'),
        ('webengine_children', 'unknown',
         'no child-process sample yet: the enumeration costs about 20 ms, '
         'so it runs in a background thread and the GUI thread never pays '
         'for it'),
        ('temp_disk', 'not_accounted',
         'the live temporary-file leases carry no byte count, so the total '
         'on disk is unknown'),
        ('dirty_records', 'not_accounted',
         'the partial project session keeps a diff base, not a dirty set: '
         'mark_page_modified (prism/partial_project.py:505) only bumps a '
         'version on the live page, and the dirty rows are derived when a '
         'save compares the live scene with the base '
         '(prism/partial_project.py:726 capture, :947 _item_patch), so no '
         'count can be read without doing that work'),
    )

    def __init__(self, limit=256 * 1024 * 1024):
        if limit < 0:
            raise ValueError('Cache limit must be nonnegative')
        self.limit = int(limit)
        self._lock = threading.RLock()
        self._caches = {}
        self._enforcing = set()
        self._tick = 0
        self._active_scope = None
        self._last_record = 0.0
        # The independent over-budget counting point. It does not depend on a
        # successful reclaim, so an excess that could not be freed (pinned
        # detail, another thread's cache) is still visible.
        self.over_budget_events = 0
        self.last_over_budget = None
        self._last_over_budget_record = None
        #: Bytes the last enforcement actually freed.
        self.last_reclaimed = 0
        #: The highest byte count ever seen per category (additive).
        self._peak_bytes = {}
        #: The bytes reclaimed per category over the life of this budget.
        self._reclaimed_bytes = {}
        #: The bytes reclaimed over the life of this budget, all categories.
        self.total_reclaimed = 0
        #: When the resource rollup was written last (it has its own
        #: throttle, so it cannot silence the snapshot or the other way round).
        self._last_resource_record = 0.0

    def register(self, owner, *, category, usage='bytes', trim='_trim_cache_to',
                priority=50, scope=None):
        """Register method names, not bound callbacks retaining entire windows.

        ``scope`` names the project the cache belongs to (``None`` for a
        process-wide cache). The scope is a string key, never an object
        address, so it stays valid after the owning session is gone.
        """
        key = id(owner)
        manager = weakref.ref(self)

        def gone(reference):
            current = manager()
            if current is not None:
                current._forget(key, reference)

        reference = weakref.ref(owner, gone)
        record = _Cache(reference, str(category), usage, trim,
                        threading.get_ident(), int(priority), 0,
                        scope=scope if scope is None else project_scope_key(scope),
                        process_wide=scope is None)
        with self._lock:
            self._tick += 1
            record.touched = self._tick
            self._caches[key] = record
        # Do not attach Python lambdas to QObject.destroyed: cyclic Python/Qt
        # GC can invoke them during wrapper destruction and crash on Windows.
        # Weakref callbacks only edit Python metadata; stale C++ wrappers are
        # pruned lazily during ordinary accounting, outside destruction.

    def _forget(self, key, reference):
        with self._lock:
            entry = self._caches.get(key)
            if entry is not None and entry.owner is reference:
                self._caches.pop(key, None)

    @staticmethod
    def _size(entry, owner):
        value = getattr(owner, entry.usage)
        return max(0, int(value() if callable(value) else value))

    def touch(self, owner):
        with self._lock:
            entry = self._caches.get(id(owner))
            if entry is not None and entry.owner() is owner:
                self._tick += 1
                entry.touched = self._tick

    def changed(self, owner):
        self.touch(owner)
        return self.enforce()

    def _resident(self):
        from PyQt6 import QtCore, sip
        result = []
        for key, entry in tuple(self._caches.items()):
            owner = entry.owner()
            if owner is None or (isinstance(owner, QtCore.QObject) and sip.isdeleted(owner)):
                self._caches.pop(key, None)
                continue
            result.append((key, entry, owner, self._size(entry, owner)))
        return result

    def _charges(self, entry):
        """True when this cache counts against the active project's budget."""
        if entry.scope is None:
            return True  # process-wide cache
        return self._active_scope is None or entry.scope == self._active_scope

    def enforce(self):
        """Trim low-priority/older caches, exposing pressure if pinned/foreign.

        A cache may keep visible/pinned resources. The snapshot reports any
        remaining excess instead of lying about total memory being bounded.

        Only the active project scope and the process-wide caches are
        charged: a cache that belongs to another project is reported as
        foreign bytes instead of taking this project's budget away.
        """
        thread = threading.get_ident()
        with self._lock:
            if thread in self._enforcing:
                return 0
            self._enforcing.add(thread)
            try:
                entries = self._resident()
                charged = [row for row in entries if self._charges(row[1])]
                total = sum(size for _, _, _, size in charged)
                reclaimed = 0
                reclaimed_by_category = {}
                for _key, entry, owner, _size in sorted(
                        charged, key=lambda row: (row[1].priority, row[1].touched)):
                    if total <= self.limit:
                        break
                    if entry.thread != thread:
                        continue  # never evict GUI pixmaps from a worker thread
                    before = self._size(entry, owner)
                    target = max(0, before - (total-self.limit))
                    getattr(owner, entry.trim)(target)
                    after = self._size(entry, owner)
                    released = max(0, before-after)
                    total -= released
                    reclaimed += released
                    reclaimed_by_category[entry.category] = (
                        reclaimed_by_category.get(entry.category, 0) + released)
                if reclaimed:
                    self._record_snapshot('enforce')
                # An excess that could not be freed is still an excess. It is
                # counted on its own, not only when a reclaim succeeded, so
                # "pinned over budget" can never be read as "no over budget".
                if total > self.limit:
                    self._record_over_budget(reclaimed)
                self.last_reclaimed = reclaimed
                # Cumulative counters: what was evicted, per category and in
                # total. They are only added to; nothing existing changes.
                self.total_reclaimed += reclaimed
                for category, size in reclaimed_by_category.items():
                    self._reclaimed_bytes[category] = (
                        self._reclaimed_bytes.get(category, 0) + size)
                return reclaimed
            finally:
                self._enforcing.remove(thread)

    # ── project scope ───────────────────────────────────────────

    @property
    def active_scope(self):
        """The project key the budget currently charges."""
        return self._active_scope

    def activate_scope(self, key, *, release_previous=True):
        """Switch the shared budget to another project scope.

        The scope being left is released, so the next project starts with the
        whole limit instead of inheriting the old project's bytes. The
        process-wide singletons (decoded pixmaps, material textures) hold the
        bytes of the project being left, so they are released with it and
        re-attributed to the project that is being opened.

        Returns the number of bytes that were actually freed.
        """
        key = project_scope_key(key)
        with self._lock:
            previous = self._active_scope
            self._active_scope = key
        released = 0
        if release_previous and previous is not None and previous != key:
            released += self.release_scope(previous)
            released += self.release_globals()
        with self._lock:
            for entry in self._caches.values():
                # A process-wide cache holds the bytes of the project that is
                # open, so it follows the active scope. Re-attributing it by
                # its original kind keeps the next project charged for its
                # own bytes, instead of leaving them on the old scope where
                # they would count as foreign and never be reclaimed.
                if entry.process_wide:
                    entry.scope = key
        self._record_snapshot('activate_scope', force=True)
        return released

    def release_scope(self, key):
        """Trim every cache that belongs to ``key``.

        Only the thread that registered a cache may trim it: a GUI pixmap
        cache must not be touched from a worker thread. A cache that cannot be
        trimmed here stays registered and stays reported, so the accounting
        never claims bytes it did not free.
        """
        key = project_scope_key(key)
        if key is None:
            return 0
        thread = threading.get_ident()
        released = 0
        with self._lock:
            for _cache_key, entry, owner, _size in self._resident():
                if entry.scope != key or entry.thread != thread:
                    continue
                before = self._size(entry, owner)
                getattr(owner, entry.trim)(0)
                after = self._size(entry, owner)
                released += max(0, before-after)
        if released:
            self._record_snapshot('release_scope', force=True)
        return released

    def release_globals(self):
        """Trim the process-wide caches that hold the previous project's bytes."""
        thread = threading.get_ident()
        released = 0
        with self._lock:
            for _cache_key, entry, owner, _size in self._resident():
                # A process-wide cache follows the active scope, so it is
                # trimmed with its project; it is trimmed here when it was
                # not attributed to one (no project is open).
                if not entry.process_wide or entry.thread != thread:
                    continue
                before = self._size(entry, owner)
                getattr(owner, entry.trim)(0)
                after = self._size(entry, owner)
                released += max(0, before-after)
        return released

    # ── observability ───────────────────────────────────────────

    def snapshot(self):
        with self._lock:
            categories = {}
            scopes = {}
            total = foreign = 0
            for _key, entry, _owner, size in self._resident():
                categories[entry.category] = categories.get(entry.category, 0)+size
                scope = entry.scope or ''
                scopes[scope] = scopes.get(scope, 0)+size
                if self._charges(entry):
                    total += size
                else:
                    foreign += size
            # The peak of each category: the highest byte count it ever held.
            for category, size in categories.items():
                if size > self._peak_bytes.get(category, 0):
                    self._peak_bytes[category] = size
            return dict(limit=self.limit, bytes=total, over_budget=max(0, total-self.limit),
                        categories=categories, caches=len(self._caches),
                        active_scope=self._active_scope, scopes=scopes,
                        foreign_bytes=foreign, resident_bytes=total+foreign,
                        reclaimed=self.last_reclaimed,
                        over_budget_events=self.over_budget_events,
                        peak_bytes=dict(self._peak_bytes),
                        total_reclaimed=self.total_reclaimed)

    def _record_over_budget(self, reclaimed=0):
        """Count one over-budget event, independently of the reclaim path.

        :meth:`enforce` does not always free anything: a visible detail is
        pinned, or the cache belongs to another thread and must not be
        touched from here. The excess is then invisible, because
        ``reclaimed`` is 0 and the snapshot is only written when something
        was reclaimed. This counting point does not depend on that, so an
        excess that stays is visible in the operation log, in
        ``over_budget_events`` and in the snapshot.

        The numbers are taken from :meth:`snapshot`, so the event, the
        snapshot and ``snapshot()['over_budget']`` always agree.
        """
        snapshot = self.snapshot()
        over_budget = snapshot['over_budget']
        if over_budget <= 0:
            return 0
        self.over_budget_events += 1
        report = dict(bytes=snapshot['bytes'], over_budget=over_budget,
                      limit=snapshot['limit'], reclaimed=int(reclaimed),
                      active_scope=snapshot['active_scope'],
                      over_budget_events=self.over_budget_events)
        self.last_over_budget = report
        self._write_over_budget(report)
        return over_budget

    def _write_over_budget(self, report):
        """Write one over-budget event to the operation log (throttled).

        The counter and ``last_over_budget`` are updated on every event; only
        the log line is throttled, so a session that stays over budget does
        not flood the trace queue.
        """
        now = time.monotonic()
        last = self._last_over_budget_record
        if last is not None and now-last < 1.0:
            return
        self._last_over_budget_record = now
        try:
            from prism.performance_trace import record
        except Exception:  # pragma: no cover - the trace module is optional
            record = None
        if record is not None:
            record('cache_budget_over_budget', **report)
        # The snapshot is what a reader of the operation log normally looks
        # at; without it the excess would only exist in this one event.
        self._record_snapshot('over_budget', force=True)

    def record_snapshot(self, reason='manual'):
        """Write the budget breakdown to the operation log.

        The snapshot is the only place that says how many bytes each class of
        rebuildable cache holds, so it is recorded whenever the numbers move
        (throttled, because caches change often) and on demand.
        """
        self._record_snapshot(reason, force=True)

    def _pinned_excess(self, snapshot):
        """The part of the excess that could not be reclaimed.

        The over-budget report says how much was over the limit when the
        enforcement gave up and how much it freed before that. The excess it
        faced was ``over_budget + reclaimed``; the part it could not free is
        that excess minus what it did free -- which is the excess that is
        still there (a pinned detail, another thread's cache). It stays
        visible instead of being hidden behind a successful reclaim.
        """
        report = self.last_over_budget
        over = snapshot['over_budget']
        if not report or over <= 0:
            return 0
        excess = int(report['over_budget']) + int(report['reclaimed'])
        pinned = max(0, excess - int(report['reclaimed']))
        return min(pinned, over)

    def record_resource_rollup(self, reason='manual', *, force=False,
                               snapshot=None):
        """Write the resource rollup: what each class of rebuildable bytes holds.

        The bytes come from the same snapshot as ``cache_budget_snapshot``, so
        the two never disagree, no cache is walked a second time and every
        byte is counted once. The rollup is throttled to about one line per
        second; ``force=True`` writes it right now.

        A class nobody measures is reported as ``not_accounted`` (or
        ``unknown`` for a value this machine cannot provide), never as a zero:
        a zero reads like "nothing is used".  A class outside the shared
        cache limit that does have a live counter is reported with that
        counter's number; the fallback above is what it gets when the
        counter has no value yet.
        """
        now = time.monotonic()
        with self._lock:
            if not force and now-self._last_resource_record < 1.0:
                return None
            self._last_resource_record = now
            if snapshot is None:
                snapshot = self.snapshot()
        try:
            from prism.performance_trace import (child_process_sample, not_accounted,
                                                 resource_accounting, unknown_resource)
            from prism.performance_trace import record_resource_rollup as emit
        except Exception:  # pragma: no cover - the trace module is optional
            return None
        limit = snapshot['limit']
        pin = self._pinned_excess(snapshot)
        classes = []
        for name, categories, owner in self.RESOURCE_CLASSES:
            current = sum(snapshot['categories'].get(category, 0)
                          for category in categories)
            evicted = sum(self._reclaimed_bytes.get(category, 0)
                          for category in categories)
            accounting = resource_accounting(name, owner=owner, budget=limit,
                                             unit='bytes', source='measured')
            accounting.observe(current, evicted=evicted, pin_over_budget=pin,
                               budget=limit)
            payload = dict(accounting.snapshot())
            payload.update(
                budget_kind='shared_cache_limit',
                budget_is_rss_cap=False,
                budget_note='shared ceiling for the rebuildable CPU caches; '
                            'not a process RSS cap',
                categories=list(categories),
                pin_over_budget_scope='shared_budget')
            classes.append((name, payload))
        # The child-process sample is read from the cache.  The enumeration
        # itself never runs on the thread that writes this rollup.
        sample = child_process_sample()
        for name, kind, why in self.UNMEASURED_CLASSES:
            payload = self._live_class_payload(name, sample)
            if payload is None:
                payload = (unknown_resource(name, why) if kind == 'unknown'
                           else not_accounted(name, why))
            classes.append((name, payload))
        # The resident memory that no cache claims.  Both terms come from
        # what this rollup already has: the cache bytes of this snapshot
        # and the process RSS of the background sample.  No extra sampling
        # is done here.
        cache_bytes = snapshot['resident_bytes']
        rss = sample.get('rss_bytes') if sample else None
        resident_unaccounted = (None if rss is None
                                else max(0, int(rss) - int(cache_bytes)))
        emit(reason=reason, classes=classes, limit=limit, bytes=snapshot['bytes'],
             over_budget=snapshot['over_budget'], pin_over_budget=pin,
             resident_bytes=snapshot['resident_bytes'],
             resident_bytes_note='the bytes the measured cache classes hold; '
                                 'the resident memory of the process itself is '
                                 'process_rss_bytes',
             foreign_bytes=snapshot['foreign_bytes'],
             reclaimed=snapshot['reclaimed'],
             total_reclaimed=self.total_reclaimed,
             active_scope=snapshot['active_scope'],
             budget_kind='shared_cache_limit', budget_is_rss_cap=False,
             category_peaks=dict(self._peak_bytes),
             process_rss_bytes=rss,
             process_rss_source=('the background sample of this process'
                                 if rss is not None else 'unknown'),
             resident_unaccounted_bytes=resident_unaccounted,
             resident_unaccounted_cache_bytes=cache_bytes,
             resident_unaccounted_status=('measured' if resident_unaccounted is not None
                                          else 'unknown'),
             resident_unaccounted_sample_age_seconds=(
                 round(max(0.0, time.perf_counter() - sample.get('monotonic', 0.0)), 3)
                 if sample else None),
             resident_unaccounted_note=(
                 'the resident memory that no cache claims: the runtime, the '
                 'browser side of WebEngine and anything that is not in a '
                 'rebuildable cache.  It is not a leak verdict, and it must '
                 'not be added to the other classes.'))
        return True

    def _live_class_payload(self, name, sample=None):
        """The live measurement of a class outside the shared cache limit.

        ``None`` means this class has no live counter, or its counter has
        no value yet; the rollup then reports the fallback status from
        :attr:`UNMEASURED_CLASSES`.
        """
        if name == 'webengine_children':
            return _webengine_children_payload(sample)
        if name == 'temp_disk':
            return _temp_disk_payload()
        return None

    def _record_snapshot(self, reason, *, force=False):
        """Write the budget breakdown to the operation log.

        The snapshot is the only place that says how many bytes each class of
        rebuildable cache holds, so it is recorded whenever the numbers move
        (throttled, because caches change often).
        """
        now = time.monotonic()
        with self._lock:
            if not force and now-self._last_record < 1.0:
                return
            self._last_record = now
        try:
            from prism.performance_trace import record
        except Exception:  # pragma: no cover - the trace module is optional
            return
        snapshot = self.snapshot()
        record('cache_budget_snapshot', reason=reason, limit=snapshot['limit'],
               bytes=snapshot['bytes'], over_budget=snapshot['over_budget'],
               categories=snapshot['categories'], scopes=snapshot['scopes'],
               foreign_bytes=snapshot['foreign_bytes'],
               resident_bytes=snapshot['resident_bytes'],
               reclaimed=snapshot['reclaimed'],
               over_budget_events=snapshot['over_budget_events'],
               active_scope=snapshot['active_scope'], caches=snapshot['caches'])
        # The resource rollup reuses this snapshot: the caches are not walked
        # again and no byte is counted twice.
        self.record_resource_rollup(reason=reason, snapshot=snapshot)


_shared = None
_shared_lock = threading.Lock()


def shared_cache_budget():
    global _shared
    with _shared_lock:
        if _shared is None:
            try:
                mb = int(os.environ.get('PRISM_CACHE_BUDGET_MB', '256'))
            except ValueError:
                mb = 256
            _shared = CacheBudget(max(32, min(mb, 4096))*1024*1024)
        return _shared


# ── project lifecycle ──────────────────────────────────────────


def open_project_resources(key, *, label=None):
    """Start the resource scope of a project that is being opened.

    The scope cancels the project's background work, owns its temporary files
    and takes over its share of the shared byte budget. Returns the scope, so
    the caller can hand it to workers.
    """
    scope = ProjectScope(project_scope_key(key) or str(key), label=label)
    register_scope(scope)
    set_current_project_scope(scope)
    shared_cache_budget().activate_scope(scope.key)
    return scope


def release_project_resources(key=None):
    """Release everything a closed project still holds.

    Called when a project is closed, before its scene is cleared:

    * the project's pending display decodes are cancelled,
    * the project's video materialization and preview tasks are cancelled,
    * the temporary files the project leased are deleted,
    * the project's share of the shared byte budget is released.

    ``key`` is the project file path; ``None`` releases the project that is
    open right now. The returned report says what was actually released, so
    a caller can log it and a test can assert it.
    """
    report = dict(decodes=0, media=0, previews=0, leases=0, bytes=0, scope=None)
    scope = current_project_scope()
    wanted = project_scope_key(key) if key else None
    released = False
    if scope is not None and (wanted is None or scope.key == wanted):
        report['scope'] = scope.key
        report['leases'] = scope.release_leases()
        scope.cancel()
        set_current_project_scope(None)
        released = True
    target = wanted or (scope.key if scope else None)
    report['decodes'] = _cancel_project_decodes(target)
    report['media'] = _cancel_project_media(target)
    if released:
        report['bytes'] = shared_cache_budget().activate_scope(None)
    return report


def _cancel_project_decodes(key):
    """Cancel the pending display decodes of one project."""
    if not key:
        return 0
    try:
        from prism.image_decode import cancel_project_decodes
    except Exception:
        logger.exception('Could not reach the display decoder')
        return 0
    return cancel_project_decodes(key)


def _cancel_project_media(key):
    """Cancel the pending video materialization and preview tasks of one project."""
    if not key:
        return 0
    cancelled = 0
    try:
        from prism.video_thumbnails import cancel_project_media
    except Exception:
        logger.exception('Could not reach the video services')
        return cancelled
    return cancel_project_media(key)
