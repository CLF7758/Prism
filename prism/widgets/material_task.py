"""Use Qt's owned thread pool for file IO and large texture processing."""
from collections import deque
from PyQt6 import QtCore
import threading
import time
import uuid
from prism.performance_trace import QueueAccounting, record, capture_context, restore_context
from prism.library_observability import event as library_event, run_bound
from prism.qt_thread_safety import gui_signal_carrier, weak_gui


class TaskSignals(QtCore.QObject):
    result = QtCore.pyqtSignal(object)
    error = QtCore.pyqtSignal(str)
    finished = QtCore.pyqtSignal()


class MaterialTask(QtCore.QRunnable):
    def __init__(self, function, obsolete=None, owner=None):
        super().__init__()
        self.function = function
        self.obsolete = obsolete
        # The carrier has no owner on purpose: it must outlive the page that
        # queued the work, so a late result cannot reach a destroyed page.
        # The page keeps the job alive until it finished, and that is where
        # the carrier is released -- on the GUI thread, never on a worker.
        #
        # It is owned by the application, though: PyQt6 delivers a queued
        # result through the carrier, so a carrier that dies with the page
        # takes the pending delivery down with it and the process dies with
        # an access violation.  The application outlives every page.
        self.signals = gui_signal_carrier(TaskSignals(),
                                          QtCore.QCoreApplication.instance())
        self.task_id = None
        self.queued_at = time.perf_counter()
        self.trace_context = capture_context()
        # -- queue accounting (optional; inert when the owner sets none) ----
        # ``wait_since`` is the monotonic time the work was submitted to the
        # queue, ``on_started``/``on_service`` let the owner count the two
        # worker transitions exactly once, on the thread that owns the count.
        self.wait_since = None
        self.on_started = None
        self.on_service = None

    def run(self):
        with restore_context(self.trace_context):self._run_bound()

    def _run_bound(self):
        started = time.perf_counter()
        record('material_task_start', task=self.task_id,
               queue_ms=round((started-self.queued_at)*1000, 3))
        if self.on_started is not None:
            wait_ms = None
            if self.wait_since is not None:
                wait_ms = (time.monotonic() - self.wait_since) * 1000.0
            self.on_started(wait_ms)

        def service_done():
            """The service ended. The owner counts it once, whoever calls."""
            if self.on_service is not None:
                self.on_service((time.perf_counter()-started)*1000)

        try:
            if self.obsolete is not None and self.obsolete():
                record('material_task_cancelled', task=self.task_id, reason='owner_superseded_before_work')
                return
            value = self.function()
            # Native IO already in progress cannot always be interrupted. Do not
            # publish its result after selection/project ownership has changed.
            if self.obsolete is not None and self.obsolete():
                record('material_task_cancelled', task=self.task_id, phase='result', reason='owner_superseded_after_work')
                return
            service_done()
            self.signals.result.emit(value)
        except Exception as error:
            library_event('material','background_task','failed',job_id=self.task_id,
                          error_type=type(error).__name__,reason=str(error))
            service_done()
            self.signals.error.emit(str(error))
        finally:
            record('material_task_work', task=self.task_id,
                   duration_ms=round((time.perf_counter()-started)*1000, 3))
            service_done()
            self.signals.finished.emit()


class _BatchItem:
    """One queued unit of material work and the state it reached."""

    __slots__ = ('key', 'value', 'status', 'generation', 'queued_at', 'started_at',
                 'job', 'queued_mono', 'acct')

    def __init__(self, key, value, generation):
        self.key = key
        self.value = value
        self.status = 'pending'
        self.generation = generation
        self.queued_at = time.perf_counter()
        self.started_at = None
        self.job = None
        # Queue accounting state of this item: queued -> started -> finished
        # -> settled. It decides which transition is still allowed, so a
        # cancelled item is never counted twice.
        self.queued_mono = time.monotonic()
        self.acct = 'queued'


