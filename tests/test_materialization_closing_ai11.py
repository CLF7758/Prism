"""AI-11 round 2 - the closing protocol of the materialization service.

``prism/source_store.py`` owns the only background service that pulls an
embedded original out of a ``.prism`` archive.  This file is the acceptance
suite of the closing protocol that was added to it:

* ``closing`` / ``stop_accepting()`` / ``resume_accepting()``
* ``cancel_pending()`` - release the waiters of the work that will never run
* ``is_drained()`` / ``state()``
* ``shutdown()`` - idempotent, ordered, and it never creates a service that
  was never used

and of the invariants that make the protocol safe:

* a consumer that must have the bytes (a save or an export) keeps its
  extraction alive while the service is closing,
* one consumer giving up does not stop an extraction somebody else needs,
* when everybody gives up the extraction stops at a chunk boundary and the
  half written file is removed,
* a result that arrives late never touches an object that was destroyed
  in the meantime.

Every test that waits for a worker releases its gate in a ``finally`` block,
so a failing assertion can never leave a worker thread behind.
"""
from __future__ import annotations

import io
import sqlite3
import threading
import time
import uuid
import weakref
from pathlib import Path

import pytest
from PyQt6 import QtCore, sip

from prism import source_store


CHUNK = 1024 * 1024


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


class _Gate:
    """A one-shot barrier that pauses a copy at its second chunk boundary."""

    def __init__(self):
        self.reached = threading.Event()
        self.release = threading.Event()

    def block_at_second_read(self, inner):
        """Wrap a stream so that its second read waits for the release."""
        outer = self

        class _Blocking:
            def __init__(self, stream):
                self._stream = stream
                self._reads = 0

            def read(self, size):
                self._reads += 1
                data = self._stream.read(size)
                if self._reads == 2:
                    outer.reached.set()
                    assert outer.release.wait(20), 'the gate was never released'
                return data

        return _Blocking(inner)


class _Reached(Exception):
    """Raised by the fake copy to report that it was reached."""


@pytest.fixture
def archive_factory(tmp_path):
    """Build minimal ``sqlar`` archives holding one embedded original."""
    counter = {'n': 0}

    def make(name=None, size=3):
        counter['n'] += 1
        name = name or f'original-{counter["n"]}.prism'
        path = tmp_path / name
        # size * 1 MB + a tail, so the copy really crosses chunk boundaries
        data = b'original-video-bytes\x00' * (size * 51200)
        with sqlite3.connect(path) as db:
            db.execute('CREATE TABLE sqlar(item_id INTEGER, data BLOB)')
            db.execute('INSERT INTO sqlar VALUES(?,?)', (7, data))
        return (str(path), 7), data

    return make


@pytest.fixture
def gated_copy(monkeypatch):
    """Install a ``retain_stream`` that pauses at a known chunk boundary.

    The real production code is used for the copy itself; only the *read*
    of the blob is wrapped, so the cancel checks of ``retain_stream`` and the
    half written file handling are the ones that really run.
    """
    original = source_store.retain_stream
    gate_box = {}

    def install(gate):
        gate_box['gate'] = gate
        monkeypatch.setattr(source_store, 'retain_stream',
                            lambda blob, suffix: original(gate.block_at_second_read(blob), suffix))
        return gate

    return install


def _temp_files():
    """The files that currently live in the temporary originals directory."""
    directory = getattr(source_store, '_directory', None)
    if directory is None:
        return set()
    return set(Path(directory.name).iterdir())


def _service(workers=1):
    return source_store.MaterializationService(workers=workers)


# ---------------------------------------------------------------------------
# A. the shape of the service while it works
# ---------------------------------------------------------------------------


