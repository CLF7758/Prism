import hashlib
import io
from pathlib import Path
import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import Mock

import pytest
from PyQt6 import QtWidgets

from prism import source_store, video_thumbnails
from prism.items import PrismVideoItem


@pytest.fixture
def archive(tmp_path):
    path = tmp_path/'original.prism'
    data = b'original-video-bytes\x00' * 120000
    with sqlite3.connect(path) as db:
        db.execute('CREATE TABLE sqlar(item_id INTEGER, data BLOB)')
        db.execute('INSERT INTO sqlar VALUES(?,?)', (7, data))
    return (str(path), 7), data


def test_archive_original_streamed_and_fingerprinted_without_read_archive(archive, monkeypatch):
    source, data = archive
    monkeypatch.setattr(source_store, 'read_archive', Mock(side_effect=AssertionError('whole BLOB read')))
    original = source_store.retain_stream
    reads = []

    def retain(blob, suffix):
        class Limited:
            def read(self, size):
                reads.append(size)
                assert 0 < size <= 1024*1024
                return blob.read(size)
        return original(Limited(), suffix)

    monkeypatch.setattr(source_store, 'retain_stream', retain)
    before = Path(source[0]).read_bytes()
    path = source_store.retain_archive_video(source)
    assert reads and Path(path).read_bytes() == data
    assert source_store.stream_fingerprint(path) == (hashlib.sha256(data).hexdigest(), len(data))
    assert Path(source[0]).read_bytes() == before
    assert source_store.retain_archive_video(source) == path


def test_concurrent_same_source_extracts_only_once(archive, monkeypatch):
    source, data = archive
    original = source_store.retain_stream
    entered, release = threading.Event(), threading.Event()
    calls = []

    def retain(blob, suffix):
        calls.append(True)
        entered.set()
        assert release.wait(5)
        return original(blob, suffix)

    monkeypatch.setattr(source_store, 'retain_stream', retain)
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(source_store.retain_archive_video, source)
        assert entered.wait(5)
        second = pool.submit(source_store.retain_archive_video, source)
        release.set()
        assert first.result(5) == second.result(5)
    assert len(calls) == 1


def test_failed_stream_removes_partial_file(monkeypatch, tmp_path):
    class Broken(io.BytesIO):
        def read(self, size):
            if self.tell():
                raise OSError('read failed')
            return super().read(size)
    directory = type('Directory', (), {'name': str(tmp_path)})()
    monkeypatch.setattr(source_store, '_directory', directory)
    with pytest.raises(OSError):
        source_store.retain_stream(Broken(b'partial'), '.mp4')
    assert not list(tmp_path.iterdir())


def test_failed_first_request_keeps_waiting_retry_guard(archive, monkeypatch, qtbot):
    source, data = archive
    original = source_store.retain_stream
    entered_first, release_first = threading.Event(), threading.Event()
    entered_retry, release_retry = threading.Event(), threading.Event()
    entered_duplicate = threading.Event()
    counter_lock = threading.Lock()
    calls = []
    key = source_store.archive_video_key(source, '.mp4')

    def users():
        with source_store._lock:
            entry = source_store._archive_extract_locks.get(key)
            return entry[1] if entry else 0

    def retain(blob, suffix):
        with counter_lock:
            calls.append(True)
            number = len(calls)
        if number == 1:
            entered_first.set()
            assert release_first.wait(5)
            raise OSError('first extraction fails')
        if number == 2:
            entered_retry.set()
            assert release_retry.wait(5)
        else:
            entered_duplicate.set()
        return original(blob, suffix)

    monkeypatch.setattr(source_store, 'retain_stream', retain)
    with ThreadPoolExecutor(max_workers=3) as pool:
        try:
            first = pool.submit(source_store.retain_archive_video, source)
            assert entered_first.wait(3)
            second = pool.submit(source_store.retain_archive_video, source)
            qtbot.waitUntil(lambda: users() == 2, timeout=3000)
            release_first.set()
            with pytest.raises(OSError, match='first extraction fails'):
                first.result(3)
            assert entered_retry.wait(3)
            third = pool.submit(source_store.retain_archive_video, source)
            qtbot.waitUntil(lambda: users() == 2 or entered_duplicate.is_set(), timeout=3000)
            assert not entered_duplicate.is_set()
            release_retry.set()
            path = second.result(3)
            assert third.result(3) == path
            assert Path(path).read_bytes() == data
            assert len(calls) == 2  # Failed first extraction + one shared retry.
            assert users() == 0
        finally:
            release_first.set()
            release_retry.set()


