import asyncio
import os
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock
import pytest
from PyQt6 import QtCore, QtGui, QtWidgets
from prism.components import bilibili_quality as quality
from prism.components.platform_links import site_request_cookies


def test_html5_upgrade_is_verified_and_requires_explicit_lower_quality_consent(monkeypatch,tmp_path):
    dash=SimpleNamespace(url='https://media.test/dash')
    mp4=SimpleNamespace(url='https://media.test/mp4')
    class Video:
        async def get_download_url(self,**kwargs):
            return {'durl':[{}]} if kwargs.get('html5') else {'dash':{'video':[dict(baseUrl=dash.url,width=852,height=480)]}}
    class Detector:
        def __init__(self,data):self.data=data
        def detect_best_streams(self):return [mp4] if 'durl' in self.data else [dash]
    async def probe(*args):return dict(width=1280,height=720)
    monkeypatch.setattr(quality,'probe_progressive',probe)
    args=(Video(),0,dict(width=1920,height=1080),None,{}, {},tmp_path,Detector)
    with pytest.raises(quality.QualityLimitedError) as failure:
        asyncio.run(quality.select_streams(*args))
    assert failure.value.available == dict(width=1280,height=720)
    changed=list(args);changed[5]={'quality_policy':'best_available'}
    assert asyncio.run(quality.select_streams(*changed)) == [mp4]


def test_original_resolution_stream_avoids_unnecessary_html5_requests(tmp_path):
    class Video:
        async def get_download_url(self,**kwargs):
            assert not kwargs.get('html5')
            return {'dash':{'video':[dict(baseUrl='stream',width=1920,height=1080)]}}
    class Detector:
        def __init__(self,data):pass
        def detect_best_streams(self):return [SimpleNamespace(url='stream')]
    streams=asyncio.run(quality.select_streams(Video(),0,dict(width=1920,height=1080),None,{}, {},tmp_path,Detector))
    assert streams[0].url=='stream'


def test_node_offers_explicit_current_quality_without_reusing_choice_for_new_url(view,monkeypatch):
    from prism.actions.video_download_workflow import controller
    monkeypatch.setattr(view,'_material_canvas',lambda:None)
    manager=controller(view)
    manager.service.submit=Mock(return_value='next-job')
    node=manager.create(QtCore.QPointF())
    entry=manager.nodes[0]
    node.url.setText('https://www.bilibili.com/video/BV1jtpv6HEAP/')
    entry['job']='restricted'
    manager.service.jobs['restricted']={'result':dict(code='quality_limited',available=dict(width=1280,height=720))}
    manager.failed('restricted',str(quality.QualityLimitedError(dict(width=1920,height=1080),dict(width=1280,height=720))))
    assert '1280×720' in node.available_quality.text()
    assert not node.available_quality.isHidden()
    if os.environ.get('PRISM_UI_EVIDENCE'):
        out=Path(os.environ['PRISM_UI_EVIDENCE']);out.mkdir(parents=True,exist_ok=True)
        font_path=Path('C:/Windows/Fonts/msyh.ttc')
        if font_path.is_file():
            font_id=QtGui.QFontDatabase.addApplicationFont(str(font_path))
            families=QtGui.QFontDatabase.applicationFontFamilies(font_id)
            if families:
                font=QtGui.QFont(families[0],10)
                node.widget().setFont(font)
                for child in node.widget().findChildren(QtWidgets.QWidget):child.setFont(font)
        node.widget().grab().save(str(out/'bili-quality-node.png'))
    node.available_quality.click()
    assert manager.service.submit.call_args.kwargs['quality_policy']=='best_available'
    assert manager.service.submit.call_args.kwargs['minimum_dimensions']==dict(width=1280,height=720)
    node.busy(False)
    node.url.setText('https://www.bilibili.com/video/BV13aNd64E51/')
    assert node.available_quality.isHidden()
    manager.service.submit.reset_mock()
    node.available_quality_requested.emit()
    manager.service.submit.assert_not_called()
    manager.service.jobs.pop('restricted')
    manager.remove(entry)
    manager.service.close()


def test_explicit_bilibili_session_is_scoped_to_bilibili(tmp_path):
    path=tmp_path/'session.txt'
    path.write_text('# Netscape HTTP Cookie File\n'
        '.bilibili.com\tTRUE\t/\tTRUE\t0\tSESSDATA\tbili-session\n'
        '.xiaohongshu.com\tTRUE\t/\tTRUE\t0\tweb_session\txhs-session\n')
    assert site_request_cookies({'cookie_file':str(path)},'bilibili.com') == {'SESSDATA':'bili-session'}


def test_explicit_720p_choice_cannot_silently_become_480p():
    request={'minimum_dimensions':dict(width=1280,height=720)}
    quality.enforce_selected(dict(width=1280,height=720),request)
    with pytest.raises(ValueError,match='低于刚才选择'):
        quality.enforce_selected(dict(width=852,height=480),request)
