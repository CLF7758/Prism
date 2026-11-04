"""Bounded opening-frame previews, decoded with the existing PyAV backend.

**Project scope.** A video that belongs to a ``.prism`` project names that
project in its ``_prism_source``. Closing the project cancels that project's
materialization and preview tasks (:meth:`VideoMaterializer.cancel_project`,
:meth:`Thumbnails.cancel_project`) instead of waiting for the application to
quit, and the extracted source file is leased to the project so its owner and
its release point are explicit.
"""
from collections import OrderedDict
from pathlib import Path
import time
import threading
import weakref
from PyQt6 import QtCore, QtGui, sip
from prism.performance_trace import QueueAccounting, measured
from prism.qt_thread_safety import gui_signal_carrier
from prism.resource_budget import project_scope_key, scope_for

MAX_PENDING = 128
CACHE_BYTES = 32*1024*1024
_service = None
_materializer = None

#: The queue categories of the two work queues of this module. The counters
#: are the shared contract of ``prism.performance_trace``: the accounting
#: only counts, it never decides what runs.
MATERIALIZE_QUEUE = 'video_materialize'
THUMBNAIL_QUEUE = 'video_thumbnail'


def source_project_scope(item):
    """The project a video item belongs to, or ``None``."""
    source = getattr(item, '_prism_source', None)
    if isinstance(source, tuple) and source and isinstance(source[0], str):
        return project_scope_key(source[0])
    return None


def _remove_file(path):
    try:
        Path(path).unlink(missing_ok=True)
    except OSError:
        pass


def _file_size(path):
    """The size of a file in bytes, or ``None`` when it cannot be read.

    Read once, by the writer that knows the file: the resource rollup then
    reads a counter instead of stat-ing the file on every rollup.
    """
    try:
        return Path(path).stat().st_size
    except OSError:
        return None


class _Job:
    """One queued job's bookkeeping.

    Plain Python on purpose: a worker thread holds this object, never a Qt
    object, and never file contents. The accounting it reports through is
    internally locked, so the GUI thread and a worker may use it at the
    same time.
    """

    __slots__ = ('key', 'submitted_at', 'accounting', 'cancelled', 'dropped')

    def __init__(self, key, submitted_at, accounting):
        self.key = key
        self.submitted_at = submitted_at
        self.accounting = accounting
        #: Set when the request was cancelled: a running job cannot be
        #: interrupted, so the work continues and only its result is dropped.
        self.cancelled = False
        #: Set when the worker had to drop the result instead of delivering it.
        self.dropped = False


class _QueueLedger:
    """The counters of one work queue of this module.

    The ledger only counts. It never decides what runs, never blocks the GUI
    thread, never keeps a Qt object and never logs per item.
    """

    def __init__(self, category, *, limit=None):
        self.accounting = QueueAccounting(category, limit=limit)
        self._jobs = {}

    # -- transitions ------------------------------------------------

    def submit(self, key):
        """Work was offered to the queue."""
        job = _Job(key, time.monotonic(), self.accounting)
        self._jobs[key] = job
        self.accounting.submitted()
        return job

    def job(self, key):
        """The bookkeeping of a queued job, or ``None``."""
        return self._jobs.get(key)

    def cancel_pending(self, key, reason):
        """A job that never ran was dropped."""
        job = self._jobs.pop(key, None)
        if job is None:
            return False
        job.cancelled = True
        self.accounting.cancelled(state='pending', reason=reason)
        return True

    def cancel_running(self, key, reason):
        """A running job was cancelled.

        The work cannot be interrupted: it continues and only its result is
        dropped. That is counted as a cancel *after* start, never as an
        instant cancel.
        """
        job = self._jobs.get(key)
        if job is None or job.cancelled:
            return False
        job.cancelled = True
        self.accounting.cancelled(state='running', reason=reason)
        return True

    def finish(self, key, *, installed=False, failed=None, discarded=None):
        """The job reached its end: installed, failed or discarded."""
        job = self._jobs.pop(key, None)
        if job is None:
            return False
        if discarded is not None or job.cancelled:
            self.accounting.stale_discarded(state='ready',
                                            reason=discarded or 'cancelled')
        elif installed:
            self.accounting.installed()
        else:
            self.accounting.failed(reason=failed or 'unknown')
        return True

    def retry(self, key, reason):
        """The work is tried again: a new attempt is offered to the queue."""
        self.accounting.retried(reason=reason)
        return self.submit(key)

    def join_consumer(self, key):
        """A client attached to the (possibly shared) job."""
        self.accounting.consumer_joined(key)

    def leave_consumer(self, key):
        """A client detached from the job."""
        self.accounting.consumer_left(key)

    def prune(self):
        """Forget the jobs whose result the worker had to drop."""
        for key, job in list(self._jobs.items()):
            if job.dropped:
                self._jobs.pop(key, None)


