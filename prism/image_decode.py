"""Read images through native Qt devices and deliver display pixels off-thread.

PyQt6 6.7 releases the GIL in QImageReader.read(), but not size(). A Python
QBuffer can reacquire it inside Qt's image-handler mutex. Passing a filename
lets Qt own its QFile and avoids that lock inversion.

**Project scope.** A display decode belongs to the project the item came from:
an item that was read from a ``.prism`` archive names that project in its
``_prism_source``. Closing a project cancels that project's pending decodes
(:meth:`DisplayDecoder.cancel_project`) instead of waiting for the application
to quit, and the temporary source a decode reads from is leased to the project
so its owner and its release point are explicit.
"""
from concurrent.futures import ThreadPoolExecutor
from collections import OrderedDict, deque
from contextlib import contextmanager
from contextvars import ContextVar
from pathlib import Path
import inspect
import logging
import tempfile
import threading
import time
import weakref

from prism.performance_trace import (QueueAccounting, current_context, measured,
                                    origin_action_context, record as trace_record,
                                    restore_context)
from prism.resource_budget import project_scope_key, scope_for
from prism.source_read_status import ReadFailure, read_failure

from PyQt6 import QtCore, QtGui, QtWidgets, sip
from prism.item_lifecycle import logical_item, is_record
from prism.item_lifecycle import is_deleted

logger = logging.getLogger(__name__)

#: The project scope a running decode belongs to. Set by the worker that runs
#: the decode, so the temporary source it opens is leased to that project.
_decode_scope = ContextVar('prism_decode_scope', default=None)

#: A decode that failed is retried after this pause. Without it a failing
#: item takes the slot of every other visible item on each round, because the
#: owner asks for the missing items in the same order every time -- and the
#: rest of the canvas is never decoded.
DECODE_RETRY_BACKOFF = 0.2


def source_project_scope(item):
    """The project a display request belongs to, or ``None``.

    An item read from a ``.prism`` archive names that project in its
    ``_prism_source``; an imported file belongs to no project.
    """
    source = getattr(item, '_prism_source', None)
    if isinstance(source, tuple) and source and isinstance(source[0], str):
        return project_scope_key(source[0])
    return None


def _remove_file(path):
    try:
        Path(path).unlink(missing_ok=True)
    except OSError:
        pass


@contextmanager
def native_reader(blob=None, path=None, scope=None):
    """Own the backing file until Qt closes it, including on Windows.

    When a project ``scope`` is given, the temporary source is leased to that
    project: the lease names the owner and is released as soon as the decode
    ends, so a half-read file never outlives the work that needed it.
    """
    temporary = None
    reader = None
    lease = None
    try:
        if blob is not None:
            temporary = tempfile.TemporaryDirectory(prefix='prism-image-decode-')
            path = str(Path(temporary.name) / 'image')
            Path(path).write_bytes(blob)
            if scope is not None:
                # The size is known here, by the writer that just wrote the
                # file: the rollup then reads a counter instead of stat-ing
                # the file every time it is written.
                lease = scope.lease('decode_temp', path,
                                    lambda released: _remove_file(released.resource_id),
                                    size_bytes=len(blob))
        reader = QtGui.QImageReader(str(path or ''))
        reader.setAutoTransform(True)
        yield reader
    finally:
        if reader is not None:
            # setDevice deletes the internally owned QFile before unlinking.
            try:
                reader.setDevice(None)
            except Exception:
                logger.debug('Could not release the image device', exc_info=True)
        if lease is not None:
            try:
                lease.release()
            except Exception:
                logger.debug('Could not release the decode lease', exc_info=True)
        if temporary is not None:
            try:
                temporary.cleanup()
            except Exception:
                # The image was already decoded. A temporary file that
                # Windows still holds must not throw the pixels away.
                logger.debug('Could not remove the decode temporary directory',
                             exc_info=True)


