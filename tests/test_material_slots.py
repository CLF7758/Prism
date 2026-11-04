"""Original bytes, shared slot references and real engine rendering."""
import hashlib
import io
from pathlib import Path

from PIL import Image
import pytest

from prism.fileio.sql import SQLiteIO
from prism.material_textures import prepare_texture, prepare_render, export_originals, selected_image


FIXTURES = Path(__file__).resolve().parents[1] / 'third_party/material-test-assets/Khronos/Models'


def test_right_slot_import_channel_and_clear_keep_original(main_window, qtbot, tmp_path):
    from PyQt6.QtCore import Qt, QMimeData, QUrl, QPoint, QPointF
    from PyQt6.QtGui import QDragEnterEvent, QDropEvent
    from PyQt6.QtWidgets import QApplication
    window = main_window
    window.open_material_library()
    page = window.material_library
    page.create_material()
    source = tmp_path / 'arbitrary-ORT.png'
    Image.new('RGB', (16, 16), (0, 96, 192)).save(source)
    slot = page.details.slots['roughness']
    mime=QMimeData()
    mime.setUrls([QUrl.fromLocalFile(str(source))])
    enter=QDragEnterEvent(QPoint(10,10),Qt.DropAction.CopyAction,mime,
                        Qt.MouseButton.LeftButton,Qt.KeyboardModifier.NoModifier)
    QApplication.sendEvent(slot.drop,enter)
    assert enter.isAccepted()
    drop=QDropEvent(QPointF(10,10),Qt.DropAction.CopyAction,mime,
                    Qt.MouseButton.LeftButton,Qt.KeyboardModifier.NoModifier)
    QApplication.sendEvent(slot.drop,drop)
    assert drop.isAccepted()
    qtbot.waitUntil(lambda: 'roughness' in page.records.get(page.selected_id)['textures'])
    slot.channel.setCurrentIndex(slot.channel.findData('G'))
    assert page.records.get(page.selected_id)['textures']['roughness']['channel'] == 'G'
    page.details.blend_mode.setCurrentIndex(page.details.blend_mode.findData('masked'))
    assert page.records.get(page.selected_id)['preview']['properties']['blendMode'] == 'masked'
    qtbot.mouseClick(slot.clear, Qt.MouseButton.LeftButton)
    assert page.records.get(page.selected_id)['textures'] == {}
    assert len(page.records.get(page.selected_id)['files']) == 1
    assert len(page.records.scene.attachments) == 1


def test_original_survives_save_reload_source_removed_and_export(main_window, tmp_path):
    scene = main_window.view.scene
    service = main_window.view.category_panel.material_service
    source = tmp_path / 'original.png'
    image = Image.new('RGBA', (17, 13), (21, 64, 193, 81))
    image.save(source)
    original = source.read_bytes()
    record = service.create(name='三合一')
    for slot, channel in [('ao', 'R'), ('roughness', 'G'), ('metallic', 'B'), ('opacity', 'A')]:
        service.configure(record['id'], slot=slot, prepared=prepare_texture(source), channel=channel)
    assert len(scene.attachments) == 1
    assert len(service.get(record['id'])['files']) == 1
    # Even a stale thumbnail pointer must never overwrite an original attachment.
    from prism.material_textures import render_signature
    service.records[0]['preview']['thumbnailAttachmentId'] = 1
    service.save_preview(record['id'], render_signature(service.get(record['id'])), original)
    assert scene.attachments[1][2] == original
    assert service.get(record['id'])['preview']['thumbnailAttachmentId'] != 1
    filename = tmp_path / 'originals.prism'
    SQLiteIO(str(filename), scene, create_new=True).write()
    source.unlink()
    SQLiteIO(str(filename), scene, readonly=True).read()
    loaded = scene.material_records[0]
    paths = export_originals(loaded, scene.attachments, tmp_path / 'export')
    assert Path(paths[0]).read_bytes() == original
    for slot, value in [('ao', 21), ('roughness', 64), ('metallic', 193), ('opacity', 81)]:
        b = loaded['textures'][slot]
        assert selected_image(scene.attachments[b['attachmentId']][2], b['channel']).getpixel((0, 0)) == value


def test_scalar_rgb_ambiguity_is_not_guessed_and_packed_conversion_is_linear(main_window, tmp_path):
    source = tmp_path / 'packed.png'
    Image.new('RGB', (8, 8), (32, 96, 192)).save(source)
    service = main_window.view.category_panel.material_service
    record = service.create()
    service.configure(record['id'], slot='roughness', prepared=prepare_texture(source))
    with pytest.raises(ValueError, match='请选择'):
        prepare_render(service.get(record['id']), service.scene.attachments, tmp_path / 'preview')
    service.configure(record['id'], slot='roughness', channel='G')
    service.configure(record['id'], slot='metallic', prepared=prepare_texture(source), channel='B')
    prepare_render(service.get(record['id']), service.scene.attachments, tmp_path / 'preview')
    with Image.open(tmp_path / 'preview/metallicRoughness.png') as image:
        assert image.getpixel((0, 0)) == (255, 96, 192)
    assert len(service.scene.attachments) == 1


