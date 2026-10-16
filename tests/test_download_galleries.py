from pathlib import Path
from unittest.mock import Mock

import pytest
from PyQt6 import QtCore, QtGui
from prism.actions.video_download_workflow import controller
from prism.components.download_result import complete_assets, validate_assets, cleanup_assets
from prism.components.bilibili_gallery import gallery_reference, dynamic_images, article_images
from prism.items import PrismVideoItem
from prism.fileio.sql import SQLiteIO


def test_gallery_originals_validation_and_cleanup(tmp_path):
    output = tmp_path/'downloads'
    output.mkdir()
    photo = output/'image.webp'
    photo.write_bytes(b'original-image')
    preview = output/'preview.png'
    preview.write_bytes(b'preview')
    clip = output/'live.mp4'
    clip.write_bytes(b'original-live-photo')
    request = dict(directory=str(output), original_destination=str(tmp_path/'reserved.mp4'))
    assets = [dict(asset_type='image', path=str(photo), preview_path=str(preview), image=dict(width=2160,height=2880)),
              dict(asset_type='video', path=str(clip))]
    results = []
    complete_assets(request, assets, lambda event, **data: results.append(data) if event=='completed' else None)
    result = results[0]
    validate_assets(request,result['assets'])
    for asset in result['assets']:
        assert Path(asset['original_path']).read_bytes()==Path(asset['path']).read_bytes()
    result['assets'][0]['path']=str(tmp_path/'outside.webp')
    with pytest.raises(ValueError):validate_assets(request,result['assets'])
    cleanup_assets(request)
    assert not (tmp_path/'reserved').exists()
    assert photo.exists() and clip.exists()


def test_mixed_gallery_import_undo_and_project_roundtrip(view, tmp_path, monkeypatch):
    monkeypatch.setattr(PrismVideoItem,'_capture_thumbnail',lambda self:None)
    monkeypatch.setattr(view,'_material_canvas',lambda:None)
    image=QtGui.QImage(64,48,QtGui.QImage.Format.Format_RGB32)
    image.fill(QtGui.QColor('green'))
    photo=tmp_path/'original.png'
    image.save(str(photo))
    preview=tmp_path/'preview.png'
    image.scaled(8,6).save(str(preview))
    clip=tmp_path/'live.mp4'
    clip.write_bytes(b'video-original-fixture')
    manager=controller(view)
    manager.create(QtCore.QPointF(120,240))
    entry=manager.nodes[0]
    entry['job']='gallery'
    manager.finished('gallery',dict(event='completed',path=str(photo),title='Gallery',assets=[
        dict(asset_type='image',path=str(photo),original_path=str(photo),preview_path=str(preview),image=dict(width=64,height=48,format='png')),
        dict(asset_type='video',path=str(clip),original_path=str(clip),media=dict(streams=[dict(codec_type='video',width=720,height=960)]))]))
    items=list(view.scene.items_for_save())
    assert len(items)==2 and not manager.nodes
    assert not items[0].sceneBoundingRect().intersects(items[1].sceneBoundingRect())
    view.undo_stack.undo()
    assert not list(view.scene.items_for_save())
    view.undo_stack.redo()
    project=str(tmp_path/'gallery.prism')
    SQLiteIO(project,view.scene,create_new=True).write()
    view.clear_scene()
    SQLiteIO(project,view.scene,readonly=True).read()
    view.scene.add_queued_items()
    restored=list(view.scene.items_for_save())
    assert len(restored)==2
    assert {getattr(item,'TYPE',None) for item in restored}=={'pixmap','video'}
    video=next(item for item in restored if item.TYPE=='video')
    assert Path(video._video_source_path).read_bytes()==clip.read_bytes()
    manager.service.close()


def test_gallery_dialog_imports_all_and_does_not_extract_images(view, monkeypatch):
    from prism.widgets.video_download import VideoDownloadDialog
    dialog=VideoDownloadDialog(view)
    result=dict(event='completed',assets=[dict(asset_type='image',path='image.webp'),dict(asset_type='video',path='live.mp4')])
    monkeypatch.setattr(dialog,'selected_result',lambda:result)
    monkeypatch.setattr(dialog,'destination',lambda result:True)
    view.do_insert_images=Mock()
    dialog.import_video()
    view.do_insert_images.assert_called_once_with(['image.webp','live.mp4'],force_video_embed=True)
    result['assets'].pop()
    dialog.extract()
    assert '只有图片' in dialog.status.text()
    dialog.service.close()


def test_bilibili_gallery_upstream_shapes():
    assert gallery_reference('https://www.bilibili.com/opus/123')==('opus',123)
    assert gallery_reference('https://t.bilibili.com/456')==('dynamic',456)
    assert gallery_reference('https://www.bilibili.com/read/cv789')==('article',789)
    assert gallery_reference('https://www.bilibili.com/video/BV123') is None
    pics,title=dynamic_images({'item':{'modules':{'module_dynamic':{'major':{'draw':{'items':[{'src':'https://i0.hdslb.com/image.jpg'}]}}}}}})
    assert pics[0]['url'].endswith('image.jpg')
    assert article_images({'type':'Root','children':[{'type':'ImageNode','url':'//i0.hdslb.com/raw.png'},{'type':'TextNode','text':'text'}]})==[{'url':'//i0.hdslb.com/raw.png'}]