@pytest.mark.parametrize('cancel', ['none', 'pause', 'cleanup', 'project', 'remove'])
def test_play_prepares_in_worker_and_rejects_late_results(qapp, qtbot, archive, monkeypatch, cancel):
    source, data = archive
    service = video_thumbnails.VideoMaterializer(qapp)
    monkeypatch.setattr(video_thumbnails, '_materializer', service)
    monkeypatch.setattr(PrismVideoItem, '_capture_thumbnail', lambda self: None)
    scene = QtWidgets.QGraphicsScene()
    scene.project_id = 'first'
    item = PrismVideoItem(filename='sample.mp4')
    item._prism_source = source
    scene.addItem(item)
    item._start_playback = Mock()
    entered, release = threading.Event(), threading.Event()
    threads = []
    original = source_store.retain_archive_video

    def prepare(*args):
        threads.append(threading.get_ident())
        entered.set()
        assert release.wait(5)
        return original(*args)

    monkeypatch.setattr(source_store, 'retain_archive_video', prepare)
    try:
        item.play()
        item.play()  # Repeated clicking shares the pending request.
        assert entered.wait(3)
        assert item._video_source_path is None
        assert threads == [threads[0]] and threads[0] != threading.get_ident()
        if cancel == 'pause':
            item.pause()
        elif cancel == 'cleanup':
            item.cleanup()
        elif cancel == 'project':
            scene.project_id = 'reopened'
        elif cancel == 'remove':
            scene.removeItem(item)
        release.set()
        qtbot.waitUntil(lambda: not service.running, timeout=5000)
        if cancel == 'none':
            item._start_playback.assert_called_once()
            assert Path(item._video_source_path).read_bytes() == data
            assert item._ensure_materialized() == item._video_source_path
        else:
            item._start_playback.assert_not_called()
    finally:
        release.set()
        service.pool.waitForDone(5000)
        service.close()
        scene.removeItem(item) if item.scene() else None
        item.cleanup()


def test_save_export_materialization_remains_synchronous(qapp, archive):
    source, data = archive
    item = PrismVideoItem(filename='sample.mp4')
    item._prism_source = source
    path = item._ensure_materialized()
    assert Path(path).read_bytes() == data
    assert Path(item._video_url.toLocalFile()) == Path(path)
    item.cleanup()


def test_same_source_play_clients_share_worker_but_cancel_independently(qapp, qtbot, archive, monkeypatch):
    source, data = archive
    service = video_thumbnails.VideoMaterializer(qapp)
    monkeypatch.setattr(video_thumbnails, '_materializer', service)
    items = [PrismVideoItem(filename='sample.mp4') for _ in range(2)]
    for item in items:
        item._prism_source = source
        item._start_playback = Mock()
    entered, release = threading.Event(), threading.Event()
    calls = []
    original = source_store.retain_archive_video
    def prepare(*args):
        calls.append(True)
        entered.set()
        assert release.wait(5)
        return original(*args)
    monkeypatch.setattr(source_store, 'retain_archive_video', prepare)
    try:
        items[0].play()
        assert entered.wait(3)
        items[1].play()
        items[0].pause()
        release.set()
        qtbot.waitUntil(lambda: not service.running, timeout=5000)
        assert len(calls) == 1
        items[0]._start_playback.assert_not_called()
        items[1]._start_playback.assert_called_once()
    finally:
        release.set()
        service.pool.waitForDone(5000)
        service.close()
        for item in items:
            item.cleanup()
