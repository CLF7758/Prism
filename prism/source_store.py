"""Original storage: import snapshots, read-only archive access and size metadata."""
from contextlib import contextmanager
from contextvars import ContextVar
from pathlib import Path
import hashlib
import os
import shutil
import sqlite3
import tempfile
import threading
import time
import uuid
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor

# Windows codecs/external readers can retain handles briefly after Qt closes.
# Best-effort tempfile finalization must never turn normal exit into an error.
_directory = None
_lock = threading.Lock()
_archive_sizes = OrderedDict()

#: The largest archive whose byte counts may be counted on the GUI thread.
#:
#: Counting the originals means reading `sqlar`, whose rows carry the image
#: bytes, so the scan moves the whole archive through the process: measured
#: 156 s for 51.81 GiB on a mechanical disk. 512 MB is a couple of seconds
#: on the same disk -- worth the exact answer -- while anything larger is
#: reported as "unknown" instead of freezing the window for minutes.
ARCHIVE_SCAN_LIMIT = 512 * 1024 * 1024
# 提取件内容指纹：retained 临时路径 -> (sha256_hexdigest, size)。
# key=提取路径；value=内容哈希+字节数；占用约百字节/条，上限 4096 条 <1MB；
# 拥有者 source_store，`_lock` 保护；淘汰 FIFO，`stream_fingerprint` 命中
# move_to_end 保热。缩略图用它把「内嵌视频源」的缓存键从 uuid 路径换成
# 内容指纹，源字节不变时跨次打开仍命中缓存。
_stream_fingerprints = OrderedDict()
_STREAM_FINGERPRINT_MAX = 4096
_materialized_archives = OrderedDict()
_archive_extract_locks = {}

#: How long an extracted original keeps its lease before it may be collected
#: again.  A consumer that still points at the file holds it, so a save or
#: an export that is reading it can never lose its source.
MATERIALIZATION_LEASE_SECONDS = 300.0

#: extracted path -> lease deadline (monotonic seconds)
_materialized_leases = {}
#: extracted path -> number of consumers that still point at the file
_materialized_holds = {}
#: extracted path -> the archive version it was extracted from
_materialized_versions = OrderedDict()
_SOURCE_VERSION_MAX = 1024
#: what the materialization actually did since the process started
_materialization_counters = {'requests': 0, 'extractions': 0, 'cache_hits': 0}
#: 每提取这么多次就顺手收一次到期的提取件
_RECLAIM_EVERY = 32
_reclaim_counter = 0


class MaterializationCancelled(RuntimeError):
    """An obsolete media request stopped at a streaming boundary."""


class MaterializationClosing(RuntimeError):
    """The materialization service stopped accepting new work.

    Raised by :meth:`MaterializationService.request` once the service was
    told to stop accepting.  It is a plain error, not a cancellation: the
    caller may retry after the host resumed the service.
    """


_media_cancel = ContextVar('media_materialization_cancel', default=None)


@contextmanager
def materialization_cancellation(cancel):
    token = _media_cancel.set(cancel)
    try:
        yield
    finally:
        _media_cancel.reset(token)


def _check_materialization_cancel(cancel):
    if cancel is not None and cancel():
        raise MaterializationCancelled('Media materialization cancelled')


def archive_video_key(archive, suffix):
    filename, item_id = archive
    from prism.storage.managed import root_for, binding
    if root_for(filename) is not None:
        ref=binding(filename,'sqlar',item_id,item_id=True)
        if ref is None:raise ValueError('Embedded source reference missing')
        return (str(Path(filename).resolve()),int(item_id),'managed',ref[0],ref[1],suffix,None)
    path = Path(filename).resolve()
    stat = path.stat()
    try:
        wal = Path(str(path)+'-wal').stat()
        wal_version = (wal.st_size, wal.st_mtime_ns)
    except FileNotFoundError:
        wal_version = None
    return (str(path), int(item_id), stat.st_size, stat.st_mtime_ns,
            stat.st_dev, stat.st_ino, suffix, wal_version)


