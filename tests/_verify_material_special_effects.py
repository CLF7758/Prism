"""Real emissive + user ORT/SSS assets, with observable rendered differences."""
import copy
import hashlib
import io
import json
from pathlib import Path

from PIL import Image, ImageChops, ImageStat
from PyQt6 import QtWidgets
from prism.material_textures import prepare_texture, prepare_render, export_originals, resolved_properties
from prism.widgets.material_render import MaterialRender
from tests.test_material_view_controls import js

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT/'artifacts/material-tests/special-effects'
SOURCE = Path(r'C:\Users\admin\Desktop\ceshi\Game\Assets\Level\YingZhuFuGuanYaGC\Mesh\Tex')


def test_real_emission_and_sss(main_window, qtbot, tmp_path):
    assert QtWidgets.QApplication.platformName() == 'windows'
    OUT.mkdir(parents=True, exist_ok=True)
    service = main_window.material_library.records
    emissive = service.create(name='Khronos actual emissive texture')
    texture = ROOT/'third_party/material-test-assets/Khronos/Models/CompareEmissiveStrength/glTF/Compare_Emissive-Strength_img0.jpg'
    service.configure(emissive['id'], slot='emissive', prepared=prepare_texture(texture))
    sss = service.create(name='User ORT R opacity G roughness B SSS')
    for slot, suffix, channel in [('basecolor','_B','RGB'), ('normal','_N','RGB'),
                                  ('opacity','_ORT','R'), ('roughness','_ORT','G'), ('sss','_ORT','B')]:
        source = next(path for path in (SOURCE/'4').iterdir() if path.stem.endswith(suffix))
        service.configure(sss['id'], slot=slot, prepared=prepare_texture(source), channel=channel)
    viewer = MaterialRender()
    qtbot.addWidget(viewer)
    viewer.resize(512,512)
    viewer.show()
    viewer.capture_enabled = False
    results, updates, errors = [], [], []
    viewer.completed.connect(lambda signature, blob: results.append(blob))
    viewer.updated.connect(updates.append)
    viewer.failed.connect(errors.append)
    report = []
    for name, material, slot in [('emissive',emissive,'emissive'), ('sss',sss,'sss')]:
        record = service.get(material['id'])
        payload = prepare_render(record, service.scene.attachments, OUT/name/'inputs')
        source_hashes = {file['sha256'] for file in record['files']}
        captures = []
        for value in (0., 1.):
            data = copy.deepcopy(payload)
            props = copy.deepcopy(record['preview'].get('properties', {}))
            props.setdefault('slots', {}).setdefault(slot, {})['value'] = value
            if name == 'emissive':
                props['slots']['basecolor'] = dict(value=0.)
            data['properties'] = resolved_properties(props, data['maps'],
                payload['properties']['sourceAutoBlendMode'], payload['properties']['sourceAutoBlendReason'])
            data['signature'] = name+str(value)
            old = len(updates)
            viewer.load_material(data, name+str(value))
            qtbot.waitUntil(lambda: len(updates)>old or bool(errors), timeout=45000)
            assert not errors, errors
            if name == 'emissive':
                js(viewer, 'scene.lights.forEach(light=>light.intensity=0);scene.environmentIntensity=0;', qtbot)
                assert js(viewer, '!!material.emissiveTexture&&material.emissiveTexture.gammaSpace', qtbot)
            else:
                js(viewer, 'scene.lights[0].intensity=3;scene.lights[0].direction=new BABYLON.Vector3(0,0,-1);scene.lights[1].intensity=0;scene.environmentIntensity=.1;', qtbot)
                assert js(viewer, '!!material.subSurface.translucencyIntensityTexture', qtbot)
                assert js(viewer, 'material.subSurface.translucencyIntensity', qtbot) == value
            old = len(results)
            viewer._capture()
            qtbot.waitUntil(lambda: len(results)>old or bool(errors), timeout=30000)
            assert not errors
            captures.append(Image.open(io.BytesIO(results[-1])).convert('RGB'))
            (OUT/(name+('-on' if value else '-off')+'.png')).write_bytes(results[-1])
        difference = sum(ImageStat.Stat(ImageChops.difference(*captures)).mean)/3
        assert difference > .05, (name, 'no visible response', difference)
        paths = export_originals(record, service.scene.attachments, tmp_path/name/'export')
        assert {hashlib.sha256(Path(path).read_bytes()).hexdigest() for path in paths} == source_hashes
        if name == 'sss':
            binding = record['textures']['sss']
            with Image.open(io.BytesIO(service.scene.attachments[binding['attachmentId']][2])) as original:
                expected = original.convert('RGBA').getchannel('B')
                expected.thumbnail((2048,2048), Image.Resampling.LANCZOS)
                with Image.open(OUT/name/'inputs/sss.png') as extracted:
                    assert extracted.tobytes() == expected.tobytes()
        report.append(dict(effect=name, actual_texture=True, average_rgb_pixel_difference=difference,
                           originals_exact=True, note='native thin-surface translucency' if name=='sss' else 'lights off: true emissive'))
    # No idle frame pump; interaction must wake the native Babylon render loop.
    qtbot.wait(1800)
    assert js(viewer, 'engine._activeRenderLoops.length', qtbot) == 0
    viewer.reset_camera()
    qtbot.wait(100)
    assert js(viewer, 'engine._activeRenderLoops.length', qtbot) > 0
    (OUT/'report.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