@measured('image.decode', 32)
def decode(blob=None, path=None, archive=None, max_side=None):
    """Return QImage and original dimensions; never construct a QPixmap."""
    if blob is None and not path and archive:
        from prism.source_store import read_archive
        blob = read_archive(archive[:2])
        if not blob and len(archive) > 2:
            blob = None
            path = archive[2]
    if blob is None and not path:
        return None
    with native_reader(blob=blob, path=path, scope=_decode_scope.get()) as reader:
        size = reader.size()
        rotated = bool(reader.transformation().value & 4)
        if max_side and size.isValid() and max(size.width(), size.height()) > max_side:
            scaled = size.scaled(max_side, max_side, QtCore.Qt.AspectRatioMode.KeepAspectRatio)
            reader.setScaledSize(QtCore.QSize(max(1, scaled.width()), max(1, scaled.height())))
        image = reader.read()
        if image.isNull():
            return None
        if not size.isValid() or size.isEmpty():
            size = image.size()
        elif rotated:
            size.transpose()
        if max_side and max(image.width(), image.height()) > max_side:
            scaled = image.size().scaled(max_side, max_side, QtCore.Qt.AspectRatioMode.KeepAspectRatio)
            image = image.scaled(QtCore.QSize(max(1, scaled.width()), max(1, scaled.height())),
                QtCore.Qt.AspectRatioMode.IgnoreAspectRatio,
                QtCore.Qt.TransformationMode.SmoothTransformation)
        return image, size


def decode_thumbnail(value, original_size, snapshot, max_side):
    """Worker-only native memory decoding of bounded JPEG/PNG previews."""
    import base64
    import io
    from PIL import Image
    from prism.image_pyramid import decode_display
    try:
        blob = base64.b64decode(value[2], validate=True)
        # Inspect dimensions before allocating Qt pixels. fromData uses Qt's
        # own memory device, avoiding both Python QBuffer callbacks and temp
        # file creation for every tiny preview.
        with Image.open(io.BytesIO(blob)) as header:
            valid = header.format in ('JPEG', 'PNG') and header.size == (value[0], value[1])
        if valid:
            image = QtGui.QImage.fromData(blob)
            if not image.isNull() and image.size() == QtCore.QSize(value[0], value[1]):
                from prism.thumbnail_cache import make_preview
                image = make_preview(image, max_side)
                return image, QtCore.QSize(*original_size)
    except (ValueError, TypeError, OSError):
        pass
    return decode_display(*snapshot, max_side=max_side)


def source_snapshot(item):
    archive = item._prism_source
    if archive and item.filename:
        archive = (*archive[:2], item.filename)
    return item._source_blob, item._source_path, archive


#: Whether the accounting interface can say that a result was **not**
#: usable.  Without the flag a failed decode would be counted as a result
#: that is ready for installation, and the ready gauge would keep counting a
#: result that never reaches the screen.
_SERVICE_DONE_HAS_USABLE = 'usable' in inspect.signature(
    QueueAccounting.service_done).parameters

#: What a diagnostic call returns when it failed.  It is not ``None``,
#: because a successful diagnostic call may return ``None`` as well.
_DIAG_FAILED = object()


class _DecodeTask:
    """One decode request and the single owner of its accounting.

    A decode is offered to the queue, may be given up before it runs, may
    run, and may deliver a result or fail.  Several of those steps move a
    gauge, and two of them can happen at the same moment: the worker that
    runs the decode and the party that gives up on it.  This record is
    where the two meet.

    Whoever claims a transition first owns it; the other one finds the task
    settled and does nothing.  That is what makes one task one settlement:
    the running slot is released once, by one party, and never by clamping
    a gauge at zero to hide a second release.
    """

    __slots__ = ('lock', 'state', 'result_ready', 'ready_released',
                 'cancel_requested', 'cancel_reason', 'origin_action_id')

    def __init__(self):
        self.lock = threading.Lock()
        #: ``pending`` (offered, no worker yet), ``running`` (a worker owns
        #: it) or ``done`` (settled: the running slot was released and the
        #: task is not counted again).
        self.state = 'pending'
        #: True when the worker produced a result that waits for the GUI.
        #: Only then may the ready gauge be released.
        self.result_ready = False
        #: True after the ready gauge was released, by an install or by a
        #: drop.
        self.ready_released = False
        #: Set when a party gave up on a task that had not started yet.
        #: The worker settles it when it takes the task.
        self.cancel_requested = False
        self.cancel_reason = None
        #: The action the request was offered under.  A worker thread does
        #: not inherit the context of the thread that asked for the decode,
        #: so without this the decode's span cannot say which user action
        #: caused it.
        self.origin_action_id = None

    def claim_ready_release(self):
        """Take the release of the ready gauge.  True for the first claimant."""
        with self.lock:
            if not self.result_ready or self.ready_released:
                return False
            self.ready_released = True
            return True


