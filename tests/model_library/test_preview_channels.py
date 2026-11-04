"""Independent pixel/channel expectations; no desktop windows or original edits."""
import base64
import hashlib
import io
import json
import struct
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from PIL import Image

from prism.model_library.preview_channels import (
    diagnostic, gltf_channel, modes_for, read_gltf)


@pytest.fixture
def source(tmp_path):
    image = Image.new('RGBA', (2, 2), (32, 128, 240, 64))
    image.save(tmp_path / 'packed.png')
    (tmp_path / 'mesh.bin').write_bytes(bytes(range(32)))
    doc = {'asset': {'version': '2.0'},
           'buffers': [{'uri': 'mesh.bin', 'byteLength': 32}],
           'images': [{'uri': 'packed.png'}], 'textures': [{'source': 0, 'sampler': 0}],
           'samplers': [{'wrapS': 33071}],
           'meshes': [{'primitives': [{'material': 0}, {'material': 1}]}],
           'materials': [
               {'name': 'cloth', 'doubleSided': True,
                'pbrMetallicRoughness': {'metallicRoughnessTexture': {'index': 0},
                   'baseColorTexture': {'index': 0}, 'roughnessFactor': .5,
                   'metallicFactor': .25, 'baseColorFactor': [1, 1, 1, .5]},
                'normalTexture': {'index': 0, 'texCoord': 1,
                   'extensions': {'KHR_texture_transform': {'offset': [.1, .2]}}},
                'occlusionTexture': {'index': 0}},
               {'name': 'metal', 'pbrMetallicRoughness': {'roughnessFactor': .25}}]}
    path = tmp_path / 'model.gltf'
    path.write_text(json.dumps(doc))
    return path, doc


@pytest.mark.parametrize('channel,expected', [('roughness', 64), ('metallic', 60),
                                            ('ao', 32), ('opacity', 32)])
def test_exact_channel_components_factors_and_geometry(source, tmp_path, channel, expected):
    path, original = source
    hashes = {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in path.parent.iterdir() if p.is_file()}
    output = gltf_channel(path, channel, tmp_path / channel)
    doc, _ = read_gltf(output)
    assert doc['meshes'] == original['meshes']
    assert doc['samplers'] == original['samplers']
    assert (output.parent / doc['buffers'][0]['uri']).read_bytes() == bytes(range(32))
    material = doc['materials'][0]
    assert material['doubleSided'] is True
    texture = doc['textures'][material['pbrMetallicRoughness']['baseColorTexture']['index']]
    with Image.open(output.parent / doc['images'][texture['source']]['uri']) as image:
        assert image.getpixel((0, 0)) == (expected,) * 3
    assert hashes == {name: hashlib.sha256((path.parent / name).read_bytes()).hexdigest() for name in hashes}


def test_normal_preserves_per_material_uv_binding(source, tmp_path):
    path, original = source
    doc, _ = read_gltf(gltf_channel(path, 'normal', tmp_path / 'normal'))
    texture = doc['materials'][0]['pbrMetallicRoughness']['baseColorTexture']
    assert texture == original['materials'][0]['normalTexture']
    assert 'baseColorTexture' not in doc['materials'][1]['pbrMetallicRoughness']
    assert doc['materials'][1]['name'] == 'metal'


def test_embedded_glb_keeps_original_buffer_bytes(tmp_path):
    stream = io.BytesIO()
    Image.new('RGB', (2, 2), (40, 120, 240)).save(stream, 'PNG')
    binary = stream.getvalue()
    binary += b'\0' * (-len(binary) % 4)
    doc = {'asset': {'version': '2.0'}, 'buffers': [{'byteLength': len(binary)}],
           'bufferViews': [{'buffer': 0, 'byteLength': len(stream.getvalue())}],
           'images': [{'bufferView': 0, 'mimeType': 'image/png'}],
           'textures': [{'source': 0}], 'materials': [{'pbrMetallicRoughness': {
               'metallicRoughnessTexture': {'index': 0}}}]}
    encoded = json.dumps(doc).encode(); encoded += b' ' * (-len(encoded) % 4)
    path = tmp_path / 'model.glb'
    path.write_bytes(struct.pack('<4sII', b'glTF', 2, 28 + len(encoded) + len(binary))
       + struct.pack('<II', len(encoded), 0x4E4F534A) + encoded
       + struct.pack('<II', len(binary), 0x004E4942) + binary)
    output = gltf_channel(path, 'roughness', tmp_path / 'cache')
    result, result_binary = read_gltf(output)
    assert result_binary == binary
    texture = result['textures'][result['materials'][0]['pbrMetallicRoughness']['baseColorTexture']['index']]
    with Image.open(output.parent / result['images'][texture['source']]['uri']) as image:
        assert image.getpixel((0, 0)) == (120, 120, 120)


