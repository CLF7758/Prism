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


def e_material_window(qtbot):
    """Independent window using production widgets/services, never user projects."""
    from types import SimpleNamespace
    from unittest.mock import MagicMock
    from PyQt6 import QtCore, QtWidgets
    from prism.widgets.material_library import MaterialLibrary
    from prism.material_service import MaterialLibraryService
    from prism.workspace_service import ResourceTreeService
    class Panel(QtCore.QObject):
        resource_nodes_changed = QtCore.pyqtSignal(object)
        material_records_changed = QtCore.pyqtSignal(object)
    window = QtWidgets.QWidget()
    qtbot.addWidget(window)
    scene = SimpleNamespace(project_id='e-test-project', material_records=[], attachments={},
                            workspace_nodes=[], material_library_state={})
    panel = Panel(window)
    panel.resource_service = ResourceTreeService(scene.project_id, parent=panel)
    panel.material_service = MaterialLibraryService(scene, panel.resource_service, panel)
    panel.resource_service.changed.connect(panel.resource_nodes_changed.emit)
    panel.material_service.changed.connect(panel.material_records_changed.emit)
    window.view = SimpleNamespace(scene=scene, category_panel=panel, mark_content_dirty=MagicMock())
    page = MaterialLibrary(window)
    layout = QtWidgets.QHBoxLayout(window)
    layout.addWidget(page, 1)
    layout.addWidget(page.details)
    page.details.setFixedWidth(360)
    window.resize(1500, 960)
    window.show()
    page.create_material()
    page._auto_timer.stop()
    return page


@pytest.fixture
def e_preview_page(monkeypatch, qtbot):
    """Real material services with controlled browser and worker delivery."""
    from unittest.mock import MagicMock
    from prism.widgets.material_render import MaterialRender
    from prism.widgets.material_cards import MaterialCards
    monkeypatch.setattr(MaterialRender, '_ensure_web', lambda self: None)
    monkeypatch.setattr(MaterialCards, '_ensure_web', lambda self: None)
    page = e_material_window(qtbot)
    viewer = page.details.inline_render
    viewer.web = MagicMock()
    viewer.ready = True
    tasks = []
    def task(function, callback, service=None, record_id=None, **kwargs):
        tasks.append(dict(function=function, callback=callback, service=service,
                          material=record_id, **kwargs))
    monkeypatch.setattr(page, '_task', task)
    page._test_tasks = tasks
    yield page
    for dialog in list(page._dialogs):
        dialog.close()
    page._auto_timer.stop()
    page._invalidate_inline_preview()


def e_cache(page):
    record = page.records.get(page.selected_id)
    payload = dict(recordId=record['id'], maps={}, signature=render_signature(record),
                   textureSignature=texture_signature(record),
                   properties=resolved_properties(record['preview'].get('properties', {}), {}, 'opaque', 'no alpha'))
    page._render_cache[(id(page.records), record['id'], texture_signature(record))] = payload
    return payload


def test_e_cached_preview_ignores_wrong_token_signature_and_duplicate_capture(e_preview_page, monkeypatch):
    from unittest.mock import MagicMock
    page = e_preview_page
    e_cache(page)
    other_listener = MagicMock()
    page.details.inline_render.updated.connect(other_listener)
    page._open_render(False)
    request = page._inline_request
    save = MagicMock(return_value=True)
    monkeypatch.setattr(page.records, 'save_preview', save)
    message = page.details.operation_status.text()
    page._inline_updated('old', request['signature'])
    page._inline_updated(request['token'], 'wrong')
    assert page.details.operation_status.text() == message
    page._inline_updated(request['token'], request['signature'])
    assert page._preview_state == 'ready'
    assert page.details.operation_status.text() == ''
    page._inline_completed(request['token'], request['signature'], b'image')
    page._inline_completed(request['token'], request['signature'], b'image')
    save.assert_called_once_with(request['material'], request['signature'], b'image')
    page.details.inline_render.updated.emit(request['signature'])
    other_listener.assert_called_once()


