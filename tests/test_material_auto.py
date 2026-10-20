"""Auto transparency, immutable source transforms and automatic card updates."""
import copy
import hashlib
import os
from pathlib import Path

import pytest
from PIL import Image

from prism.material_textures import (prepare_texture, prepare_render, texture_signature,
    classify_transparency, resolved_properties, render_signature)


def test_auto_transparency_handles_cutout_antialias_and_continuous_alpha():
    assert classify_transparency(None)[0]=='opaque'
    assert classify_transparency(Image.new('L',(32,32),255))[0]=='opaque'
    mask=Image.new('L',(100,100),0)
    mask.paste(255,(20,20,80,80))
    assert classify_transparency(mask)[0]=='masked'
    assert classify_transparency(Image.new('L',(32,32),128))[0]=='translucent'
    mask.paste(128,(20,20,21,80))
    assert classify_transparency(mask)[0]=='masked'


def test_base_alpha_auto_and_disable_without_changing_original(main_window,tmp_path):
    image=Image.new('RGBA',(8,8),(255,40,0,255))
    image.putpixel((0,0),(255,40,0,0))
    source=tmp_path/'color.png';image.save(source)
    original=source.read_bytes()
    service=main_window.view.category_panel.material_service
    record=service.create()
    service.configure(record['id'],slot='basecolor',prepared=prepare_texture(source))
    payload=prepare_render(service.get(record['id']),service.scene.attachments,tmp_path/'a')
    assert payload['properties']['blendMode']=='masked'
    assert 'opacity' in payload['maps']
    service.configure(record['id'],properties_patch={'ignoreBaseAlpha':True})
    payload=prepare_render(service.get(record['id']),service.scene.attachments,tmp_path/'b')
    assert payload['properties']['blendMode']=='opaque'
    assert 'opacity' not in payload['maps']
    assert service.scene.attachments[1][2]==original


def test_invert_flip_and_parameter_cache_preserve_original(main_window,tmp_path):
    source=tmp_path/'gray.png'
    image=Image.new('L',(2,1));image.putdata([20,180]);image.save(source)
    original=source.read_bytes()
    service=main_window.view.category_panel.material_service
    record=service.create()
    service.configure(record['id'],slot='roughness',prepared=prepare_texture(source))
    before=texture_signature(service.get(record['id']))
    service.configure(record['id'],slot_settings=('roughness',{'factor':.4}))
    assert texture_signature(service.get(record['id']))==before
    service.configure(record['id'],slot_settings=('roughness',{'invert':True,'flipX':True}))
    payload=prepare_render(service.get(record['id']),service.scene.attachments,tmp_path/'proxy')
    with Image.open(tmp_path/'proxy/roughness.png') as proxy:
        assert proxy.tobytes()==bytes([75,235])
    assert payload['properties']['controls']['roughness']['amount']==.4
    assert service.scene.attachments[1][2]==original
    service.configure(record['id'],slot_settings=('roughness',{'enabled':False}))
    payload=prepare_render(service.get(record['id']),service.scene.attachments,tmp_path/'disabled')
    assert 'roughness' not in payload['maps']


def test_auto_blend_reverts_when_opacity_strength_restored():
    props={'slots':{'opacity':{'value':.5}}}
    first=resolved_properties(props,{},'opaque','no alpha')
    assert first['blendMode']=='translucent'
    props['slots']['opacity']['value']=1
    second=resolved_properties(props,{},first['sourceAutoBlendMode'],first['sourceAutoBlendReason'])
    assert second['blendMode']=='opaque'


def test_default_startup_does_not_disable_webgl(monkeypatch):
    from prism.webengine_runtime import configure_webengine
    monkeypatch.delenv('QTWEBENGINE_CHROMIUM_FLAGS',raising=False)
    configure_webengine()
    assert '--disable-gpu' not in os.environ['QTWEBENGINE_CHROMIUM_FLAGS']
    assert '--disable-software-rasterizer' not in os.environ['QTWEBENGINE_CHROMIUM_FLAGS']


def test_automatic_preview_slider_and_card_update(main_window,qtbot,tmp_path):
    from PyQt6.QtWidgets import QApplication
    if QApplication.platformName()!='windows':
        pytest.skip('Native Windows UI+WebGL acceptance')
    window=main_window;window.resize(1500,960);window.show();window.open_material_library()
    page=window.material_library;page.create_material()
    source=tmp_path/'rough.png';Image.new('L',(64,64),170).save(source)
    page.import_slot('roughness',str(source))
    def rendered():
        record=page.records.get(page.selected_id)
        return record['preview'].get('signature')==render_signature(record) and not record['preview'].get('stale',True)
    qtbot.waitUntil(rendered,timeout=45000)
    original=source.read_bytes()
    first=page.records.get(page.selected_id)
    attachment_id=first['preview']['thumbnailAttachmentId']
    thumbnail=page.records.scene.attachments[attachment_id][2]
    page.details.slots['roughness'].slider.setValue(250)
    qtbot.waitUntil(rendered,timeout=30000)
    updated=page.records.get(page.selected_id)
    assert updated['preview']['properties']['slots']['roughness']['factor']==.25
    assert page.records.scene.attachments[attachment_id][2]!=thumbnail
    assert page.records.scene.attachments[updated['textures']['roughness']['attachmentId']][2]==original
    qtbot.waitUntil(lambda: page.materials.ready,timeout=15000)
    qtbot.wait(200)
    assert page.details.auto_mode.text()=='自动：不透明'
    assert page.materials.assets[0]['thumbnail'].startswith('data:image/png')
    # Coalesce rapid events, then preserve the newest value and its thumbnail.
    for value in (800,100,650,375):
        page.details.slots['roughness'].slider.setValue(value)
    page.change_slot_options('basecolor', {'color':[.4,.8,1.]})
    qtbot.waitUntil(rendered,timeout=30000)
    final=page.records.get(page.selected_id)
    assert final['preview']['properties']['slots']['roughness']['factor']==.375
    assert final['preview']['properties']['slots']['basecolor']['color']==[.4,.8,1.]
