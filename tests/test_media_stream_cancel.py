"""Cooperative media cancellation preserves active shared original consumers."""
import io
from pathlib import Path
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import Mock

import pytest
from PyQt6 import QtWidgets

from prism import source_store, video_thumbnails
from prism.items import PrismVideoItem
from tests.test_video_materialization import archive


def wait_clients(key, count):
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline:
        with source_store._lock:
            entry = source_store._archive_extract_locks.get(key)
            if entry and entry[1] == count:
                return
        time.sleep(.005)
    raise AssertionError('expected shared extraction clients did not register')


def test_stream_cancel_removes_partial_and_fingerprint(tmp_path, monkeypatch):
    monkeypatch.setattr(source_store, '_directory', type('Directory', (), {'name': str(tmp_path)})())
    cancelled = threading.Event()
    reads = []
    class Stream(io.BytesIO):
        def read(self, size):
            reads.append(size)
            chunk = super().read(size)
            cancelled.set()
            return chunk
    with pytest.raises(source_store.MaterializationCancelled):
        source_store.retain_stream(Stream(b'x' * (3 * 1024 * 1024)), '.mp4', cancel=cancelled.is_set)
    assert reads == [1024 * 1024]
    assert list(tmp_path.iterdir()) == []


def test_cancelled_leader_keeps_valid_shared_consumer(archive, monkeypatch):
    source, data = archive
    entered, release, cancelled = threading.Event(), threading.Event(), threading.Event()
    original = source_store.retain_stream
    calls = []
    def retain(stream, suffix):
        calls.append(True)
        entered.set()
        assert release.wait(3)
        return original(stream, suffix)
    monkeypatch.setattr(source_store, 'retain_stream', retain)
    key = source_store.archive_video_key(source, '.mp4')
    before = Path(source[0]).read_bytes()
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(source_store.retain_archive_video, source, cancel=cancelled.is_set)
        try:
            assert entered.wait(3)
            second = pool.submit(source_store.retain_archive_video, source)
            wait_clients(key, 2)
            cancelled.set()
            release.set()
            with pytest.raises(source_store.MaterializationCancelled):
                first.result(3)
            path = second.result(3)
            assert Path(path).read_bytes() == data
            assert len(calls) == 1
        finally:
            release.set()
    assert Path(source[0]).read_bytes() == before
    assert key not in source_store._archive_extract_locks


def test_cancelled_waiter_does_not_wait_for_shared_holder(archive, monkeypatch):
    source, data = archive
    entered, release, cancelled = threading.Event(), threading.Event(), threading.Event()
    original = source_store.retain_stream
    def retain(stream, suffix):
        entered.set()
        assert release.wait(3)
        return original(stream, suffix)
    monkeypatch.setattr(source_store, 'retain_stream', retain)
    key = source_store.archive_video_key(source, '.mp4')
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(source_store.retain_archive_video, source)
        try:
            assert entered.wait(3)
            second = pool.submit(source_store.retain_archive_video, source, cancel=cancelled.is_set)
            wait_clients(key, 2)
            cancelled.set()
            with pytest.raises(source_store.MaterializationCancelled):
                second.result(1)
            assert not first.done()
            release.set()
            assert Path(first.result(3)).read_bytes() == data
        finally:
            release.set()


def test_pause_cancels_inflight_copy_and_removes_partial(qapp, qtbot, archive, tmp_path, monkeypatch):
    source, data = archive
    retained = tmp_path / 'retained'
    retained.mkdir()
    monkeypatch.setattr(source_store, '_directory', type('Directory', (), {'name': str(retained)})())
    monkeypatch.setattr(PrismVideoItem, '_capture_thumbnail', lambda self: None)
    service = video_thumbnails.VideoMaterializer(qapp)
    monkeypatch.setattr(video_thumbnails, '_materializer', service)
    item = PrismVideoItem(filename='sample.mp4')
    item._prism_source = source
    item._start_playback = Mock()
    entered, release = threading.Event(), threading.Event()
    original = source_store.retain_stream
    reads = []
    def retain(blob, suffix):
        class Stream:
            def read(self, size):
                reads.append(size)
                chunk = blob.read(size)
                entered.set()
                assert release.wait(3)
                return chunk
        return original(Stream(), suffix)
    monkeypatch.setattr(source_store, 'retain_stream', retain)
    try:
        item.play()
        qtbot.waitUntil(entered.is_set, timeout=3000)
        item.pause()
        # Same GUI callback the service's 50 ms timer runs.
        service.cancel_obsolete()
        release.set()
        qtbot.waitUntil(lambda: not service.running, timeout=3000)
        assert reads == [1024 * 1024]
        assert not list(retained.iterdir())
        item._start_playback.assert_not_called()
    finally:
        release.set()
        service.pool.waitForDone(3000)
        service.close()
        item.cleanup()