class MaterializeVideo(QtCore.QRunnable):
    def __init__(self, key, archive, suffix, closed, scope=None, job=None):
        super().__init__()
        self.key, self.archive, self.suffix = key, archive, suffix
        self.closed = closed
        self.scope = scope
        self.cancelled = threading.Event()
        # The carrier has no owner on purpose: it must outlive the service
        # that queued the work.  The service keeps the job alive until it
        # finished, and that is where the carrier is released -- on the GUI
        # thread, never on a worker.
        self.signals = gui_signal_carrier(Signals(),
                                          QtCore.QCoreApplication.instance())
        #: The queue bookkeeping of this job (plain Python, no Qt object).
        self.job = job

    def run(self):
        if self.closed.is_set():
            # The job was dropped before a worker picked it up. The owner
            # already counted the cancel; nothing runs and nothing is emitted.
            return
        started = time.monotonic()
        accounting = self.job.accounting if self.job is not None else None
        if accounting is not None:
            wait_ms = None
            if self.job.submitted_at is not None:
                wait_ms = max(0.0, (started - self.job.submitted_at)*1000)
            accounting.started(wait_ms=wait_ms)
        try:
            try:
                from prism.source_store import retain_archive_video, materialization_cancellation
                with materialization_cancellation(lambda: self.closed.is_set() or self.cancelled.is_set()):
                    path, error = retain_archive_video(self.archive, self.suffix), None
            except Exception as exception:
                path, error = None, str(exception)
            if path and self.scope is not None:
                # The extracted source is a temporary file of this project: it is
                # leased, so closing the project deletes it instead of leaving it
                # in the process-wide temporary directory forever.
                #
                # The size is read here, by the writer that just created the
                # file: the rollup then reads a counter instead of stat-ing
                # the file every time it is written.
                self.scope.lease('video_extract', path,
                                 lambda released: _remove_file(released.resource_id),
                                 size_bytes=_file_size(path))
            if not self.closed.is_set():
                try:
                    self.signals.done.emit(self.key, path, error)
                except RuntimeError:
                    if not sip.isdeleted(self.signals):
                        raise
        finally:
            if accounting is not None:
                accounting.service_done(service_ms=(time.monotonic()-started)*1000)


