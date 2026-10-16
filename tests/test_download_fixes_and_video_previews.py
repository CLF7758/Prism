import os
import asyncio
import sys
from pathlib import Path
import re
import subprocess
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from PyQt6 import QtCore, QtGui
from prism.actions.video_download_workflow import controller
from prism.components.platform_links import xhs_destination, douyin_identifier, xhs_page_failure, xhs_request_cookie
from prism.components.platform_video_worker import require_original_resolution
from prism.items import PrismVideoItem, PrismPixmapItem


def test_share_destination_uses_target_id_and_retains_only_access_fields():
    url, message = xhs_destination('https://www.xiaohongshu.com/explore?target_note_id=6acf3e73000000001801b988&xsec_token=example&share_id=private&undertake_note_error=unavailable')
    assert '/explore/6acf3e73000000001801b988?' in url
    assert 'xsec_token=example' in url
    assert 'share_id' not in url and 'undertake_note_error' not in url
    assert message == 'unavailable'
    with pytest.raises(ValueError):
        xhs_destination('https://attacker.test/note')


def test_xhs_selected_session_is_used_and_other_sites_are_excluded(tmp_path):
    path = tmp_path/'cookies.txt'
    path.write_text('# Netscape HTTP Cookie File\n'
        '.xiaohongshu.com\tTRUE\t/\tTRUE\t0\tweb_session\txhs-session\n'
        'edith.xiaohongshu.com\tFALSE\t/\tTRUE\t0\ta1\tsite-value\n'
        '.example.test\tTRUE\t/\tTRUE\t0\tsecret\tother-site\n'
        '.evilxiaohongshu.com\tTRUE\t/\tTRUE\t0\tsecret\twrong-domain\n', encoding='utf8')
    cookie = xhs_request_cookie({'cookie_file':str(path)})
    assert 'web_session=xhs-session' in cookie and 'a1=site-value' in cookie
    assert 'other-site' not in cookie and 'wrong-domain' not in cookie
    assert xhs_request_cookie({}) == ''
    path.write_text('# Netscape HTTP Cookie File\n.xiaohongshu.com\tTRUE\t/\tTRUE\t1\tweb_session\texpired\n')
    with pytest.raises(ValueError, match='未过期'):
        xhs_request_cookie({'cookie_file':str(path)})


def test_xhs_worker_passes_selected_session_to_upstream(tmp_path, monkeypatch):
    from prism.components import platform_video_worker as worker
    path = tmp_path/'cookies.txt'
    path.write_text('# Netscape HTTP Cookie File\n.xiaohongshu.com\tTRUE\t/\tTRUE\t0\tweb_session\tchosen\n')
    observed = {}
    class StopBeforeNetwork(Exception):
        pass
    class XHS:
        def __init__(self, **kwargs):
            observed.update(kwargs)
        async def __aenter__(self):
            raise StopBeforeNetwork()
        async def __aexit__(self, *args):
            pass
    monkeypatch.setitem(sys.modules, 'source', SimpleNamespace(XHS=XHS))
    monkeypatch.setitem(sys.modules, 'source.application.download', SimpleNamespace(Download=type('Download',(),{})))
    with pytest.raises(StopBeforeNetwork):
        asyncio.run(worker.xiaohongshu({'platform_root':str(tmp_path),'cookie_file':str(path)},tmp_path))
    assert observed['cookie'] == 'web_session=chosen'
    assert observed['video_preference'] == 'resolution'


def test_xhs_failure_preserves_stage_without_echoing_private_response():
    state = {'note': {'serverRequestInfo': {'state': 'fail', 'errorCode': -510001,
        'errMsg': 'https://example.test/?xsec_token=secret'}}}
    message = xhs_page_failure(state)
    assert '-510001' in message
    assert 'secret' not in message and 'example.test' not in message
    assert not xhs_page_failure({'note': {'serverRequestInfo': {'state': 'success'}}})
    assert not xhs_page_failure({'note': []})
    with pytest.raises(ValueError, match='验证'):
        xhs_destination('https://www.xiaohongshu.com/website-login/captcha?verifyUuid=secret')


def test_douyin_redirect_uses_browser_transport_and_skips_blocked_web_page():
    fetcher = SimpleNamespace(_DOUYIN_VIDEO_URL_PATTERN=re.compile(r'video/(\d+)'),
        _DOUYIN_NOTE_URL_PATTERN=re.compile(r'note/(\d+)'), _DOUYIN_INVAILID_URL_PATTERN=re.compile(r'vid=(\d+)'))
    response = SimpleNamespace(url='https://www.douyin.com/video/7690133449600334193', raise_for_status=Mock())
    session = Mock()
    session.get.return_value = response
    assert douyin_identifier('https://v.douyin.com/hI8G3tF7InY/',session,fetcher) == '7690133449600334193'
    response.raise_for_status.assert_not_called()
    session.reset_mock()
    assert douyin_identifier(response.url,session,fetcher) == '7690133449600334193'
    session.get.assert_not_called()


def test_no_silent_low_resolution_substitution():
    require_original_resolution(dict(width=1920,height=1080),dict(width=1920,height=1080))
    require_original_resolution(dict(width=1080,height=1920),dict(width=1920,height=1080))
    with pytest.raises(ValueError, match='852×480'):
        require_original_resolution(dict(width=1920,height=1080),dict(width=852,height=480))


