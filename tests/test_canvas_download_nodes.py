import json
import os
from pathlib import Path
import time
from unittest.mock import Mock

import pytest
from PyQt6 import QtCore, QtGui, QtWidgets, sip
from prism.actions.video_download_workflow import controller
from prism.items import PrismPixmapItem, PrismVideoItem
from prism.video_urls import platform_for_url


def test_platform_routing_respects_domain_boundaries():
    assert platform_for_url('https://v.douyin.com/abc/') == 'douyin'
    assert platform_for_url('https://xhslink.cn/o/abc') == 'xiaohongshu'
    assert platform_for_url('https://www.bilibili.com/video/BV123') == 'bilibili'
    assert platform_for_url('https://b23.tv/abc') == 'bilibili'
    assert platform_for_url('https://bilibili.com.attacker.test/') == 'ytdlp'


def test_node_position_scope_cancel_and_not_saved(view, monkeypatch):
    manager = controller(view)
    monkeypatch.setattr(view, '_material_canvas', lambda: None)
    manager.service.submit = Mock(return_value='job')
    manager.service.cancel = Mock()
    point = QtCore.QPointF(123, 456)
    node = manager.create(point)
    assert node.pos() == point
    assert node not in list(view.scene.items_for_save())
    node.url.setText('https://v.douyin.com/test/')
    node.start.click()
    assert manager.service.submit.call_args.kwargs['context']['canvas_id'] == 'default-canvas'
    assert not node.start.isEnabled()
    manager.progress('job', dict(phase='download', downloaded=50, total=100))
    assert node.bar.value() == 500
    view.current_canvas_id = 'different'
    manager.sync()
    assert not node.isVisible()
    node.cancel.click()
    manager.service.cancel.assert_called_with('job')
    view.current_canvas_id = 'default-canvas'
    manager.sync()
    assert node.isVisible()
    manager.remove(manager.nodes[0])
    assert not manager.nodes
    manager.service.close()


def test_completion_inserts_into_origin_without_stealing_selection(view, monkeypatch, tmp_path):
    monkeypatch.setattr(PrismVideoItem, '_capture_thumbnail', lambda self: None)
    monkeypatch.setattr(view, '_material_canvas', lambda: None)
    photo = PrismPixmapItem(QtGui.QImage(30, 20, QtGui.QImage.Format.Format_RGB32))
    photo._canvas_id = 'default-canvas'
    view.scene.addItem(photo)
    photo.setSelected(True)
    manager = controller(view)
    node = manager.create(QtCore.QPointF(123, 456))
    entry = manager.nodes[0]
    entry['job'] = 'job'
    path = tmp_path/'clip.mp4'
    path.write_bytes(b'fixture')
    view.scene.workspace_pages.append(dict(id='other-canvas', kind='canvas', title='Other'))
    view.current_canvas_id = 'other-canvas'
    photo._canvas_id = 'other-canvas'
    manager.finished('job', dict(event='completed', path=str(path), title='test'))
    video = next(i for i in view.scene.items_for_save() if i.TYPE == 'video')
    assert video._canvas_id == 'default-canvas'
    assert video.pos() == QtCore.QPointF(123, 456)
    assert photo.isSelected() and not video.isSelected()
    assert view.current_canvas_id == 'other-canvas'
    assert not video.isVisible()
    assert video._video_source_path == str(path)
    view.undo_stack.undo()
    assert video.scene() is None
    view.undo_stack.redo()
    assert video.scene() is view.scene
    manager.remove(entry)
    manager.service.close()


def test_deleted_scene_cancels_stale_nodes_without_touching_new_project(view, monkeypatch):
    monkeypatch.setattr(view, '_material_canvas', lambda: None)
    manager = controller(view)
    node = manager.create(QtCore.QPointF())
    view.scene.clear()
    assert sip.isdeleted(node)
    manager.sync()
    assert manager.nodes == []
    manager.service.close()


def test_real_canvas_node_text_input_and_double_click(view, qtbot, monkeypatch):
    monkeypatch.setattr(view, '_material_canvas', lambda: None)
    manager = controller(view)
    view.window().show()
    node = manager.create(view.mapToScene(view.viewport().rect().center()))
    view.centerOn(node)
    qtbot.wait(50)
    point = view.mapFromScene(node.mapToScene(QtCore.QPointF(node.url.geometry().center())))
    qtbot.mouseClick(view.viewport(), QtCore.Qt.MouseButton.LeftButton, pos=point)
    qtbot.keyClicks(view.viewport(), 'https://example.com/a.mp4')
    assert node.url.text() == 'https://example.com/a.mp4'
    qtbot.mouseDClick(view.viewport(), QtCore.Qt.MouseButton.LeftButton, pos=point)
    assert view.scene.crop_item is None
    manager.remove(manager.nodes[0])
    manager.service.close()


@pytest.mark.skipif(os.environ.get('PRISM_LIVE_VIDEO') != '1', reason='Explicit live-platform acceptance only')
def test_three_original_links_through_real_service(view, qtbot):
    from prism.video_download_service import VideoDownloadService
    destination = Path(os.environ['PRISM_ACCEPTANCE_OUTPUT'])
    destination.mkdir(parents=True, exist_ok=True)
    service = VideoDownloadService(view)
    results, errors, events = [], [], []
    service.finished.connect(lambda key, result: results.append(result))
    service.failed.connect(lambda key, message: errors.append(dict(job=key, error=message)))
    service.progress.connect(lambda key, result: events.append((key, result.get('phase'))))
    ticks = []
    sources = {}
    timer = QtCore.QTimer()
    timer.setInterval(20)
    timer.timeout.connect(lambda: ticks.append(time.monotonic()))
    timer.start()
    try:
        for url in ('https://v.douyin.com/jqRzPBPNiwc/', 'https://xhslink.cn/o/G0DxqcoL7W', 'https://www.bilibili.com/video/BV13aNd64E51/'):
            sources[service.submit(url, destination, retain_original=True)] = url
        qtbot.waitUntil(lambda: len(results)+len(errors) == 3, timeout=900000)
        report = dict(results=results, errors=errors, progress_events=len(events), ui_ticks=len(ticks),
            max_tick_gap_seconds=max((b-a for a,b in zip(ticks,ticks[1:])), default=0))
        (destination/'integration-results.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
        # The later original-quality policy explicitly forbids accepting an
        # anonymous 480p substitute for Bilibili's advertised 1080p source.
        for error in errors:
            assert 'bilibili.com' in sources[error['job']] and service.jobs[error['job']]['result'].get('code') == 'quality_limited', errors
        assert len(results)+len(errors) == 3 and len(results) >= 2
        assert all(r['event'] == 'completed' and Path(r['path']).is_file() for r in results)
        import hashlib
        for result in results:
            with Path(result['path']).open('rb') as source, Path(result['original_path']).open('rb') as retained:
                assert hashlib.file_digest(source, 'sha256').digest() == hashlib.file_digest(retained, 'sha256').digest()
        assert len(ticks) > 10
    finally:
        timer.stop()
        service.close()