def test_e_same_signature_new_generation_rejects_old_error_and_capture(e_preview_page, monkeypatch):
    from unittest.mock import MagicMock
    page = e_preview_page
    e_cache(page)
    page._open_render(False)
    first = page._inline_request
    page._open_render(False)
    second = page._inline_request
    assert second['generation'] > first['generation'] and not first['valid']
    save = MagicMock()
    monkeypatch.setattr(page.records, 'save_preview', save)
    message = page.details.operation_status.text()
    page._inline_failed(first['token'], 'old failure')
    page._inline_updated(first['token'], first['signature'])
    page._inline_completed(first['token'], first['signature'], b'old image')
    assert page.details.operation_status.text() == message
    save.assert_not_called()
    page._inline_failed(second['token'], 'current failure')
    page._inline_updated(second['token'], second['signature'])
    assert page._preview_state == 'error'
    assert page.details.operation_status.text() == '渲染失败：current failure'


def test_e_initial_preparation_coalesces_100_changes_and_uses_cached_final_value(e_preview_page):
    page = e_preview_page
    page._open_render(False)
    first = page._inline_request
    task = page._test_tasks[-1]
    payload = task['function']()
    count = len(page._test_tasks)
    for value in range(1, 101):
        page.change_slot_options('roughness', {'value': value / 100})
        page._open_render(False)
    assert len(page._test_tasks) == count
    assert page._inline_preparation is first
    assert page._inline_pending is page._inline_request
    task['callback'](payload)
    assert page.details.inline_render.payload is None
    task['finally_callback']()
    assert page._inline_preparation is None and page._inline_pending is None
    assert len(page._test_tasks) == count
    final = page.details.inline_render.payload
    current = page.records.get(page.selected_id)
    assert final['signature'] == render_signature(current)
    assert final['properties']['controls']['roughness']['amount'] == 1


def test_e_texture_transform_changes_do_not_use_old_preparation(e_preview_page):
    page = e_preview_page
    page._open_render(False)
    task = page._test_tasks[-1]
    payload = task['function']()
    old_texture = payload['textureSignature']
    page.change_slot_options('roughness', {'invert': True})
    page._open_render(False)
    task['callback'](payload)
    task['finally_callback']()
    assert page.details.inline_render.payload is None
    assert page._inline_preparation is page._inline_request
    new_task = page._test_tasks[-1]
    assert new_task is not task
    new_payload = new_task['function']()
    assert new_payload['textureSignature'] != old_texture
    new_task['callback'](new_payload)
    new_task['finally_callback']()
    assert page.details.inline_render.payload['textureSignature'] == new_payload['textureSignature']


def test_e_large_preparation_failure_and_callbacks_never_change_inline_state(e_preview_page):
    page = e_preview_page
    e_cache(page)
    page._open_render(False)
    inline = page._inline_request
    page._open_render(True)
    dialog = page._dialogs[-1]
    request = dialog.material_request
    message = page.details.operation_status.text()
    dialog.material_viewer.request_updated.emit(request['token'], request['signature'])
    dialog.material_viewer.request_failed.emit(request['token'], 'large failure')
    assert page._inline_request is inline
    assert page.details.operation_status.text() == message
    assert dialog.material_state.text() == '渲染失败：large failure'
    page._render_cache.clear()
    page._open_render(True)
    prepare = page._test_tasks[-1]
    prepare['error_callback']('large prepare failure')
    prepare['finally_callback']()
    assert page.details.operation_status.text() == message
    assert page._dialogs[-1].material_state.text() == '渲染失败：large prepare failure'


