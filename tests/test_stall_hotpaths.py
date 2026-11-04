"""Headless regression tests for confirmed save/LOD stall hot-path defects.
No QApplication, QWidget, desktop input, real project I/O or GC policy change.
"""
from collections import OrderedDict
import gc
import time
from types import MethodType, SimpleNamespace
import weakref
from unittest.mock import Mock

import pytest

from prism import lod
from prism.fileio import save_preparation as preparation


class Signal:
    def __init__(self):
        self.callbacks = []
    def connect(self, callback, *args):
        self.callbacks.append(callback)
    def emit(self, *args):
        for callback in tuple(self.callbacks):
            callback(*args)


class Owner:
    def __init__(self, visible=False, preview=None):
        self.visible = visible
        self._preview_pixmap = preview
        self._grayscale_cache = 'old-gray'


class Pixmap:
    def __init__(self, key):
        self.key = key
    def cacheKey(self):
        return self.key


class TrimFixture:
    _budget_bytes = lod.DisplayCache._budget_bytes
    _trim_cache_to = lod.DisplayCache._trim_cache_to
    def __init__(self, pinned, reclaimable):
        self.entries = OrderedDict()
        self.overviews = OrderedDict()
        self.overview_bytes = 0
        self.owners = []
        self.checked = 0
        for index in range(pinned + reclaimable):
            pixel = Pixmap(index)
            owner = Owner(index < pinned, pixel)
            self.owners.append(owner)
            self.entries[index] = (weakref.ref(owner), pixel, 4)
        self.bytes = len(self.entries)*4
    def _entry_visible(self, owner):
        self.checked += 1
        return owner is not None and owner.visible


def test_lod_trim_scans_each_candidate_once(monkeypatch):
    monkeypatch.setattr(lod, 'is_deleted', lambda owner: False)
    cache = TrimFixture(256, 256)
    cache._trim_cache_to(256*4)
    assert list(cache.entries) == list(range(256))
    assert cache.bytes == 256*4
    assert cache.checked <= 512, 'pinned prefix must not be rescanned for each eviction'
    assert all(owner._preview_pixmap is not None for owner in cache.owners[:256])
    assert all(owner._preview_pixmap is None for owner in cache.owners[256:])


def test_lod_within_budget_does_not_snapshot_or_scan_entries():
    cache = TrimFixture(4, 4)
    class DoNotIterate(OrderedDict):
        def __iter__(self):
            raise AssertionError('no trim is necessary within the budget')
    cache.entries = DoNotIterate(cache.entries)
    cache._trim_cache_to(cache.bytes)
    assert cache.checked == 0 and len(cache.entries) == 8


def test_lod_trim_preserves_newer_pixels_and_oldest_unpinned_order(monkeypatch):
    monkeypatch.setattr(lod, 'is_deleted', lambda owner: False)
    cache = TrimFixture(2, 4)
    cache.owners[2]._preview_pixmap = Pixmap('newer-version')
    cache._trim_cache_to(4*4)
    assert list(cache.entries) == [0, 1, 4, 5]
    assert cache.owners[2]._preview_pixmap.cacheKey() == 'newer-version'
    assert cache.owners[3]._preview_pixmap is None
    assert cache.bytes == 4*4


def test_lod_all_visible_are_pinned_and_overviews_go_first(monkeypatch):
    monkeypatch.setattr(lod, 'is_deleted', lambda owner: False)
    cache = TrimFixture(8, 0)
    cache.overviews[(1, 1, 'overview')] = object()
    cache.overview_bytes = 4
    cache._trim_cache_to(0)
    assert not cache.overviews and cache.overview_bytes == 0
    assert len(cache.entries) == 8 and cache.bytes == 32
    assert cache.checked == 8


class Payload:
    def __init__(self, count=0):
        self.rows = [{'id': index, 'details': {'tags': [str(index)]}}
                     for index in range(count)]


