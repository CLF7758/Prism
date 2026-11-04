import json

from PIL import Image

from prism.model_library.contracts import ModelJobRequest,ModelRequestIdentity,ModelJobKind
from prism.model_library.multimaterial.catalog import catalog_to_payload
from prism.model_library.multimaterial.inspectors import inspect_source
from prism.model_library.multimaterial.bundled_runtime import assimp_path
from prism.model_library.native_jobs import NativeModelJobController,RuntimeConfig
from prism.model_library.native_protocol import WORKER_NAME_PREVIEW


def test_real_worker_error_and_resource_events_are_observable(tmp_path):
    source=tmp_path/'no-uv.obj'
    source.write_text('v 0 0 0\nv 1 0 0\nv 0 1 0\nf 1 2 3\n')
    catalog=inspect_source(source)
    artifact=tmp_path/'input.render.json'
    payload=dict(model_path=str(source),catalog=catalog_to_payload(catalog),
        materials={'m':dict(color=[1,1,1],double_sided=True)},
        assignments={b.binding_id:'m' for b in catalog.binding_sites},assimp_dll=str(assimp_path()))
    events=[]
    controller=NativeModelJobController(RuntimeConfig(cache_root=tmp_path/'native',
        worker_target='prism.components.material_preview',worker_worker_args=('--headless',),
        worker_name=WORKER_NAME_PREVIEW,env={'QT_QPA_PLATFORM':'offscreen'},shutdown_timeout_s=2),
        lambda name,**fields:events.append((name,fields)))
    try:
        for index in range(2):
            if index:
                image=tmp_path/'texture.png';Image.new('RGB',(8,8),'red').save(image)
                payload['materials']['m']['maps']={'basecolor':{'path':str(image),'texcoord':0,'color_space':'srgb'}}
            artifact.write_text(json.dumps(payload))
            identity=ModelRequestIdentity('evidence-session',1,'real-model','request-'+str(index),1,0,0)
            job=controller.submit(ModelJobRequest(identity,ModelJobKind.PREVIEW,str(source)),kind='evidence',
                                  payload=dict(action='load',artifact_ref=str(artifact),draft_generation=4))
            result=controller.wait(job,timeout=60)
            assert controller.state(job)==('failed' if index else 'completed')
            if index:assert result is None,'Failed jobs must not fabricate a successful result'
            else:assert result['state']=='completed'
        failures=[fields for name,fields in events if name=='material.native.failed']
        assert failures and failures[-1]['job_id']==job
        assert failures[-1]['identity']['request_id']=='request-1'
        assert failures[-1]['draft_generation']==4
        assert failures[-1]['error_site'] and failures[-1]['call_chain']
        resources=[fields for name,fields in events if name=='material.native.resources']
        assert resources and resources[-1]['actors']>0
        assert resources[-1]['gpu_bytes']=='unmeasured'
        assert 'process_rss_bytes' in resources[-1]
        timings=[fields for name,fields in events if name=='material.native.timing']
        assert timings and timings[0]['source_read_parse_ms']>=0
        assert timings[0]['timing']['render_calls']>0
        environment=[fields for name,fields in events if name=='material.native.environment']
        assert environment and ('gpu_renderer' in environment[0] or environment[0].get('state')=='unavailable')
    finally:controller.shutdown()