def retain_archive_video(archive, suffix='.mp4', *, cancel=None):
    """Stream an embedded original once per source version, without a BLOB copy.

    Callers needing a synchronous source (save/export) retain that contract;
    playback calls this on its worker pool. Connections and blobs are local
    to the calling thread. Concurrent playback/export share the same result.
    """
    cancel = cancel if cancel is not None else _media_cancel.get()
    _check_materialization_cancel(cancel)
    key = archive_video_key(archive, suffix)
    client = object()
    with _lock:
        _materialization_counters['requests'] += 1
        cached = _materialized_archives.get(key)
        if cached and os.path.isfile(cached):
            _materialized_archives.move_to_end(key)
            _materialization_counters['cache_hits'] += 1
            _materialized_leases[cached] = (
                time.monotonic() + MATERIALIZATION_LEASE_SECONDS)
            return cached
        entry = _archive_extract_locks.get(key)
        if entry is None:
            entry = [threading.Lock(), 0, {}]
            _archive_extract_locks[key] = entry
        # Count waiters as well as the current holder. In particular a failed
        # first extraction must not detach the guard while a retry waits on it.
        entry[1] += 1
        entry[2][client] = cancel
        guard = entry[0]
    acquired = False
    def all_cancelled():
        # Snapshot under the registry lock; never execute client callbacks while
        # holding it. A synchronous save/export (None) always keeps work alive.
        with _lock:
            checks = tuple(entry[2].values())
        if checks and all(check is not None and check() for check in checks):
            return True
        # The owner gave up as well - the service is closing, or the caller
        # cancelled. The stream has to notice that at the next chunk
        # boundary, otherwise the half written file stays on disk.
        return cancel is not None and cancel()
    try:
        while not acquired:
            _check_materialization_cancel(cancel)
            acquired = guard.acquire(timeout=.05)
        if acquired:
            with _lock:
                cached = _materialized_archives.get(key)
                if cached and os.path.isfile(cached):
                    _materialization_counters['cache_hits'] += 1
                    _materialized_leases[cached] = (
                        time.monotonic() + MATERIALIZATION_LEASE_SECONDS)
            if cached and os.path.isfile(cached):
                return cached
            from prism.storage.managed import root_for, open_original
            connection = None
            path = None
            try:
                if root_for(key[0]) is not None:
                    context=open_original(key[0],'sqlar',key[1],item_id=True)
                else:
                    connection = sqlite3.connect(Path(key[0]).as_uri()+'?mode=ro', uri=True)
                    connection.execute('BEGIN')
                    row=connection.execute('SELECT rowid,length(data) FROM sqlar WHERE item_id=?',(key[1],)).fetchone()
                    if not row or not row[1]:raise ValueError('Embedded video has no original bytes')
                    context=connection.blobopen('sqlar','data',row[0],readonly=True)
                with context as blob:
                    with materialization_cancellation(all_cancelled):
                        path = retain_stream(blob, suffix)
                if archive_video_key(archive, suffix) != key:
                    raise RuntimeError('Video archive changed during extraction')
            except BaseException:
                if path:
                    Path(path).unlink(missing_ok=True)
                raise
            finally:
                if connection is not None:connection.close()
            with _lock:
                _materialized_archives[key] = path
                _materialized_archives.move_to_end(key)
                _materialization_counters['extractions'] += 1
                _materialized_leases[path] = (
                    time.monotonic() + MATERIALIZATION_LEASE_SECONDS)
                _materialized_versions[path] = key
                _materialized_versions.move_to_end(path)
                while len(_materialized_versions) > _SOURCE_VERSION_MAX:
                    _materialized_versions.popitem(last=False)
                while len(_materialized_archives) > 256:
                    _evicted, evicted_path = _materialized_archives.popitem(last=False)
                    # The file itself stays readable for whoever still points
                    # at it; only its lease runs out, so it can be collected.
                    _materialized_leases.setdefault(
                        evicted_path,
                        time.monotonic() + MATERIALIZATION_LEASE_SECONDS)
            _check_materialization_cancel(cancel)
            _maybe_reclaim()
            return path
    finally:
        if acquired:
            guard.release()
        with _lock:
            entry[2].pop(client, None)
            entry[1] -= 1
            if entry[1] == 0 and _archive_extract_locks.get(key) is entry:
                _archive_extract_locks.pop(key, None)