class VideoMaterializer(QtCore.QObject):
    """Bounded shared extraction; result application and player setup stay GUI-only."""
    def __init__(self, app):
        super().__init__(app)
        self.pool = QtCore.QThreadPool(self)
        self.pool.setMaxThreadCount(2)
        self.closed = threading.Event()
        self.waiting, self.running, self.clients = OrderedDict(), {}, {}
        self.scopes = {}
        #: The counters of this queue. They only count: they never decide
        #: what runs and never keep a Qt object.
        self._ledger = _QueueLedger(MATERIALIZE_QUEUE, limit=MAX_PENDING)
        closed = self.closed
        self.destroyed.connect(lambda *_: closed.set())
        app.aboutToQuit.connect(self.close)
        self.cancel_timer = QtCore.QTimer(self)
        self.cancel_timer.setInterval(50)
        self.cancel_timer.timeout.connect(self.cancel_obsolete)

    def close(self):
        self.closed.set()
        self.cancel_timer.stop()
        for task in self.running.values():
            task.cancelled.set()
        for key in tuple(self.waiting):
            self._ledger.cancel_pending(key, 'shutdown')
        self.waiting.clear()
        for key in tuple(self.running):
            self._ledger.cancel_running(key, 'shutdown')
        for key in tuple(self.clients):
            for _entry in self.clients.get(key, ()):
                self._ledger.leave_consumer(key)
        self.clients.clear()
        self.scopes.clear()
        self.pool.clear()
        self._ledger.accounting.rollup(force=True, reason='shutdown')

    # ── project scope ───────────────────────────────────────────

    def cancel_project(self, key):
        """Cancel the materialization of one project's videos.

        Returns the number of requests that were dropped. The extracted
        source file is not deleted here: it is leased to the project, and the
        lease is released when the project scope is released.
        """
        key = project_scope_key(key)
        if key is None:
            return 0
        cancelled = 0
        for request_key in tuple(self.clients):
            archive = request_key[0]
            if not archive or project_scope_key(archive[0]) != key:
                continue
            for ref, token, _scene, _project in self.clients.pop(request_key, []):
                self._ledger.leave_consumer(request_key)
                client = ref()
                if (client is not None and not sip.isdeleted(client)
                        and getattr(client, '_play_preparation_token', None) == token):
                    client._play_preparation_key = None
            if request_key in self.waiting:
                self.waiting.pop(request_key, None)
                self._ledger.cancel_pending(request_key, 'project_closed')
            self.scopes.pop(request_key, None)
            task = self.running.get(request_key)
            if task is not None:
                task.cancelled.set()
                # The extraction cannot be interrupted: it continues and only
                # its result is dropped.
                self._ledger.cancel_running(request_key, 'project_closed')
            cancelled += 1
        return cancelled

    @staticmethod
    def client_valid(client, key):
        ref, token, scene_ref, project = client
        item = ref()
        if item is None or sip.isdeleted(item):
            return False
        if (getattr(item, '_play_preparation_token', None) != token or
                tuple(item._prism_source or ()) != key[0]):
            return False
        scene = scene_ref() if scene_ref else None
        if ((scene_ref is not None and (scene is None or sip.isdeleted(scene)))
                or item.scene() is not scene
                or getattr(scene, 'project_id', None) != project
                or getattr(scene, '_disposed', False)):
            return False
        from prism.item_lifecycle import is_deleted
        return not is_deleted(item)

    def cancel_obsolete(self):
        # QObject/scene inspection stays on GUI; workers see Events only.
        for key in tuple(self.clients):
            clients = self.clients[key]
            valid = [client for client in clients if self.client_valid(client, key)]
            self.clients[key] = valid
            for _dropped in range(len(clients) - len(valid)):
                self._ledger.leave_consumer(key)
            if not valid:
                if key in self.waiting:
                    self.waiting.pop(key, None)
                    self._ledger.cancel_pending(key, 'obsolete')
                task = self.running.get(key)
                if task is not None:
                    task.cancelled.set()
                    self._ledger.cancel_running(key, 'obsolete')
                else:
                    self.clients.pop(key, None)
        if not self.running and not self.waiting:
            self.cancel_timer.stop()

    def request(self, item):
        if self.closed.is_set():
            return
        archive = tuple(item._prism_source)
        suffix = Path(item.filename or '').suffix or '.mp4'
        # No filesystem/SQLite access on this path. Source stat/validation is
        # done inside the worker; archive tuple identifies concurrent clients.
        key = (archive, suffix)
        if getattr(item, '_play_preparation_key', None) == key:
            # The same item asks again: the job that is already queued or
            # running serves it as well.
            self._ledger.accounting.coalesced(reason='already_requested')
            return
        token = getattr(item, '_play_preparation_token', 0)+1
        item._play_preparation_token, item._play_preparation_key = token, key
        scene = item.scene()
        shared = key in self.running or key in self.clients
        self.clients.setdefault(key, []).append(
            (weakref.ref(item), token, weakref.ref(scene) if scene else None,
             getattr(scene, 'project_id', None)))
        self._ledger.join_consumer(key)
        if shared:
            # The job already exists: this client rides along with it.
            self._ledger.accounting.coalesced(reason='already_requested')
        else:
            self._ledger.submit(key)
        if key not in self.running:
            self.waiting[key] = (archive, suffix)
        self.scopes.setdefault(key, project_scope_key(archive[0]) if archive else None)
        while len(self.waiting) > MAX_PENDING:
            oldest, _ = self.waiting.popitem(last=False)
            self._ledger.cancel_pending(oldest, 'queue_full')
            for ref, token, _scene, _project in self.clients.pop(oldest, []):
                self._ledger.leave_consumer(oldest)
                client = ref()
                if client is not None and not sip.isdeleted(client) and getattr(client, '_play_preparation_token', None) == token:
                    client._play_preparation_key = None
            self.scopes.pop(oldest, None)
        self.cancel_timer.start()
        self.pump()

    def pump(self):
        self._ledger.prune()
        while not self.closed.is_set() and self.waiting and len(self.running) < 2:
            key, (archive, suffix) = self.waiting.popitem(last=False)
            task = MaterializeVideo(key, archive, suffix, self.closed,
                                    scope_for(self.scopes.get(key)),
                                    job=self._ledger.job(key))
            self.running[key] = task
            task.signals.done.connect(self.done)
            self.pool.start(task)

    @QtCore.pyqtSlot(object, object, object)
    def done(self, key, path, error):
        task = self.running.pop(key, None)
        if self.closed.is_set():
            self._ledger.finish(key, discarded='shutdown')
            return
        # A new playback client can arrive after every previous client cancelled
        # but before that worker acknowledged cancellation. Retry its still-valid
        # request rather than delivering the obsolete worker's failure.
        if error and task is not None and task.cancelled.is_set():
            clients = self.clients.get(key, ())
            valid = [client for client in clients if self.client_valid(client, key)]
            for _dropped in range(len(clients) - len(valid)):
                self._ledger.leave_consumer(key)
            self.clients[key] = valid
            if valid:
                self._ledger.retry(key, 'cancelled_retry')
                self.waiting[key] = key
                self.pump()
                return
        served = 0
        for ref, token, scene_ref, project in self.clients.pop(key, []):
            self._ledger.leave_consumer(key)
            item = ref()
            if item is None or sip.isdeleted(item):
                continue
            if getattr(item, '_play_preparation_token', None) != token:
                continue
            item._play_preparation_key = None
            scene = scene_ref() if scene_ref else None
            if ((scene_ref is not None and (scene is None or sip.isdeleted(scene)))
                    or item.scene() is not scene
                    or getattr(scene, 'project_id', None) != project
                    or getattr(scene, '_disposed', False)
                    or tuple(item._prism_source or ()) != key[0]):
                continue
            from prism.item_lifecycle import is_deleted
            if is_deleted(item):
                continue
            if error:
                from prism.performance_trace import record
                record('video.materialize_failed', error=error)
                continue
            item._video_source_path = path
            item._video_url = QtCore.QUrl.fromLocalFile(path)
            item._thumbnail_request_key = None
            item._start_playback()
            served += 1
        if error:
            self._ledger.finish(key, failed=error)
        elif served:
            self._ledger.finish(key, installed=True)
        else:
            # The work is done, but nobody wanted its result any more.
            self._ledger.finish(key, discarded='no_client')
        self.pump()