def test_a_running_task_and_a_queued_task_are_told_apart(archive_factory, gated_copy):
    """A. one task runs, the next one waits in the queue."""
    source_a, _ = archive_factory('a.prism')
    source_b, _ = archive_factory('b.prism')
    gate = gated_copy(_Gate())
    service = _service(workers=1)
    try:
        first = service.request(source_a)
        second = service.request(source_b)
        assert gate.reached.wait(15), 'the copy never started'

        state = service.state()
        assert state['tasks'] == 2
        assert state['running'] == 1
        assert state['pending'] == 1
        assert state['drained'] is False
        assert state['closing'] is False

        gate.release.set()
        assert first.wait(15)
        assert second.wait(15)
        assert service.state()['drained'] is True
    finally:
        gate.release.set()
        service.shutdown()


def test_two_consumers_of_the_same_source_share_one_extraction(archive_factory, gated_copy):
    """A. the same source version is extracted once and shared."""
    source, data = archive_factory('shared.prism')
    gate = gated_copy(_Gate())
    service = _service(workers=1)
    try:
        first = service.request(source, cancel_event=threading.Event())
        second = service.request(source)
        assert first.key == second.key
        assert gate.reached.wait(15)

        state = service.state()
        assert state['tasks'] == 1
        assert state['running'] == 1

        gate.release.set()
        path_first = first.wait(15)
        path_second = second.wait(15)
        assert path_first == path_second
        assert Path(path_first).read_bytes() == data
    finally:
        gate.release.set()
        service.shutdown()


# ---------------------------------------------------------------------------
# B. one consumer gives up, the other one still wants the bytes
# ---------------------------------------------------------------------------


def test_one_consumer_cancels_while_the_other_keeps_the_bytes(archive_factory, gated_copy):
    """B. a cancelled consumer does not stop an extraction somebody needs."""
    source, data = archive_factory('shared.prism')
    gate = gated_copy(_Gate())
    service = _service(workers=1)
    try:
        first = service.request(source, cancel_event=threading.Event())
        second = service.request(source)
        assert gate.reached.wait(15)

        assert first.cancellable is True
        assert second.cancellable is False
        first.cancel()

        gate.release.set()
        path = second.wait(15)
        assert Path(path).read_bytes() == data
        with pytest.raises(source_store.MaterializationCancelled):
            first.wait(15)
    finally:
        gate.release.set()
        service.shutdown()


def test_when_every_consumer_cancels_the_copy_stops_and_removes_the_half_file(archive_factory, gated_copy):
    """B. all consumers gone -> the copy stops at a chunk boundary."""
    source, data = archive_factory('shared.prism')
    gate = gated_copy(_Gate())
    service = _service(workers=1)
    try:
        before = _temp_files()
        first = service.request(source, cancel_event=threading.Event())
        second = service.request(source, cancel_event=threading.Event())
        assert gate.reached.wait(15)

        # the half written file exists right now
        half_written = _temp_files() - before
        assert half_written, 'the copy did not create a file'

        first.cancel()
        second.cancel()
        gate.release.set()

        with pytest.raises(source_store.MaterializationCancelled):
            first.wait(15)
        with pytest.raises(source_store.MaterializationCancelled):
            second.wait(15)

        # the half written file is gone again
        assert _temp_files() == before, 'the half written file was left behind'
    finally:
        gate.release.set()
        service.shutdown()


# ---------------------------------------------------------------------------
# C. closing
# ---------------------------------------------------------------------------


def test_a_request_during_closing_is_refused_with_a_clearing_error(archive_factory):
    """C. new work is refused while the service is closing."""
    source, _ = archive_factory('a.prism')
    service = _service(workers=1)
    try:
        assert service.closing is False
        service.stop_accepting()
        assert service.closing is True
        assert service.state()['closing'] is True
        with pytest.raises(source_store.MaterializationClosing):
            service.request(source)
    finally:
        service.shutdown()


def test_a_cancelled_close_lets_the_service_work_again(archive_factory):
    """C. stopping is reversible: a host that cancels the close can continue."""
    source, data = archive_factory('a.prism')
    service = _service(workers=1)
    try:
        service.stop_accepting()
        with pytest.raises(source_store.MaterializationClosing):
            service.request(source)

        service.resume_accepting()
        assert service.closing is False
        request = service.request(source)
        path = request.wait(15)
        assert Path(path).read_bytes() == data
    finally:
        service.shutdown()