def _maybe_reclaim():
    """Periodically collect the extractions whose lease ran out.

    The sweep is what makes the lease a real mechanism: without it the
    temporary directory would only ever be emptied when the process ends.
    It runs outside the registry lock, and it never touches a file a
    consumer still holds.
    """
    global _reclaim_counter
    with _lock:
        _reclaim_counter += 1
        due = _reclaim_counter >= _RECLAIM_EVERY
        if due:
            _reclaim_counter = 0
    if due:
        reclaim_expired_materializations()


def materialization_counters():
    """How much work the materialization did since the process started.

    ``extractions`` counts the times bytes were really pulled out of an
    archive, ``cache_hits`` the requests that found a ready file.  Tests
    and the performance log read these instead of guessing.
    """
    with _lock:
        return dict(_materialization_counters)


def renew_materialization_lease(path, seconds=None):
    """Push the lease of an extracted original forward."""
    if not path:
        return
    seconds = MATERIALIZATION_LEASE_SECONDS if seconds is None else seconds
    with _lock:
        _materialized_leases[str(path)] = time.monotonic() + seconds


def hold_materialization(path):
    """Keep an extracted original alive while a consumer points at it.

    A held file is never collected, so a save or an export that reads it
    can not lose its source half way through.
    """
    if not path:
        return
    key = str(path)
    with _lock:
        if key not in _materialized_leases:
            return
        _materialized_holds[key] = _materialized_holds.get(key, 0) + 1


def release_materialization(path):
    """Give up the hold taken by :func:`hold_materialization`."""
    if not path:
        return
    key = str(path)
    with _lock:
        count = _materialized_holds.get(key, 0) - 1
        if count > 0:
            _materialized_holds[key] = count
        else:
            _materialized_holds.pop(key, None)


@contextmanager
def materialization_hold(path):
    """Hold an extracted original for the duration of one read."""
    hold_materialization(path)
    try:
        yield path
    finally:
        release_materialization(path)


def reclaim_expired_materializations(now=None):
    """Collect the extracted originals whose lease ran out.

    A file that a consumer still holds is left alone: the bytes a save is
    reading must not disappear underneath it.  Returns the paths that were
    collected.
    """
    now = time.monotonic() if now is None else now
    expired = []
    with _lock:
        for path, deadline in list(_materialized_leases.items()):
            if deadline > now:
                continue
            if _materialized_holds.get(path, 0) > 0:
                continue
            del _materialized_leases[path]
            _materialized_versions.pop(path, None)
            for key in [k for k, v in _materialized_archives.items()
                        if v == path]:
                del _materialized_archives[key]
            expired.append(path)
    for path in expired:
        try:
            Path(path).unlink(missing_ok=True)
        except OSError:
            pass
    return tuple(expired)


def stream_source_version(path):
    """The archive version an extracted original came from, or ``None``."""
    with _lock:
        value = _materialized_versions.get(str(path))
        if value is not None:
            _materialized_versions.move_to_end(str(path))
        return value


def preview_cache_key(archive, suffix, spec):
    """Cache key of a preview derived from an embedded original.

    The key names the **source version** (archive path, item id, size,
    modification time, inode and WAL state) *and* the **spec** (target
    size, sampling).  Two versions of the same item, and two specs of the
    same version, therefore never share a preview.
    """
    return archive_video_key(archive, suffix) + (spec,)


class _MaterializationClient:
    """One consumer of a background extraction."""

    __slots__ = ('cancel_event',)

    def __init__(self, cancel_event=None):
        self.cancel_event = cancel_event

    def cancelled(self):
        """Whether this consumer gave up on the extraction."""
        if self.cancel_event is None:
            # A consumer that must have the bytes keeps the work alive.
            return False
        return self.cancel_event.is_set()