@pytest.mark.parametrize('scale', [.5, 1, 2])
def test_drag_download_title_at_different_zoom_levels(view, qtbot, monkeypatch, scale):
    monkeypatch.setattr(view, '_material_canvas', lambda: None)
    manager = controller(view)
    view.window().show()
    view.setTransform(QtGui.QTransform.fromScale(scale,scale))
    node = manager.create(view.mapToScene(view.viewport().rect().center()))
    view.centerOn(node)
    qtbot.wait(30)
    original = QtCore.QPointF(node.pos())
    start = node.deviceTransform(view.viewportTransform()).map(QtCore.QPointF(node.title.geometry().center())).toPoint()
    end = start+QtCore.QPoint(50,30)
    qtbot.mousePress(view.viewport(),QtCore.Qt.MouseButton.LeftButton,pos=start)
    qtbot.mouseMove(view.viewport(),pos=end)
    qtbot.mouseRelease(view.viewport(),QtCore.Qt.MouseButton.LeftButton,pos=end)
    assert not node.dragging
    assert abs(node.x()-original.x()-50/scale) < 3
    assert abs(node.y()-original.y()-30/scale) < 3
    manager.remove(manager.nodes[0])
    manager.service.close()


def test_completion_removes_node_only_after_safe_insertion(view, monkeypatch, tmp_path):
    monkeypatch.setattr(view, '_material_canvas', lambda: None)
    monkeypatch.setattr(PrismVideoItem,'_capture_thumbnail',lambda self: None)
    manager = controller(view)
    node = manager.create(QtCore.QPointF(100,200))
    entry = manager.nodes[0]
    entry['job'] = 'job'
    node.dragging = True
    path = tmp_path/'video.mp4'
    path.write_bytes(b'test')
    manager.finished('job',dict(event='completed',path=str(path),title='test'))
    assert entry in manager.nodes
    node.dragging = False
    node.setPos(300,400)
    manager.sync()
    assert not manager.nodes and node.scene() is None
    video = next(i for i in view.scene.items_for_save() if i.TYPE == 'video')
    assert video.pos() == QtCore.QPointF(300,400)
    manager.service.close()


def test_crop_has_eight_points_and_mid_edge_drag(view, qtbot):
    image = QtGui.QImage(300,200,QtGui.QImage.Format.Format_RGB32)
    image.fill(QtGui.QColor('blue'))
    item = PrismPixmapItem(image)
    view.scene.addItem(item)
    item.setSelected(True)
    view.window().show()
    view.centerOn(item)
    item.enter_crop_mode()
    assert len(item.crop_control_points()) == 8
    if directory := os.environ.get('PRISM_UI_EVIDENCE'):
        Path(directory).mkdir(parents=True,exist_ok=True)
        qtbot.wait(30)
        view.viewport().grab().save(str(Path(directory)/'crop-controls.png'))
    start = view.mapFromScene(item.mapToScene(item.crop_control_points()[-1]))
    end = start-QtCore.QPoint(40,0)
    qtbot.mousePress(view.viewport(),QtCore.Qt.MouseButton.LeftButton,pos=start)
    qtbot.mouseMove(view.viewport(),pos=end)
    qtbot.mouseRelease(view.viewport(),QtCore.Qt.MouseButton.LeftButton,pos=end)
    assert abs(item.crop_temp.right()-260) < 2
    assert item.crop_temp.top() == 0 and item.crop_temp.bottom() == 200
    item.exit_crop_mode(True)
    view.undo_stack.undo()
    assert item.crop.width() == 300


@pytest.fixture
def dark_opening_clip(tmp_path):
    binary = Path(os.environ['LOCALAPPDATA'])/'Prism/components/video-download/bin/ffmpeg.exe'
    if not binary.exists():
        pytest.skip('FFmpeg fixture generator unavailable')
    path = tmp_path/'opening.mp4'
    subprocess.run([str(binary),'-v','error','-f','lavfi','-i','color=c=black:s=160x90:r=30:d=0.2',
        '-f','lavfi','-i','color=c=blue:s=160x90:r=30:d=0.6','-filter_complex','[0:v][1:v]concat=n=2:v=1:a=0[out]',
        '-map','[out]','-c:v','libx264','-pix_fmt','yuv420p',str(path)],check=True,capture_output=True,timeout=20,
        creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
    return path


def test_video_canvas_and_inspector_use_opening_frame_and_center_it(view, qtbot, dark_opening_clip):
    from prism.fileio.video_pyav import available
    if not available(): pytest.skip('PyAV unavailable')
    item = PrismVideoItem(QtCore.QUrl.fromLocalFile(str(dark_opening_clip)),str(dark_opening_clip))
    item._canvas_id = 'default-canvas'
    view.scene.addItem(item)
    item.setSelected(True)
    view.window().show()
    qtbot.waitUntil(lambda: item._thumbnail_ready,timeout=15000)
    pixel = item.pixmap().toImage().pixelColor(20,20)
    assert pixel.blue() > 150 and pixel.red() < 50
    assert item._video_dimensions == QtCore.QSize(160,90)
    assert item.get_extra_save_data()['videoWidth'] == 160
    panel = view._detail_panel
    panel._refresh_item(item)
    panel._path_label.setText('C:/very-long-folder/'*35)
    qtbot.wait(60)
    panel._resize_preview()
    label = panel._preview_label
    assert label.pixmap().width() <= label.width()
    rendered = label.grab().toImage()
    row = rendered.height()//2
    xs = [x for x in range(rendered.width()) if rendered.pixelColor(x,row).blue()>150 and rendered.pixelColor(x,row).red()<70]
    assert xs
    assert abs(xs[0]-(rendered.width()-1-xs[-1])) <= 3
    if directory := os.environ.get('PRISM_UI_EVIDENCE'):
        Path(directory).mkdir(parents=True,exist_ok=True)
        rendered.save(str(Path(directory)/'video-inspector.png'))
    assert '160' in panel._dims_label.text() and '90' in panel._dims_label.text()
