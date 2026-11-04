"""Native Babylon transparency responses using a bundled Khronos source."""
import hashlib
import io
import json
from pathlib import Path
from PIL import Image,ImageChops,ImageStat
from prism.material_textures import prepare_texture,prepare_render,export_originals
from prism.widgets.material_render import MaterialRender
from tests.test_material_view_controls import js

ROOT=Path(__file__).resolve().parents[1]
OUT=ROOT/'artifacts/material-tests/transparency-audit-20261021'

def test_actual_alpha_source_auto_modes_and_rendered_response(main_window,qtbot,tmp_path):
    source=ROOT/'third_party/material-test-assets/Khronos/Models/AlphaBlendModeTest/glTF/AlphaBlendLabels.png'
    original=source.read_bytes()
    service=main_window.material_library.records
    record=service.create(name='Khronos alpha mode audit')
    service.configure(record['id'],slot='basecolor',prepared=prepare_texture(source))
    viewer=MaterialRender();qtbot.addWidget(viewer);viewer.resize(512,512);viewer.show()
    images=[];errors=[];report=[]
    viewer.completed.connect(lambda signature,blob:images.append(blob))
    viewer.failed.connect(errors.append)
    OUT.mkdir(parents=True,exist_ok=True)
    captures={}
    for label,mode,ignore,opacity in [('opaque','opaque',True,1.),('translucent','translucent',False,1.),('translucent-half','translucent',False,.5),('translucent-reset','translucent',False,1.)]:
        service.configure(record['id'],properties_patch={'ignoreBaseAlpha':ignore},slot_settings=('opacity',{'value':opacity}))
        payload=prepare_render(service.get(record['id']),service.scene.attachments,tmp_path/'shared-inputs')
        assert payload['properties']['blendMode']==mode,payload['properties']
        before=len(images)
        viewer.load_material(payload,label)
        qtbot.waitUntil(lambda:len(images)>before or bool(errors),timeout=45000)
        assert not errors,errors
        native=js(viewer,'material.transparencyMode',qtbot)
        expected=js(viewer,"BABYLON.PBRMaterial.PBRMATERIAL_"+{'opaque':'OPAQUE','masked':'ALPHATEST','translucent':'ALPHABLEND'}[mode],qtbot)
        assert native==expected
        assert js(viewer,'material.alpha',qtbot)==opacity
        blob=images[-1];(OUT/(label+'.png')).write_bytes(blob)
        captures[label]=Image.open(io.BytesIO(blob)).convert('RGB')
        qtbot.wait(100)
        viewer.web.grab().save(str(OUT/(label+'-live.png')))
        report.append(dict(label=label,mode=mode,babylon_mode=native,alpha=opacity,automatic_mode=True))
    difference={}
    for left,right in [('opaque','translucent'),('translucent','translucent-half'),('translucent-half','translucent-reset')]:
        value=sum(ImageStat.Stat(ImageChops.difference(captures[left],captures[right])).mean)/3
        assert value>.05,(left,right,value)
        difference[left+'-'+right]=value
    paths=export_originals(service.get(record['id']),service.scene.attachments,tmp_path/'export')
    assert {hashlib.sha256(Path(p).read_bytes()).hexdigest() for p in paths}=={hashlib.sha256(original).hexdigest()}
    assert source.read_bytes()==original
    (OUT/'report.json').write_text(json.dumps(dict(cases=report,pixel_differences=difference,original_export_exact=True),indent=2),encoding='utf-8')