class _MaterializationTask:
    """The extraction itself, shared by the requests that want it."""

    def __init__(self, key, archive, suffix):
        self.key = key
        self.archive = archive
        self.suffix = suffix
        self.started = threading.Event()
        self.finished = threading.Event()
        self.path = None
        self.error = None
        self.cancelled = False
        self._clients = []
        self._lock = threading.Lock()
        self._finished_once = False

    def attach(self, cancel_event):
        """Add a consumer; returns its client token."""
        client = _MaterializationClient(cancel_event)
        with self._lock:
            self._clients.append(client)
        return client

    def detach(self, client):
        """Drop a consumer that gave up."""
        with self._lock:
            if client in self._clients:
                self._clients.remove(client)

    def all_cancelled(self):
        """True when nobody is waiting for the bytes any more."""
        with self._lock:
            clients = list(self._clients)
        if not clients:
            return True
        return all(client.cancelled() for client in clients)

    def has_mandatory_client(self):
        """True when a consumer must have the bytes and cannot give up.

        A synchronous save or export asks for the extraction without a
        cancel event: it needs the real file, so the work must not be
        stopped while it is still running.
        """
        with self._lock:
            clients = list(self._clients)
        return any(client.cancel_event is None for client in clients)

    def _finish(self, path, error, cancelled=False):
        if self._finished_once:
            return
        self._finished_once = True
        self.path = path
        self.error = error
        self.cancelled = cancelled
        self.finished.set()


class MaterializationRequest:
    """One extraction of an embedded original, running on a worker thread.

    ``wait`` blocks the caller until the bytes are on disk; ``cancel``
    stops the extraction at the next chunk boundary - but only when no
    other consumer is waiting for it - and removes the half written file.
    """

    def __init__(self, task, client):
        self._task = task
        self._client = client
        self._cancelled = False

    @property
    def key(self):
        """The source version this request extracts."""
        return self._task.key

    @property
    def cancellable(self):
        """Whether this request can be given up on."""
        return self._client.cancel_event is not None

    def cancel(self):
        """Give up on this extraction; the half written file is removed."""
        self._cancelled = True
        if self._client.cancel_event is not None:
            self._client.cancel_event.set()
        self._task.detach(self._client)

    @property
    def done(self):
        return self._task.finished.is_set()

    @property
    def path(self):
        """The extracted path, or ``None`` while the request is running."""
        return self._task.path

    @property
    def error(self):
        """The failure, if the extraction failed."""
        return self._task.error

    def wait(self, timeout=None):
        """Block until the extraction finished; return the extracted path."""
        if not self._task.finished.wait(timeout):
            raise TimeoutError('Materialization did not finish in time')
        if self._task.error is not None:
            raise self._task.error
        if self._cancelled or self._task.cancelled:
            raise MaterializationCancelled('Media materialization cancelled')
        return self._task.path


