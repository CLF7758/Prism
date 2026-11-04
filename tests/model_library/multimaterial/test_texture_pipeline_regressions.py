import json
from types import SimpleNamespace

from PIL import Image

from prism.model_library.multimaterial.contracts import MaterialDefinition,TextureUse
from prism.model_library.multimaterial.presentation import material_spec


def test_derived_pbr_images_are_not_decoded_again_on_numeric_edits(tmp_path,monkeypatch):
    source=tmp_path/'packed.png';Image.new('RGBA',(16,16),(20,150,80,128)).save(source)
    definition=MaterialDefinition('m','s','m','pbr',textures=(
        TextureUse('roughness','r','G',0,'linear'),TextureUse('opacity','r','A',0,'linear')),
        properties_json=json.dumps({'slots':{'roughness':{'factor':.8}}}))
    paths={'r':str(source)}
    first=material_spec(definition,paths,tmp_path)
    def unexpected(*args,**kwargs):raise AssertionError('Cached derived maps must not decode their sources')
    monkeypatch.setattr(Image,'open',unexpected)
    second=material_spec(definition,paths,tmp_path)
    assert first==second


def test_cover_failures_reach_existing_log_with_model_identity(monkeypatch):
    from prism.model_library.multimaterial import application
    events=[]
    monkeypatch.setattr(application,'record_model_event',lambda name,**fields:events.append((name,fields)))
    service=application.ProjectMaterialService.__new__(application.ProjectMaterialService)
    service._closed=False
    service.owner=SimpleNamespace(repository=SimpleNamespace(context=SimpleNamespace(project_session='session',open_epoch=2)))
    service._native_ready={'model'}
    service.native_event('material.native.failed',native_role='cover',job_id='job',
        identity=dict(model_id='model',request_id='request',appearance_revision=3),
        action='cover',error_type='RuntimeError',reason='Shader compilation failed')
    name,event=events[-1]
    assert name=='material.native.failed'
    assert event['model_id']=='model' and event['request_id']=='request' and event['state_revision']==3
    assert event['project_session']=='session'
    service.native_event('native_worker_lost',native_role='cover')
    assert service._native_ready=={'model'}