def test_e_hidden_and_selection_changed_drop_inline_preparation(e_preview_page, monkeypatch):
    from unittest.mock import MagicMock
    page = e_preview_page
    page._open_render(False)
    first = page._inline_request
    task = page._test_tasks[-1]
    payload = task['function']()
    record = page.records.create(name='next')
    page._selection_changed(record['id'])
    page._auto_timer.stop()
    assert not first['valid'] and page._inline_request is None
    task['callback'](payload)
    task['finally_callback']()
    assert page.details.inline_render.payload is None
    e_cache(page)
    page._open_render(False)
    request = page._inline_request
    save = MagicMock()
    monkeypatch.setattr(page.records, 'save_preview', save)
    page.hide()
    page._inline_completed(request['token'], request['signature'], b'old')
    page._inline_failed(request['token'], 'old')
    assert page._preview_state == 'idle'
    assert page.details.operation_status.text() == ''
    save.assert_not_called()


def test_e_preparation_failure_retains_fixed_status_and_retry(e_preview_page):
    page = e_preview_page
    page._open_render(False)
    task = page._test_tasks[-1]
    task['error_callback']('准备失败：中文原因' * 30)
    task['finally_callback']()
    assert page._preview_state == 'error'
    assert page.details.render_button.isVisible()
    assert page.details.operation_status.isVisible()
    assert page._inline_preparation is None


def test_e_slider_release_submits_final_value_without_waiting_for_timer(e_preview_page):
    page = e_preview_page
    e_cache(page)
    slot = page.details.slots['roughness']
    slot.slider.setValue(375)
    assert page._auto_timer.isActive()
    slot.slider.sliderReleased.emit()
    current = page.records.get(page.selected_id)
    assert page._inline_request['signature'] == render_signature(current)
    assert page.details.inline_render.payload['properties']['controls']['roughness']['amount'] == .375
    assert not page._auto_timer.isActive()


def test_e_capture_cannot_erase_newer_nonrender_operation_message(e_preview_page, monkeypatch):
    from unittest.mock import MagicMock
    page = e_preview_page
    e_cache(page)
    page._open_render(False)
    request = page._inline_request
    monkeypatch.setattr(page.records, 'save_preview', MagicMock(return_value=True))
    page.details.operation_status.setText('已导出原始贴图')
    page._inline_completed(request['token'], request['signature'], b'image')
    assert page.details.operation_status.text() == '已导出原始贴图'


def test_e_pruning_retains_latest_viewer_pending_texture_directory(e_preview_page, tmp_path):
    from types import SimpleNamespace
    from unittest.mock import MagicMock
    page = e_preview_page
    directory = MagicMock()
    directory.name = str(tmp_path / 'latest-input')
    page._render_directories.append(directory)
    viewer = page.details.inline_render
    viewer._pending = SimpleNamespace(payload={'maps': {'normal': (Path(directory.name) / 'normal.png').as_uri()}})
    page._render_cache.clear()
    page._prune_render_cache()
    directory.cleanup.assert_not_called()
    assert directory in page._render_directories
    # Keep the test fake out of viewer state before fixture teardown.
    viewer._pending = None
    page._render_directories.remove(directory)


def test_e_project_service_change_rejects_old_callbacks_with_same_material_id(e_preview_page, monkeypatch):
    from types import SimpleNamespace
    from unittest.mock import MagicMock
    from prism.material_service import MaterialLibraryService
    page = e_preview_page
    e_cache(page)
    page._open_render(False)
    request = page._inline_request
    old = page.records
    replacement_scene = SimpleNamespace(project_id=old.project_id,
                                        material_records=copy.deepcopy(old.records), attachments={})
    panel = page.window_host.view.category_panel
    replacement = MaterialLibraryService(replacement_scene, panel.resource_service, panel)
    panel.material_service = replacement
    save = MagicMock()
    monkeypatch.setattr(old, 'save_preview', save)
    before = page.details.operation_status.text()
    page._inline_completed(request['token'], request['signature'], b'old')
    page._inline_failed(request['token'], 'old')
    assert page.details.operation_status.text() == before
    save.assert_not_called()
    panel.material_service = old