class MaterializationService:
    """Extracts embedded originals on background threads.

    One task per source version: two requests for the same version share
    the extraction, and two versions never share one.  A consumer that
    gives up does not stop an extraction somebody else still needs.  This
    service is the only place that knows how an embedded original is
    pulled out of a .prism archive, so the GUI thread never does that
    work itself.

    **Closing protocol.**  The service exposes the same four steps every
    other background service in Prism has to offer, so a host that closes
    the window does not need a private hook per service:

    ``closing``
        Whether the service stopped accepting new work.
    ``stop_accepting()``
        Refuse new requests.  This is reversible: a host whose close was
        cancelled calls ``resume_accepting()`` and the service works
        again.
    ``cancel_pending()``
        Cancel optional requests that have not started yet and tell their
        waiters. Queued save/export requests still run to completion.
    ``is_drained()``
        Whether every started extraction has finished.

    ``shutdown()`` runs those steps in order and then stops the worker
    threads.  It is idempotent and never creates a service that was
    never used.
    """

    def __init__(self, workers=2):
        self._lock = threading.Lock()
        self._tasks = {}
        self._futures = {}
        self._executor = ThreadPoolExecutor(
            max_workers=workers, thread_name_prefix='prism-materialize')
        #: Set by :meth:`stop_accepting`, cleared by :meth:`resume_accepting`.
        self._stop = threading.Event()
        self._closed = False

    # -- the shared closing protocol -------------------------------------
    @property
    def closing(self):
        """Whether the service stopped accepting new work."""
        return self._stop.is_set()

    def stop_accepting(self):
        """Refuse new requests.  Reversible with :meth:`resume_accepting`."""
        self._stop.set()

    def resume_accepting(self):
        """Resume a reversible stop; return False after final shutdown."""
        with self._lock:
            if self._closed:
                return False
            self._stop.clear()
            return True

    def cancel_pending(self):
        """Cancel optional extractions that have not started yet.

        A cancelled request's waiter is told so it does not block on work
        that will never run.  Returns how many requests were cancelled.
        A task with a mandatory consumer is retained even while queued.
        """
        cancelled = 0
        with self._lock:
            # Serialize cancellation with attaching another consumer. A
            # queued save/export needs its bytes just as a running one does.
            for key, future in list(self._futures.items()):
                task = self._tasks.get(key)
                if (task is None or task.started.is_set()
                        or task.has_mandatory_client()):
                    continue
                if future.cancel():
                    # The worker will never run: release its waiters.
                    task._finish(None, None, cancelled=True)
                    cancelled += 1
        return cancelled

    def is_drained(self):
        """Whether every extraction that started has finished."""
        with self._lock:
            tasks = list(self._tasks.values())
        return all(task.finished.is_set() for task in tasks)

    def state(self):
        """A snapshot of the service, for hosts and for tests."""
        with self._lock:
            tasks = list(self._tasks.values())
        return {
            'closing': self.closing,
            'closed': self._closed,
            'tasks': len(tasks),
            'running': len([t for t in tasks
                            if t.started.is_set() and not t.finished.is_set()]),
            'pending': len([t for t in tasks
                            if not t.started.is_set() and not t.finished.is_set()]),
            'drained': all(t.finished.is_set() for t in tasks),
        }

    # -- work ------------------------------------------------------------
    def request(self, archive, suffix='.mp4', *, cancel_event=None):
        """Start (or join) the extraction of one embedded original.

        The call returns immediately; the caller waits with
        :meth:`MaterializationRequest.wait` when it needs the path.
        """
        try:
            key = archive_video_key(archive, suffix)
        except OSError as error:
            task = _MaterializationTask(None, archive, suffix)
            client = task.attach(cancel_event)
            task.error = error
            task.finished.set()
            return MaterializationRequest(task, client)
        with self._lock:
            if self._stop.is_set() or self._closed:
                # The service is closing: say so instead of scheduling work
                # that nobody will ever wait for.
                raise MaterializationClosing(
                    'The materialization service is closing and does not '
                    'accept new work')
            task = self._tasks.get(key)
            if task is None or task.finished.is_set():
                task = _MaterializationTask(key, archive, suffix)
                self._tasks[key] = task
                client = task.attach(cancel_event)
                start = True
            else:
                client = task.attach(cancel_event)
                start = False
            if start:
                # Registration and submission must be atomic with shutdown:
                # no accepted task may exist without a future to drain.
                try:
                    future = self._executor.submit(self._run, task)
                except BaseException as error:
                    task._finish(None, error)
                    self._tasks.pop(key, None)
                    raise
                self._futures[key] = future
        return MaterializationRequest(task, client)

    def _cancel_predicate(self, task):
        """The cooperative cancel check of one running extraction.

        The extraction stops when nobody wants the bytes any more, or when
        the service is closing and no consumer needs the bytes for a save
        or an export.
        """
        def should_cancel():
            if task.all_cancelled():
                return True
            if self._stop.is_set() and not task.has_mandatory_client():
                return True
            return False
        return should_cancel

    def _run(self, task):
        task.started.set()
        try:
            with materialization_cancellation(self._cancel_predicate(task)):
                path = retain_archive_video(task.archive, task.suffix)
        except MaterializationCancelled:
            task._finish(None, None, cancelled=True)
        except BaseException as error:
            task._finish(None, error)
        else:
            task._finish(path, None)

    def forget(self, request):
        """Drop a finished task from the registry."""
        with self._lock:
            for key, task in list(self._tasks.items()):
                if task is request._task:
                    del self._tasks[key]
                    self._futures.pop(key, None)

    def shutdown(self, wait=True):
        """Stop accepting work, release the waiters and stop the threads.

        The order matters: first no new work is accepted, then the
        optional requests that never started are cancelled and their waiters are
        told, then the running extractions are asked to stop at their next
        chunk boundary, and only then the worker threads are joined.  A
        consumer that must have the bytes (a save or an export) keeps its
        extraction alive until it finishes.
        """
        with self._lock:
            already_closed = self._closed
            self._closed = True
        self._stop.set()
        self.cancel_pending()
        if already_closed:
            # Idempotent: a second shutdown only honours the wait.
            if wait:
                self._executor.shutdown(wait=True)
            return
        self._executor.shutdown(wait=wait)