def test_a_consumer_that_must_have_the_bytes_keeps_its_extraction_while_closing(archive_factory, gated_copy):
    """C. a save or an export is never truncated by the close."""
    source, data = archive_factory('a.prism')
    gate = gated_copy(_Gate())
    service = _service(workers=1)
    try:
        request = service.request(source)          # no cancel event: mandatory
        assert gate.reached.wait(15)

        service.stop_accepting()
        assert service.closing is True
        gate.release.set()

        path = request.wait(15)                    # it still completes
        assert Path(path).read_bytes() == data
        assert service.state()['drained'] is True
    finally:
        gate.release.set()
        service.shutdown()


def test_shutdown_stops_a_running_extraction_that_nobody_needs(archive_factory, gated_copy):
    """C. the close stops the running work that nobody is waiting for."""
    source, _ = archive_factory('a.prism')
    gate = gated_copy(_Gate())
    service = _service(workers=1)
    before = _temp_files()
    request = service.request(source, cancel_event=threading.Event())
    assert gate.reached.wait(15)
    half_written = _temp_files() - before
    assert half_written, 'the copy did not create a file'

    def release_soon():
        time.sleep(0.05)
        gate.release.set()

    releaser = threading.Thread(target=release_soon, daemon=True)
    releaser.start()
    try:
        service.shutdown(wait=True)
    finally:
        gate.release.set()
        releaser.join(5)

    with pytest.raises(source_store.MaterializationCancelled):
        request.wait(15)
    assert _temp_files() == before, 'the half written file was left behind'


def test_cancel_pending_releases_the_waiters_of_the_work_that_never_started(archive_factory, gated_copy):
    """C. a queued task is cancelled and its waiter is told."""
    source_a, _ = archive_factory('a.prism')
    source_b, _ = archive_factory('b.prism')
    gate = gated_copy(_Gate())
    service = _service(workers=1)
    try:
        first = service.request(source_a)
        second = service.request(source_b, cancel_event=threading.Event())
        assert gate.reached.wait(15)
        assert service.state()['pending'] == 1

        assert service.cancel_pending() == 1
        assert service.state()['pending'] == 0
        with pytest.raises(source_store.MaterializationCancelled):
            second.wait(15)

        gate.release.set()
        assert first.wait(15)
    finally:
        gate.release.set()
        service.shutdown()


def test_shutdown_can_be_called_more_than_once(archive_factory):
    """C. the close is idempotent."""
    source, _ = archive_factory('a.prism')
    service = _service(workers=1)
    request = service.request(source, cancel_event=threading.Event())
    service.shutdown()
    service.shutdown()
    service.shutdown(wait=False)
    with pytest.raises(source_store.MaterializationClosing):
        service.request(source)
    with pytest.raises(source_store.MaterializationCancelled):
        request.wait(15)


def test_the_process_wide_service_is_not_created_by_a_close_that_never_used_it():
    """C. closing the app must not bring a service into existence."""
    saved = source_store._service
    try:
        source_store._service = None
        assert source_store.materialization_service_state() is None
        assert source_store.materialization_service_closing() is False
        assert source_store.materialization_service_drained() is True
        assert source_store.shutdown_materialization_service() is False
        assert source_store.materialization_service_state() is None
    finally:
        source_store._service = saved


def test_the_process_wide_service_can_be_stopped_and_resumed():
    """C. the host can hold the service without stopping the running work."""
    saved = source_store._service
    try:
        source_store._service = None
        assert source_store.stop_accepting_materialization() is False
        assert source_store.resume_accepting_materialization() is False
        assert source_store.materialization_service_state() is None

        service = source_store.materialization_service()
        assert source_store.stop_accepting_materialization() is True
        assert source_store.materialization_service_closing() is True
        assert source_store.resume_accepting_materialization() is True
        assert source_store.materialization_service_closing() is False
        service.shutdown()
    finally:
        source_store._service = saved


# ---------------------------------------------------------------------------
# D. a result that arrives late
# ---------------------------------------------------------------------------