def test_e_capture_receipt_rechecks_selection_after_synchronous_service_signal(e_preview_page, monkeypatch):
    page = e_preview_page
    e_cache(page)
    page._open_render(False)
    request = page._inline_request
    other = page.records.create(name='other')
    def save(*_args):
        page._selection_changed(other['id'])
        return True
    monkeypatch.setattr(page.records, 'save_preview', save)
    page._inline_completed(request['token'], request['signature'], b'image')
    assert page.selected_id == other['id']
    assert page._inline_request is None
    assert page._preview_state == 'idle'
    assert page.details.operation_status.text() == ''
    assert page.details.preview_status.text() != '自动更新 · 已同步到材质卡片'


@pytest.mark.parametrize('texture_change', [False, True])
def test_e_large_preparation_follows_own_latest_material_after_selection_changes(e_preview_page, texture_change):
    page = e_preview_page
    material_id = page.selected_id
    other = page.records.create(name='other')
    page._open_render(True)
    dialog = page._dialogs[-1]
    first = dialog.material_request
    task = page._test_tasks[-1]
    payload = task['function']()
    page.records.configure(material_id, blend_mode='masked',
                           properties_patch={'ignoreBaseAlpha': True} if texture_change else {})
    page._selection_changed(other['id'])
    page._auto_timer.stop()
    count = len(page._test_tasks)
    task['callback'](payload)
    task['finally_callback']()
    assert page.selected_id == other['id']
    assert len(page._dialogs) == 1 and page._dialogs[0] is dialog
    assert not first['valid']
    latest = dialog.material_request
    assert latest['signature'] == render_signature(page.records.get(material_id))
    if texture_change:
        assert len(page._test_tasks) == count + 1
        assert latest['state'] == 'preparing'
        next_task = page._test_tasks[-1]
        next_task['callback'](next_task['function']())
        next_task['finally_callback']()
    else:
        assert len(page._test_tasks) == count
    assert latest['state'] == 'updating'
    assert dialog.material_viewer._pending.payload['recordId'] == material_id
    assert dialog.material_viewer._pending.payload['properties']['blendMode'] == 'masked'
    assert page._inline_request is None


def test_e_large_old_preparation_recovers_synced_request_invalidated_by_selection_reset(e_preview_page):
    page = e_preview_page
    material_id = page.selected_id
    other = page.records.create(name='other')
    page._open_render(True)
    dialog = page._dialogs[-1]
    task = page._test_tasks[-1]
    payload = task['function']()
    page.change_slot_options('roughness', {'value': .42})
    e_cache(page)
    page._open_render(False)
    synchronized = dialog.material_request
    assert synchronized['state'] == 'updating'
    page._selection_changed(other['id'])
    page._auto_timer.stop()
    assert synchronized['signature'] != render_signature(page.records.get(material_id))
    task['callback'](payload)
    task['finally_callback']()
    latest = dialog.material_request
    assert latest is not synchronized and not synchronized['valid']
    assert latest['signature'] == render_signature(page.records.get(material_id))
    assert latest['state'] == 'updating'
    assert len(page._dialogs) == 1 and page.selected_id == other['id']


def test_e_large_synced_request_reset_after_preparation_has_ended(e_preview_page):
    page = e_preview_page
    material_id = page.selected_id
    other = page.records.create(name='other')
    e_cache(page)
    page._open_render(True)
    dialog = page._dialogs[-1]
    assert dialog.material_preparation is None
    page.change_slot_options('roughness', {'value': .42})
    page._open_render(False)
    synchronized = dialog.material_request
    page._selection_changed(other['id'])
    page._auto_timer.stop()
    latest = dialog.material_request
    assert latest is not synchronized and not synchronized['valid']
    assert latest['signature'] == render_signature(page.records.get(material_id))
    assert latest['state'] == 'updating'
    assert page._inline_request is None


def test_e_large_preparation_repeated_latest_intents_do_not_duplicate_workers(e_preview_page):
    page = e_preview_page
    page._open_render(True)
    dialog = page._dialogs[-1]
    task = page._test_tasks[-1]
    payload = task['function']()
    count = len(page._test_tasks)
    for _ in range(20):
        page._open_render(True, dialog=dialog)
    assert len(page._test_tasks) == count
    task['callback'](payload)
    task['finally_callback']()
    assert dialog.material_request['state'] == 'updating'
    assert dialog.material_preparation is None
    assert len(page._test_tasks) == count