_service = None
_service_lock = threading.Lock()


def materialization_service():
    """The process wide background extraction service."""
    global _service
    with _service_lock:
        if _service is None:
            _service = MaterializationService()
        return _service


def materialization_service_state():
    """The state of the process wide service, without creating one.

    A host that is closing the window must be able to ask whether the
    materialization service is still busy **without** bringing a service
    into existence that was never used.  Returns ``None`` when no service
    was ever created.
    """
    with _service_lock:
        service = _service
    if service is None:
        return None
    return service.state()


def materialization_service_closing():
    """Whether the process wide service stopped accepting new work."""
    state = materialization_service_state()
    return bool(state and state['closing'])


def materialization_service_drained():
    """Whether the process wide service has no unfinished extraction.

    A service that was never created counts as drained: there is nothing
    to wait for.
    """
    state = materialization_service_state()
    return True if state is None else bool(state['drained'])


def stop_accepting_materialization():
    """Stop the process wide service from accepting new work.

    This is the reversible half of the close: a host that is not sure yet
    whether the window really closes calls it as soon as the close is
    certain, and calls :func:`resume_accepting_materialization` when the
    close was cancelled.  The work that is already running is **not**
    stopped here - a save or an export that still needs its bytes keeps
    them.

    Returns ``True`` when a service was told to stop and ``False`` when
    there was none.  It never creates a service that was never used.
    """
    with _service_lock:
        service = _service
    if service is None:
        return False
    service.stop_accepting()
    return True


def resume_accepting_materialization():
    """Let the process wide service accept new work again.

    The counterpart of :func:`stop_accepting_materialization`, for a host
    whose close was cancelled. Returns ``True`` when accepting resumed and
    ``False`` when there is no service or final shutdown already happened.
    """
    with _service_lock:
        service = _service
    if service is None:
        return False
    return service.resume_accepting()


def shutdown_materialization_service(wait=True):
    """Stop the process wide materialization service, if there is one.

    Returns ``True`` when a service was actually shut down and ``False``
    when there was none.  It never creates a service that was never
    used, and it never stops work that a save or an export still needs.
    """
    global _service
    with _service_lock:
        service = _service
    if service is None:
        return False
    service.shutdown(wait=wait)
    return True


def remember_stream_fingerprint(path, fingerprint, size):
    """Register the content fingerprint of a retained stream file."""
    with _lock:
        _stream_fingerprints[str(path)] = (fingerprint, int(size))
        _stream_fingerprints.move_to_end(str(path))
        while len(_stream_fingerprints) > _STREAM_FINGERPRINT_MAX:
            _stream_fingerprints.popitem(last=False)


def stream_fingerprint(path):
    """Return ``(sha256_hexdigest, size)`` for a retained path, or ``None``."""
    with _lock:
        value = _stream_fingerprints.get(str(path))
        if value is not None:
            _stream_fingerprints.move_to_end(str(path))
        return value


def remember_archive_sizes(filename, sizes):
    """Publish byte metadata gathered by project IO, never by a paint event."""
    path = str(Path(filename).resolve())
    stat = os.stat(path)
    key = (path, stat.st_mtime_ns, stat.st_size)
    with _lock:
        for previous in [k for k in _archive_sizes if k[0] == path]:
            del _archive_sizes[previous]
        _archive_sizes[key] = dict(sizes)
        while len(_archive_sizes) > 8:
            _archive_sizes.popitem(last=False)


def known_archive_sizes(filename, stat):
    key = (str(Path(filename).resolve()), stat.st_mtime_ns, stat.st_size)
    with _lock:
        sizes = _archive_sizes.get(key)
        if sizes is not None:
            _archive_sizes.move_to_end(key)
        return sizes


def reserve_file(suffix):
    """Reserve an original path; a streaming worker writes the snapshot.

    The main process owns the directory lifetime, so worker exit cannot remove
    the original used by a canvas item and its undo history.
    """
    global _directory
    if suffix not in ('.mp4', '.mkv', '.webm', '.mov'):
        raise ValueError('Unsupported retained media suffix')
    with _lock:
        if _directory is None:
            _directory = tempfile.TemporaryDirectory(prefix='prism-originals-', ignore_cleanup_errors=True)
        return str(Path(_directory.name)/(uuid.uuid4().hex+suffix))