def test_sss_and_displacement_are_not_faked(source):
    reasons = modes_for(source[0])
    assert reasons['sss'] and reasons['displacement']


def test_implicit_gltf_material_gets_format_default_normal(tmp_path):
    path = tmp_path / 'implicit.gltf'
    path.write_text(json.dumps({'asset': {'version': '2.0'},
                               'meshes': [{'primitives': [{}]}]}))
    doc, _ = read_gltf(gltf_channel(path, 'normal', tmp_path / 'cache'))
    material = doc['materials'][doc['meshes'][0]['primitives'][0]['material']]
    factor = material['pbrMetallicRoughness']['baseColorFactor']
    assert factor[:2] == pytest.approx([.21404114, .21404114])
    assert factor[2:] == [1, 1]


def test_obj_normal_requires_declared_normal_not_bump(tmp_path):
    path = tmp_path / 'model.obj'
    path.write_text('mtllib model.mtl\nusemtl cloth\nv 0 0 0\n')
    mtl = tmp_path / 'model.mtl'
    Image.new('RGB', (2, 2), (128, 128, 255)).save(tmp_path / 'n.png')
    mtl.write_text('newmtl cloth\nmap_Bump n.png\n')
    assert modes_for(path)['normal']
    mtl.write_text('newmtl cloth\nnorm n.png\n')
    assert not modes_for(path)['normal']
    target, options = diagnostic(path, 'normal', {}, tmp_path / 'cache')
    assert 'usemtl cloth' in target.read_text()
    assert 'map_Kd deps/' in (target.parent / 'channels.mtl').read_text()
    assert options['model.unlit'] is True


def test_preview_theme_and_channel_controls(qtbot, qapp):
    from prism.widgets.preview_theme import apply_preview_theme
    from prism.widgets.model_preview import ModelPreviewWindow
    from prism.model_library.contracts import ModelCapabilities
    previous = qapp.styleSheet()
    palette, font = qapp.palette(), qapp.font()
    try:
        apply_preview_theme(qapp)
        assert '#E7E9ED' in qapp.styleSheet()
        assert 'url(assets/icons/' not in qapp.styleSheet()
        dialog = ModelPreviewWindow(); qtbot.addWidget(dialog)
        dialog.set_capabilities(ModelCapabilities(preview=True))
        dialog.set_available_channels({'normal': '', 'sss': 'unsupported'})
        assert dialog.mode.model().item(dialog.mode.findData('normal')).isEnabled()
        assert not dialog.mode.model().item(dialog.mode.findData('sss')).isEnabled()
        dialog.show_mode('normal')
        assert not dialog.cover_button.isEnabled()
        dialog.set_capabilities(ModelCapabilities(preview=True))
        assert not dialog.cover_button.isEnabled()
        dialog.show_mode('material')
        assert dialog.cover_button.isEnabled()
    finally:
        qapp.setStyleSheet(previous)
        qapp.setPalette(palette)
        qapp.setFont(font)


def test_preview_present_repeats_raise_and_activation_without_desktop(qtbot, qapp):
    from prism.components.model_preview import PreviewHost
    host = object.__new__(PreviewHost)
    host._closed = False
    # A real host owns the application object it asks about the shutdown
    # state; a stub without it would fail before the behaviour is reached.
    host._app = qapp
    shell = Mock(); shell.isMinimized.return_value = False
    host._shell = shell
    host._present_window()
    assert shell.raise_.call_count == 1
    assert shell.activateWindow.call_count == 1
    qtbot.waitUntil(lambda: shell.raise_.call_count == 2)
    shell.isMinimized.return_value = True
    host._present_window()
    shell.showNormal.assert_called_once()
    host._closed = True  # queued callback must not touch a closed window


def test_double_click_opens_clicked_id_not_previous_selection():
    from prism.widgets.model_library import ModelLibrary
    selection = SimpleNamespace(current='previous')
    page = SimpleNamespace(selection=selection, selected_id='previous', open_preview=Mock())
    page._selection_changed = lambda mid: setattr(selection, 'current', mid)
    ModelLibrary._open_preview(page, 'clicked')
    page.open_preview.assert_called_once_with('clicked')


def test_windows_foreground_permission_is_only_given_while_owner_active(monkeypatch):
    import ctypes
    from prism.model_library import native_jobs
    user32 = Mock()
    user32.GetForegroundWindow.return_value = 123
    monkeypatch.setattr(native_jobs.sys, 'platform', 'win32')
    monkeypatch.setattr(ctypes, 'windll', SimpleNamespace(user32=user32))
    native_jobs._allow_preview_foreground(456, 999)
    user32.AllowSetForegroundWindow.assert_not_called()
    native_jobs._allow_preview_foreground(456, 123)
    user32.AllowSetForegroundWindow.assert_called_once_with(456)
