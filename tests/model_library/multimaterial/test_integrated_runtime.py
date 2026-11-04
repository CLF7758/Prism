"""Real isolated-worker checks; never opens a desktop window."""
from dataclasses import asdict
import json
import hashlib
from pathlib import Path
import pytest

from prism.model_library.contracts import ModelJobRequest, ModelJobKind, ModelRequestIdentity
from prism.model_library.native_jobs import NativeModelJobController, RuntimeConfig
from prism.model_library.native_protocol import WORKER_NAME_PREVIEW
from prism.model_library.multimaterial.catalog import catalog_to_payload
from prism.model_library.multimaterial.inspectors import inspect_source
from prism.model_library.multimaterial.bundled_runtime import assimp_path


@pytest.mark.parametrize('relative',[
    'khronos_box_glb/Box.glb','khronos_box_gltf/Box.gltf',
    'assimp_multi_mesh_named/cubes_with_names.fbx'])
def test_real_source_binding_loads_in_worker(tmp_path,relative):
    source=Path(__file__).parents[1]/'acceptance'/'samples'/relative
    catalog=inspect_source(source)
    artifact=tmp_path/'source.render.json'
    artifact.write_text(json.dumps(dict(model_path=str(source.resolve()),catalog=catalog_to_payload(catalog),
        materials={d.material_id:dict(color=[.6,.6,.6],double_sided=True) for d in catalog.definitions},
        assignments={b.binding_id:b.original_material_id for b in catalog.binding_sites},
        assimp_dll=str(assimp_path()))),encoding='utf-8')
    controller=NativeModelJobController(RuntimeConfig(cache_root=tmp_path/'native',
        worker_target='prism.components.material_preview',worker_worker_args=(),worker_name=WORKER_NAME_PREVIEW,
        env={'QT_QPA_PLATFORM':'offscreen'},shutdown_timeout_s=2))
    try:
        identity=ModelRequestIdentity('source-test',1,'model','load',1,0,0)
        job=controller.submit(ModelJobRequest(identity,ModelJobKind.PREVIEW,str(source)),
            kind='material-test',payload=dict(action='load',artifact_ref=str(artifact)))
        result=controller.wait(job,timeout=60)
        assert result and result['state']=='completed',result
        assert set(result['bindings'])=={b.binding_id for b in catalog.binding_sites}
    finally:controller.shutdown()


def test_real_obj_worker_load_highlight_and_wireframe(tmp_path):
    source = tmp_path / 'two.obj'
    source.write_text('mtllib two.mtl\no Left\nv -2 0 0\nv -1 0 0\nv -1 1 0\n'
                      'usemtl Red\nf 1 2 3\no Right\nv 1 0 0\nv 2 0 0\nv 2 1 0\n'
                      'usemtl Blue\nf 4 5 6\n', encoding='utf-8')
    source.with_suffix('.mtl').write_text('newmtl Red\nKd 1 0 0\nnewmtl Blue\nKd 0 0 1\n')
    catalog = inspect_source(source)
    assert len(catalog.binding_sites) == 2
    assignments = {b.binding_id: b.original_material_id for b in catalog.binding_sites}
    artifact = tmp_path / 'render.json'
    artifact.write_text(json.dumps(dict(model_path=str(source), catalog=catalog_to_payload(catalog),
        materials={d.material_id: dict(color=[1, 0, 0], double_sided=True) for d in catalog.definitions},
        assignments=assignments, assimp_dll=str(assimp_path()))), encoding='utf-8')
    config = RuntimeConfig(cache_root=tmp_path / 'native', worker_target='prism.components.material_preview',
        worker_name=WORKER_NAME_PREVIEW, worker_worker_args=(), env={'QT_QPA_PLATFORM': 'offscreen'},
        shutdown_timeout_s=2)
    controller = NativeModelJobController(config)
    try:
        import time
        for n, payload in enumerate((dict(action='load', artifact_ref=str(artifact)),
                                     dict(action='highlight', binding_ids=[catalog.binding_sites[0].binding_id]),
                                     dict(action='load', artifact_ref=str(artifact)),
                                     dict(action='mode', mode='wireframe'),
                                     dict(action='thumbnail', material=dict(color=[1,0,0]),
                                          output=str(tmp_path/'sphere.png')))):
            identity = ModelRequestIdentity('test-session', 1, 'model', str(n), 1, 0, 0)
            job = controller.submit(ModelJobRequest(identity, ModelJobKind.PREVIEW, str(source)),
                                    kind='material-test', payload=payload)
            result = controller.wait(job, timeout=60)
            assert result and result['state'] == 'completed', result
            if payload['action'] == 'highlight':
                assert result['highlight_binding_count'] == 1
                time.sleep(1.4)  # The worker's GUI loop expires the transient feedback.
            elif n == 2:
                assert result['highlight_binding_count'] == 0, 'Material reload must not resurrect selection'
            controller.forget(job)
        from PIL import Image
        with Image.open(tmp_path/'sphere.png') as image:
            assert image.size == (128, 128)
            assert image.getextrema()[0][1] > 0
    finally:
        controller.shutdown()