def retain_file(filename):
    global _directory
    with _lock:
        if _directory is None:
            _directory = tempfile.TemporaryDirectory(prefix='prism-originals-', ignore_cleanup_errors=True)
        target = Path(_directory.name) / (uuid.uuid4().hex + Path(filename).suffix)
    temporary = target.with_suffix(target.suffix+'.partial')
    try:
        before = os.stat(filename)
        digest = hashlib.sha256()
        count = 0
        with open(filename, 'rb') as source, temporary.open('xb') as output:
            opened = os.fstat(source.fileno())
            while chunk := source.read(1024*1024):
                digest.update(chunk)
                output.write(chunk)
                count += len(chunk)
            output.flush()
            os.fsync(output.fileno())
            ended = os.fstat(source.fileno())
        after = os.stat(filename)
        # Windows stat(path) and fstat(fd) need not expose the same ctime:
        # one may be creation time while the other reports change/write time.
        # Compare identity/length/mtime across APIs, and ctime only within the
        # same API before/after. Never drop identity or payload verification.
        stamp = lambda value: (value.st_dev, value.st_ino, value.st_size, value.st_mtime_ns)
        stable_identity = stamp(before) == stamp(opened) == stamp(ended) == stamp(after)
        stable_path_ctime = before.st_ctime_ns == after.st_ctime_ns
        stable_handle_ctime = opened.st_ctime_ns == ended.st_ctime_ns
        if (not stable_identity or not stable_path_ctime or not stable_handle_ctime
                or count != before.st_size):
            raise OSError('Source changed while retaining imported original')
        check = hashlib.sha256()
        with temporary.open('rb') as retained:
            while chunk := retained.read(1024*1024):
                check.update(chunk)
        if check.digest() != digest.digest():
            raise OSError('Retained original verification failed')
        os.replace(temporary, target)
    finally:
        temporary.unlink(missing_ok=True)
    return str(target)


def retain_bytes(data, suffix='.png'):
    global _directory
    with _lock:
        if _directory is None:
            _directory = tempfile.TemporaryDirectory(prefix='prism-originals-', ignore_cleanup_errors=True)
        target = Path(_directory.name) / (uuid.uuid4().hex + suffix)
    temporary = target.with_suffix(target.suffix+'.partial')
    try:
        with temporary.open('xb') as output:
            output.write(data)
            output.flush()
            os.fsync(output.fileno())
        if hashlib.sha256(temporary.read_bytes()).digest() != hashlib.sha256(data).digest():
            raise OSError('Retained original bytes did not verify')
        os.replace(temporary,target)
    finally:
        temporary.unlink(missing_ok=True)
    return str(target)


def retain_stream(stream, suffix, *, cancel=None):
    """Keep an embedded video without allocating its entire blob in memory.

    The caller's stream is short-lived (inside a ``blobopen`` context), so it is
    read to completion here; the extracted file is the persistent original that
    playback/save/export read from, not a reclaimable lease. While copying we
    compute a SHA-256 content fingerprint so the thumbnail cache can key embedded
    videos by source bytes rather than the per-open uuid path.
    """
    global _directory
    with _lock:
        if _directory is None:
            _directory = tempfile.TemporaryDirectory(prefix='prism-originals-', ignore_cleanup_errors=True)
        target = Path(_directory.name)/(uuid.uuid4().hex+suffix)
    cancel = cancel if cancel is not None else _media_cancel.get()
    digest = hashlib.sha256()
    size = 0
    try:
        with target.open('wb') as output:
            while True:
                _check_materialization_cancel(cancel)
                chunk = stream.read(1024*1024)
                if not chunk:
                    break
                output.write(chunk)
                digest.update(chunk)
                size += len(chunk)
            _check_materialization_cancel(cancel)
    except BaseException:
        target.unlink(missing_ok=True)
        raise
    remember_stream_fingerprint(str(target), digest.hexdigest(), size)
    return str(target)