class OperationFixture:
    def __init__(self, *, running=False, canceled=False):
        self.scene = SimpleNamespace(project_id='memory-project')
        self.view = SimpleNamespace(scene=self.scene, _save_preparing=True,
            filename='memory.prism', worker=None)
        self.filename = 'memory.prism'
        self.silent = True
        self.session = None
        self.project_id = self.scene.project_id
        self.operation_id = 'memory-operation'
        self.error = None
        self.partial = None
        self.canonical = {}
        self.board = Payload()
        self.running = running
        self.worker = SimpleNamespace(func=self.run, args=(), canceled=canceled,
            isRunning=lambda: self.running)
        self.worker.kwargs = {'worker': self.worker}
        self.view.worker = self.worker
        self.view._save_operation = self
        self.view._save_board_snapshot = self.board
        self.view.on_saving_finished = self.receipt
        self.timer = SimpleNamespace(stop=Mock(), start=Mock())
        self.ready = __import__('threading').Event()
        self.accepted = __import__('threading').Event()
        self.revision = (0, 0, 0)
        self._capture_last_end = time.monotonic()
        self._capture_clock = self._capture_last_end
        self._capture_stage = 'board'
        self._capture_wait_s = self._capture_work_s = 0
        self._capture_max_wait_s = self._capture_max_slice_s = 0
        self._capture_slices = self._capture_steps = 0
        self._capture_slow_slices = self._capture_slice_samples = 0
        self._capture_sample_mode = 'normal'
        self._capture_sample_limit = 16
        self._capture_last_sample = float('-inf')
        self._capture_last_sample_stage = None
        self._capture_max_slice_stage = self._capture_max_slice_stage_end = None
        self._receipt_finished = False
        self._worker_stopped = False
        self._capture_released = False
        self._steps = (None for _ in ())
        self.deleted_later = False
        self.receipt_calls = 0
        self.build_outcome = lambda errors: SimpleNamespace(commit_state='committed_complete')
        self._revision = lambda: self.revision
        for name in ('_owned', '_owned_source', '_drain', 'finish', '_release_capture', '_thread_finished'):
            method = getattr(preparation.SavePreparation, name, None)
            if method is not None:
                setattr(self, name, MethodType(method, self))
        self.SLICE_STEPS = preparation.SavePreparation.SLICE_STEPS
        self.SLICE_SECONDS = preparation.SavePreparation.SLICE_SECONDS
    def run(self, **kwargs):
        pass
    def receipt(self, filename, errors, **fields):
        self.receipt_calls += 1
        assert self.board is not None, 'private snapshot must survive acknowledgment'
        self.view._save_board_snapshot = None
    def deleteLater(self):
        self.deleted_later = True


def test_save_receipt_waits_for_native_thread_stop_before_releasing():
    operation = OperationFixture(running=True)
    worker = operation.worker
    payload_ref = weakref.ref(operation.board)
    operation.finish(operation.filename, [])
    assert operation.receipt_calls == 1
    assert payload_ref() is not None
    assert not operation.deleted_later, 'QObject must outlive native worker access'
    operation.running = False
    operation._thread_finished()
    assert payload_ref() is None
    assert worker.func is None and worker.args == () and worker.kwargs == {}
    assert operation.board is None and operation.worker is None
    assert operation.deleted_later


def test_native_stop_waits_for_gui_receipt_and_cleanup_is_idempotent():
    operation = OperationFixture()
    worker = operation.worker
    operation._thread_finished()
    assert operation.board is not None and worker.func is not None
    assert not operation.deleted_later
    operation.finish(operation.filename, [])
    operation._thread_finished()
    assert operation.board is None and worker.kwargs == {}
    assert operation.receipt_calls == 1


def test_stale_save_receipt_releases_private_data_without_touching_new_save():
    operation = OperationFixture()
    worker = operation.worker
    current = object()
    operation.view._save_operation = current
    operation._thread_finished()
    operation.finish(operation.filename, [])
    assert operation.view._save_operation is current
    assert operation.view._save_preparing
    assert operation.receipt_calls == 0
    assert operation.board is None and worker.func is None and worker.kwargs == {}


def test_canceled_capture_closes_suspended_copy_generator():
    operation = OperationFixture(canceled=True)
    closed = []
    reference = []
    def suspended():
        private = Payload(1000)
        reference.append(weakref.ref(private))
        try:
            yield None
            yield private
        finally:
            closed.append(True)
    operation._steps = suspended()
    next(operation._steps)
    assert reference[0]() is not None
    operation._drain()
    assert isinstance(operation.error, preparation.PartialSaveCancelled)
    assert operation.ready.is_set()
    assert operation._steps is None
    assert closed == [True] and reference[0]() is None


def test_capture_conflict_also_closes_generator_and_preserves_error():
    operation = OperationFixture()
    closed = []
    def suspended():
        try:
            yield None
        finally:
            closed.append(True)
    operation._steps = suspended()
    next(operation._steps)
    operation.view._save_operation = object()
    operation._drain()
    assert isinstance(operation.error, preparation.PartialSaveConflict)
    assert closed == [True] and operation.ready.is_set()


def test_completed_save_releases_acyclic_private_rows_before_next_gc():
    operation = OperationFixture()
    operation.board = Payload(20000)
    operation.view._save_board_snapshot = operation.board
    reference = weakref.ref(operation.board)
    operation._thread_finished()
    operation.finish(operation.filename, [])
    assert reference() is None, 'must be refcount-released, not wait for gen2'


