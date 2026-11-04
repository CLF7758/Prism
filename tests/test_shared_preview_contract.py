"""Shared material interpretation, real Babylon adapter and transient feedback."""
import json
import subprocess
import time
from pathlib import Path

import pytest

from prism.material_parameters import resolved_controls
from prism.material_preview_profile import ASSET_ROOT, preview_profile, profile_signature
from prism.material_textures import resolved_properties
from prism.model_library.multimaterial.presentation import material_parameters, material_spec
from prism.model_library.multimaterial.contracts import MaterialDefinition, TextureUse


def test_both_adapters_use_identical_mapped_controls_and_linear_colours():
    properties = {'slots': {
        'basecolor': {'color': [.2, .4, .8], 'value': 1.3},
        'roughness': {'value': .2, 'factor': .7},
        'metallic': {'value': .1, 'factor': .4},
        'normal': {'value': 2}, 'ao': {'value': .6},
        'emissive': {'color': [.3, .5, .7], 'value': 2}}}
    maps = {'basecolor', 'roughness', 'metallic', 'normal', 'ao', 'emissive'}
    browser = resolved_properties(properties, maps, 'opaque', '')['controls']
    native = material_parameters(properties, maps)
    assert native['color'] == pytest.approx([c * browser['basecolor']['amount']
                                           for c in browser['basecolor']['linearColor']])
    assert native['roughness'] == browser['roughness']['amount'] == .7
    assert native['metallic'] == browser['metallic']['amount'] == .4
    assert native['normal_scale'] == browser['normal']['amount']
    assert native['occlusion_strength'] == browser['ao']['amount']
    assert native['emissive_factor'] == pytest.approx([
        c * browser['emissive']['amount'] for c in browser['emissive']['linearColor']])
    assert material_parameters({}, {'basecolor'})['color'] == [1, 1, 1]
    # File-authored linear tint is evidence, not a UI sRGB colour to convert again.
    authored = material_parameters({'basecolor_color_space': 'linear',
        'slots': {'basecolor': {'color': [.2158605] * 3, 'value': 2}}}, {'basecolor'})
    assert authored['color'] == pytest.approx([.431721] * 3)


def test_normal_fallback_is_shared_but_authored_convention_is_preserved(tmp_path):
    from PIL import Image
    from dataclasses import replace
    image = tmp_path / 'normal.png'
    Image.new('RGB', (2, 2), (128, 128, 255)).save(image)
    use = TextureUse('normal', 'normal', 'RGB', 0, 'linear')
    definition = MaterialDefinition('m', 'm', 'M', 'pbr', (use,))
    spec = material_spec(definition, {'normal': str(image)}, tmp_path)
    assert spec['maps']['normal']['normal_convention'] == preview_profile()['normalConvention']
    explicit = replace(definition, textures=(replace(use, normal_convention='OpenGL'),))
    assert material_spec(explicit, {'normal': str(image)}, tmp_path)['maps']['normal']['normal_convention'] == 'OpenGL'
    gltf = replace(definition, textures=(replace(use, normal_convention='opengl'),))
    assert material_spec(gltf, {'normal': str(image)}, tmp_path)['maps']['normal']['normal_convention'] == 'OpenGL'
    assert spec['preview_profile'] == profile_signature('model')


@pytest.mark.parametrize('legacy_controls', [False, True])
def test_babylon_applies_the_shared_resolved_values(tmp_path, legacy_controls):
    """Real bundled Babylon PBR object; this proves inputs, not GPU pixels."""
    properties = {'slots': {'basecolor': {'color': [.2, .4, .8], 'value': 1.3},
                            'roughness': {'factor': .7}, 'metallic': {'factor': .4}}}
    props = resolved_properties(properties, {'basecolor', 'roughness', 'metallic'}, 'opaque', '')
    if legacy_controls:
        for control in props['controls'].values():
            control.pop('linearColor')
    data = {'properties': props, 'previewProfile': preview_profile()}
    path = tmp_path / 'request.json'
    path.write_text(json.dumps(data), encoding='utf-8')
    script = r'''
const fs=require('fs'),vm=require('vm');
const B=require(process.argv[1]+'/babylon/babylon.js');
const engine=new B.NullEngine();const scene=new B.Scene(engine);
const material=new B.PBRMaterial('test',scene);
const source=fs.readFileSync(process.argv[1]+'/render.js','utf8');
const fn=source.slice(source.indexOf('function applySettings('),source.indexOf('async function finish('));
const maps={basecolor:new B.Texture(null,scene),metallicRoughness:new B.Texture(null,scene)};
const data=JSON.parse(fs.readFileSync(process.argv[2],'utf8'));
try{
  const context={B,material,maps,data};vm.createContext(context);
  vm.runInContext(fn+';applySettings(data);',context);
  console.log(JSON.stringify({color:material.albedoColor.asArray(),roughness:material.roughness,
    metallic:material.metallic,invertNormalMapY:material.invertNormalMapY}));
}finally{scene.dispose();engine.dispose();}
'''
    result = subprocess.run(['node', '-e', script, str(ASSET_ROOT), str(path)],
        capture_output=True, text=True, timeout=20,
        creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
    assert result.returncode == 0, result.stdout + result.stderr
    actual = json.loads(result.stdout.splitlines()[-1])
    native = material_parameters(properties, {'basecolor', 'roughness', 'metallic'})
    assert actual['color'] == pytest.approx(native['color'])
    assert actual['roughness'] == native['roughness']
    assert actual['metallic'] == native['metallic']
    assert actual['invertNormalMapY'] == (preview_profile()['normalConvention'] == 'DirectX')


def test_preview_profile_is_detached_and_injected_without_mutating_input(qtbot, monkeypatch):
    from prism.widgets.material_render import MaterialRender
    monkeypatch.setattr(MaterialRender, '_ensure_web', lambda self: None)
    viewer = MaterialRender()
    qtbot.addWidget(viewer)
    payload = {'properties': {}, 'maps': {}, 'signature': 'immutable'}
    viewer.load_material(payload, 'request')
    assert payload == {'properties': {}, 'maps': {}, 'signature': 'immutable'}
    assert viewer._pending.payload['previewProfile'] == preview_profile()
    assert viewer._pending.payload['properties']['controls'] == resolved_controls({})
    copy = preview_profile()
    copy['untexturedColor'][0] = 999
    assert preview_profile()['untexturedColor'][0] != 999


def test_library_lighting_overrides_share_environment_and_render_rules():
    material = preview_profile()
    model = preview_profile('model')
    assert (material['environmentIntensity'], material['exposure']) == (1.0, 1.0)
    assert (model['environmentIntensity'], model['exposure']) == (1.3, 1.5)
    for key in material.keys() - {'environmentIntensity', 'exposure'}:
        assert model[key] == material[key]
    assert 'libraries' not in model
    assert profile_signature() != profile_signature('model')


def test_highlight_expires_and_expired_queued_selection_cannot_flash(qtbot):
    from prism.widgets.preview_highlight import PreviewHighlight
    frames = []
    feedback = PreviewHighlight(frames.append)
    feedback.show(['single-material-model'], time.monotonic() + .05)
    assert frames == [('single-material-model',)]
    qtbot.waitUntil(lambda: frames[-1] == (), timeout=2000)
    assert not feedback.timer.isActive()
    feedback.show(['old'], time.monotonic() - 1)
    assert frames == [('single-material-model',), ()]
    feedback.show(['new'])
    feedback.clear()  # Used by interaction, mode change and scene replacement.
    assert frames[-2:] == [('new',), ()]