def test_e_large_preparation_late_error_does_not_overwrite_latest_synced_request(e_preview_page):
    page = e_preview_page
    page._open_render(True)
    dialog = page._dialogs[-1]
    task = page._test_tasks[-1]
    payload = task['function']()
    page.records.configure(page.selected_id, blend_mode='masked')
    latest_payload = page._payload_for_record(payload, page.records.get(page.selected_id))
    page._load_dialog_payload(dialog, latest_payload)
    latest = dialog.material_request
    count = len(page._test_tasks)
    task['error_callback']('old preparation failed')
    task['callback'](payload)
    task['finally_callback']()
    assert dialog.material_request is latest
    assert latest['state'] == 'updating'
    assert dialog.material_state.text() == '正在更新预览…'
    assert len(page._test_tasks) == count


@pytest.mark.parametrize('closed', [False, True])
def test_e_large_preparation_cannot_restart_closed_or_new_service_dialog(e_preview_page, closed):
    from types import SimpleNamespace
    from prism.material_service import MaterialLibraryService
    page = e_preview_page
    page._open_render(True)
    dialog = page._dialogs[-1]
    task = page._test_tasks[-1]
    payload = task['function']()
    page.records.configure(page.selected_id, blend_mode='masked')
    old_service = page.records
    if closed:
        dialog.close()
    else:
        panel = page.window_host.view.category_panel
        replacement = SimpleNamespace(project_id=old_service.project_id,
                                      material_records=copy.deepcopy(old_service.records), attachments={})
        panel.material_service = MaterialLibraryService(replacement, panel.resource_service, panel)
    count = len(page._test_tasks)
    task['callback'](payload)
    task['finally_callback']()
    assert len(page._test_tasks) == count
    assert dialog.material_viewer.payload is None
    page.window_host.view.category_panel.material_service = old_service


def test_e_large_changed_preparation_error_retries_latest_without_wrong_status(e_preview_page):
    page = e_preview_page
    page._open_render(True)
    dialog = page._dialogs[-1]
    task = page._test_tasks[-1]
    page.records.configure(page.selected_id, blend_mode='masked')
    page._auto_timer.stop()
    task['error_callback']('stale failure')
    task['finally_callback']()
    assert dialog.material_request['state'] == 'preparing'
    assert dialog.material_state.text() == '正在准备预览…'
    assert page._test_tasks[-1] is not task
    latest_task = page._test_tasks[-1]
    latest_task['error_callback']('current failure')
    latest_task['finally_callback']()
    assert dialog.material_request['state'] == 'error'
    assert dialog.material_state.text() == '渲染失败：current failure'
    assert page._test_tasks[-1] is latest_task