class _Batch:
    """State of one submitted batch; two batches never share mutable state."""

    __slots__ = ('id', 'generation', 'items', 'pending', 'running', 'index', 'results',
                 'work', 'valid', 'key_of', 'cancelled', 'active', 'held', 'started_at',
                 'slice', 'slice_items', 'slice_started', 'slices', 'over_budget', 'counts',
                 'last_slice_ms', 'max_slice_ms', 'accounting', 'acct_lock','trace_context')

    def __init__(self, batch_id, generation):
        self.trace_context=capture_context()
        self.id = batch_id
        self.generation = generation
        self.items = []
        self.pending = deque()
        self.running = {}
        self.index = {}
        self.results = {}
        self.work = None
        self.valid = None
        self.key_of = None
        self.cancelled = False
        self.active = True
        self.held = False
        self.started_at = time.perf_counter()
        self.slice = 0
        self.slice_items = 0
        self.slice_started = 0.0
        self.slices = 0
        self.over_budget = 0
        self.counts = dict(completed=0, failed=0, cancelled=0)
        self.last_slice_ms = 0.0
        self.max_slice_ms = 0.0
        # Queue accounting for this batch plus the lock that keeps the worker
        # thread and the owning thread from counting one item twice.
        self.accounting = None
        self.acct_lock = threading.Lock()

    @property
    def settled(self):
        return self.counts['completed'] + self.counts['failed'] + self.counts['cancelled']