def read_archive(archive):
    """Read original bytes with a thread-local, read-only connection."""
    filename, item_id = archive
    from prism.storage.managed import root_for, read_bytes
    if root_for(filename) is not None:
        return read_bytes(filename, 'sqlar', item_id, item_id=True)
    connection = sqlite3.connect(Path(filename).resolve().as_uri() + '?mode=ro', uri=True)
    try:
        row = connection.execute('SELECT data FROM sqlar WHERE item_id = ?',
                                 (item_id,)).fetchone()
        return row[0] if row else None
    finally:
        connection.close()


class ArchiveReadSession:
    """Bounded, worker-local read-only connections for one batch operation."""
    def __init__(self):
        import threading
        self._local = threading.local()
        self._lock = threading.Lock()
        self._connections = []

    def read(self, archive):
        from collections import OrderedDict
        filename, item_id = archive
        from prism.storage.managed import root_for
        if root_for(filename) is not None:
            return read_archive(archive)
        if not hasattr(self._local, 'archives'):
            self._local.archives = OrderedDict()
        archives = self._local.archives
        connection = archives.get(filename)
        if connection is None:
            connection = sqlite3.connect(Path(filename).resolve().as_uri()+'?mode=ro',
                                         uri=True, check_same_thread=False)
            archives[filename] = connection
            with self._lock:
                self._connections.append(connection)
            while len(archives) > 2:
                _, oldest = archives.popitem(last=False)
                oldest.close()
                with self._lock:
                    self._connections.remove(oldest)
        else:
            archives.move_to_end(filename)
        cursor = connection.execute('SELECT data FROM sqlar WHERE item_id=?', (item_id,))
        try:
            row = cursor.fetchone()
            return row[0] if row else None
        finally:
            cursor.close()

    def close(self):
        with self._lock:
            for connection in self._connections:
                connection.close()
            self._connections.clear()


def storage_bytes(items, cache, *, cancel=None):
    """Read stored byte counts without decoding originals; cache archive metadata."""
    total = 0
    archive_keys = {}
    path_sizes = {}
    for item in items:
        if cancel is not None and cancel():
            return None
        if isinstance(item, dict):
            continue
        source = getattr(item, '_prism_source', None) or getattr(item, '_asset_source', None)
        try:
            if source:
                if source[0] not in archive_keys:
                    stat = os.stat(source[0])
                    archive_keys[source[0]] = (source[0], stat.st_mtime_ns, stat.st_size)
                key = archive_keys[source[0]]
                if key not in cache:
                    sizes = known_archive_sizes(source[0], stat)
                    if sizes is None:
                        # The archive has to be read to learn the sizes, and
                        # `sqlar` holds every image byte -- so a count means
                        # streaming the whole project off the disk. Measured
                        # 156 s for 51.81 GiB on a mechanical disk.
                        #
                        # This runs on the GUI thread (the inspector asks for
                        # the occupied size while it paints), so for a big
                        # archive the answer is "unknown" rather than a
                        # multi-minute freeze. The inspector renders that.
                        # A small archive is read exactly as before.
                        if stat.st_size > ARCHIVE_SCAN_LIMIT:
                            sizes = {}
                        else:
                            archive = sqlite3.connect(
                                Path(source[0]).resolve().as_uri() + '?mode=ro',
                                uri=True, timeout=.05)
                            try:
                                sizes = dict(archive.execute(
                                    'SELECT item_id, COALESCE(sz,0) FROM sqlar '
                                    'WHERE sz IS NOT NULL'))
                                missing = archive.execute(
                                    'SELECT item_id, COALESCE(length(data),0) '
                                    'FROM sqlar WHERE sz IS NULL').fetchall()
                            finally:
                                archive.close()
                            for item_id, size in missing:
                                sizes[item_id] = size
                    for previous in [previous for previous in cache if previous[0] == source[0]]:
                        del cache[previous]
                    cache[key] = sizes
                size = cache[key].get(source[1])
                if size is None:
                    return None
                total += size
            elif getattr(item, '_source_blob', None):
                total += len(item._source_blob)
            else:
                import stat as stat_module
                filename = getattr(item, '_source_path', '') or getattr(item, 'filename', '') or ''
                if filename not in path_sizes:
                    info = os.stat(filename)
                    if not stat_module.S_ISREG(info.st_mode):
                        return None
                    path_sizes[filename] = info.st_size
                total += path_sizes[filename]
        except (OSError, sqlite3.Error):
            return None
    return total

