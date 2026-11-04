"""A background worker must never destroy a Qt object.

Qt 6.7 (``src/corelib/kernel/qobject.cpp``) refuses to stop a timer that
belongs to another thread::

    void QObject::killTimer(int id) {
        if (Q_UNLIKELY(thread() != QThread::currentThread())) {
            qWarning("QObject::killTimer: Timers cannot be stopped from another thread");
            return;
        }

and ``QObjectPrivate::~QObjectPrivate()`` warns the same way and skips
the unregister.  The owner's dispatcher keeps a timer id whose object is
already gone, fires it later, and the process dies with an access
violation.

The rule these tests pin down: **a worker may use a Qt object, never own
one**.  The GUI side keeps the job alive until its result was delivered,
so everything the job held is released on the GUI thread.
"""
import gc
import threading
import weakref

import pytest

from PyQt6 import QtCore, sip

from prism.qt_thread_safety import JobRegistry, drain_thread_pools, weak_gui


class _Carrier(QtCore.QObject):
    """A signal carrier that records the thread it was destroyed on."""

    def __init__(self, released):
        super().__init__()
        self._released = released

    def __del__(self):
        self._released.append(threading.current_thread().name)


class _Job(QtCore.QRunnable):
    """One background job that owns a carrier."""

    def __init__(self, released):
        super().__init__()
        self.signals = _Carrier(released)

    def run(self):
        pass


def _pool():
    pool = QtCore.QThreadPool()
    pool.setMaxThreadCount(1)
    return pool


def test_the_job_is_released_on_the_gui_thread(qapp):
    """The carrier outlives the pool's deletion of the runnable.

    The pool deletes the runnable **in the worker thread**.  Without the
    registry that deletion released the last reference to the carrier, and
    the carrier died on the worker thread.
    """
    released = []
    registry = JobRegistry()
    pool = _pool()
    job = _Job(released)
    registry.start(pool, job)

    assert pool.waitForDone(5000)

    # The pool deleted the runnable in the worker thread.  The GUI side
    # still holds the job, so the carrier is still alive.
    assert released == [], 'the carrier was released by the worker'

    registry.release(job)
    del job
    gc.collect()

    assert released == [threading.current_thread().name]


def test_a_job_without_the_registry_is_released_by_the_worker():
    """The old behaviour, kept as the measured baseline of the A/B."""
    released = []
    pool = _pool()
    # No reference on this side: the pool owns the only one, and it drops
    # it in the worker thread.
    pool.start(_Job(released))

    assert pool.waitForDone(5000)
    gc.collect()

    assert released and released[0] != threading.current_thread().name, \
        'the worker did not release the carrier'


def test_the_queue_with_a_running_timer_is_not_released_by_the_worker(qapp):
    """A queue that owns a running timer must not die on a worker thread.

    ``MaterialBatchQueue`` owns two timers: a 250 ms watchdog and a
    single-shot yield.  A worker that held the queue would own its timers
    too, and Qt cannot stop a timer from another thread.
    """
    from prism.widgets.material_task import MaterialBatchQueue

    queue = MaterialBatchQueue(parent=None, chunk_size=1, max_workers=1)
    queue._watchdog.start()
    released = []
    weakref.finalize(queue, released.append, threading.current_thread().name)

    queue.submit([1], work=lambda item: item)
    queue._pool.waitForDone(5000)

    # The GUI side lets go.  The worker must not be the last owner.
    del queue
    gc.collect()

    assert released, 'the queue was never released'
    assert released[0] == threading.current_thread().name


def test_the_carrier_survives_the_cycle_that_holds_it(qapp):
    """The carrier must not be collected together with the job.

    PyQt6 delivers a queued result through the carrier.  When the carrier
    dies with the job, the pending delivery has no receiver left and the
    process dies with an access violation.
    """
    from prism.widgets.material_task import MaterialTask

    job = MaterialTask(lambda: 'value')
    carrier = job.signals
    del job
    gc.collect()

    assert not sip.isdeleted(carrier), 'the carrier died with its job'


def test_the_queue_delivers_its_result_and_releases_the_job(qapp):
    """The result reaches the consumer and the job leaves the running set.

    A connected callable that declares more parameters than the signal
    carries makes the emission fail, and the queue would never finish.
    """
    from prism.widgets.material_task import MaterialBatchQueue

    queue = MaterialBatchQueue(parent=None, chunk_size=1, max_workers=1)
    seen = []
    queue.item_done.connect(lambda key, value: seen.append((key, value)))
    queue.submit([7], work=lambda item: item, key=lambda item: 'k')
    queue._pool.waitForDone(5000)
    qapp.processEvents()

    assert seen == [('k', 7)]
    snapshot = queue.snapshot()
    assert snapshot['running'] == 0
    assert snapshot['completed'] == 1


def test_weak_gui_reaches_the_object_only_while_it_is_alive():
    class Owner(QtCore.QObject):
        def value(self):
            return 7

    owner = Owner()
    alive = weak_gui(owner)
    assert alive() is owner
    assert alive().value() == 7

    method = weak_gui(owner.value)
    assert method()() == 7
    del owner
    gc.collect()
    assert alive() is None
    assert method() is None


def test_weak_gui_keeps_a_bound_method_from_keeping_its_object_alive():
    class Owner(QtCore.QObject):
        def value(self):
            return 7

    owner = Owner()
    method = weak_gui(owner.value)
    assert method() is not None
    del owner
    gc.collect()
    assert method() is None


def test_the_registry_releases_only_the_job_it_was_given():
    registry = JobRegistry()
    first, second = _Job([]), _Job([])
    registry.start(_pool(), first)
    registry.start(_pool(), second)
    registry.release(first)
    registry.release(None)
    registry.clear()
    # Releasing twice is normal: a late result may arrive after the cancel.
    registry.release(first)
    registry.release(second)


def test_draining_the_pools_waits_for_the_workers(qapp):
    """The shutdown drain lets the workers finish before the app is gone."""
    finished = []

    class Slow(QtCore.QRunnable):
        def run(self):
            import time
            time.sleep(0.2)
            finished.append(threading.current_thread().name)

    # A pool the application owns: the drain finds every such pool.
    pool = QtCore.QThreadPool(qapp)
    pool.setMaxThreadCount(1)
    pool.start(Slow())

    busy = drain_thread_pools(timeout_ms=5000)

    assert finished, 'the drain returned before the worker finished'
    assert busy == 0
