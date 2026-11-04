"""Background work must never destroy a Qt object.

Qt 6.7 refuses to stop a timer that belongs to another thread and leaves
its id registered in the owner's event dispatcher
(``src/corelib/kernel/qobject.cpp``)::

    void QObject::killTimer(int id) {
        if (Q_UNLIKELY(thread() != QThread::currentThread())) {
            qWarning("QObject::killTimer: Timers cannot be stopped from another thread");
            return;
        }

``QObjectPrivate::~QObjectPrivate()`` warns about the same thing and
skips the unregister, so the owner's dispatcher keeps a timer id whose
object is already gone.  The dispatcher fires it later and the process
dies with an access violation.  Prism saw the warning 27 times, always
after a project was opened, always on a QThreadPool worker thread.

Two rules keep a worker away from that:

* a worker may **use** a Qt object, never **own** one -- the signal
  carrier a worker emits on belongs to the GUI side, because the pool
  deletes its runnable **in the worker thread**;
* a worker callback that names a GUI object holds it **weakly**, so the
  last reference to an object with a running timer always dies on the
  GUI thread.
"""
import inspect
import time
import weakref
from weakref import WeakMethod

from PyQt6 import QtCore


def weak_gui(owner):
    """A callable that reaches ``owner`` only while it is alive.

    The returned callable gives ``None`` once the object is gone, which
    is what a worker needs: a dead GUI object is a normal cancellation,
    not an error.  A bound method is held through its object, so the
    method cannot keep a GUI widget alive on a worker thread.
    """
    if owner is None:
        return lambda: None
    if inspect.ismethod(owner):
        try:
            reference = WeakMethod(owner)
        except TypeError:
            return lambda: owner

        def bound():
            return reference()

        return bound
    try:
        reference = weakref.ref(owner)
    except TypeError:
        return lambda: owner

    def alive():
        return reference()

    return alive


def gui_signal_carrier(carrier, owner):
    """Give a signal carrier an owner that outlives the page and return it.

    A carrier without an owner belongs to Python, and Python hands the
    carrier to the thread pool: the pool deletes its runnable **in the
    worker thread**, and the carrier's C++ object goes with it.  With an
    owner the carrier is destroyed by the GUI thread instead, and the
    worker only ever borrows it.

    The owner must outlive the page that queued the work, and a queued
    result is delivered *through* the carrier: when the carrier dies, the
    pending delivery has no receiver left and the process dies with an
    access violation.  The application outlives every page, so it is the
    owner of last resort.
    """
    if owner is None or carrier.parent() is not None:
        return carrier
    if carrier.thread() != QtCore.QThread.currentThread():
        # A Qt object may only be given to an owner in its own thread.
        return carrier
    carrier.setParent(owner)
    return carrier


class JobRegistry:
    """Keeps background jobs alive until their result was delivered.

    A thread pool deletes its runnable **in the worker thread**.  When the
    GUI side let go of the job first, that deletion released the last
    reference to a Qt object -- and a Qt object that owns a running timer
    must not be destroyed by a worker thread: Qt cannot stop the timer
    from another thread, leaves the timer id registered in the owner's
    dispatcher, and fires the dead timer later.

    The registry is the GUI side's promise: *this job is still mine until
    its result was delivered*.  The job is released on the GUI thread, so
    everything the job held is released there too.
    """

    def __init__(self):
        self._jobs = set()

    def start(self, pool, job):
        """Queue the job and remember it until it is released."""
        self._jobs.add(job)
        pool.start(job)
        return job

    def release(self, job):
        """Forget the job.  Call this on the GUI thread."""
        if job is not None:
            self._jobs.discard(job)

    def clear(self):
        """Forget every job.  Call this when the owner goes away."""
        self._jobs.clear()


def drain_thread_pools(timeout_ms=5000):
    """Wait for the Qt thread pools to finish before the app is torn down.

    The pools delete their runnable in the worker thread, and that
    runnable can be the last owner of a GUI object.  Draining the pools
    before the application objects are destroyed keeps that release on a
    thread that still exists.

    Returns the number of pools that were still busy after the timeout.
    """
    app = QtCore.QCoreApplication.instance()
    if app is None:
        return 0

    pools = []

    def add(pool):
        if pool is None:
            return
        for known in pools:
            if known is pool:
                return
        pools.append(pool)

    for pool in app.findChildren(QtCore.QThreadPool):
        add(pool)
    add(QtCore.QThreadPool.globalInstance())
    # This pool is the only one without a parent: it has to outlive the
    # application object that created it.
    from prism import catalog_browser
    add(getattr(catalog_browser, '_POOL', None))

    deadline = time.monotonic() + max(0.0, timeout_ms / 1000.0)
    busy = 0
    for pool in pools:
        remaining = max(0, int((deadline - time.monotonic()) * 1000))
        if not pool.waitForDone(remaining):
            busy += 1
    return busy