@pytest.mark.parametrize('synchronized_reset', [False, True])
def test_e_native_delayed_preparation_and_displacement_shape_settle(qtbot, tmp_path, monkeypatch, synchronized_reset):
    """Exercise both repaired await branches in the production Windows viewer."""
    import json
    import threading
    import prism.widgets.material_library as library_module
    from PyQt6 import QtWidgets
    if QtWidgets.QApplication.platformName() != 'windows':
        pytest.skip('native Windows Qt/WebGL acceptance')
    page = e_material_window(qtbot)
    source = tmp_path / 'displacement-original.png'
    Image.new('L', (32, 32), 170).save(source)
    original = source.read_bytes()
    page.import_slot('displacement', str(source))
    material_id = page.selected_id
    qtbot.waitUntil(lambda: page._preview_state == 'ready' and not page._jobs, timeout=45000)
    other = page.records.create(name='other')
    page._auto_timer.stop()
    cached_inputs = copy.deepcopy(page.details.inline_render.payload)
    page._render_cache.clear()
    gate = threading.Event()
    entered = threading.Event()
    original_prepare = library_module.prepare_render
    def delayed_prepare(*args):
        entered.set()
        if not gate.wait(15):
            raise RuntimeError('test preparation gate timed out')
        return original_prepare(*args)
    monkeypatch.setattr(library_module, 'prepare_render', delayed_prepare)
    page._open_render(True)
    dialog = page._dialogs[-1]
    try:
        qtbot.waitUntil(entered.is_set, timeout=8000)
        first = dialog.material_request
        if synchronized_reset:
            page.change_slot_options('roughness', {'value': .42})
            current = page.records.get(material_id)
            page._render_cache[(id(page.records), material_id, texture_signature(current))] = cached_inputs
            page._open_render(False)
            assert dialog.material_request is not first
        else:
            page.records.configure(material_id, blend_mode='masked')
        page._selection_changed(other['id'])
        page._auto_timer.stop()
        gate.set()
        qtbot.waitUntil(lambda: dialog.material_request['state'] == 'ready', timeout=45000)
        assert dialog.material_request is not first and not first['valid']
        assert len(page._dialogs) == 1 and page.selected_id == other['id']
        viewer = dialog.material_viewer
        def query(expression):
            result = []
            viewer.web.page().runJavaScript(expression, result.append)
            qtbot.waitUntil(lambda: bool(result), timeout=8000)
            return result[0]
        assert query('current.signature') == render_signature(page.records.get(material_id))
        # Delay the old load's image await, not the replacement shapes' real IO.
        query("""(() => {
          const original = displacement;
          let once = true;
          window._eDisplacementWaiting = false;
          displacement = async (...args) => {
            if (once) {
              once = false;
              await new Promise(resolve => {
                window._eReleaseDisplacement = resolve;
                window._eDisplacementWaiting = true;
              });
            }
            return original(...args);
          };
          return true;
        })()""")
        payload = copy.deepcopy(viewer.payload)
        payload['properties']['controls']['displacement']['value'] = .12
        viewer.load_material(payload, 'e-delayed-displacement')
        qtbot.waitUntil(lambda: query('window._eDisplacementWaiting === true'), timeout=8000)
        viewer.set_shape('sphere')
        viewer.set_shape('cylinder')
        query('window._eReleaseDisplacement(); true')
        qtbot.waitUntil(lambda: viewer._active is not None and viewer._active.phase == 'updated', timeout=30000)
        assert viewer.token == 'e-delayed-displacement'
        assert query('prismRender.diagnostics.shape') == 'cylinder'
        assert query('prismRender.diagnostics.ready') is True
        assert page.records.scene.attachments[page.records.get(material_id)['textures']['displacement']['attachmentId']][2] == original
        directory = Path(os.environ.get('E_EVIDENCE_DIR', str(tmp_path)))
        directory.mkdir(parents=True, exist_ok=True)
        name = 'native-await-reset.json' if synchronized_reset else 'native-await-branches.json'
        (directory / name).write_text(json.dumps(dict(
            platform=QtWidgets.QApplication.platformName(),
            original_dialog_reused=True, selection_independent_recovery=True,
            inline_sync_then_selection_reset=synchronized_reset,
            delayed_displacement_final_shape='cylinder', renderer_phase=viewer._active.phase,
            original_bytes_unchanged=True), ensure_ascii=False, indent=2), encoding='utf-8')
    finally:
        gate.set()
        dialog.close()
        qtbot.waitUntil(lambda: not page._jobs and not page._dialogs, timeout=20000)


