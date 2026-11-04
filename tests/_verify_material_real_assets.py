"""Manual acceptance run: native Windows GPU + the user's external originals."""
import hashlib
import io
import json
from pathlib import Path

import pytest
from PIL import Image
from PyQt6 import QtCore

from prism.material_textures import prepare_texture, prepare_render, export_originals
from prism.widgets.material_render import MaterialRender
from prism.fileio.sql import SQLiteIO

ROOT = Path(__file__).resolve().parents[1]
SOURCE = Path(r'C:\Users\admin\Desktop\ceshi\Game\Assets\Level\YingZhuFuGuanYaGC\Mesh\Tex')
OUT = ROOT / 'artifacts/material-tests'


def test_real_assets_original_roundtrip_and_engine(main_window, qtbot, tmp_path):
    if not SOURCE.exists():
        pytest.skip('User-owned source textures are intentionally not distributed')
    from PyQt6.QtWidgets import QApplication
    assert QApplication.platformName() == 'windows'
    OUT.mkdir(parents=True, exist_ok=True)
    window = main_window
    window.resize(1500, 960)
    window.show()
    window.open_material_library()
    service = window.material_library.records
    scene = window.view.scene
    viewer = MaterialRender()
    qtbot.addWidget(viewer)
    viewer.resize(640, 640)
    viewer.show()
    results, errors = [], []
    viewer.completed.connect(lambda signature, blob: results.append((signature, blob)))
    viewer.failed.connect(errors.append)
    cases = [
        ('石砖 · ORM', '1', [('basecolor','_D','RGB'),('normal','_N','RGB'),
                           ('ao','_ORM','R'),('roughness','_ORM','G'),('metallic','_ORM','B')], 'opaque'),
        ('头骨 · 用户确认 RGB 接法', '2', [('basecolor','_BC','RGB'),('normal','_N','RGB'),
                           ('ao','_AO','R'),('roughness','_AO','G'),('metallic','_AO','B')], 'opaque'),
        ('地面 · 8K 独立贴图与置换', '3', [('basecolor','Albedo','RGB'),('normal','Normal','RGB'),
                           ('ao','AO','RGB'),('roughness','Roughness','RGB'),('displacement','Displacement','RGB')], 'opaque'),
        ('植物 · R 遮罩 G 粗糙 B SSS', '4', [('basecolor','_B','RGB'),('normal','_N','RGB'),
                           ('opacity','_ORT','R'),('roughness','_ORT','G'),('sss','_ORT','B')], 'masked'),
    ]
    report = []
    for index, (name, folder, bindings, mode) in enumerate(cases):
        record = service.create(name=name)
        originals = {}
        for slot, suffix, channel in bindings:
            file = next(p for p in (SOURCE/folder).iterdir() if p.stem.endswith(suffix))
            if file not in originals:
                originals[file] = prepare_texture(file)
            service.configure(record['id'], slot=slot, prepared=originals[file], channel=channel)
        service.configure(record['id'], blend_mode=mode, normal_convention='DirectX')
        record = service.get(record['id'])
        payload = prepare_render(record, scene.attachments, OUT / f'render-inputs-{index+1}')
        # Check every extracted scalar pixel against the real source's selected band.
        for slot, suffix, channel in bindings:
            if channel not in ('R','G','B','A'):
                continue
            file = next(p for p in originals if p.stem.endswith(suffix))
            with Image.open(file) as source:
                expected=source.convert('RGBA').getchannel(channel)
                expected.thumbnail((2048,2048),Image.Resampling.LANCZOS)
                with Image.open(OUT/f'render-inputs-{index+1}'/(slot+'.png')) as output:
                    assert output.tobytes()==expected.tobytes()
        before = len(results)
        viewer.load_material(payload, str(index))
        qtbot.waitUntil(lambda: len(results)>before or bool(errors),timeout=60000)
        assert not errors, errors
        from tests.test_material_view_controls import js
        assert js(viewer,'material.transparencyMode',qtbot)==js(viewer,
            'BABYLON.PBRMaterial.PBRMATERIAL_'+('ALPHATEST' if mode=='masked' else 'OPAQUE'),qtbot)
        signature, blob = results[-1]
        assert service.save_preview(record['id'],signature,blob)
        (OUT/f'material-{index+1}.png').write_bytes(blob)
        record = service.get(record['id'])
        exported=export_originals(record,scene.attachments,tmp_path/f'export-{index+1}')
        expected_hashes={info['sha256'] for info,_ in originals.values()}
        assert {hashlib.sha256(Path(p).read_bytes()).hexdigest() for p in exported}==expected_hashes
        report.append(dict(name=name, originalFiles=len(originals),
            originalHashes=sorted(expected_hashes), sourceBytes=sum(len(blob) for _,blob in originals.values()),
            channelsVerified=True, originalExportVerified=True, engineRendered=True))
    filename=OUT/'真实素材验收.prism'
    SQLiteIO(str(filename),scene,create_new=True).write()
    SQLiteIO(str(filename),scene,readonly=True).read()
    # Validate all originals from the saved project, not the in-memory import.
    for record in scene.material_records:
        for file in record['files']:
            blob=scene.attachments[file['attachmentId']][2]
            assert hashlib.sha256(blob).hexdigest()==file['sha256']
    window.view.category_panel._install_resource_service()
    page=window.material_library
    page.load_from_scene()
    page._selection_changed(scene.material_records[-1]['id'])
    qtbot.waitUntil(lambda: page.materials.ready,timeout=15000)
    qtbot.wait(1500)
    window.grab().save(str(OUT/'material-library-real-assets.png'))
    (OUT/'report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