class _DecodeCall:
    """The work of one decode request, together with its settlement.

    The callable carries the settlement record of its own request, so the
    worker that runs it releases the running slot of exactly that task.
    The signature of ``_run_measured`` does not change.
    """

    __slots__ = ('_call', 'task')

    def __init__(self, call, task):
        self._call = call
        self.task = task

    def __call__(self):
        return self._call()


def _task_of(token):
    """The settlement record of a request token, or ``None``.

    Older tokens -- and the ones a test builds by hand -- carry no record.
    They are handled exactly as they were before.
    """
    try:
        task = token[6]
    except (IndexError, TypeError):
        return None
    return task if isinstance(task, _DecodeTask) else None


class DisplayDecoder(QtCore.QObject):
    completed = QtCore.pyqtSignal(object, object)

    def __init__(self, app):
        super().__init__(app)
        self.pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix='PrismImageDecode')
        self.closed = False
        self.requests = OrderedDict()
        self.max_pending = 8
        # What this queue was offered, what a worker began, what reached the
        # screen and what never made it. The accounting only counts: it never
        # decides, never blocks the GUI thread and never holds a Qt object.
        self._accounting = QueueAccounting('image_decode', limit=self.max_pending)
        self.waiting_owners = weakref.WeakSet()
        self.deferred_full = 0
        self._priority_round = weakref.WeakKeyDictionary()
        self._last_deferred_report = 0.0
        self.ready = deque()
        self.install_timer = QtCore.QTimer(self)
        self.install_timer.setObjectName('images.install_display_levels')
        self.install_timer.setSingleShot(True)
        self.install_timer.setInterval(16)
        self.install_timer.timeout.connect(self._install_ready)
        self.repaint_timer = QtCore.QTimer(self)
        self.repaint_timer.setObjectName('images.repaint_owners')
        self.repaint_timer.setSingleShot(True)
        self.repaint_timer.setInterval(16)
        self.repaint_timer.timeout.connect(self._repaint_waiting)
        self._busy_owners = weakref.WeakSet()
        self._busy_items = weakref.WeakKeyDictionary()
        self.busy_timer = QtCore.QTimer(self)
        self.busy_timer.setSingleShot(True)
        self.busy_timer.setInterval(max(250, int(DECODE_RETRY_BACKOFF * 1000)))
        self.busy_timer.timeout.connect(self._retry_busy_sources)
        self.sample_timer = QtCore.QTimer(self)
        self.sample_timer.setObjectName('images.sample_pending')
        self.sample_timer.setInterval(2000)
        self.sample_timer.timeout.connect(self._sample_pending)
        self.sample_timer.start()
        self.max_pending_seen = 0
        #: How often each diagnostic call site failed.  The counter is
        #: bounded by the number of call sites and the last error is kept
        #: short, so the degradation marker stays bounded.
        self._diag_failures = {}
        self._diag_last_error = None
        self.completed.connect(self._queue_complete, QtCore.Qt.ConnectionType.QueuedConnection)
        app.aboutToQuit.connect(self.shutdown)

    def _diag(self, name, call, *args, **kwargs):
        """Run one diagnostic call in isolation.

        A diagnostic failure must never break the work it describes: the
        decode still runs, the pixels still reach the item, the request
        still ends.  The failure is counted per call site and the last
        error is kept short, so the marker stays bounded.  The failure is
        reported through the standard logging channel and never through the
        module that just failed: a broken trace module must not be called
        again to report that it is broken.

        Business failures are **not** handled here.  A decode that raises
        keeps its own error path, and its exception is not swallowed.
        """
        try:
            return call(*args, **kwargs)
        except Exception as exc:        # noqa: BLE001 - isolated on purpose
            self._diag_failures[name] = self._diag_failures.get(name, 0) + 1
            try:
                detail = repr(exc)
            except Exception:            # noqa: BLE001
                detail = '<unprintable>'
            self._diag_last_error = ('%s: %s' % (name, detail))[:200]
            try:
                logger.warning('image_decode: diagnostic %s failed', name,
                               exc_info=True)
            except Exception:            # noqa: BLE001
                pass
            return _DIAG_FAILED

    def _repaint_waiting(self):
        owners = list(self.waiting_owners)
        self.waiting_owners.clear()
        for owner in owners:
            if not is_deleted(owner) and owner.isVisible():
                refresh = getattr(owner, 'request_display_refresh', None)
                if refresh is not None:
                    owner.queue_visible_details()
                    getattr(owner, 'wake_display_queue', refresh)()
                    continue
                renderer = getattr(owner, '_overview_renderer', None)
                if renderer is not None:
                    renderer.queue_visible_details()
                target = owner.viewport() if hasattr(owner, 'viewport') else owner
                target.update()

    def _retry_busy_sources(self):
        if self.closed:
            return
        owners = list(self._busy_owners)
        self._busy_owners.clear()
        self.waiting_owners.update(owners)
        items = list(self._busy_items.items())
        self._busy_items.clear()
        for item, (scene_ref, source) in items:
            if (not is_deleted(item) and scene_ref() is not None
                    and item.scene() is scene_ref() and source_snapshot(item) == source):
                item.update()
        self._repaint_waiting()

    def _sample_pending(self):
        """Record how many display decodes are waiting.

        The queue is capped at ``max_pending``: a path that queued every
        visible record at once would show up here as a pending count above
        the cap. That is the difference between a paged decode and a full
        decode of the project, so it is sampled continuously instead of
        only when something goes wrong.
        """
        pending = self.pending_count()
        if pending > self.max_pending_seen:
            self.max_pending_seen = pending
        try:
            from prism.performance_trace import record
        except Exception:  # pragma: no cover - the trace module is optional
            return
        self._diag('record_pending', record, 'image_decode_pending',
                   pending=pending, max_pending=self.max_pending,
                   max_pending_seen=self.max_pending_seen,
                   waiting_owners=len(self.waiting_owners),
                   deferred_full=self.deferred_full,
                   deferred_unit='request_attempts',
                   deferred_cumulative=True,
                   diag_failures=dict(self._diag_failures),
                   diag_last_error=self._diag_last_error)

    def _defer_full(self, owner):
        self.waiting_owners.add(owner)
        self.deferred_full += 1
        now = time.monotonic()
        if now - self._last_deferred_report >= 1.0:
            self._last_deferred_report = now
            self._diag('record_deferred', trace_record,
                       'image_decode_deferred', reason='queue_full_deferred',
                       deferred_full=self.deferred_full,
                       unit='request_attempts', cumulative=True,
                       pending=len(self.requests), limit=self.max_pending)

    def shutdown(self):
        if self.closed:
            return
        if self.deferred_full:
            self._sample_pending()
        self.closed = True
        self.repaint_timer.stop()
        self.busy_timer.stop()
        self._busy_owners.clear()
        self._busy_items.clear()
        self.install_timer.stop()
        self.sample_timer.stop()
        # A result that waits for the GUI is dropped with the queue.  The
        # ready gauge of a task is released once, by whoever gets there
        # first: the queue that is closed or the install that runs later.
        for token, _result in self.ready:
            task = _task_of(token)
            if task is not None and task.claim_ready_release():
                self._diag('stale_discarded', self._accounting.stale_discarded,
                           state='ready', reason='shutdown')
        self.ready.clear()
        self.waiting_owners.clear()
        for token, future in self.requests.values():
            task = _task_of(token)
            if task is not None:
                self._give_up(task, future, 'shutdown')
                if task.claim_ready_release():
                    self._diag('stale_discarded',
                               self._accounting.stale_discarded,
                               state='ready', reason='shutdown')
            item = token[0]()
            if (item is not None and not is_deleted(item)
                    and item._display_decode_request is token):
                item._display_decode_request = None
        self.requests.clear()
        self._diag('rollup', self._accounting.rollup, force=True, reason='shutdown')
        self.pool.shutdown(wait=False, cancel_futures=True)

    # ── project scope ───────────────────────────────────────────

    def cancel_project(self, key):
        """Cancel every pending display decode that belongs to one project.

        Returns the number of requests that were dropped. A decode that is
        already running cannot be interrupted, but its result is discarded:
        the request is gone and the item no longer waits for it, so nothing
        is installed into the project that was closed.
        """
        key = project_scope_key(key)
        if key is None:
            return 0
        doomed = []
        for request_key, (token, future) in list(self.requests.items()):
            if token[5] != key:
                continue
            doomed.append((request_key, token, future))
        total = len(doomed)
        for index, (request_key, token, future) in enumerate(doomed):
            self.requests.pop(request_key, None)
            item = token[0]()
            if (item is not None and not is_deleted(item)
                    and getattr(item, '_display_decode_request', None) is token):
                item._display_decode_request = None
            # The work that could not be stopped is recorded once, with the
            # last request of this project.  The settlement record decides
            # whether the request was still pending or already running: a
            # decode that already runs cannot be stopped, and it is reported
            # as such, never as an instant cancel.
            task = _task_of(token)
            if task is not None:
                self._give_up(task, future, 'project_closed',
                              remaining=total if index == total - 1 else None)
        if total:
            self._diag('rollup', self._accounting.rollup, force=True,
                       reason='project_closed')
        return total

    def pending_count(self, key=None):
        """The number of pending display decodes, optionally of one project."""
        if key is None:
            return len(self.requests)
        key = project_scope_key(key)
        return sum(1 for token, _future in self.requests.values() if token[5] == key)

    @staticmethod
    def _run_scoped(call, scope):
        """Run one decode with its project scope visible to the decoder."""
        if scope is None:
            return call()
        var = _decode_scope.set(scope)
        try:
            return call()
        finally:
            _decode_scope.reset(var)

    @staticmethod
    def _needed(token):
        owner = token[4]() if token[4] else None
        item = token[0]()
        if token[4] is None:
            return True
        return (owner is not None and not is_deleted(owner) and item is not None
                and not is_deleted(item) and owner.isVisible() and owner.needs_display(item))

    def _cancel(self, key, reason='not_needed'):
        """Drop one request. Returns ``True`` when it was really cancelled."""
        token, future = self.requests[key]
        task = _task_of(token)
        if task is not None:
            with task.lock:
                if task.state != 'pending':
                    # It runs already: it cannot be stopped and it keeps its
                    # slot, so the caller has to offer the queue another item.
                    return False
                if not future.cancel():
                    return False
                task.state = 'done'
        elif not future.cancel():
            return False
        self.requests.pop(key, None)
        item = token[0]()
        if item is not None and not is_deleted(item) and item._display_decode_request is token:
            item._display_decode_request = None
        # The callable never ran, so the work was still pending.
        self._diag('cancelled', self._accounting.cancelled,
                   state='pending', reason=reason)
        return True

    def _settle_success(self, task, service_start):
        """The decode produced a result: the worker releases the slot.

        The worker that ran the decode owns the running slot and releases
        it here.  A party that gave up on the task settled it already, and
        then the worker releases nothing.
        """
        service_ms = (time.perf_counter()-service_start)*1000
        with task.lock:
            if task.state == 'done':
                return
            task.state = 'done'
            outcome = self._diag('service_done', self._accounting.service_done,
                                 service_ms=service_ms)
            if outcome is not _DIAG_FAILED:
                task.result_ready = True

    def _settle_failure(self, task, service_start):
        """The decode failed: the worker releases the slot.

        ``failed`` is the result count and ``service_done`` is the release
        of the running slot.  They are two different things and each of
        them happens once.  The failure is counted without a state, so it
        never moves a gauge: only the party that owns the slot may do that.
        """
        service_ms = (time.perf_counter()-service_start)*1000
        with task.lock:
            if task.state == 'done':
                return
            task.state = 'done'
            if _SERVICE_DONE_HAS_USABLE:
                self._diag('failed', self._accounting.failed,
                           reason='decode_failed')
                self._diag('service_done', self._accounting.service_done,
                           service_ms=service_ms, usable=False)
            else:
                # The interface cannot say that the result was unusable. The
                # result is counted as ready and released again right away,
                # so the ready gauge does not keep a result that will never
                # be installed.
                self._diag('service_done', self._accounting.service_done,
                           service_ms=service_ms)
                self._diag('failed', self._accounting.failed,
                           reason='decode_failed', state='ready')

    def _give_up(self, task, future, reason, remaining=None):
        """Give up one request and release what it still holds.

        The worker that runs the decode and the party that gives up on it
        meet in the task record.  Whoever gets there first owns the
        transition; the other one finds the task settled and does nothing.

        A request that is already running cannot be stopped: it keeps
        running and only its result is dropped.  The party that gives it up
        releases the running slot, because the worker must not release it
        a second time when it ends.  The price of that choice is written
        down in the changes: an abandoned decode is not counted as
        completed and its service time is not recorded.
        """
        with task.lock:
            if task.state == 'done':
                return False
            task.cancel_reason = reason
            if task.state == 'running':
                task.state = 'done'
                owner = 'running'
            elif future.cancel():
                # The work never ran: the slot it held was the pending one.
                task.state = 'done'
                owner = 'pending'
            else:
                # A worker is about to take it. The worker owns the slot it
                # takes and settles the task itself.
                task.cancel_requested = True
                owner = None
        if owner == 'running':
            self._diag('cancelled', self._accounting.cancelled,
                       state='running', reason=reason, remaining=remaining)
        elif owner == 'pending':
            self._diag('cancelled', self._accounting.cancelled,
                       state='pending', reason=reason, remaining=remaining)
        return owner is not None

    def _run_measured(self, call, scope, submit_time):
        """Run one decode and report the queue wait and the service time.

        The wait is the real queue wait -- from the moment the work was
        offered to the moment a worker began it -- and never the service
        time. A decode that fails is still work that was completed.

        The worker owns the running slot of the task it runs: it releases
        the slot when the decode ends, whether it succeeded or failed.  A
        party that gives up on the task settles it first, and the worker
        then finds the task settled and releases nothing.  Neither of them
        can release the slot twice, and the slot is never clamped at zero
        to hide a second release.
        """
        task = getattr(call, 'task', None)
        if task is None:
            task = _DecodeTask()
        worker_start = time.perf_counter()
        with task.lock:
            if task.state != 'pending':
                # The request was given up before this worker took it.
                return None
            task.state = 'running'
            self._diag('started', self._accounting.started,
                       wait_ms=(worker_start-submit_time)*1000)
            if task.cancel_requested:
                # The request was given up while it was handed to a worker.
                # The worker owns the slot it just took, so it settles the
                # task itself.
                task.state = 'done'
                self._diag('cancelled', self._accounting.cancelled,
                           state='running', reason=task.cancel_reason)
                return None
        service_start = time.perf_counter()
        try:
            # The decode runs on a worker thread that inherited no context, so
            # the action id captured where the request was offered is restored
            # here -- and only here, so the decode's span says what caused it.
            with restore_context(origin_action_context(task.origin_action_id)):
                result = self._run_scoped(call, scope)
        except BaseException:
            self._settle_failure(task, service_start)
            raise
        if result is None:
            # The decode ran and produced no image. That is a failure, not a
            # result that was thrown away later -- and the "ready" gauge must
            # not keep counting a result that will never be installed.
            self._settle_failure(task, service_start)
            return result
        self._settle_success(task, service_start)
        return result

    def request(self, item, max_side=None, owner=None):
        item = logical_item(item)
        if self.closed:
            return
        if getattr(item, '_display_decode_request', None) is not None:
            # The request is merged into the one that is already running.
            self._diag('coalesced', self._accounting.coalesced,
                       reason='already_pending')
            if owner is not None:
                self.waiting_owners.add(owner)
            return
        # A decode that failed is retried, but not at once. The owner asks
        # for the missing items in the same order on every round, so an item
        # that keeps failing would take the slot of every other visible item
        # and nothing else would ever be decoded.
        failed_at = getattr(item, '_display_decode_failed_at', None)
        if failed_at is not None and time.monotonic()-failed_at < DECODE_RETRY_BACKOFF:
            # The same work will be offered again once the pause is over.
            self._diag('retried', self._accounting.retried,
                       reason='decode_retry_backoff')
            if owner is not None:
                self.waiting_owners.add(owner)
            return
        task = _DecodeTask()
        # The worker thread does not inherit this thread's context, so the
        # action that asked for the decode has to travel with the task.
        task.origin_action_id = current_context().origin_action_id
        token = (weakref.ref(item), weakref.ref(item.scene()), source_snapshot(item), max_side,
                 weakref.ref(owner) if owner is not None else None,
                 source_project_scope(item), task)
        if not self._needed(token):
            # Nothing wants this result any more: it is never offered.
            self._diag('rejected', self._accounting.rejected, reason='not_needed')
            return
        for key, (pending, _) in list(self.requests.items()):
            if not self._needed(pending):
                self._cancel(key, reason='not_needed')
        if len(self.requests) >= self.max_pending:
            if owner is not None:
                self.waiting_owners.add(owner)
            priority = getattr(owner, 'display_priority', None)
            queued = [(key, pending[0]()) for key, (pending, future) in self.requests.items()
                      if pending[4] is not None and pending[4]() is owner
                      and not future.running() and not future.done()]
            # Running decodes finish normally; replace only work not started yet.
            if priority is None or not queued:
                self._diag('rejected', self._accounting.rejected,
                           reason='queue_full')
                return
            key, farthest = max(queued, key=lambda pair: priority(pair[1]))
            if priority(item) >= priority(farthest):
                self._diag('rejected', self._accounting.rejected,
                           reason='queue_full')
                return
            if not self._cancel(key, reason='preempted'):
                self._diag('rejected', self._accounting.rejected,
                           reason='queue_full')
                return
        item._display_decode_request = token
        from prism.image_pyramid import decode_display
        thumbnail = getattr(item, '_display_thumbnail', None)
        size = item._image_size
        # Zoom/crop may request a large cache level for a tiny original.
        # A saved preview covering every original pixel is sufficient even
        # then; do not reopen the archive merely because the screen is large.
        original_side = max(size.width(), size.height())
        required_side = min(max_side, original_side) if max_side and original_side > 0 else max_side
        if (required_side is not None and required_side <= 192 and isinstance(thumbnail, list)
                and len(thumbnail) == 3 and type(thumbnail[0]) is int and type(thumbnail[1]) is int
                and 0 < thumbnail[0] <= 192 and 0 < thumbnail[1] <= 192
                and max(thumbnail[:2]) >= required_side
                and isinstance(thumbnail[2], str) and len(thumbnail[2]) <= 220000):
            call = _DecodeCall(
                lambda: decode_thumbnail(tuple(thumbnail), (size.width(), size.height()),
                                        token[2], max_side), task)
        else:
            call = _DecodeCall(
                lambda: decode_display(*token[2], max_side=max_side), task)
        # The temporary source this decode opens is leased to the project the
        # item came from, so its owner and release point stay explicit.
        submit_time = time.perf_counter()
        # The work is counted as offered before it is handed to a worker: a
        # worker may begin it before this thread reaches the next line, and
        # the offer must be the earlier of the two.
        # The count is a diagnostic: it never decides whether the work is
        # handed to a worker.
        self._diag('submitted', self._accounting.submitted)
        future = self.pool.submit(self._run_measured, call, scope_for(token[5]),
                                  submit_time)
        self.requests[id(token)] = (token, future)
        if owner is not None and len(self.requests) >= self.max_pending:
            self.waiting_owners.add(owner)
        future.add_done_callback(lambda done: self._deliver(token, done))

    def _deliver(self, token, future):
        if future.cancelled():
            # Already counted where the request was cancelled.
            return
        if self.closed:
            # The result is dropped: the application is going away.  The
            # ready gauge of the task is released once, by whoever gets
            # there first.
            task = _task_of(token)
            if task is not None and task.claim_ready_release():
                self._diag('stale_discarded', self._accounting.stale_discarded,
                           state='ready', reason='closed')
            return
        try:
            result = future.result()
        except Exception as error:
            self._diag('decode_failed_log', logger.debug,
                       'Could not decode display image', exc_info=True)
            result = read_failure(error)
        try:
            self.completed.emit(token, result)
        except RuntimeError:
            pass  # QApplication has already destroyed this dispatcher.

    @QtCore.pyqtSlot(object, object)
    def _queue_complete(self, token, result):
        if self.closed:
            # Counted by the shutdown path.
            return
        self.ready.append((token, result))
        if not self.install_timer.isActive():
            self.install_timer.start(0 if token[3] is not None and token[3] <= 192 else 16)

    def _install_ready(self):
        started = time.perf_counter()
        while self.ready:
            self._complete(*self.ready.popleft())
            if (time.perf_counter()-started)*1000 >= 4:
                break
        if self.ready and not self.closed:
            self.install_timer.start(0)
        if self.waiting_owners and not self.closed:
            self.repaint_timer.stop()
            self._repaint_waiting()

    def _discard(self, reason, task=None):
        """A finished result is dropped: it is no longer wanted."""
        if task is None or task.claim_ready_release():
            self._diag('stale_discarded', self._accounting.stale_discarded,
                       state='ready', reason=reason)
        self._diag('rollup', self._accounting.rollup)

    @QtCore.pyqtSlot(object, object)
    def _complete(self, token, result):
        task = _task_of(token)
        self.requests.pop(id(token), None)
        if self.waiting_owners and not self.closed and not self.repaint_timer.isActive():
            self.repaint_timer.start()
        for key, (pending, _) in list(self.requests.items()):
            if not self._needed(pending):
                self._cancel(key, reason='not_needed')
        item, scene = token[0](), token[1]()
        if item is None or is_deleted(item):
            self._discard('item_deleted', task)
            return
        if getattr(item, '_display_decode_request', None) is not token:
            # The request was cancelled or superseded while it ran.
            self._discard('not_current', task)
            return
        item._display_decode_request = None
        if not self._needed(token):
            self._discard('not_needed', task)
            return
        if scene is None or is_deleted(scene) or item.scene() is not scene:
            self._discard('scene_changed', task)
            return
        if source_snapshot(item) != token[2]:
            item.update()
            self._discard('source_changed', task)
            return
        if result is None or isinstance(result, ReadFailure):
            # The decode failed: the worker that ran it already counted it
            # as ``failed``. It is not a result that was thrown away.
            failure = result if isinstance(result, ReadFailure) else ReadFailure('decode_failed', False, '')
            item._source_read_error = failure
            item._display_decode_failed = not failure.retryable
            item._display_decode_failed_source = token[2]
            item._display_decode_failed_at = time.monotonic()
            self._diag('read_unavailable', trace_record, 'image.source_unavailable',
                       reason=failure.kind, retryable=failure.retryable)
            if failure.retryable:
                owner = token[4]() if token[4] else None
                if owner is not None and not is_deleted(owner):
                    self._busy_owners.add(owner)
                elif len(self._busy_items) < self.max_pending:
                    self._busy_items[item] = (token[1], token[2])
                if not self.busy_timer.isActive():
                    self.busy_timer.start()
            self._diag('rollup', self._accounting.rollup)
            return
        image, size = result
        item._source_read_error = None
        item._display_decode_failed = False
        item._display_decode_failed_at = None
        item._grayscale_cache = None
        if item._image_size != size:
            item.prepareGeometryChange()
            item._image_size = size
            if item.crop.isEmpty():
                item.reset_crop()
        if token[3] is not None and token[3] > 192:
            from prism.lod import cache
            display = QtGui.QPixmap.fromImage(image)
            if item._preview_pixmap is None:
                item._preview_pixmap = display.scaled(192, 192, QtCore.Qt.AspectRatioMode.KeepAspectRatio,
                                                      QtCore.Qt.TransformationMode.SmoothTransformation)
            cache().store(item, token[3], display, notify=False)
        elif token[3] is not None:
            item._preview_pixmap = QtGui.QPixmap.fromImage(image)
        else:
            target = item.graphics_item() if is_record(item) else item
            QtWidgets.QGraphicsPixmapItem.setPixmap(target, QtGui.QPixmap.fromImage(image))
            from prism.items import _schedule_budget_check
            _schedule_budget_check(scene)
        # The pixels are in the item now: the result reached its destination.
        # The counts are diagnostics: they never decide whether the item is
        # updated and whether the cache announces the new pixels.
        if task is None or task.claim_ready_release():
            self._diag('installed', self._accounting.installed)
        self._diag('rollup', self._accounting.rollup)
        item.update()
        from prism.lod import cache
        cache().track_preview(item, notify=False)
        cache().changed.emit(item)