def request_materialization(item):
    global _materializer
    app = QtCore.QCoreApplication.instance()
    if app is None:
        return
    if _materializer is None or sip.isdeleted(_materializer):
        _materializer = VideoMaterializer(app)
    _materializer.request(item)

#: 预览规格：键的一部分。规格（目标尺寸/采样策略）变化即换新键，旧图自然失效。
SPEC = 'opening-640'


def _source_key(path, stat):
    """稳定源键：内嵌视频按内容指纹，引用/外部视频按 (路径, 版本)。

    - 内嵌视频每次打开都会提取到新的 uuid 临时路径，旧键 (path, size, mtime)
      因此永不命中；`source_store.stream_fingerprint` 命中时改用内容哈希——
      字节不变则跨次打开命中。
    - 引用/外部视频用 (str(path), (st_size, st_mtime_ns))，与旧语义一致。
    - SPEC 使预览规格变化自动失效。
    """
    from prism import source_store
    fingerprint = source_store.stream_fingerprint(str(path))
    if fingerprint is not None:
        return (fingerprint[0], fingerprint[1], SPEC)
    return (str(path), (stat.st_size, stat.st_mtime_ns), SPEC)


@measured('video.preview.decode', 32)
def opening_frame(path):
    from prism.fileio.video_pyav import av, OPEN_OPTIONS, frame_to_qimage
    first = None
    best_score = -1
    started = time.monotonic()
    with av.open(path, options=OPEN_OPTIONS, timeout=(5, 5)) as container:
        stream = next(s for s in container.streams if s.type == 'video')
        stream.codec_context.thread_count = 1
        info = dict(width=stream.codec_context.width, height=stream.codec_context.height)
        info['duration'] = container.duration/av.time_base if container.duration else None
        start_time = None
        decoded = 0
        for index, frame in enumerate(container.decode(stream)):
            decoded = index + 1
            if start_time is None:
                start_time = frame.time or 0
            ratio = min(1, 640/max(frame.width, frame.height))
            small = frame.reformat(width=max(1, round(frame.width*ratio)), height=max(1, round(frame.height*ratio)), format='rgb24')
            sample = frame.reformat(width=64, height=36, format='gray').to_ndarray()
            brightness = float(sample.mean())
            visible_fraction = float((sample > 48).mean())
            score = brightness+visible_fraction*30
            if first is None or score > best_score+1:
                first = frame_to_qimage(small)
                best_score = score
            # Reject the almost-black first instant of a fade-in as well as
            # blank frames. If the entire opening is dark, keep its clearest
            # sampled frame rather than returning the initial black frame.
            if brightness > 18 or visible_fraction > .2:
                info['decoded_frames'] = decoded
                return frame_to_qimage(small), info
            if index >= 179 or (frame.time is not None and frame.time-start_time >= 5) or time.monotonic()-started > 8:
                break
    if first is None:
        raise ValueError('No decodable video frame')
    info['decoded_frames'] = decoded
    return first, info


