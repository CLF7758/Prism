"""Real Qt/IPC/GPU verification with WA_DontShowOnScreen, no desktop operations."""
import hashlib
import json
import os
from pathlib import Path
import time

from prism.model_library.native_jobs import NativeModelJobController, RuntimeConfig
from prism.model_library.native_protocol import WORKER_NAME_PREVIEW
from prism.model_library.contracts import ModelRequestIdentity, ModelJobRequest, ModelJobKind

ROOT=Path(__file__).resolve().parents[1]
OUT=ROOT/'artifacts/shared-renderer-integration-20261102'
cases=json.loads((OUT/'inputs.json').read_text())
os.environ.pop('QT_OPENGL',None)
controller=NativeModelJobController(RuntimeConfig(cache_root=OUT/'qt-hidden-worker',
    worker_target='prism.components.material_preview',worker_worker_args=('--headless',),
    worker_name=WORKER_NAME_PREVIEW,startup_timeout_s=25,shutdown_timeout_s=3,
    env={'QT_QPA_PLATFORM':'windows','QTWEBENGINE_CHROMIUM_FLAGS':'--no-sandbox --disable-gpu-shader-disk-cache'}))
report=dict(runtime='QtWebEngine hidden native Windows surface',cases=[],desktop_control=False)
try:
    probe=controller.probe(timeout=30)
    assert probe['available'],probe
    report['handshake']=probe['handshake'].to_payload()
    def request(case,action,**payload):
        identity=ModelRequestIdentity('shared-audit',1,case['name'],str(time.monotonic_ns()),1,1,0)
        job=controller.submit(ModelJobRequest(identity,ModelJobKind.PREVIEW,case['scene']['geometry']['binary_path']),
            kind='shared-test',payload=dict(action=action,**payload))
        result=controller.wait(job,timeout=90)
        assert result and result.get('state')=='completed',result
        controller.forget(job)
        return result
    for case in cases:
        loaded=request(case,'load',artifact_ref=case['artifact_ref'])
        assert len(loaded['bindings'])==len(case['scene']['geometry']['meshes'])
        saved_camera=loaded['camera']
        request(case,'highlight',binding_ids=loaded['bindings'][:1])
        request(case,'mode',mode='wireframe')
        request(case,'mode',mode='material')
        cover=request(case,'cover',camera=saved_camera,output=str(OUT/(case['name']+'-qt-cover.png')))
        assert Path(cover['output_path']).is_file()
        assert Path(cover['output_path']).read_bytes().startswith(b'\x89PNG')
        state=request(case,'reset')
        assert state['camera']['renderer']=='babylon'
        sphere=OUT/(case['name']+'-qt-ball.png')
        thumbnail=request(case,'thumbnail',material=next(iter(case['scene']['materials'].values())),output=str(sphere))
        assert sphere.is_file() and thumbnail['thumbnail']['bytes']==sphere.stat().st_size
        report['cases'].append(dict(name=case['name'],bindings=len(loaded['bindings']),
            vertices=loaded['statistics']['points'],resources=loaded['resources'],
            coverBytes=Path(cover['output_path']).stat().st_size,
            sphereBytes=sphere.stat().st_size,
            originalHashesUnchanged=all(hashlib.sha256(Path(p).read_bytes()).hexdigest()==h for p,h in case['hashes'].items())))
        assert report['cases'][-1]['originalHashesUnchanged']
        print(json.dumps(report['cases'][-1]),flush=True)
    report['rawFormats']=[]
    for label,source in [('fbx',next(Path(p) for p in cases[0]['hashes'] if Path(p).suffix.lower()=='.fbx')),
            ('obj',ROOT/'tests/model_library/acceptance/samples/assimp_concave/concave_polygon.obj'),
            ('glb',ROOT/'tests/model_library/acceptance/samples/khronos_box_glb/Box.glb'),
            ('gltf',ROOT/'tests/model_library/acceptance/samples/khronos_box_gltf/Box.gltf')]:
        identity=ModelRequestIdentity('shared-audit',1,'raw-'+label,str(time.monotonic_ns()),1,0,0)
        job=controller.submit(ModelJobRequest(identity,ModelJobKind.THUMBNAIL,str(source)),kind='thumbnail',
            payload=dict(model_path=str(source),width=256,height=256))
        result=controller.wait(job,timeout=90)
        assert result and result['state']=='completed' and result['output_path'],(result,vars(controller._jobs[job]))
        assert Path(result['output_path']).read_bytes().startswith(b'\x89PNG')
        report['rawFormats'].append(dict(format=label,vertices=result['statistics']['points'],
                                         triangles=result['statistics']['triangles'],cover=True))
        controller.forget(job)
        print(json.dumps(report['rawFormats'][-1]),flush=True)
    # Coloured SSS is valid input in both libraries and must not block a model.
    colour_material=json.loads(json.dumps(next(iter(cases[0]['scene']['materials'].values()))))
    colour_material['maps']['sss']=colour_material['maps']['basecolor']
    colour_material['sssColor']=True
    colour_ball=OUT/'colour-sss-qt-ball.png'
    colour=request(cases[0],'thumbnail',material=colour_material,output=str(colour_ball))
    assert colour_ball.is_file() and colour['thumbnail']['bytes']==colour_ball.stat().st_size
    report['colourSSS']=dict(compiled=True,thumbnailBytes=colour_ball.stat().st_size)
    report['status']='passed'
except BaseException as error:
    report.update(status='failed',error=str(error));raise
finally:
    controller.shutdown()
    (OUT/'qt-worker-report.json').write_text(json.dumps(report,indent=2),encoding='utf-8')