def test_same_bytes_keep_each_original_filename(main_window, tmp_path):
    first = tmp_path/'first.png'
    second = tmp_path/'second.png'
    Image.new('L',(8,8),80).save(first)
    second.write_bytes(first.read_bytes())
    service=main_window.view.category_panel.material_service
    record=service.create()
    service.configure(record['id'],slot='roughness',prepared=prepare_texture(first))
    service.configure(record['id'],slot='ao',prepared=prepare_texture(second))
    record=service.get(record['id'])
    assert len(service.scene.attachments)==1
    assert {f['name'] for f in record['files']}=={'first.png','second.png'}
    assert record['textures']['ao']['name']=='second.png'
    paths=export_originals(record,service.scene.attachments,tmp_path/'export')
    assert {Path(p).name for p in paths}=={'first.png','second.png'}


def test_engine_renders_official_emissive_texture(qtbot, tmp_path):
    from PyQt6.QtWidgets import QApplication
    if QApplication.platformName() != 'windows':
        pytest.skip('Actual GPU rendering is verified in a separate native Windows run')
    from prism.widgets.material_render import MaterialRender
    path = FIXTURES / 'CompareEmissiveStrength/glTF/Compare_Emissive-Strength_img0.jpg'
    info, blob = prepare_texture(path)
    record = dict(textures={'emissive': dict(info, attachmentId=1, channel='RGB')}, preview={})
    payload = prepare_render(record, {1: (info['name'], info['mime'], blob)}, tmp_path)
    viewer = MaterialRender()
    qtbot.addWidget(viewer)
    viewer.resize(480, 480)
    results, errors = [], []
    viewer.completed.connect(lambda signature, data: results.append(data))
    viewer.failed.connect(errors.append)
    viewer.show()
    viewer.load_material(payload, 'test')
    qtbot.waitUntil(lambda: bool(results or errors), timeout=45000)
    assert not errors, errors
    with Image.open(io.BytesIO(results[0])) as image:
        assert len(image.getcolors(image.width * image.height)) > 100
    (tmp_path / 'emissive-render.png').write_bytes(results[0])


def test_glb_uses_official_loader_and_preserves_pbr(qtbot, tmp_path):
    from PyQt6.QtWidgets import QApplication
    if QApplication.platformName() != 'windows':
        pytest.skip('Native Windows WebGL acceptance')
    from prism.widgets.glb_engine_view import GLBEngineWindow
    window = GLBEngineWindow(path=FIXTURES/'BoxTextured/glTF-Binary/BoxTextured.glb')
    qtbot.addWidget(window)
    window.show()
    viewer = window.viewer
    errors=[]
    viewer.failed.connect(errors.append)
    qtbot.waitUntil(lambda: viewer.ready,timeout=10000)
    def diagnostics():
        result=[]
        viewer.web.page().runJavaScript('prismRender.diagnostics || null',result.append)
        qtbot.waitUntil(lambda: bool(result))
        return result[0]
    qtbot.waitUntil(lambda: bool(errors or diagnostics()),timeout=30000)
    assert not errors, errors
    state=diagnostics()
    assert state['loader']=='Babylon.js glTF 2.0'
    assert state['materials'] > 0 and state['textures'] > 0
    assert not viewer._render_deadline.isActive()
    viewer.web.grab().save(str(tmp_path/'official-glb-viewer.png'))


def test_engine_alpha_modes_use_explicit_alpha_not_filename(qtbot, tmp_path):
    from PyQt6.QtWidgets import QApplication
    if QApplication.platformName() != 'windows':
        pytest.skip('Native Windows WebGL acceptance')
    from prism.widgets.material_render import MaterialRender
    path=FIXTURES/'AlphaBlendModeTest/glTF/AlphaBlendLabels.png'
    info,blob=prepare_texture(path)
    attachments={1:(info['name'],info['mime'],blob)}
    viewer=MaterialRender()
    qtbot.addWidget(viewer)
    viewer.resize(512,512)
    viewer.show()
    results,errors=[],[]
    viewer.completed.connect(lambda signature,data: results.append(data))
    viewer.failed.connect(errors.append)
    for mode in ('opaque','masked','translucent'):
        record=dict(textures={
            'basecolor':dict(info,attachmentId=1,channel='RGB'),
            'opacity':dict(info,attachmentId=1,channel='A')},
            preview={'properties':{'blendMode':mode}})
        payload=prepare_render(record,attachments,tmp_path/mode)
        before=len(results)
        viewer.load_material(payload,mode)
        qtbot.waitUntil(lambda: len(results)>before or bool(errors),timeout=40000)
        assert not errors,errors
        (tmp_path/(mode+'.png')).write_bytes(results[-1])
    assert len({hashlib.sha256(b).hexdigest() for b in results})==3


def test_hidden_slot_construction_never_shows_temporary_windows(qtbot, qapp):
    from PyQt6 import QtCore, QtWidgets
    from prism.material_parameters import RANGES
    from prism.widgets.material_slots import MaterialSlot
    from prism.widgets.widget_construction import widget_steps

    owner = QtWidgets.QWidget()
    qtbot.addWidget(owner)
    shown = []
    class WindowShows(QtCore.QObject):
        def eventFilter(self, widget, event):
            if (event.type() == QtCore.QEvent.Type.Show
                    and isinstance(widget, QtWidgets.QWidget) and widget.isWindow()):
                shown.append(type(widget).__name__)
            return False
    watcher = WindowShows(owner)
    qapp.installEventFilter(watcher)
    try:
        for key in RANGES:
            for _ in widget_steps(MaterialSlot, key, key, owner):
                qapp.processEvents()
        assert shown == [], 'Hidden inspector construction exposed temporary top-level windows'
    finally:
        qapp.removeEventFilter(watcher)