def request_display(item, max_side=None, owner=None):
    if getattr(item, 'TYPE', None) in ('video', 'glb'):
        return  # Their embedded sources are not image files.
    item = logical_item(item)
    app = QtWidgets.QApplication.instance()
    if getattr(item, '_display_decode_failed', False):
        if getattr(item, '_display_decode_failed_source', None) == source_snapshot(item):
            return
        item._display_decode_failed = False
    if app is None or item.scene() is None:
        return
    dispatcher = getattr(app, '_prism_display_decoder', None)
    if dispatcher is None:
        dispatcher = app._prism_display_decoder = DisplayDecoder(app)
    if owner is not None and len(dispatcher.requests) >= dispatcher.max_pending:
        # A paint pass can visit thousands of records. Do not repeatedly
        # reprioritize/cancel the same eight jobs for every painted record;
        # completed jobs schedule the next visible batch through this owner.
        # Allow the real request path to replace unstarted, farther work,
        # once per owner per UI turn window. Existing requests still coalesce;
        # a paint pass cannot perform an O(visible * pending) priority sweep.
        if getattr(item, '_display_decode_request', None) is None:
            now = time.monotonic()
            last = dispatcher._priority_round.get(owner, float('-inf'))
            if (not callable(getattr(owner, 'display_priority', None))
                    or now - last < .016):
                dispatcher._defer_full(owner)
                return
            dispatcher._priority_round[owner] = now
    dispatcher.request(item, max_side, owner)


def cancel_project_decodes(key):
    """Cancel the pending display decodes of one project.

    The entry point for the project lifecycle: closing a project must not
    wait for the application to quit before its decodes stop.
    """
    app = QtWidgets.QApplication.instance()
    dispatcher = getattr(app, '_prism_display_decoder', None) if app is not None else None
    if dispatcher is None:
        return 0
    return dispatcher.cancel_project(key)
