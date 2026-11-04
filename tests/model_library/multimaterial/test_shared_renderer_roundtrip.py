"""Opt-in real storage/service/Qt GPU chain; every worker window stays hidden."""
import hashlib
import json
import os
from pathlib import Path
import time

import pytest


@pytest.mark.skipif(os.environ.get('PRISM_TEST_SHARED_NATIVE')!='1', reason='Explicit hidden native GPU verification')
def test_real_composition_can_open_model_preview(view, qtbot, tmp_path, monkeypatch):
    from dataclasses import replace
    from prism.model_library import native_jobs
    from prism.model_library import composition
    from prism.model_library.multimaterial import application
    from prism.model_library.contracts import ModelQuery
    real_config = native_jobs.RuntimeConfig
    def hidden_config(*args, **kwargs):
        return replace(real_config(*args, **kwargs), worker_worker_args=('--headless',),
            env={'QT_QPA_PLATFORM':'windows',
                 'QTWEBENGINE_CHROMIUM_FLAGS':'--no-sandbox --disable-gpu-shader-disk-cache'})
    monkeypatch.setattr(native_jobs, 'RuntimeConfig', hidden_config)
    emitted=[]
    original=composition.record_model_event
    def observed(name, **fields):
        emitted.append((name,fields))
        original(name,**fields)
    monkeypatch.setattr(composition,'record_model_event',observed)
    monkeypatch.setattr(application,'record_model_event',observed)
    project = tmp_path / 'composition.prism'
    view.do_save(str(project), create_new=True, silent=True)
    qtbot.waitUntil(lambda:view.filename == str(project) and view._save_operation is None, timeout=30000)
    window = view.parent
    window.open_model_library()
    manager = window.model_composition
    qtbot.waitUntil(lambda:manager.service is not None, timeout=20000)
    service = manager.service
    qtbot.waitUntil(lambda:service.capabilities.preview, timeout=60000)
    source = Path('E:/Prism-stress-100k/Game/LZPV/Assets/Envs/Slay/Assets/SetDec/WoodCarving/SM_WoodCarving.FBX')
    handle = service.import_paths((str(source),))
    qtbot.waitUntil(lambda:service.importer.job_state(handle) in ('completed','failed'), timeout=30000)
    assert service.importer.job_state(handle) == 'completed'
    record = service.query(ModelQuery()).items[0]
    job = service.open_preview(record.model_id)
    material = service.multimaterial
    qtbot.waitUntil(lambda:material._jobs[job].done(), timeout=90000)
    assert material._jobs[job].exception() is None
    qtbot.waitUntil(lambda:record.model_id in material._native_ready, timeout=30000)
    assert manager.service is service
    frames=[fields for name,fields in emitted if name=='material.first_frame' and fields.get('native_role')=='preview']
    assert len(frames)==1
    assert frames[0]['model_id']==record.model_id


