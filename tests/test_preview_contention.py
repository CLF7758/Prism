"""A busy disposable cache must not destroy previews or stall zoom workers."""
import sqlite3
import time
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

from PyQt6 import QtGui
from prism.thumbnail_cache import ThumbnailCache


def picture():
    image = QtGui.QImage(32, 16, QtGui.QImage.Format.Format_RGB32)
    image.fill(QtGui.QColor('red'))
    return image


def test_busy_writer_is_a_short_miss_and_preserves_existing_previews(tmp_path):
    cache = ThumbnailCache(str(tmp_path))
    assert cache.store('existing', picture(), 320, 160)
    writer = sqlite3.connect(cache.path)
    try:
        writer.execute('BEGIN IMMEDIATE')
        start = time.perf_counter()
        assert not cache.store('blocked', picture(), 320, 160)
        assert time.perf_counter()-start < 1
        assert cache._failures == 0 and not cache._broken
        assert cache.load('existing').width == 320
    finally:
        writer.rollback()
        writer.close()
    assert cache.store('after', picture(), 320, 160)
    assert cache.load('existing') is not None
    assert cache.load('after') is not None
    cache.close()


def test_locked_first_connection_does_not_delete_the_database(tmp_path):
    seed = ThumbnailCache(str(tmp_path))
    assert seed.store('existing', picture(), 320, 160)
    seed.close()
    writer = sqlite3.connect(seed.path)
    writer.execute('PRAGMA journal_mode=DELETE')
    writer.execute('BEGIN EXCLUSIVE')
    cache = ThumbnailCache(str(tmp_path))
    try:
        start = time.perf_counter()
        assert cache.load('existing') is None
        assert time.perf_counter()-start < 1
        assert cache._failures == 0 and not cache._broken
    finally:
        writer.rollback()
        writer.close()
    assert cache.load('existing').width == 320
    cache.close()


def test_concurrent_initialization_keeps_successfully_written_entries(tmp_path):
    cache = ThumbnailCache(str(tmp_path))
    barrier = Barrier(8)
    def work(index):
        barrier.wait(timeout=5)
        try:
            return index, cache.store(str(index), picture(), 320, 160)
        finally:
            cache.close()
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(work, range(8)))
    assert any(saved for _index, saved in results)
    assert cache._failures == 0 and not cache._broken
    for index, saved in results:
        if saved:
            assert cache.load(str(index)).width == 320
    cache.close()