def test_first_managed_save_projections_release_only_after_receipt_and_stop():
    operation = OperationFixture(running=True)
    worker = operation.worker
    projection = Payload(2000)
    projection._cancel = lambda: operation.worker.canceled
    reference = weakref.ref(projection)
    worker.managed_captured_items = [projection]
    del projection
    def receipt(filename, errors, **kwargs):
        assert reference() is not None
        assert len(worker.managed_captured_items) == 1
        operation.view._save_board_snapshot = None
    operation.view.on_saving_finished = receipt
    operation.finish(operation.filename, [])
    assert reference() is not None
    operation.running = False
    operation._thread_finished()
    assert worker.managed_captured_items == () and reference() is None


def test_generator_close_error_does_not_replace_cancel_or_block_worker(monkeypatch):
    operation = OperationFixture(canceled=True)
    class Capture:
        def close(self):
            raise ValueError('injected cleanup failure')
    operation._steps = Capture()
    report = Mock()
    monkeypatch.setattr(preparation.logger, 'exception', report)
    operation._drain()
    assert isinstance(operation.error, preparation.PartialSaveCancelled)
    assert operation.ready.is_set() and operation._steps is None
    assert operation._capture_steps == 0
    report.assert_called_once()


def test_cancel_closes_real_sliced_copy_memo_without_changing_live_source():
    operation = OperationFixture(canceled=True)
    copied = []
    class Marker:
        def __deepcopy__(self, memo):
            result = Marker()
            memo[id(self)] = result
            copied.append(weakref.ref(result))
            return result
    marker = Marker()
    shared = [{'tags': ['live']}] * 1000
    source = {'marker': marker, 'rows': shared}
    operation._steps = preparation.sliced_copy(source)
    next(operation._steps)
    assert copied and copied[0]() is not None
    operation._drain()
    assert copied[0]() is None and operation._steps is None
    assert source['marker'] is marker and source['rows'] is shared
    assert shared[0]['tags'] == ['live']


def test_completed_capture_keeps_existing_alias_and_cycle_copy_contract():
    shared = ['tag']
    source = {'first': shared, 'second': shared}
    source['cycle'] = source
    steps = preparation.sliced_copy(source)
    while True:
        try:
            next(steps)
        except StopIteration as done:
            copied = done.value
            break
    assert copied is not source
    assert copied['first'] is copied['second'] and copied['first'] is not shared
    assert copied['cycle'] is copied


def test_real_qthread_receipt_before_stop_keeps_capture_without_widgets(tmp_path):
    from PyQt6 import QtCore
    from prism.fileio.save_preparation import SaveWorker, SavePreparation
    import threading

    app = QtCore.QCoreApplication.instance() or QtCore.QCoreApplication([])
    assert not hasattr(app, 'topLevelWidgets'), 'no QApplication is allowed'
    target = str(tmp_path / 'never-written.prism')
    view = QtCore.QObject()
    view.scene = SimpleNamespace(project_id='core-only-project')
    view._content_revision = view._undo_revision = 0
    view.undo_stack = SimpleNamespace(index=lambda: 0)
    view.filename = target
    operation = SavePreparation(view, target, False, True)
    operation.board = Payload(5000)
    payload_ref = weakref.ref(operation.board)
    view._save_operation = operation
    view._save_preparing = True
    view._save_board_snapshot = operation.board
    release = threading.Event()
    entered = threading.Event()
    receipts = []
    def receipt(filename, errors, **kwargs):
        assert payload_ref() is not None
        view._save_board_snapshot = None
        receipts.append(filename)
    view.on_saving_finished = receipt
    operation.build_outcome = lambda errors: SimpleNamespace(commit_state='committed_complete')
    def run(*, worker):
        worker.finished.emit(target, [])
        entered.set()
        if not release.wait(5):
            raise RuntimeError('test did not release native worker')
    worker = SaveWorker(run)
    operation.worker = worker
    view.worker = worker
    worker.finished.connect(operation.finish)
    stopped = QtCore.QThread.finished.__get__(worker, type(worker))
    stopped.connect(operation._thread_finished)
    def until(predicate):
        deadline = time.monotonic()+5
        while not predicate() and time.monotonic() < deadline:
            app.processEvents(QtCore.QEventLoop.ProcessEventsFlag.AllEvents, 10)
        assert predicate(), 'queued signal was not delivered'
    try:
        worker.start()
        assert entered.wait(5)
        until(lambda: operation._receipt_finished)
        assert receipts == [target]
        assert worker.isRunning()
        assert payload_ref() is not None and not operation._capture_released
        release.set()
        assert worker.wait(5000)
        until(lambda: operation._capture_released)
        assert payload_ref() is None
        assert worker.kwargs == {} and worker.func is None
        assert not __import__('pathlib').Path(target).exists()
    finally:
        release.set()
        assert worker.wait(5000), 'never leave a live QThread behind'