@pytest.mark.skipif(os.environ.get('PRISM_TEST_SHARED_NATIVE')!='1', reason='Explicit hidden native GPU verification')
def test_texture_edits_save_reopen_and_render_with_shared_backend(tmp_path,qapp,qtbot):
    from tests.model_library.test_storage import make_scene,create_project
    from prism.model_library.project_storage import open_model_context
    from prism.model_library.repository import ModelRepositoryAdapter
    from prism.model_library.importer import ModelImporter
    from prism.model_library.bundles import ModelBundleStore
    from prism.model_library.service import ModelLibraryService
    from prism.model_library.babylon_backend import BabylonBackend
    from prism.model_library.native_jobs import NativeModelJobController,RuntimeConfig
    from prism.model_library.native_protocol import WORKER_NAME_PREVIEW
    from prism.model_library.multimaterial.application import ProjectMaterialService
    from prism.model_library.multimaterial.contracts import MaterialEditRequest,MaterialOperation
    source=Path('E:/Prism-stress-100k/Game/LZPV/Assets/Envs/Slay/Assets/SetDec/WoodCarving')
    assert source.is_dir()
    originals={p:hashlib.sha256(p.read_bytes()).hexdigest() for p in source.iterdir() if p.is_file()}
    project=create_project(tmp_path/'roundtrip.prism',make_scene())
    context=open_model_context(project,open_epoch=1)
    repo=ModelRepositoryAdapter(context,cover_cache_root=tmp_path/'covers')
    importer=ModelImporter(context,repo)
    job=importer.import_paths([source/'SM_WoodCarving.FBX'])
    qtbot.waitUntil(lambda:importer.job_state(job) not in ('queued','running'),timeout=30000)
    assert importer.job_state(job)=='completed'
    from prism.model_library.contracts import ModelQuery
    model_id=repo.query(ModelQuery()).items[0].model_id
    events=[]
    config=RuntimeConfig(cache_root=tmp_path/'native',worker_target='prism.components.material_preview',
        worker_worker_args=('--headless',),worker_name=WORKER_NAME_PREVIEW,startup_timeout_s=25,shutdown_timeout_s=3,
        env={'QT_QPA_PLATFORM':'windows','QTWEBENGINE_CHROMIUM_FLAGS':'--no-sandbox --disable-gpu-shader-disk-cache'})
    preview=NativeModelJobController(config)
    from dataclasses import replace
    covers=NativeModelJobController(replace(config,cache_root=tmp_path/'cover-worker'))
    backend=BabylonBackend(config,covers)
    owner=ModelLibraryService(repo,bundles=ModelBundleStore(context,repo,root=str(tmp_path/'bundles')),
        backend=backend,preview_controller=preview,on_commit=lambda before,after:None)
    service=ProjectMaterialService(owner,preview,covers);owner.multimaterial=service
    service._listeners['audit']=events.append
    try:
        target=service.target_for(repo.get(model_id));item=service._load(target)
        working=item['state'].materials[0].working_id
        for index,(slot,suffix,channel) in enumerate([('basecolor','BC','RGB'),('normal','N','RGB'),
                ('roughness','ORM','G'),('metallic','ORM','B'),('ao','ORM','R')]):
            target=service.target_for(repo.get(model_id))
            request=MaterialEditRequest('edit-'+str(index),'gesture-'+str(index),target,MaterialOperation.PATCH,(working,),
                parameters_json=json.dumps({'command_contract':1,'patch':{'texture':{
                    'slot':slot,'action':'set','path':str(source/('T_WoodCarving_'+suffix+'.PNG')),'channel':channel}}}))
            handle=service.commit(request)
            qtbot.waitUntil(lambda:service._jobs[handle].done(),timeout=30000)
            assert not [e for e in events if e.get('kind')=='commit_failed'],events
            assert service.target_for(repo.get(model_id)).state_revision==index+1
        target=service.target_for(repo.get(model_id))
        request=MaterialEditRequest('roughness-final','final',target,MaterialOperation.PATCH,(working,),
            parameters_json=json.dumps({'command_contract':1,'patch':{'slots':{'roughness':{'factor':.31}}}}))
        handle=service.commit(request);qtbot.waitUntil(lambda:service._jobs[handle].done(),timeout=30000)
        assert not [e for e in events if e.get('kind')=='commit_failed'],events
        state=service._load(service.target_for(repo.get(model_id)))['state']
        assert json.loads(service._resolved(service._models[model_id],working,state).properties_json)['slots']['roughness']['factor']==.31
        # Closing a preview restores its opening baseline, including material
        # edits that were already committed while that preview was open.
        opened=service.open_model(repo.get(model_id))
        qtbot.waitUntil(lambda:service._jobs[opened].done(),timeout=90000)
        assert service._jobs[opened].exception() is None
        target=service.target_for(repo.get(model_id))
        changed=MaterialEditRequest('preview-change','preview-change',target,MaterialOperation.PATCH,(working,),
            parameters_json=json.dumps({'slots':{'roughness':{'factor':.67}}}))
        edited=service.commit(changed)
        qtbot.waitUntil(lambda:service._jobs[edited].done(),timeout=30000)
        assert json.loads(service._resolved(service._models[model_id],working).properties_json)['slots']['roughness']['factor']==.67
        service.native_event('material.preview_closed',native_role='preview',identity={})
        qtbot.waitUntil(lambda:model_id not in service._preview_baselines and model_id not in service._closing_previews,timeout=30000)
        assert json.loads(service._resolved(service._models[model_id],working).properties_json)['slots']['roughness']['factor']==.31
        qtbot.waitUntil(lambda:not service._native_running and service._render_pool.stats()['active']==0
                        and service._render_pool.stats()['pending']==0,timeout=90000)
        service.render_cover(model_id)
        qtbot.waitUntil(lambda:service._covers[model_id].done() and not service._cover_pending,timeout=90000)
        # The derived cache is stored through the existing repository receipt.
        qtbot.waitUntil(lambda:repo.get_payload(model_id)['thumbnail'].get('renderer')=='babylon-shared-1',timeout=15000)
        thumbnail=repo.get_payload(model_id)['thumbnail']
        assert (repo.cover_cache_root/(thumbnail['cache_key']+'.png')).is_file()
        reopened=ModelRepositoryAdapter(open_model_context(project,open_epoch=2),cover_cache_root=tmp_path/'covers')
        block=reopened.get_payload(model_id)['multimaterial']
        assert len(block['state']['materials'])==1
        # Stored assignments are sparse overrides; reloading merges the source slots.
        restored_owner=ModelLibraryService(reopened,bundles=ModelBundleStore(reopened.context,reopened,
            root=str(tmp_path/'reopened-bundles')),on_commit=lambda before,after:None)
        restored=ProjectMaterialService(restored_owner,NativeModelJobController(replace(config,cache_root=tmp_path/'reopened-native')))
        try:
            restored_item=restored._load(restored.target_for(reopened.get(model_id)))
            assert len(restored_item['state'].assignments)==3
            definition=restored._resolved(restored_item,working)
            assert json.loads(definition.properties_json)['slots']['roughness']['factor']==.31
            assert {use.slot for use in definition.textures} >= {'basecolor','normal','roughness','metallic','ao'}
            job=restored.open_model(reopened.get(model_id))
            qtbot.waitUntil(lambda:restored._jobs[job].done(),timeout=90000)
            assert model_id in restored._native_ready
        finally:restored.close();restored_owner.close()
        assert len(block['resources'])==3
        for rid,entry in block['resources'].items():
            assert hashlib.sha256(reopened.resources.read_all(entry['attachment_id'])).hexdigest()==rid.removeprefix('sha256:')
        assert originals=={p:hashlib.sha256(p.read_bytes()).hexdigest() for p in originals}
    finally:
        service.close();owner.close();importer.close_project(context.project_session)