class Signals(QtCore.QObject):
    done = QtCore.pyqtSignal(object, object, object)


class Decode(QtCore.QRunnable):
    def __init__(self, key, closed=None, path=None, cancel=None, job=None):
        super().__init__()
        self.key = key
        self.path = path
        # The carrier has no owner on purpose; see ``MaterializeVideo``.
        self.signals = gui_signal_carrier(Signals(),
                                          QtCore.QCoreApplication.instance())
        self.closed = closed if closed is not None else threading.Event()
        #: Set when the project this decode belongs to is closed.
        self.cancel = cancel if cancel is not None else threading.Event()
        #: The queue bookkeeping of this job (plain Python, no Qt object).
        self.job = job

    def run(self):
        if self.closed.is_set() or self.cancel.is_set():
            # The job was dropped before a worker picked it up. The owner
            # already counted the cancel; nothing is decoded.
            return
        started = time.monotonic()
        accounting = self.job.accounting if self.job is not None else None
        if accounting is not None:
            wait_ms = None
            if self.job.submitted_at is not None:
                wait_ms = max(0.0, (started - self.job.submitted_at)*1000)
            accounting.started(wait_ms=wait_ms)
        dropped = False
        try:
            try:
                # ``path`` 是显式解码路径（目录/指纹键不再把路径放 key[0]）；
                # 旧调用方（测试/直接构造）只传 key 时回退 key[0]。
                source = self.path if self.path is not None else self.key[0]
                image, info = opening_frame(source)
            except Exception as error:
                image, info = None, {'error': type(error).__name__}
            if self.closed.is_set() or self.cancel.is_set():
                # The frame was decoded, but nobody wants it any more.
                dropped = True
            else:
                try:
                    self.signals.done.emit(self.key, image, info)
                except RuntimeError:
                    # QApplication teardown can destroy the receiver/signals while a
                    # native decoder is finishing. An exception escaping QRunnable.run
                    # aborts the process on Windows. Preserve unrelated exceptions.
                    if not sip.isdeleted(self.signals):
                        raise
        finally:
            if accounting is not None:
                accounting.service_done(service_ms=(time.monotonic()-started)*1000)
                if dropped:
                    # The result never reached its destination: it is discarded,
                    # not installed.
                    accounting.stale_discarded(state='ready', reason='result_dropped')
                    if self.job is not None:
                        self.job.dropped = True