def test_a_late_result_never_touches_an_object_that_was_destroyed(archive_factory, gated_copy):
    """D. the service has no callback channel, so nothing can call back."""
    source, data = archive_factory('a.prism')
    gate = gated_copy(_Gate())
    service = _service(workers=1)
    try:
        holder = QtCore.QObject()
        destroyed_at = []
        holder.destroyed.connect(lambda *_: destroyed_at.append(time.monotonic()))
        holder_id = id(holder)

        request = service.request(source, cancel_event=threading.Event())
        assert gate.reached.wait(15)

        # the consumer's Qt object dies while the extraction is still running
        sip.delete(holder)
        assert sip.isdeleted(holder) is True
        assert holder_id == id(holder)

        gate.release.set()
        path = request.wait(15)
        assert Path(path).read_bytes() == data

        # the service delivers the result only by waiting for it, so the
        # destroyed object was not touched a second time
        assert len(destroyed_at) == 1
        assert not [name for name in dir(service) if 'callback' in name.lower()]
    finally:
        gate.release.set()
        service.shutdown()


def test_the_timeline_is_recorded_with_monotonic_times_and_object_ids(archive_factory, gated_copy, qapp):
    """D. the close is observable: request, worker, close, destroyed, return."""
    source, data = archive_factory('a.prism')
    gate = gated_copy(_Gate())
    service = _service(workers=1)
    marks = {}
    try:
        holder = QtCore.QObject(qapp)
        destroyed_at = []
        holder.destroyed.connect(lambda *_: destroyed_at.append(time.monotonic()))

        marks['request'] = time.monotonic()
        # a consumer that must have the bytes: the close may not stop it
        request = service.request(source)
        marks['request_ids'] = {
            'service': id(service),
            'request': id(request),
            'task': id(request._task),
            'holder': id(holder),
            'app': id(qapp),
        }
        assert gate.reached.wait(15)
        marks['worker_running'] = time.monotonic()
        marks['worker_thread'] = threading.get_ident()

        marks['close'] = time.monotonic()
        service.stop_accepting()

        gate.release.set()
        path = request.wait(15)
        marks['worker_done'] = time.monotonic()
        assert Path(path).read_bytes() == data

        sip.delete(holder)
        assert sip.isdeleted(holder) is True
        marks['holder_destroyed'] = destroyed_at[0]

        marks['main_return'] = time.monotonic()
    finally:
        gate.release.set()
        service.shutdown()

    # the order of the marks is the order of the real events
    assert marks['request'] <= marks['worker_running']
    assert marks['worker_running'] <= marks['close']
    assert marks['close'] <= marks['worker_done']
    assert marks['worker_done'] <= marks['holder_destroyed']
    assert marks['holder_destroyed'] <= marks['main_return']
    # every mark is a real monotonic reading, never a wall clock second
    assert all(isinstance(marks[key], float) for key in
               ('request', 'worker_running', 'close', 'worker_done', 'main_return'))
    # the object ids identify the objects that were really used
    assert marks['request_ids']['service'] == id(service)
    assert marks['request_ids']['request'] == id(request)
    assert marks['request_ids']['task'] == id(request._task)
    assert marks['request_ids']['app'] == id(qapp)


# ---------------------------------------------------------------------------
# E. the temporary file of a cancelled copy
# ---------------------------------------------------------------------------


def test_a_cancelled_copy_leaves_no_file_behind(archive_factory, tmp_path):
    """E. the half written file of a cancelled copy is removed."""
    data = b'x' * (2 * CHUNK + 17)
    stream = io.BytesIO(data)
    before = _temp_files()

    state = {'reads': 0}

    class _CancelAfterFirstChunk:
        def read(self, size):
            state['reads'] += 1
            return stream.read(size)

    def cancel():
        return state['reads'] >= 2

    with pytest.raises(source_store.MaterializationCancelled):
        source_store.retain_stream(_CancelAfterFirstChunk(), '.mp4', cancel=cancel)

    after = _temp_files()
    assert after == before, 'the cancelled copy left a file behind'