def test_e_native_material_window_continuous_updates_and_dual_viewer(qtbot, tmp_path):
    """Native Qt + Babylon + actual worker IO; independent test project only."""
    import json
    from PyQt6 import QtCore, QtWidgets
    if QtWidgets.QApplication.platformName() != 'windows':
        pytest.skip('native Windows Qt/WebGL acceptance')
    page = e_material_window(qtbot)
    details = page.details
    source = tmp_path / 'e-original.png'
    Image.new('L', (64, 64), 170).save(source)
    original = source.read_bytes()
    page.import_slot('roughness', str(source))
    def rendered():
        record = page.records.get(page.selected_id)
        return (record['preview'].get('signature') == render_signature(record)
                and not record['preview'].get('stale', True))
    qtbot.waitUntil(rendered, timeout=45000)
    qtbot.waitUntil(lambda: not page._jobs, timeout=10000)
    qtbot.wait(100)
    baseline = details.sections.mapTo(details, QtCore.QPoint()).y()
    positions = []
    for turn in range(3):
        for value in (800, 100, 650, 375):
            details.slots['roughness'].slider.setValue(value)
            qtbot.wait(45)
            positions.append(details.sections.mapTo(details, QtCore.QPoint()).y())
        details.slots['roughness'].slider.sliderReleased.emit()
        qtbot.waitUntil(rendered, timeout=30000)
        positions.append(details.sections.mapTo(details, QtCore.QPoint()).y())
    assert set(positions) == {baseline}, positions
    page.open_preview()
    dialog = page._dialogs[-1]
    qtbot.waitUntil(lambda: dialog.material_request['state'] == 'ready', timeout=45000)
    inline_request = page._inline_request
    right_message = details.operation_status.text()
    dialog.material_viewer.request_failed.emit(inline_request['token'], 'wrong viewer/token')
    assert details.operation_status.text() == right_message
    details.slots['roughness'].slider.setValue(275)
    details.slots['roughness'].slider.sliderReleased.emit()
    qtbot.waitUntil(rendered, timeout=30000)
    qtbot.waitUntil(lambda: dialog.material_request['state'] == 'ready', timeout=30000)
    def query(viewer, expression):
        result = []
        viewer.web.page().runJavaScript(expression, result.append)
        qtbot.waitUntil(lambda: bool(result), timeout=8000)
        return result[0]
    inline_signature = query(details.inline_render, 'current.signature')
    dialog_signature = query(dialog.material_viewer, 'current.signature')
    assert inline_signature == dialog_signature == render_signature(page.records.get(page.selected_id))
    assert query(details.inline_render, 'material.roughness') == .275
    before_error = details.sections.mapTo(details, QtCore.QPoint()).y()
    page._inline_failed(page._inline_request['token'], '中文预览错误：详细原因' * 40)
    qtbot.wait(100)
    assert details.sections.mapTo(details, QtCore.QPoint()).y() == before_error
    page.update_preview()
    qtbot.waitUntil(rendered, timeout=30000)
    qtbot.waitUntil(lambda: page._preview_state == 'ready', timeout=30000)
    current = page.records.get(page.selected_id)
    assert page.records.scene.attachments[current['textures']['roughness']['attachmentId']][2] == original
    assert current['preview']['properties']['slots']['roughness']['factor'] == .275
    directory = Path(os.environ.get('E_EVIDENCE_DIR', str(tmp_path)))
    directory.mkdir(parents=True, exist_ok=True)
    scale = os.environ.get('QT_SCALE_FACTOR', '1')
    details.grab().save(str(directory / ('native-rightbar-' + scale + '.png')))
    report = dict(platform=QtWidgets.QApplication.platformName(), scale=scale,
                  device_pixel_ratio=details.devicePixelRatioF(),
                  logical_dpi=details.screen().logicalDotsPerInch(),
                  baseline_tab_y=baseline, measured_tab_y=positions,
                  state_tab_delta=0, signature=inline_signature,
                  final_factor=.275, original_sha256=hashlib.sha256(original).hexdigest(),
                  two_viewers_same_signature=True, original_bytes_unchanged=True,
                  window='independent material window, production Qt/Babylon/services')
    (directory / ('native-rightbar-' + scale + '.json')).write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    page.hide()
    assert page._inline_request is None and details.inline_render.payload is None
    assert dialog.material_viewer.payload is not None
    dialog.close()
    qtbot.waitUntil(lambda: not page._dialogs, timeout=8000)