class Thumbnails(QtCore.QObject):
    def __init__(self, app):
        super().__init__(app)
        self.pool = QtCore.QThreadPool(self)
        self.pool.setMaxThreadCount(2)
        self.waiting = OrderedDict()
        self.running = {}
        self.clients = {}
        self.task_scope = {}
        self.cache = OrderedDict()
        self.bytes = 0
        from prism.resource_budget import shared_cache_budget
        self._cache_budget = shared_cache_budget()
        self._cache_budget.register(self, category='video_preview', priority=20)
        #: The counters of this queue. They only count: they never decide
        #: what runs and never keep a Qt object.
        self._ledger = _QueueLedger(THUMBNAIL_QUEUE, limit=MAX_PENDING)
        self.closed = threading.Event()
        closed = self.closed
        self.destroyed.connect(lambda *_args: closed.set())
        app.aboutToQuit.connect(self.close)

    def close(self):
        if self.closed.is_set():
            return
        self.closed.set()
        for key in tuple(self.waiting):
            self._ledger.cancel_pending(key, 'shutdown')
        self.waiting.clear()
        for key in tuple(self.clients):
            for _entry in self.clients.get(key, ()):
                self._ledger.leave_consumer(key)
        self.clients.clear()
        self.task_scope.clear()
        for key in tuple(self.running):
            self._ledger.cancel_running(key, 'shutdown')
        self.cache.clear()
        self.bytes = 0
        self.pool.clear()
        self._ledger.accounting.rollup(force=True, reason='shutdown')

    # ── project scope ───────────────────────────────────────────

    def set_project_scope(self, key):
        """Re-attribute the preview cache to the project that is open.

        The opening frames belong to the project they were decoded from, so
        the cache is charged to that project and released with it.
        """
        self._cache_budget.register(self, category='video_preview', priority=20,
                                    scope=project_scope_key(key))
        self._trim_cache_to(0)

    def cancel_project(self, key):
        """Cancel the preview decodes of one project.

        Returns the number of clients that were dropped. A decode that is
        already running cannot be interrupted, but its result is discarded:
        no client of the closed project is left to receive it.
        """
        key = project_scope_key(key)
        if key is None:
            return 0
        cancelled = 0
        for request_key in tuple(self.clients):
            clients = self.clients.get(request_key, ())
            mine = [entry for entry in clients if entry[1] == key]
            if not mine:
                continue
            kept = [entry for entry in clients if entry[1] != key]
            if kept:
                self.clients[request_key] = kept
            else:
                self.clients.pop(request_key, None)
                if request_key in self.waiting:
                    self.waiting.pop(request_key, None)
                    self._ledger.cancel_pending(request_key, 'project_closed')
            for ref, _scope in mine:
                self._ledger.leave_consumer(request_key)
                client = ref()
                if client is not None and not sip.isdeleted(client):
                    client._thumbnail_request_key = None
            cancelled += len(mine)
        for request_key, task in list(self.running.items()):
            if self.task_scope.get(request_key) != key:
                continue
            task.cancel.set()
            # A running decode cannot be interrupted: it continues and only
            # its result is dropped.
            self._ledger.cancel_running(request_key, 'project_closed')
        return cancelled

    def _trim_cache_to(self, target):
        while self.cache and self.bytes > target:
            _, (old, _) = self.cache.popitem(last=False)
            self.bytes -= old.sizeInBytes()

    def request(self, item):
        if self.closed.is_set():
            return True  # Do not start a fallback decoder during shutdown.
        url = item._video_url
        if not url or not url.isLocalFile():
            return False
        try:
            path = Path(url.toLocalFile()).resolve()
            stat = path.stat()
        except OSError:
            return False
        key = _source_key(path, stat)
        if getattr(item, '_thumbnail_request_key', None) == key:
            # The same item asks again: the decode that is already queued or
            # running serves it as well.
            self._ledger.accounting.coalesced(reason='already_requested')
            return True
        item._thumbnail_request_key = key
        if key in self.cache:
            image, info = self.cache[key]
            self.cache.move_to_end(key)
            self._cache_budget.touch(self)
            self.apply(item, image, info)
            return True
        scope = source_project_scope(item)
        shared = key in self.running or key in self.clients
        self.clients.setdefault(key, []).append((weakref.ref(item), scope))
        self._ledger.join_consumer(key)
        if shared:
            # The decode already exists: this client rides along with it.
            self._ledger.accounting.coalesced(reason='already_requested')
        else:
            self._ledger.submit(key)
        if key not in self.running:
            self.waiting[key] = str(path)
        self.task_scope.setdefault(key, scope)
        while len(self.waiting) > MAX_PENDING:
            oldest, _ = self.waiting.popitem(last=False)
            self._ledger.cancel_pending(oldest, 'queue_full')
            for ref, _scope in self.clients.pop(oldest, []):
                self._ledger.leave_consumer(oldest)
                if (client := ref()) is not None and not sip.isdeleted(client):
                    client._thumbnail_request_key = None
            self.task_scope.pop(oldest, None)
        self.pump()
        return True

    def pump(self):
        if self.closed.is_set():
            return
        self._ledger.prune()
        while self.waiting and len(self.running) < 2:
            key, path = self.waiting.popitem(last=False)
            task = Decode(key, self.closed, path, job=self._ledger.job(key))
            self.running[key] = task
            task.signals.done.connect(self.done)
            self.pool.start(task)

    @QtCore.pyqtSlot(object, object, object)
    def done(self, key, image, info):
        self.running.pop(key, None)
        self.task_scope.pop(key, None)
        if self.closed.is_set():
            self._ledger.finish(key, discarded='shutdown')
            return
        if image is None:
            from prism.performance_trace import record
            record('video.preview.failed', reason=info.get('error', 'unknown'), fallback='qtmultimedia')
            self._ledger.finish(key, failed=info.get('error', 'unknown'))
        else:
            from prism.performance_trace import record
            record('video.preview.decoded', frames=info.get('decoded_frames', 0))
            self._ledger.finish(key, installed=True)
        if image is not None:
            self.cache[key] = (image, info)
            self.bytes += image.sizeInBytes()
            while self.bytes > CACHE_BYTES and self.cache:
                _, (old, _) = self.cache.popitem(last=False)
                self.bytes -= old.sizeInBytes()
            self._cache_budget.changed(self)
        for ref, _scope in self.clients.pop(key, []):
            self._ledger.leave_consumer(key)
            item = ref()
            if item is None or sip.isdeleted(item) or getattr(item, '_thumbnail_request_key', None) != key:
                continue
            if image is not None:
                self.apply(item, image, info)
            else:
                item._capture_thumbnail_qt()
        self.pump()

    @staticmethod
    def apply(item, image, info):
        item.prepareGeometryChange()
        item._video_dimensions = QtCore.QSize(info['width'], info['height'])
        item._video_metadata = info
        item.setPixmap(QtGui.QPixmap.fromImage(image))
        item._thumbnail_ready = True
        if item._control_bar:
            item._control_bar.reposition()
        item.update()
        if item.scene():
            item.scene()._invalidate_view_bounds()
            item.scene().video_preview_changed.emit(item)


def request(item):
    # Importing PyAV loads native codecs and can block for hundreds of ms.
    # The worker imports/probes it; an unavailable backend follows the same
    # asynchronous QtMultimedia fallback as an undecodable video.
    global _service
    app = QtCore.QCoreApplication.instance()
    if _service is None or sip.isdeleted(_service):
        _service = Thumbnails(app)
    return _service.request(item)


def set_project_scope(key):
    """Re-attribute the media caches to the project that is open.

    The opening frames belong to the project they were decoded from, so they
    are charged to that project and released when it is closed.
    """
    global _service
    app = QtCore.QCoreApplication.instance()
    if app is None:
        return None
    if _service is None or sip.isdeleted(_service):
        _service = Thumbnails(app)
    _service.set_project_scope(key)
    return _service


def cancel_project_media(key):
    """Cancel the video work of one project: materialization and previews."""
    cancelled = 0
    materializer = _materializer
    if materializer is not None and not sip.isdeleted(materializer):
        cancelled += materializer.cancel_project(key)
    service = _service
    if service is not None and not sip.isdeleted(service):
        cancelled += service.cancel_project(key)
    return cancelled
