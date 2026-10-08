"""Image-reader lock and asynchronous canvas regressions."""
import os
from pathlib import Path
import subprocess
import sqlite3
import sys
import threading
from unittest.mock import Mock

import pytest
from PyQt6 import QtCore, QtGui, QtWidgets

from prism import fileio, image_decode, thumbnail_cache


def source(tmp_path):
    image = QtGui.QImage(1600, 900, QtGui.QImage.Format.Format_ARGB32)
    image.fill(QtGui.QColor(40, 80, 120, 128))
    path = tmp_path / 'source.png'
    assert image.save(str(path))
    return path


def test_native_reader_keeps_file_alive_and_cleans_up(qapp, tmp_path):
    with image_decode.native_reader(blob=source(tmp_path).read_bytes()) as reader:
        path = Path(reader.fileName())
        assert path.exists()
        assert isinstance(reader.device(), QtCore.QFile)
        assert reader.read().size() == QtCore.QSize(1600, 900)
    assert not path.exists()


def test_preview_preserves_dimensions_and_transparency(qapp, tmp_path):
    image, size = thumbnail_cache.decode_preview_image(source(tmp_path).read_bytes())
    assert size == QtCore.QSize(1600, 900)
    assert max(image.width(), image.height()) == thumbnail_cache.PREVIEW_MAX_SIDE
    assert image.pixelColor(0, 0).alpha() == 128
    assert thumbnail_cache.decode_preview_image(b'broken image') == (None, None)


def test_preview_applies_exif_rotation(qapp, tmp_path):
    from PIL import Image
    path = tmp_path / 'rotated.jpg'
    exif = Image.Exif()
    exif[274] = 6
    Image.new('RGB', (800, 400), 'red').save(path, exif=exif)
    image, size = thumbnail_cache.decode_preview_image(path.read_bytes())
    assert size == QtCore.QSize(400, 800)
    assert image.height() == thumbnail_cache.PREVIEW_MAX_SIDE
    assert image.width() < image.height()


def test_archive_source_can_decode_without_original_file(qapp, tmp_path):
    path = source(tmp_path)
    archive = tmp_path / 'board.prism'
    with sqlite3.connect(archive) as connection:
        connection.execute('CREATE TABLE sqlar (item_id INTEGER, data BLOB)')
        connection.execute('INSERT INTO sqlar VALUES (?, ?)', (7, path.read_bytes()))
    path.unlink()
    image, size = image_decode.decode(archive=(str(archive), 7), max_side=192)
    assert size == QtCore.QSize(1600, 900)
    assert image.width() == 192


@pytest.mark.parametrize('missing_preview', [False, True])
def test_paint_queues_decode_without_reading_source(view, qtbot, tmp_path,
                                                  monkeypatch, missing_preview):
    item = fileio._load_image_item(str(source(tmp_path)))
    view.scene.addItem(item)
    if missing_preview:
        item._preview_pixmap = None
    real_decode = image_decode.decode
    worker_threads = []

    def checked_decode(*args, **kwargs):
        worker_threads.append(QtCore.QThread.currentThread())
        return real_decode(*args, **kwargs)

    monkeypatch.setattr(image_decode, 'decode', checked_decode)
    monkeypatch.setattr(item, 'original_bytes', Mock(side_effect=AssertionError('GUI file read')))
    monkeypatch.setattr(item, '_decode_source_blob', Mock(side_effect=AssertionError('GUI decode')))
    canvas = QtGui.QImage(32, 32, QtGui.QImage.Format.Format_RGB32)
    painter = QtGui.QPainter(canvas)
    try:
        display, _ = item._pixmap_for_painting(painter)
        assert not item.has_full_pixmap()
        assert display.isNull() == missing_preview
        qtbot.waitUntil(lambda: item._display_decode_request is None, timeout=5000)
        if missing_preview:
            assert item._preview_pixmap is not None
            item._pixmap_for_painting(painter)
            qtbot.waitUntil(item.has_full_pixmap, timeout=5000)
        assert item.has_full_pixmap()
        assert QtWidgets.QGraphicsPixmapItem.pixmap(item).size() == QtCore.QSize(1600, 900)
        assert worker_threads
        assert all(thread != QtWidgets.QApplication.instance().thread()
                   for thread in worker_threads)
    finally:
        painter.end()


@pytest.mark.parametrize('action', ['adjust', 'remove', 'delete'])
def test_late_decode_does_not_overwrite_or_access_removed_item(view, qtbot,
                                                              tmp_path, monkeypatch, action):
    item = fileio._load_image_item(str(source(tmp_path)))
    view.scene.addItem(item)
    entered, release, finished = threading.Event(), threading.Event(), threading.Event()
    real_decode = image_decode.decode

    def delayed(*args, **kwargs):
        entered.set()
        assert release.wait(5)
        result = real_decode(*args, **kwargs)
        finished.set()
        return result

    monkeypatch.setattr(image_decode, 'decode', delayed)
    image_decode.request_display(item)
    try:
        qtbot.waitUntil(entered.is_set, timeout=5000)
        if action == 'adjust':
            replacement = QtGui.QPixmap(1600, 900)
            replacement.fill(QtGui.QColor('blue'))
            item.set_preview_pixmap(replacement)
        elif action == 'remove':
            view.scene.removeItem(item)
        else:
            view.scene.clear()
        release.set()
        qtbot.waitUntil(finished.is_set, timeout=5000)
        qtbot.wait(50)
        if action == 'adjust':
            assert item.pixmap().toImage().pixelColor(0, 0) == QtGui.QColor('blue')
        elif action == 'remove':
            assert not item.has_full_pixmap()
    finally:
        release.set()


def test_concurrent_header_and_blob_reads_finish(qapp, tmp_path):
    # Use a child so a native lock regression fails with a timeout rather
    # than hanging the entire test runner.
    path = source(tmp_path)
    code = '''
import sys, threading
from PyQt6 import QtGui
from prism.image_decode import decode
path = sys.argv[1]
blob = open(path, 'rb').read()
barrier = threading.Barrier(2)
def headers():
    barrier.wait()
    for _ in range(1000):
        assert QtGui.QImageReader(path).size().width() == 1600
worker = threading.Thread(target=headers)
worker.start()
barrier.wait()
for _ in range(100):
    assert decode(blob=blob, max_side=192)[1].width() == 1600
worker.join()
'''
    env = os.environ.copy()
    env['PYTHONPATH'] = os.pathsep.join(sys.path)
    completed = subprocess.run([sys._base_executable, '-c', code, str(path)],
                               env=env, capture_output=True, text=True, timeout=30)
    assert completed.returncode == 0, completed.stderr