class MaterialBatchQueue(QtCore.QObject):
    """Cancelable, sharded and measurable queue of material work.

    The queue keeps a bounded window of jobs alive: at most ``chunk_size``
    items are in flight, so submitting 5000 materials never becomes 5000
    queued jobs. Work runs on the queue's own thread pool and every result
    is published on the thread that owns this object.

    Each batch owns a generation token. Cancelling a batch or submitting a
    new one bumps the token, so a result that arrives late is dropped
    instead of overwriting a newer selection. Between two slices the queue
    hands control back to the event loop, so the UI keeps breathing.
    """

    started = QtCore.pyqtSignal(str, int)
    slice_started = QtCore.pyqtSignal(str, int)
    slice_finished = QtCore.pyqtSignal(str, int, float)
    item_done = QtCore.pyqtSignal(str, object)
    item_failed = QtCore.pyqtSignal(str, str)
    item_cancelled = QtCore.pyqtSignal(str)
    progress = QtCore.pyqtSignal(int, int)
    finished = QtCore.pyqtSignal(str, bool)

    def __init__(self, parent=None, *, chunk_size=4, slice_budget_ms=4000,
                 max_workers=2, item_timeout_ms=0):
        super().__init__(parent)
        self.chunk_size = max(1, int(chunk_size))
        self.slice_budget_ms = max(0, int(slice_budget_ms))
        self.item_timeout_ms = max(0, int(item_timeout_ms))
        self._pool = QtCore.QThreadPool(self)
        self._pool.setMaxThreadCount(max(1, int(max_workers)))
        self._generation = 0
        self._batch = None
        self._yield = QtCore.QTimer(self)
        self._yield.setSingleShot(True)
        self._yield.timeout.connect(self._dispatch)
        self._watchdog = QtCore.QTimer(self)
        self._watchdog.setInterval(250)
        self._watchdog.timeout.connect(self._check_timeouts)

    # -- public API ---------------------------------------------------------

    def submit(self, items, work, *, batch_id=None, key=None, valid=None, hold=False):
        """Queue ``items`` for ``work`` and return the new batch id.

        ``work(item)`` runs on the queue's thread pool; an exception fails
        that item only and never aborts the batch. ``key(item)`` gives an
        item its stable identity and ``valid(key)`` is consulted before
        dispatch and again before a result is published, so an item whose
        identity expired (selection changed, record deleted) is cancelled
        instead of written back.

        ``hold`` keeps the batch open for later ``add()`` calls, so a large
        library can be fed chunk by chunk instead of being materialised as
        one list; ``close()`` then ends the batch.
        """
        if not callable(work):
            raise TypeError('work 必须是可调用对象')
        self.cancel()
        self._generation += 1
        generation = self._generation
        batch = _Batch(batch_id or uuid.uuid4().hex[:12], generation)
        batch.work = work
        batch.valid = valid
        batch.key_of = key
        batch.held = bool(hold)
        for item in items:
            identity = self._identity(key, item)
            entry = _BatchItem(identity, item, generation)
            batch.items.append(entry)
            batch.index.setdefault(identity, entry)
            batch.pending.append(entry)
        self._batch = batch
        batch.accounting = QueueAccounting('material_batch')
        if batch.items:
            batch.accounting.submitted(len(batch.items))
        record('material_batch_submit', batch=batch.id, total=len(batch.items),
               chunk_size=self.chunk_size, slice_budget_ms=self.slice_budget_ms,
               workers=self._pool.maxThreadCount())
        self.started.emit(batch.id, len(batch.items))
        if self.item_timeout_ms:
            self._watchdog.start()
        self._dispatch()
        return batch.id

    def add(self, items, *, key=None):
        """Add items to the running batch without starting a new one.

        The queue keeps a bounded window of jobs alive, so a caller can
        feed a 5000 material library chunk by chunk and never hold more
        than ``chunk_size`` jobs in flight.
        """
        batch = self._batch
        if batch is None or not batch.active:
            return 0
        count = 0
        for item in items:
            identity = self._identity(key, item)
            entry = _BatchItem(identity, item, batch.generation)
            batch.items.append(entry)
            batch.index.setdefault(identity, entry)
            batch.pending.append(entry)
            count += 1
        if count:
            if batch.accounting is not None:
                batch.accounting.submitted(count)
            self._dispatch()
        return count

    def close(self):
        """End a held batch once no more item will be added."""
        batch = self._batch
        if batch is None or not batch.active:
            return False
        batch.held = False
        self._maybe_next(batch)
        return True

    def cancel(self, batch_id=None):
        """Cancel the running batch; results still in flight are dropped."""
        batch = self._batch
        if batch is None or not batch.active:
            return False
        if batch_id is not None and batch_id != batch.id:
            return False
        batch.cancelled = True
        self._generation += 1
        # Items still outstanding when the cancel landed.
        outstanding = len(batch.pending) + len(batch.running)
        while batch.pending:
            self._cancel_item(batch, batch.pending.popleft(), reason='batch_cancelled')
        for entry in list(batch.running):
            state = self._cancel_item(batch, entry, reason='batch_cancelled',
                                      remaining=outstanding)
            if state == 'running':
                # ``remaining`` is recorded once per cancel operation.
                outstanding = None
        record('material_task_cancelled', task=batch.id, batch=batch.id,
               phase='batch', count=batch.counts['cancelled'], reason='batch_cancelled')
        self._report(batch)
        self._finish(batch)
        return True

    def cancel_item(self, key):
        """Cancel one item; a running item's result is dropped when it lands."""
        batch = self._batch
        if batch is None or not batch.active:
            return False
        entry = batch.index.get(key)
        if entry is None or entry.status in ('completed', 'failed', 'cancelled'):
            return False
        was_pending = entry.status == 'pending'
        if was_pending:
            try:
                batch.pending.remove(entry)
            except ValueError:
                pass
        self._cancel_item(batch, entry, reason='item_cancelled',
                          remaining=len(batch.pending) + len(batch.running))
        self._report(batch)
        if was_pending:
            self._maybe_next(batch)
        return True

    def snapshot(self):
        """Read-only view of the queue state; baseline scripts poll this."""
        batch = self._batch
        if batch is None:
            return {'batch': '', 'active': False, 'cancelled': False,
                    'generation': self._generation, 'total': 0, 'queued': 0, 'running': 0,
                    'draining': 0, 'completed': 0, 'failed': 0, 'cancelled': 0, 'settled': 0,
                    'slices': 0, 'last_slice_ms': 0.0, 'max_slice_ms': 0.0,
                    'over_budget_slices': 0, 'elapsed_ms': 0.0,
                    'chunk_size': self.chunk_size, 'slice_budget_ms': self.slice_budget_ms,
                    'max_workers': self._pool.maxThreadCount(), 'items': {}}
        return {'batch': batch.id, 'active': batch.active, 'cancelled': batch.cancelled,
                'generation': self._generation, 'total': len(batch.items),
                'queued': len(batch.pending), 'running': len(batch.running),
                'draining': len(batch.running) if batch.cancelled else 0,
                'completed': batch.counts['completed'], 'failed': batch.counts['failed'],
                'cancelled': batch.counts['cancelled'], 'settled': batch.settled,
                'slices': batch.slices, 'last_slice_ms': round(batch.last_slice_ms, 3),
                'max_slice_ms': round(batch.max_slice_ms, 3),
                'over_budget_slices': batch.over_budget, 'held': batch.held,
                'elapsed_ms': round((time.perf_counter()-batch.started_at)*1000, 3),
                'chunk_size': self.chunk_size, 'slice_budget_ms': self.slice_budget_ms,
                'max_workers': self._pool.maxThreadCount(),
                'items': {entry.key: entry.status for entry in batch.items}}

    def results(self):
        """``{key: value}`` of the items that completed in the last batch."""
        batch = self._batch
        return dict(batch.results) if batch is not None else {}

    def pending_keys(self):
        """Keys that have not settled yet."""
        batch = self._batch
        if batch is None:
            return []
        return [entry.key for entry in batch.items if entry.status in ('pending', 'running')]

    def generation(self):
        """The current generation token; callers compare it to detect staleness."""
        return self._generation

    # -- internals ----------------------------------------------------------

    @staticmethod
    def _identity(key, item):
        if key is None:
            return str(item)
        try:
            return str(key(item))
        except Exception:
            return str(item)

    def _is_current(self, batch):
        return batch is self._batch and batch.active

    def _is_valid(self, batch, identity):
        """Call the caller's predicate on the owning thread only."""
        if batch.valid is None:
            return True
        try:
            return bool(batch.valid(identity))
        except Exception:
            return False

    def _obsolete(self, batch, entry):
        """Cheap thread-safe staleness check, called from the worker thread."""
        return (batch.cancelled or entry.status in ('cancelled', 'failed')
                or batch.generation != self._generation)

    def _start(self, batch, entry):
        entry.status = 'running'
        entry.started_at = time.perf_counter()
        # The worker must not keep this queue alive.  The queue owns two
        # running timers, and a queue that dies on the worker thread
        # leaves its timer ids registered in the GUI dispatcher: Qt cannot
        # stop a timer from another thread and fires the dead timer later.
        queue = weak_gui(self)

        def obsolete(b=batch, e=entry, queue=queue):
            owner = queue()
            return True if owner is None else owner._obsolete(b, e)

        def started(wait_ms, b=batch, e=entry, queue=queue):
            owner = queue()
            if owner is not None:
                owner._account_started(b, e, wait_ms)

        def service(service_ms, b=batch, e=entry, queue=queue):
            owner = queue()
            if owner is not None:
                owner._account_service(b, e, service_ms)

        job = MaterialTask(lambda: batch.work(entry.value), obsolete)
        job.task_id = entry.key
        job.trace_context=batch.trace_context
        job.wait_since = entry.queued_mono
        job.on_started = started
        job.on_service = service
        entry.job = job
        batch.running[entry] = job
        batch.slice_items += 1
        # The queued callbacks must not keep the queue alive either.  PyQt6
        # delivers a plain callable in the thread that owns the signal, so
        # the pool deletes the runnable -- and with it the last reference
        # to a queue that owns running timers -- in the worker thread.  A
        # weak capture ends that path and keeps a late result away from a
        # queue that is already gone.
        #
        # A connected callable must declare exactly the arguments the signal
        # carries: PyQt6 calls it with the signal's own arguments and treats
        # every declared parameter as required.  A callable with extra
        # parameters therefore fails at emission time, and the job never
        # leaves the running set.  Everything else comes from the closure.
        def on_result(value):
            owner = queue()
            if owner is None:
                return
            run_bound(batch.trace_context, owner._on_result, batch, entry, value)

        def on_error(message):
            owner = queue()
            if owner is None:
                return
            run_bound(batch.trace_context, owner._on_error, batch, entry, message)

        def on_finished():
            owner = queue()
            if owner is None:
                return
            run_bound(batch.trace_context, owner._on_finished, batch, entry)

        job.signals.result.connect(on_result)
        job.signals.error.connect(on_error)
        job.signals.finished.connect(on_finished)
        self._pool.start(job)

    def _dispatch(self):
        batch = self._batch
        if batch is None or not batch.active:
            return
        if batch.cancelled:
            self._finish(batch)
            return
        if not batch.pending and not batch.running:
            if batch.held:
                return  # more items may still be added
            self._finish(batch)
            return
        if not batch.running and batch.pending:
            batch.slice += 1
            batch.slice_items = 0
            batch.slice_started = time.perf_counter()
            batch.slices += 1
            self.slice_started.emit(batch.id, batch.slice)
        while (batch.pending and batch.slice_items < self.chunk_size
               and len(batch.running) < self.chunk_size):
            entry = batch.pending.popleft()
            if batch.cancelled or not self._is_valid(batch, entry.key):
                self._cancel_item(batch, entry, reason='batch_cancelled'
                                  if batch.cancelled else 'invalid')
                continue
            self._start(batch, entry)

    def _on_result(self, batch, entry, value):
        if not self._is_current(batch) or entry.status in ('completed', 'failed', 'cancelled'):
            return
        if self._obsolete(batch, entry) or not self._is_valid(batch, entry.key):
            self._cancel_item(batch, entry, reason='stale')
            self._report(batch)
            return
        entry.status = 'completed'
        batch.counts['completed'] += 1
        batch.results[entry.key] = value
        self.item_done.emit(entry.key, value)
        self._account_installed(batch, entry)
        self._report(batch)

    def _on_error(self, batch, entry, message):
        if not self._is_current(batch) or entry.status in ('completed', 'failed', 'cancelled'):
            return
        if self._obsolete(batch, entry):
            self._cancel_item(batch, entry, reason='stale')
            self._report(batch)
            return
        entry.status = 'failed'
        batch.counts['failed'] += 1
        self._account_failed(batch, entry, reason='error')
        self.item_failed.emit(entry.key, message)
        self._report(batch)

    def _on_finished(self, batch, entry):
        batch.running.pop(entry, None)
        if entry.status == 'running':
            # The job ended without a result: it was cancelled or went stale.
            entry.status = 'cancelled'
            # The worker finished, but its result is dropped and never
            # installed.
            self._account_cancelled(batch, entry, reason='dropped')
            if self._is_current(batch):
                batch.counts['cancelled'] += 1
                self.item_cancelled.emit(entry.key)
                self._report(batch)
        if self._is_current(batch):
            self._maybe_next(batch)

    def _cancel_item(self, batch, entry, reason='cancelled', remaining=None):
        if entry.status in ('completed', 'failed', 'cancelled'):
            return None
        entry.status = 'cancelled'
        batch.counts['cancelled'] += 1
        state = self._account_cancelled(batch, entry, reason=reason, remaining=remaining)
        if self._is_current(batch):
            self.item_cancelled.emit(entry.key)
        return state

    # -- queue accounting ---------------------------------------------------
    #
    # One item passes through queued -> started -> finished -> settled. The
    # state lives on the item and the transitions happen under the batch lock,
    # so the worker thread and the owning thread cannot count one item twice.

    def _account_started(self, batch, entry, wait_ms):
        """The pool picked the item up and the work began."""
        with batch.acct_lock:
            if entry.acct != 'queued':
                return
            entry.acct = 'started'
            if batch.accounting is not None:
                batch.accounting.started(wait_ms=wait_ms)

    def _account_service(self, batch, entry, service_ms):
        """The worker finished the item; the result may still wait for the GUI."""
        with batch.acct_lock:
            if entry.acct != 'started':
                return
            entry.acct = 'finished'
            if batch.accounting is not None:
                batch.accounting.service_done(service_ms=service_ms)

    def _account_installed(self, batch, entry):
        """The result reached the consumer."""
        with batch.acct_lock:
            if entry.acct != 'finished':
                return
            entry.acct = 'settled'
            if batch.accounting is not None:
                batch.accounting.installed()

    def _account_failed(self, batch, entry, reason):
        """The item failed. A running worker keeps its slot until it ends."""
        with batch.acct_lock:
            if entry.acct == 'settled':
                return
            state = entry.acct
            if state in ('queued', 'finished'):
                entry.acct = 'settled'
            if batch.accounting is not None:
                # The gauge the failed work occupied is released here: a
                # queued item was never going to be installed, and a finished
                # one produced a result nobody will install. A running worker
                # keeps its slot until it really ends.
                if state == 'queued':
                    batch.accounting.failed(reason=reason, state='pending')
                elif state == 'finished':
                    batch.accounting.failed(reason=reason, state='ready')
                else:
                    batch.accounting.failed(reason=reason)

    def _account_cancelled(self, batch, entry, reason='cancelled', remaining=None):
        """The item was cancelled. Returns where it was when the cancel landed."""
        with batch.acct_lock:
            state = entry.acct
            if state == 'settled':
                return None
            entry.acct = 'settled'
            if batch.accounting is None:
                return state
            if state == 'queued':
                batch.accounting.cancelled(state='pending', reason=reason)
                return 'pending'
            if state == 'started':
                # Work that already started cannot be interrupted: the queue
                # drops its result, the worker keeps running.
                batch.accounting.cancelled(state='running', remaining=remaining,
                                           reason=reason)
                return 'running'
            batch.accounting.cancelled(state='ready', reason=reason)
            return 'ready'

    def _maybe_next(self, batch):
        if not self._is_current(batch):
            return
        if batch.cancelled:
            self._finish(batch)
            return
        if batch.running:
            return  # the slice still has work in flight
        if not batch.pending:
            if batch.held:
                return  # more items may still be added
            self._finish(batch)
            return
        self._close_slice(batch)
        # Hand control back to the event loop before the next slice.
        self._yield.start(0)

    def _close_slice(self, batch):
        duration = (time.perf_counter()-batch.slice_started)*1000
        over = bool(self.slice_budget_ms) and duration > self.slice_budget_ms
        if over:
            batch.over_budget += 1
        batch.last_slice_ms = duration
        batch.max_slice_ms = max(batch.max_slice_ms, duration)
        record('material_batch_slice', batch=batch.id, slice=batch.slice,
               items=batch.slice_items, duration_ms=round(duration, 3),
               budget_ms=self.slice_budget_ms, over_budget=over)
        self.slice_finished.emit(batch.id, batch.slice, duration)

    def _check_timeouts(self):
        batch = self._batch
        if batch is None or not batch.active or not self.item_timeout_ms:
            return
        now = time.perf_counter()
        for entry in list(batch.running):
            if entry.started_at is None:
                continue
            if (now-entry.started_at)*1000 <= self.item_timeout_ms:
                continue
            entry.status = 'failed'
            batch.counts['failed'] += 1
            self._account_failed(batch, entry, reason='timeout')
            self.item_failed.emit(entry.key, '材质任务超时')
            self._report(batch)

    def _report(self, batch):
        record('material_batch_progress', batch=batch.id, settled=batch.settled,
               total=len(batch.items), queued=len(batch.pending),
               running=len(batch.running), completed=batch.counts['completed'],
               failed=batch.counts['failed'], cancelled=batch.counts['cancelled'],
               elapsed_ms=round((time.perf_counter()-batch.started_at)*1000, 3))
        self.progress.emit(batch.settled, len(batch.items))
        if batch.accounting is not None:
            batch.accounting.rollup(batch=batch.id, phase='progress')

    def _finish(self, batch):
        if not batch.active:
            return
        batch.active = False
        if self.item_timeout_ms:
            self._watchdog.stop()
        record('material_batch_finished', batch=batch.id, cancelled=batch.cancelled,
               elapsed_ms=round((time.perf_counter()-batch.started_at)*1000, 3),
               completed=batch.counts['completed'], failed=batch.counts['failed'],
               cancelled_count=batch.counts['cancelled'], slices=batch.slices,
               over_budget_slices=batch.over_budget)
        if batch.accounting is not None:
            batch.accounting.rollup(force=True, reason='batch_finished', batch=batch.id)
        self.finished.emit(batch.id, batch.cancelled)
