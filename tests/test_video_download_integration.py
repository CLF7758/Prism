"""Original retention and the real upstream download path, using local fixtures."""
import functools
import http.server
import io
import json
from pathlib import Path
import sqlite3
import subprocess
import threading
from unittest.mock import Mock
import pytest
from PyQt6 import QtCore
from prism import source_store
from prism.items import PrismVideoItem
from prism.fileio.sql import SQLiteIO
from prism.video_download_service import VideoDownloadService, component_directory


def test_platform_share_text_and_errors():
    from prism.video_urls import normalize_share_url
    from prism.components.video_download_worker import explain_error
    assert normalize_share_url(r'4.66 复制打开抖音 https\://v.douyin.com/jqRzPBPNiwc/ k@P.xf') == 'https://v.douyin.com/jqRzPBPNiwc/'
    assert normalize_share_url('https://www.bilibili.com/video/BV13aNd64E51/?p=2&trackid=private') == 'https://www.bilibili.com/video/BV13aNd64E51/?p=2'
    assert '登录状态' in explain_error('Fresh cookies are needed')
    assert '访问风控' in explain_error('HTTP Error 412')


def test_streamed_original_roundtrip(view, tmp_path, monkeypatch):
    monkeypatch.setattr(PrismVideoItem, '_capture_thumbnail', lambda self: None)
    original = b'video-original\x00'*200000
    class Bounded(io.BytesIO):
        def read(self, size=-1):
            assert 0 < size <= 1024*1024
            return super().read(size)
    path = source_store.retain_stream(Bounded(original), '.mp4')
    item = PrismVideoItem(QtCore.QUrl.fromLocalFile(path), 'sample.mp4')
    item._video_source_path = path
    item._canvas_id = 'default-canvas'
    view.scene.addItem(item)
    project = str(tmp_path/'video.prism')
    SQLiteIO(project, view.scene, create_new=True).write()
    with sqlite3.connect(project) as database:
        assert database.execute('SELECT data FROM sqlar').fetchone()[0] == original
    view.clear_scene()
    SQLiteIO(project, view.scene, readonly=True).read()
    view.scene.add_queued_items()
    restored = next(i for i in view.scene.items() if getattr(i, 'TYPE', None) == 'video')
    assert restored._video_blob is None
    assert Path(restored._video_source_path).read_bytes() == original
    copy = restored.create_copy()
    assert copy._video_source_path == restored._video_source_path
    SQLiteIO(str(tmp_path/'resaved.prism'), view.scene, create_new=True).write()
    with sqlite3.connect(tmp_path/'resaved.prism') as database:
        assert database.execute('SELECT data FROM sqlar').fetchone()[0] == original


def test_download_dialog_and_project_guard(view, monkeypatch):
    from prism.widgets.video_download import VideoDownloadDialog
    dialog = VideoDownloadDialog(view)
    view.do_insert_images = Mock()
    result = dict(event='completed', path='video.mp4', context=dict(project_id='stale', canvas_id='default-canvas'))
    assert not dialog.destination(result)
    view.do_insert_images.assert_not_called()
    dialog.service.close()
    dialog.deleteLater()


def test_actual_ytdlp_download_and_probe(view, tmp_path, qtbot, monkeypatch):
    monkeypatch.setenv('NO_PROXY', '127.0.0.1,localhost')
    monkeypatch.setenv('no_proxy', '127.0.0.1,localhost')
    root = component_directory()
    if not (root/'runtime/Scripts/python.exe').exists():
        pytest.skip('Optional yt-dlp component is not installed')
    clip = tmp_path/'sample.mp4'
    subprocess.run([str(root/'bin/ffmpeg.exe'), '-v', 'error', '-f', 'lavfi',
        '-i', 'color=c=blue:s=160x90:d=0.5', '-c:v', 'libx264', '-pix_fmt', 'yuv420p', str(clip)],
        check=True, capture_output=True, timeout=30,
        creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
    class Handler(http.server.SimpleHTTPRequestHandler):
        def log_message(self, *args):
            pass
    handler = functools.partial(Handler, directory=str(tmp_path))
    server = http.server.ThreadingHTTPServer(('127.0.0.1', 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    service = VideoDownloadService(view)
    results, errors = [], []
    service.finished.connect(lambda key, data: results.append(data))
    service.failed.connect(lambda key, text: errors.append(text))
    try:
        service.submit(f'http://127.0.0.1:{server.server_port}/sample.mp4', tmp_path/'downloads')
        qtbot.waitUntil(lambda: bool(results or errors), timeout=60000)
        assert not errors, errors
        assert results[0]['event'] == 'completed'
        assert Path(results[0]['path']).read_bytes() == clip.read_bytes()
    finally:
        service.close()
        server.shutdown()
        server.server_close()