def test_material_commit_survives_reopen_and_retry(tmp_path, qapp, qtbot, monkeypatch):
    import time
    from prism.model_library.project_storage import open_model_context
    from prism.model_library.repository import ModelRepositoryAdapter
    from prism.model_library.importer import ModelImporter
    from prism.model_library.bundles import ModelBundleStore
    from prism.model_library.service import ModelLibraryService
    from prism.model_library.multimaterial.application import ProjectMaterialService
    from prism.model_library.multimaterial.contracts import MaterialEditRequest, MaterialOperation
    from tests.model_library.test_storage import make_scene, create_project
    source=tmp_path/'sample.obj'
    source.write_text('mtllib sample.mtl\nv 0 0 0\nv 1 0 0\nv 0 1 0\nvt 0 0\nvt 1 0\nvt 0 1\nusemtl Red\nf 1/1 2/2 3/3\n')
    source.with_suffix('.mtl').write_text('newmtl Red\nKd 1 0 0\nPr 0.6\nnewmtl Unused\nKd 0 0 1\n')
    project=create_project(tmp_path/'edit.prism',make_scene())
    context=open_model_context(project,open_epoch=1)
    repo=ModelRepositoryAdapter(context)
    importer=ModelImporter(context,repo)
    job=importer.import_paths([source])
    deadline=time.monotonic()+15
    while importer.job_state(job) in ('queued','running') and time.monotonic()<deadline:time.sleep(.02)
    assert importer.job_state(job)=='completed'
    import sqlite3
    with sqlite3.connect(context.metadata_path) as db:
        model_id=db.execute('SELECT id FROM model_records').fetchone()[0]
    owner=ModelLibraryService(repo,bundles=ModelBundleStore(context,repo,root=str(tmp_path/'bundles')),
        on_commit=lambda before,after:None)
    controller=NativeModelJobController(RuntimeConfig(cache_root=tmp_path/'native'))
    service=ProjectMaterialService(owner,controller)
    try:
        target=service.target_for(repo.get(model_id));item=service._load(target)
        working=item['state'].materials[0]
        assert target.model_id not in service._native_ready
        assert service.capabilities(target).edit_material
        assert not service.capabilities(target).pick_material
        request=MaterialEditRequest('durable-request','gesture',target,MaterialOperation.PATCH,
            (working.working_id,),parameters_json=json.dumps({'command_contract':1,
                'patch':{'slots':{'roughness':{'value':.31}}}}))
        # Commit persistence independently of native rendering (covered above).
        from threading import Event
        render_entered, render_release = Event(), Event()
        def blocked_render(*args, **kwargs):
            render_entered.set()
            assert render_release.wait(10)
        service._render=blocked_render
        events=[];service.subscribe(events.append)
        first=service.commit(request);service._jobs[first].result(timeout=15)
        assert any(e['kind']=='commit' for e in events), events
        assert render_entered.wait(5)
        before=repo.get_payload(model_id)
        repeat=service.commit(request);service._jobs[repeat].result(timeout=15)
        assert repo.get_payload(model_id)==before
        render_release.set()
        service._render=lambda *args,**kwargs:None
        service._models.clear()
        reopened=service._load(service.target_for(repo.get(model_id)))
        material=service._resolved(reopened,working.working_id)
        assert json.loads(material.properties_json)['slots']['roughness']['value']==.31
        renamed_job=service.commit(MaterialEditRequest('durable-rename','rename-gesture',
            reopened['state'].target,MaterialOperation.PATCH,(working.working_id,),
            parameters_json=json.dumps({'patch':{'display_name':'Renamed wood'}})))
        service._jobs[renamed_job].result(timeout=15)
        service._models.clear()
        reopened=service._load(service.target_for(repo.get(model_id)))
        assert next(m for m in reopened['state'].materials
                    if m.working_id==working.working_id).display_name=='Renamed wood'
        assert service._resolved(reopened,working.working_id).name=='Renamed wood'
        assert repo.resources.read_all(before['files'][0]['attachment_id'])==source.read_bytes()
        # Copy is a content snapshot; later source edits cannot change it.
        clipboard=service.copy_material(reopened['state'].target,working.working_id)
        unique=service._edit(reopened,MaterialEditRequest('unique','g2',reopened['state'].target,
            MaterialOperation.MAKE_UNIQUE,(working.working_id,),
            (reopened['state'].assignments[0].binding_id,)))
        reopened['state']=unique
        copy_id=unique.assignments[0].working_id
        pasted=service._edit(reopened,MaterialEditRequest('paste','g3',unique.target,
            MaterialOperation.PASTE,(copy_id,),parameters_json=json.dumps({'material_clipboard':clipboard})))
        assert pasted.assignments==unique.assignments
        assert json.loads(service._resolved(reopened,copy_id,pasted).properties_json)['slots']['roughness']['value']==.31
        # Library replacement captures the metadata at selection time.
        library_record=dict(id='library',name='Frozen',textures={},
            properties={'slots':{'roughness':{'value':.72},'basecolor':{'color':[0,1,0]}}})
        token=service.freeze_library_record(library_record)
        library_record['properties']['slots']['roughness']['value']=.99
        replacement=service._edit(reopened,MaterialEditRequest('replace','g4',unique.target,
            MaterialOperation.REPLACE_SURFACE,(copy_id,),parameters_json=json.dumps({
                'replace':{'entry_id':'library','snapshot_token':token,'mode':'replace_surface'}})))
        assert json.loads(service._resolved(reopened,copy_id,replacement).properties_json)['slots']['roughness']['value']==.72
        service._models.clear()
        changed_target=service.target_for(repo.get(model_id))
        changed=service.commit(MaterialEditRequest('change-after-copy','g5',changed_target,
            MaterialOperation.PATCH,(working.working_id,),parameters_json=json.dumps({
                'patch':{'slots':{'roughness':{'value':.55}}}})))
        service._jobs[changed].result(timeout=15)
        paste_target=service.target_for(repo.get(model_id))
        pasted_job=service.commit(MaterialEditRequest('durable-paste','g6',paste_target,
            MaterialOperation.PASTE,(working.working_id,),parameters_json=json.dumps({'material_clipboard':clipboard})))
        service._jobs[pasted_job].result(timeout=15)
        service._models.clear()
        restored=service._load(service.target_for(repo.get(model_id)))
        assert json.loads(service._resolved(restored,working.working_id).properties_json)['slots']['roughness']['value']==.31
        # Retrying a saved preview is read-only even across failure/success.
        before_retry=repo.get_payload(model_id)
        seen=[]
        token=service.subscribe(seen.append)
        def fail_retry(*args,**kwargs):raise RuntimeError('injected retry failure')
        service._render=fail_retry
        retry_target=restored['state'].target
        retry_job=service.retry_committed_preview(retry_target,'retry-failed')
        service._jobs[retry_job].result(timeout=15)
        assert any(e['kind']=='runtime_failed' and e.get('request_id')=='retry-failed' for e in seen)
        assert repo.get_payload(model_id)==before_retry
        service._render=lambda *args,**kwargs:None
        retry_job=service.retry_committed_preview(retry_target,'retry-ok')
        service._jobs[retry_job].result(timeout=15)
        assert any(e['kind']=='runtime_completed' and e.get('request_id')=='retry-ok' for e in seen)
        assert repo.get_payload(model_id)==before_retry
        service.unsubscribe(token)
        # Durable undo/redo survives cache eviction/reopen and advances revision.
        def history_command(operation, name):
            current=service.target_for(repo.get(model_id))
            job=service.commit(MaterialEditRequest(name,name,current,operation))
            service._jobs[job].result(timeout=15)
            service._models.clear()
            return service._load(service.target_for(repo.get(model_id)))
        undone=history_command(MaterialOperation.UNDO,'undo-paste')
        assert json.loads(service._resolved(undone,working.working_id).properties_json)['slots']['roughness']['value']==.55
        redone=history_command(MaterialOperation.REDO,'redo-paste')
        assert json.loads(service._resolved(redone,working.working_id).properties_json)['slots']['roughness']['value']==.31
        undone=history_command(MaterialOperation.UNDO,'undo-again')
        changed=service.commit(MaterialEditRequest('branch-edit','branch-edit',undone['state'].target,
            MaterialOperation.PATCH,(working.working_id,),parameters_json=json.dumps({'patch':{'slots':{'roughness':{'value':.62}}}})))
        service._jobs[changed].result(timeout=15)
        branch=repo.get_payload(model_id)
        assert branch['multimaterial']['history']['redo']==[]
        failed=[];service.subscribe(failed.append)
        history_command(MaterialOperation.REDO,'redo-after-branch')
        assert any(e['kind']=='commit_failed' for e in failed)
        assert repo.get_payload(model_id)==branch
        assert repo.resources.read_all(before['files'][0]['attachment_id'])==source.read_bytes()


        repo.cover_cache_root=tmp_path/'covers'
        service.cover_controller=NativeModelJobController(RuntimeConfig(cache_root=tmp_path/'cover-worker',
            worker_target='prism.components.material_preview',worker_worker_args=(),
            worker_name=WORKER_NAME_PREVIEW,env={'QT_QPA_PLATFORM':'offscreen'},shutdown_timeout_s=2))
        service.render_cover(model_id)
        service._covers[model_id].result(timeout=60)
        assert repo.get_payload(model_id)['thumbnail']['state']=='ready'
        assert repo.thumbnail_data_url(model_id).startswith('data:image/png;base64,')
        # Exercise the actual panel with queued GUI delivery and the real worker.
        from PyQt6 import QtCore
        from prism.widgets.model_material_panel import ModelMaterialPanel
        class Bridge(QtCore.QObject):
            delivered=QtCore.pyqtSignal(object)
            def __init__(self):
                super().__init__();self.delivered.connect(self.run)
            @QtCore.pyqtSlot(object)
            def run(self,callback):callback()
        bridge=Bridge();owner._post=bridge.delivered.emit
        service._render=ProjectMaterialService._render.__get__(service)
        service.controller.shutdown()
        service.controller=NativeModelJobController(RuntimeConfig(cache_root=tmp_path/'edit-worker',
            worker_target='prism.components.material_preview',worker_worker_args=(),
            worker_name=WORKER_NAME_PREVIEW,env={'QT_QPA_PLATFORM':'offscreen'},shutdown_timeout_s=2))
        panel=ModelMaterialPanel();qtbot.addWidget(panel)
        from PyQt6 import QtGui
        from prism import constants
        from prism.utils import create_palette_from_dict
        font_file=Path('C:/Windows/Fonts/msyh.ttc')
        if font_file.exists():
            font_id=QtGui.QFontDatabase.addApplicationFont(str(font_file))
            families=QtGui.QFontDatabase.applicationFontFamilies(font_id)
            if families:panel.setFont(QtGui.QFont(families[0],9))
        assets=Path(__file__).parents[3]/'prism'/'assets'
        panel.setPalette(create_palette_from_dict(constants.COLORS))
        panel.setStyleSheet((assets/'theme.qss').read_text(encoding='utf-8').replace(
            'url(assets/icons/','url('+(assets/'icons').as_posix()+'/'))
        panel.set_port(service);panel.set_base_source(service)
        panel.reset(service.target_for(repo.get(model_id)))
        panel.resize(390,1050);panel.show()
        qtbot.waitUntil(lambda:len(panel.rows())>0,timeout=15000)
        panel.select_material(working.working_id)
        qtbot.waitUntil(lambda:panel.capabilities.edit_material,timeout=30000)
        assert panel.selected_working_id==working.working_id
        assert panel.materials.material_model().row_at(panel.materials.currentIndex()).working_id==working.working_id
        unused=next(m for m in panel._state.materials if m.display_name=='Unused')
        assert panel.select_material(unused.working_id)
        assert panel.editor.title.text()=='Unused'
        assert panel.editor.editable()
        panel.select_material(working.working_id)
        qtbot.waitUntil(lambda:bool(panel.materials.material_model()._thumbs),timeout=30000)
        from PIL import Image
        texture=tmp_path/'green.png';Image.new('RGB',(4,4),(0,255,0)).save(texture)
        from PyQt6 import QtWidgets, QtCore
        def choose_texture(key,path,channel='RGB'):
            control=panel.editor.slots[key]
            if not control.fold.isChecked():control.fold.click()
            # Only the OS picker is substituted. Real button/slot signals,
            # queued service, disk writes and native rendering remain active.
            monkeypatch.setattr(QtWidgets.QFileDialog,'getOpenFileName',
                lambda *args,**kwargs:(str(path),'PNG'))
            revision=panel.target().state_revision
            control.drop.click()
            qtbot.waitUntil(lambda:panel.target().state_revision>revision,timeout=30000)
            qtbot.waitUntil(lambda:path.name in control.filename.text(),timeout=15000)
            if control.channel.currentData()!=channel:
                revision=panel.target().state_revision
                control.channel.setCurrentIndex(control.channel.findData(channel))
                qtbot.waitUntil(lambda:panel.target().state_revision>revision,timeout=30000)
            assert control.channel.currentData()==channel
        choose_texture('basecolor',texture)
        qtbot.waitUntil(lambda:'green.png' in panel.editor.slots['basecolor'].filename.text(),timeout=15000)
        assert panel.editor.slots['basecolor'].isEnabled()
        normal=tmp_path/'normal.png';Image.new('RGB',(4,4),(128,128,255)).save(normal)
        choose_texture('normal',normal)
        qtbot.waitUntil(lambda:'normal.png' in panel.editor.slots['normal'].filename.text(),timeout=15000)
        # The service must enforce the same vector constraint as the widget.
        item=service._load(panel.target())
        before=item['state']
        invalid=MaterialEditRequest('invalid-normal-channel','invalid-normal',panel.target(),
            MaterialOperation.PATCH,(working.working_id,),parameters_json=json.dumps({
                'patch':{'texture':{'slot':'normal','action':'channel','channel':'A'}}}))
        with pytest.raises(ValueError):service._edit(item,invalid)
        assert item['state']==before
        for key,channel in (('roughness','G'),('metallic','B'),('ao','R'),('opacity','A'),('emissive','RGB'),('sss','R'),('displacement','R')):
            choose_texture(key,texture,channel)
        # Exercise every supported slider through a Qt keyboard event, then
        # independently read the effective persisted property after its receipt.
        for key in ('basecolor','normal','roughness','metallic','ao','opacity','emissive','sss','displacement'):
            control=panel.editor.slots[key]
            revision=panel.target().state_revision
            control.slider.setFocus()
            direction=QtCore.Qt.Key.Key_PageDown if control.slider.value()==control.slider.maximum() else QtCore.Qt.Key.Key_PageUp
            qtbot.keyClick(control.slider,direction)
            expected=control.amount.value()
            qtbot.waitUntil(lambda:panel.target().state_revision>revision,timeout=30000)
            loaded=service._load(panel.target())
            properties=json.loads(service._resolved(loaded,working.working_id).properties_json)
            field='factor' if key in ('roughness','metallic') else 'value'
            assert properties['slots'][key][field]==pytest.approx(expected),key
        for key in ('basecolor','normal','roughness','metallic','ao','opacity','emissive','sss','displacement'):
            control=panel.editor.slots[key]
            for enabled in (False,True):
                revision=panel.target().state_revision
                assert control.enabled.isChecked()!=enabled
                control.enabled.click()
                qtbot.waitUntil(lambda:panel.target().state_revision>revision,timeout=30000)
                loaded=service._load(panel.target())
                effective=service._resolved(loaded,working.working_id)
                properties=json.loads(effective.properties_json)
                assert properties['slots'][key]['enabled'] is enabled,key
                from prism.model_library.multimaterial.presentation import material_spec
                rendered=material_spec(effective,loaded['paths'],service._root)
                if key=='basecolor' and not enabled and 'basecolor' in rendered['maps']:
                    # Alpha still needs a color carrier. Disabling the color
                    # map must remove its RGB, not remove the alpha binding.
                    with Image.open(rendered['maps']['basecolor']['path']) as carrier:
                        assert carrier.convert('RGB').getextrema()==((255,255),)*3
                else:
                    assert (key in rendered['maps']) is enabled,key
            for inverted in (True,False):
                revision=panel.target().state_revision
                assert control.invert.isChecked()!=inverted
                control.invert.click()
                qtbot.waitUntil(lambda:panel.target().state_revision>revision,timeout=30000)
                loaded=service._load(panel.target())
                properties=json.loads(service._resolved(loaded,working.working_id).properties_json)
                assert properties['slots'][key]['invert'] is inverted,key
        latest=panel.target().state_revision
        qtbot.waitUntil(lambda:repo.get_payload(model_id).get('thumbnail',{}).get('material_state_revision')==latest,
                        timeout=30000)
        service._models.clear()
        reopened=service._load(service.target_for(repo.get(model_id)))
        effective=service._resolved(reopened,working.working_id)
        assert {t.slot for t in effective.textures}=={'basecolor','normal','roughness','metallic','ao','opacity','emissive','sss','displacement'}
        from prism.model_library.multimaterial.presentation import material_spec
        spec=material_spec(effective,reopened['paths'],service._root)
        assert spec['alpha_mode']=='BLEND'
        assert any(spec['emissive_factor'])
        assert all(hashlib.sha256(repo.resources.read_all(v['attachment_id'])).hexdigest()==v['sha256']
                   for v in repo.get_payload(model_id)['multimaterial']['resources'].values())
        # History must retain managed texture originals even when the import
        # location disappears. These are generated temporary test files only.
        original_colour=texture.read_bytes()
        clear_target=reopened['state'].target
        clear_request=MaterialEditRequest('clear-colour-history','clear-colour-history',clear_target,
            MaterialOperation.PATCH,(working.working_id,),parameters_json=json.dumps({
                'patch':{'texture':{'slot':'basecolor','action':'clear'}}}))
        clear_job=panel._commit(clear_request);service._jobs[clear_job].result(timeout=30)
        qtbot.waitUntil(lambda:panel.target().state_revision>clear_target.state_revision,timeout=30000)
        texture.unlink()
        service._models.clear()
        undo_target=service.target_for(repo.get(model_id))
        undo_job=panel._commit(MaterialEditRequest('undo-colour-history','undo-colour-history',undo_target,MaterialOperation.UNDO))
        service._jobs[undo_job].result(timeout=30)
        service._models.clear()
        restored_colour=service._load(service.target_for(repo.get(model_id)))
        colour=next(t for t in service._resolved(restored_colour,working.working_id).textures if t.slot=='basecolor')
        assert Path(restored_colour['paths'][colour.resource_id]).read_bytes()==original_colour
        assert all(hashlib.sha256(repo.resources.read_all(v['attachment_id'])).hexdigest()==v['sha256']
                   for v in repo.get_payload(model_id)['multimaterial']['resources'].values())
        qtbot.waitUntil(lambda:panel.target().state_revision==restored_colour['state'].target.state_revision,timeout=30000)
        assert len([row for row in panel.rows() if row.working_id])==2
        assert len(panel.rows())==2
        qtbot.waitUntil(lambda:bool(panel.materials.material_model().thumbnail('working:'+working.working_id)),timeout=30000)
        evidence=Path(__file__).parents[3]/'artifacts'/'model-material-integration'
        evidence.mkdir(parents=True,exist_ok=True)
        panel.grab().save(str(evidence/'panel-offscreen.png'))
        panel.detach_port()
    finally:
        service.close()
        owner.bundles.release_all()
        owner.close()
