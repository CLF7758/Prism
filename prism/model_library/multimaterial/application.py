"""Project material coordinator. Heavy work stays off Qt's thread.

One serialized edit queue, immutable requests, database compare-and-swap and a
bundled renderer. The default scaffold remains disabled outside composition.
"""
import base64
import copy
import hashlib
import io
import json
import mimetypes
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict,replace
from pathlib import Path

from . import bundled_runtime
from .catalog import catalog_to_payload,catalog_from_payload,_definition_from_payload,_definition_to_payload
from .contracts import (MaterialTarget,MaterialState,WorkingMaterial,MaterialAssignment,
    MaterialCapabilityReport,MaterialCommitReceipt,MaterialRenderReceipt,MaterialOperation,MaterialEditRequest,
    TextureUse,TexturePatch,PatchAction,MaterialDefinition)
from .inspectors import inspect_source
from .state_store import decode_state
from ..contracts import ModelRequestIdentity,ModelJobRequest,ModelJobKind
from ..events import record_model_event
from prism.library_observability import observed, stage as audit_stage, event as library_event, run_bound
from prism.performance_trace import capture_context


class ProjectMaterialService:
    def __init__(self,owner,controller,cover_controller=None):
        self.owner=owner;self.controller=controller
        from .scheduling import MaterialScheduler
        self._pool=MaterialScheduler()
        self._render_pool=MaterialScheduler()
        # Texture icons are pure picture decoding: they touch no model state
        # and no native worker, so they get their own lane instead of queueing
        # behind a render that may run for seconds.
        self._texture_pool=MaterialScheduler()
        # The small sphere preview of every material row is a decoration, but
        # it is a *native* render of a heavy model: measured 0.3-1.7 s each,
        # and a model with twelve materials queued twelve of them.  On the
        # render lane they pushed the interactive preview of the *next* model
        # back by 3.3 s and the last row icon waited 12.4 s.  They use their
        # own lane and their own headless helper, so they cannot delay a
        # preview the user is waiting for.
        self._thumbnail_pool=MaterialScheduler()
        self._listeners={};self._models={};self._jobs={};self._cancelled=set()
        self._closed=False;self._active=None;self._native_ready=set()
        self._job_requests={};self._native_running={}
        self._native_traces={}
        self._preview_baselines={}
        self._closing_previews=set()
        self._clipboard={};self._lock=threading.RLock()
        self._load_lock=threading.RLock()
        self._library_records={}
        # The icon job currently running for each model, so that a newer
        # material can supersede the one that is still decoding.
        self._texture_jobs={}
        # Two workers fill this cache at the same time; the bounded LRU is the
        # project's own thread-safe map for exactly that.
        from prism.material_cache import MaterialCache
        self._texture_icons=MaterialCache(128,8*1024*1024,
                                          cost=lambda key,value:len(value))
        self.cover_controller=cover_controller
        self._cover_pool=ThreadPoolExecutor(max_workers=1,thread_name_prefix='material-cover')
        self._covers={}
        self._failed_covers={}
        self._cover_pending=set()
        # The unpacked originals are addressed by their content digest, so
        # they do not belong to one session: the session directory is thrown
        # away every time the model library is opened, and unpacking 130 MB
        # of textures again cost 1.9 s every time.
        material_root=getattr(getattr(owner,'repository',None),'material_cache_root',None)
        self._root=Path(material_root) if material_root else Path(owner.bundles.root)/'material-work'
        self._root.mkdir(parents=True,exist_ok=True)

    def subscribe(self,callback):
        key=uuid.uuid4().hex;self._listeners[key]=callback;return key

    def unsubscribe(self,key):self._listeners.pop(key,None)

    def _publish(self,**event):
        def deliver():
            if self._closed:return
            for callback in tuple(self._listeners.values()):callback(event)
        self._post(deliver)

    def _post(self,callback):
        context=capture_context()
        self.owner._post(lambda:run_bound(context,callback))

    def _remember_native_trace(self,request_id):
        with self._lock:
            while len(self._native_traces)>=128:self._native_traces.pop(next(iter(self._native_traces)))
            self._native_traces[request_id]=capture_context()

    def target_for(self,record):
        context=self.owner.repository.context
        block=(record.unknown_fields or {}).get('multimaterial') or {}
        cached=self._models.get(record.model_id)
        revision=(cached['state'].target.state_revision if cached else
                  int((block.get('state') or {}).get('state_revision',0)))
        return MaterialTarget(context.project_session,context.open_epoch,record.model_id,
            record.source_revision,max(1,int(block.get('catalog_revision',1))),revision)

    def warm_native(self):
        """Start the preview helper before the user opens a model.

        The helper loads VTK before it answers the handshake, which costs
        about two and a half seconds.  The first preview of a session used
        to pay all of it while the user waited.  The helper gives its
        memory back on its own when it was never used, so a library that is
        only browsed costs nothing.
        """
        self.controller.warm()

    def capabilities(self,target=None,working_ids=()):
        writable=not self.owner._readonly and not self._closed
        verified=bool(target and target.model_id in self._native_ready)
        return MaterialCapabilityReport(read_catalog=not self._closed,
            edit_material=writable,assign_material=writable and verified,
            pick_material=verified,normal_override=verified,wireframe=verified,
            reason='' if verified else '打开模型预览，确认部位映射后即可编辑')

    def _queue(self,kind,target,fn,request=None,generation=None,lane=None,replace_key=None):
        if self._closed:raise RuntimeError('模型材质会话已关闭')
        job=uuid.uuid4().hex
        queued_at=time.perf_counter()
        trace_context=capture_context()
        library_event('model','job','queued',**asdict(target),job_id=job,
                      request_id=request.request_id if request else job,action=kind)
        if request:self._job_requests[job]=(request.request_id,target,kind)
        for old in tuple(self._jobs):
            if len(self._jobs)<=128:break
            if self._jobs[old].done():
                self._jobs.pop(old,None);self._job_requests.pop(old,None);self._cancelled.discard(old)
        digest=self._request_digest(request) if request else {}
        def run():
            if job in self._cancelled:
                library_event('model','job','cancelled',**asdict(target),job_id=job,action=kind,
                              reason='cancelled_before_start')
                return
            try:
                if kind in ('commit','inspect'):
                    record_model_event('material.'+('commit' if kind=='commit' else 'parse')+'.queued',
                        **asdict(target),request_id=request.request_id if request else job,
                        gesture_id=request.gesture_id if request else None,
                        material_id=','.join(request.working_ids) if request else None,
                        **digest)
                with audit_stage('model','job_'+kind,**asdict(target),job_id=job,
                        request_id=request.request_id if request else job,
                        draft_generation=generation,
                        queue_ms=(time.perf_counter()-queued_at)*1000):
                    fn(job)
            except Exception as error:
                if job in self._cancelled:return
                event=dict(kind='failed' if kind=='inspect' else kind+'_failed',
                    target=target,reason=str(error),error_code=getattr(error,'code','E_MATERIAL_FAILED'))
                if kind=='preview' and request is None:event['kind']='runtime_failed'
                if request:event['request_id']=request.request_id
                if generation is not None:event['draft_generation']=generation
                self._publish(**event)
                self._post(lambda:self.owner._publish(dict(kind='material_'+kind,job_id=job,
                    model_id=target.model_id,state='failed',error_message=event['reason'])))
                record_model_event('material.'+('parse' if kind=='inspect' else 'commit' if kind=='commit' else 'install')+'.failed',
                    **asdict(target),request_id=request.request_id if request else job,
                    gesture_id=request.gesture_id if request else None,draft_generation=generation,
                    error_code=event['error_code'],reason=str(error)[:300],**digest)
        if replace_key is None:
            replace_key = ('draft', target.model_id) if kind == 'preview' and request is not None else ('refresh', target.model_id) if kind == 'refresh' else None
        priority = 0 if kind == 'commit' else 10 if kind == 'inspect' else 30 if kind == 'refresh' else 20
        executor = lane if lane is not None else (self._pool if kind in ('commit','inspect') or request is not None else self._render_pool)
        self._jobs[job]=executor.submit(run_bound,trace_context,run,
            priority=priority,replace_key=replace_key)
        if callable(getattr(executor,'stats',None)):
            library_event('model','scheduler','snapshot',**asdict(target),job_id=job,
                action=kind,request_id=request.request_id if request else job,**executor.stats())
        return job

    @staticmethod
    def _request_digest(request):
        """A small, safe summary of one command for the event log.

        "贴不上去" must be diagnosable from the log alone: which operation,
        which slot, which texture action, and a digest of the source path -
        never the path itself and never the image bytes.
        """
        info=dict(operation=str(getattr(request.operation,'value',request.operation)),
                  working_count=len(request.working_ids or ()),
                  binding_count=len(request.binding_ids or ()))
        try:
            parameters=json.loads(request.parameters_json or '{}')
        except (TypeError,ValueError):
            parameters={}
        patch=parameters.get('patch') or {}
        slots=(patch.get('slots') or {})
        if slots:
            info['slots']=','.join(sorted(str(k) for k in slots))[:200]
        texture=patch.get('texture') or {}
        if texture:
            info['texture_slot']=str(texture.get('slot',''))
            info['texture_action']=str(texture.get('action',''))
            source=str(texture.get('path') or '')
            if source:
                info['texture_source']=hashlib.sha256(source.encode('utf-8')).hexdigest()[:12]
        replacement=parameters.get('replace') or {}
        if replacement:
            info['replace_mode']=str(replacement.get('mode',''))
            info['replace_token']=str(replacement.get('snapshot_token',''))[:16]
        return info

    def inspect(self,target):
        def work(job):
            item=self._load(target)
            if job not in self._cancelled:
                self._publish(kind='catalog',target=item['state'].target,
                    catalog=item['catalog'],state=item['state'])
        return self._queue('inspect',target,work)

    def _load(self,target):
        with self._load_lock:
            return self._load_serial(target)

    @observed('model','source_catalog_read')
    def _load_serial(self,target):
        self._assert_session(target)
        item=self._models.get(target.model_id)
        if item and item['state'].target.source_revision==target.source_revision:return item
        payload=self.owner.repository.get_payload(target.model_id)
        if payload is None:raise KeyError('模型不存在')
        if payload['source_revision']!=target.source_revision:raise ValueError('原件版本已变化')
        bundle=self.owner.bundles.prepare(target.model_id,target.source_revision)
        path=Path(bundle.package_root)/bundle.main_relative_path
        block=payload.get('multimaterial') or {}
        catalog_ref=block.get('catalog_ref') or {}
        if catalog_ref:
            blob=self.owner.repository.resources.read_all(int(catalog_ref['attachment_id']))
            if hashlib.sha256(blob).hexdigest()!=catalog_ref['sha256']:raise ValueError('Stored material catalog digest mismatch')
            if int(block.get('source_revision',target.source_revision))!=target.source_revision:
                raise ValueError('Material catalog belongs to a different source revision')
            catalog=catalog_from_payload(json.loads(blob))
        else:
            catalog=inspect_source(path,revision=target.catalog_revision)
        resources=copy.deepcopy(block.get('resources') or {})
        source_files={str(f.get('sha256')):f for f in payload.get('files',[]) if f.get('sha256')}
        paths={}
        gltf_data=None
        for use in (t for d in catalog.definitions for t in d.textures):
            if not use.resource_id or use.resource_id in paths:continue
            digest=use.resource_id.removeprefix('sha256:')
            if digest in source_files:
                entry=source_files[digest]
                resources[use.resource_id]=dict(attachment_id=entry['attachment_id'],sha256=digest,
                    locator=entry['relative_path'],size=entry['size_bytes'])
                paths[use.resource_id]=str(Path(bundle.package_root)/entry['relative_path'])
            elif path.suffix.lower() in ('.gltf','.glb'):
                from prism.model_library.preview_channels import read_gltf,_image_bytes
                if gltf_data is None:gltf_data=read_gltf(path)
                doc,binary=gltf_data
                texture_index=json.loads(use.unknown_json or '{}').get('texture_index')
                if texture_index is not None:
                    texture=doc['textures'][texture_index]
                    blob=_image_bytes(doc,binary,texture['source'],path.parent)
                    if hashlib.sha256(blob).hexdigest()!=digest:raise ValueError('内嵌贴图摘要不一致')
                    folder=self._root/'images';folder.mkdir(exist_ok=True)
                    suffix='.png' if blob.startswith(b'\x89PNG') else '.jpg' if blob.startswith(b'\xff\xd8') else '.image'
                    output=folder/(digest+suffix)
                    from .presentation import _publish_cache_file
                    _publish_cache_file(output,lambda staged:staged.write_bytes(blob))
                    paths[use.resource_id]=str(output)
            else:
                candidate=path.parent/use.source_locator.replace('\\','/')
                if candidate.is_file():paths[use.resource_id]=str(candidate)
        state=self._initial(catalog,target)
        if block.get('state'):
            stored=decode_state(block['state'],target)
            changes={m.working_id:m for m in stored.materials}
            merged={m.working_id:m for m in state.materials};merged.update(changes)
            assigned={a.binding_id:a.working_id for a in state.assignments}
            assigned.update({a.binding_id:a.working_id for a in stored.assignments})
            state=replace(stored,materials=tuple(merged.values()),
                          assignments=tuple(MaterialAssignment(k,v) for k,v in assigned.items()))
        for rid,entry in resources.items():
            if rid in paths or entry.get('attachment_id') is None:continue
            output=self._root/(rid.replace(':','_')+Path(entry.get('locator','.png')).suffix)
            # The file name carries the content digest and the publish is
            # atomic, so a file that is already there is the one this
            # attachment holds.  Reading it again cost 1.8 s for a 130 MB
            # model, on every single load.
            if not output.exists():
                blob=self.owner.repository.resources.read_all(int(entry['attachment_id']))
                from .presentation import _publish_cache_file
                _publish_cache_file(output,lambda staged:staged.write_bytes(blob))
            paths[rid]=str(output)
        item=dict(catalog=catalog,state=state,bundle=bundle,path=str(path),resources=resources,
            paths=paths,snapshots=copy.deepcopy(block.get('snapshots') or {}),payload=payload)
        self._models[target.model_id]=item
        return item

    @staticmethod
    def _initial(catalog,target):
        materials=tuple(WorkingMaterial('w-'+hashlib.sha256(d.material_id.encode()).hexdigest()[:24],
            d.material_id,d.name) for d in catalog.definitions)
        ids={m.source_material_id:m.working_id for m in materials}
        return MaterialState(target,materials,tuple(MaterialAssignment(b.binding_id,ids[b.original_material_id])
            for b in catalog.binding_sites if b.original_material_id in ids))

    def _assert_session(self,target):
        context=self.owner.repository.context
        if self._closed or target.project_session!=context.project_session or target.open_epoch!=context.open_epoch:
            raise ValueError('工程会话已变化')

    def bases(self,target,catalog,state):
        item=self._models.get(target.model_id)
        if not item:return {}
        definitions={d.material_id:d for d in catalog.definitions}
        return {m.working_id:self._normalise(_definition_from_payload(item['snapshots'][m.base_snapshot_ref])
                if m.base_snapshot_ref else definitions[m.source_material_id], neutral_textures=not m.base_snapshot_ref)
            for m in state.materials if m.base_snapshot_ref in item['snapshots'] or m.source_material_id in definitions}

    @staticmethod
    def _normalise(definition, *, neutral_textures=True):
        p=json.loads(definition.properties_json or '{}');slots=copy.deepcopy(p.get('slots') or {})
        def first(*keys,default=None):
            for key in keys:
                if key in p:
                    value=p[key]
                    return value[0] if isinstance(value,list) and len(value)==1 else value
            return default
        color=first('basecolor_factor','diffuse','kd_color','kd',
            default=[1,1,1,1])
        if isinstance(color,(tuple,list)):
            slots.setdefault('basecolor',dict(color=list(color[:3]),factor=1))
            if definition.workflow != 'prism-library':
                p.setdefault('basecolor_color_space', 'linear')
        slots.setdefault('roughness',dict(value=first('roughness_factor','roughness','pr',default=1 if definition.workflow.startswith('gltf') else .5)))
        slots.setdefault('metallic',dict(value=first('metallic_factor','metallic','pm',default=1 if definition.workflow.startswith('gltf') else 0)))
        for slot in ('roughness','metallic'):
            slots[slot].setdefault('factor',slots[slot]['value'] if definition.workflow.startswith('gltf') else 1)
        # Texture inspection starts at the same neutral multipliers as the
        # material library. User adjustments are layered on later by _resolved.
        if neutral_textures:
            for texture in definition.textures:
                slots.setdefault(texture.slot, {}).update(value=1., factor=1., color=[1.,1.,1.])
        slots.setdefault('opacity',dict(value=first('opacity','d',default=color[3] if isinstance(color,list) and len(color)>3 else 1)))
        p['slots']=slots
        if not definition.workflow.startswith('gltf'):
            p.setdefault('alpha_mode','BLEND' if float(slots['opacity']['value'])<1
                         or any(t.slot=='opacity' for t in definition.textures) else 'OPAQUE')
        if 'emissive_factor' not in p:
            p['emissive_factor']=first('emissive_factor_color','ke_color',default=[0,0,0])
        source=json.loads(definition.unknown_json or '{}').get('source') or {}
        extensions=source.get('extensions') or {}
        if 'KHR_materials_unlit' in extensions:p['unlit']=True
        if 'KHR_materials_emissive_strength' in extensions:
            strength=float(extensions['KHR_materials_emissive_strength'].get('emissiveStrength',1))
            p['emissive_factor']=[float(c)*strength for c in p.get('emissive_factor',[0,0,0])]
        return replace(definition,properties_json=json.dumps(p,ensure_ascii=False))

    def copy_material(self,target,working_id):
        item=self._models.get(target.model_id)
        if not item:raise ValueError('材质尚未载入')
        definition=self._resolved(item,working_id)
        token='sha256:'+hashlib.sha256(json.dumps(_definition_to_payload(definition),sort_keys=True).encode()).hexdigest()
        self._clipboard[token]=dict(definition=_definition_to_payload(definition),
            resources=copy.deepcopy(item['resources']),paths=dict(item['paths']))
        return dict(schema=1,project_id=self.owner.repository.context.project_id,snapshot_token=token)

    def freeze_library_record(self,record):
        """Capture metadata on the GUI thread; read managed image bytes later."""
        frozen=copy.deepcopy(record)
        token='sha256:'+hashlib.sha256(json.dumps(frozen,sort_keys=True,ensure_ascii=False).encode()).hexdigest()
        with self._lock:self._library_records[token]=frozen
        return token

    def _library_definition(self,item,token):
        with self._lock:record=copy.deepcopy(self._library_records.get(token))
        if record is None:raise ValueError('Material library snapshot expired')
        textures=[]
        prepared_sources={}
        for slot,binding in (record.get('textures') or {}).items():
            if slot not in ('basecolor','normal','roughness','metallic','ao','opacity','emissive','sss','displacement'):continue
            aid=binding.get('attachmentId',binding.get('attachment_id'))
            if aid is None:raise ValueError('Material texture has no managed original')
            if aid not in prepared_sources:
                blob=self.owner.repository.resources.read_all(int(aid))
                rid='sha256:'+hashlib.sha256(blob).hexdigest()
                path=self._root/(rid.replace(':','_')+'.texture')
                from .presentation import _publish_cache_file
                _publish_cache_file(path,lambda staged:staged.write_bytes(blob))
                prepared_sources[aid]=(rid,path)
            rid,path=prepared_sources[aid]
            item['paths'][rid]=str(path)
            textures.append(TextureUse(slot,rid,binding.get('channel','RGB'),0,
                'srgb' if slot in ('basecolor','emissive') else 'linear'))
        properties=record.get('properties') or (record.get('preview') or {}).get('properties') or {}
        from prism.material_parameters import normal_convention
        convention=normal_convention(properties)
        textures=[replace(t,normal_convention=convention) if t.slot=='normal' else t for t in textures]
        return MaterialDefinition(token,record.get('id',''),record.get('name',''),
            'prism-library',tuple(textures),json.dumps(properties))

    def _resolved(self,item,working_id,state=None):
        state=state or item['state'];m=next(m for m in state.materials if m.working_id==working_id)
        # Resolve against this command's resource/snapshot scope, never a
        # different cached model scope through bases().
        if m.base_snapshot_ref:
            base=_definition_from_payload(item['snapshots'][m.base_snapshot_ref])
        else:
            base=next(d for d in item['catalog'].definitions if d.material_id==m.source_material_id)
        base=self._normalise(base, neutral_textures=not m.base_snapshot_ref)
        textures={t.slot:t for t in base.textures}
        for patch in m.texture_patches:
            if patch.action==PatchAction.CLEAR:textures.pop(patch.slot,None)
            elif patch.action==PatchAction.SET:textures[patch.slot]=patch.value
        props=json.loads(base.properties_json or '{}');changes=json.loads(m.property_patch_json or '{}')
        for key,value in changes.items():
            if key=='slots':
                slots=props.setdefault('slots',{})
                for slot,options in value.items():
                    slots.setdefault(slot,{}).update(options)
                    if slot == 'basecolor' and 'color' in options:
                        props['basecolor_color_space'] = 'srgb'
            else:props[key]=value
        return replace(base,name=m.display_name,textures=tuple(textures.values()),properties_json=json.dumps(props))

    def _note_edit(self,request,state,name,**extra):
        """One line per edit action: what, how many, on which model.

        Never the patch body: a texture patch holds bytes and a note patch holds
        text. Only counts, slot names and a token prefix are recorded.
        """
        fields = dict(
            project_session=state.target.project_session,
            open_epoch=state.target.open_epoch,
            model_id=state.target.model_id,
            source_revision=state.target.source_revision,
            catalog_revision=state.target.catalog_revision,
            state_revision=state.target.state_revision,
            request_id=request.request_id,
            operation=str(getattr(request.operation,'value',request.operation)),
            material_count=len(request.working_ids),
            binding_count=len(request.binding_ids),
            state='applied')
        # Merge, never "pass extra and hope it does not collide": a duplicate
        # keyword here raises before the logger is even reached, which would
        # fail the edit itself.
        fields.update(extra)
        record_model_event('material.edit.'+name, **fields)

    @observed('model','binding')
    def _edit(self,item,request):
        state=item['state'];parameters=json.loads(request.parameters_json or '{}')
        parameters.pop('command_contract',None)
        mats={m.working_id:m for m in state.materials}
        assignment={a.binding_id:a.working_id for a in state.assignments}
        for wid in request.working_ids:
            if wid not in mats:raise ValueError('目标材质已不存在')
        operation=request.operation
        if operation not in (MaterialOperation.RESTORE_MODEL,MaterialOperation.RESTORE_PREVIEW) and not request.working_ids:
            raise ValueError('An explicit working material target is required')
        if any(key in parameters for key in ('working_ids','binding_ids','targets')):
            raise ValueError('Targets must be declared in the command envelope')
        if operation in (MaterialOperation.REPLACE_SURFACE,MaterialOperation.REPLACE_FULL):
            replacement=parameters.get('replace') or {}
            token=replacement.get('snapshot_token')
            if not token:raise ValueError('Material replacement requires a frozen snapshot')
            definition=self._library_definition(item,token)
            for wid in request.working_ids:
                effective=definition
                if operation==MaterialOperation.REPLACE_SURFACE:
                    original=self._resolved(item,wid,state)
                    from .replacement import replace_surface
                    effective=replace_surface(original, definition)
                snapshot='sha256:'+hashlib.sha256(json.dumps(_definition_to_payload(effective),sort_keys=True).encode()).hexdigest()
                item['snapshots'][snapshot]=_definition_to_payload(effective)
                mats[wid]=replace(mats[wid],base_snapshot_ref=snapshot,texture_patches=(),property_patch_json='{}')
            self._note_edit(request,state,
                'replace_surface' if operation==MaterialOperation.REPLACE_SURFACE else 'replace_full',
                kept_slots=sorted({'opacity','sss','displacement'}) if operation==MaterialOperation.REPLACE_SURFACE else [],
                snapshot=snapshot[:16])
        elif operation==MaterialOperation.PATCH:
            patch=parameters.get('patch',parameters)
            name = patch.get('display_name')
            if name is not None:
                if not isinstance(name, str) or not name.strip() or len(name.strip()) > 160:
                    raise ValueError('材质名称必须为 1 至 160 个字符')
            touched=[];textured=[];channels=[]
            for wid in request.working_ids:
                m=mats[wid]
                if name is not None:
                    m=replace(m,display_name=name.strip())
                properties=json.loads(m.property_patch_json or '{}')
                if patch.get('reset_adjustments'):
                    from prism.material_parameters import reset_texture_adjustments, slot_options
                    effective = self._resolved(item,wid,state)
                    mapped = tuple(texture.slot for texture in effective.textures)
                    effective_properties = json.loads(effective.properties_json or '{}')
                    overrides = properties.get('slots', {})
                    needs_reset = any(set(options) & {'value','factor','color'} for options in overrides.values())
                    needs_reset = needs_reset or any(
                        slot_options(effective_properties,slot,True)['amount'] != 1
                        or slot_options(effective_properties,slot,True)['color'] != [1,1,1]
                        for slot in mapped)
                    if not needs_reset:
                        continue
                    properties = reset_texture_adjustments(properties, mapped)
                for slot,options in patch.get('slots',{}).items():
                    if slot not in touched:touched.append(slot)
                    from prism.material_parameters import validate_slot_patch
                    properties.setdefault('slots',{}).setdefault(slot,{}).update(validate_slot_patch(slot,options))
                    if slot=='opacity' and options.get('value',1)<1:
                        effective=json.loads(self._resolved(item,wid,state).properties_json or '{}')
                        if effective.get('alpha_mode','OPAQUE')=='OPAQUE':properties['alpha_mode']='BLEND'
                patches={p.slot:p for p in m.texture_patches}
                texture=patch.get('texture')
                if texture:
                    slot=texture['slot'];action=texture['action']
                    from prism.material_parameters import validate_texture_channel, texture_color_space
                    texture_color_space(slot)  # reject unknown roles before I/O or registry mutation
                    if action in ('set','channel'):
                        validate_texture_channel(slot,texture.get('channel','RGB') if action=='set' else texture.get('channel'))
                    textured.append(slot+':'+action)
                    if action in ('inherit','clear'):patches[slot]=TexturePatch(slot,PatchAction(action))
                    elif action=='set':
                        if not isinstance(texture.get('path'),str) or not texture['path']:
                            raise ValueError('请先选择贴图文件')
                        path=Path(texture['path']);blob=path.read_bytes()
                        from prism.material_textures import open_decoded_texture
                        with open_decoded_texture(blob) as image:
                            width,height=image.size
                        rid='sha256:'+hashlib.sha256(blob).hexdigest()
                        library_event('model','texture_evidence','verified',**asdict(state.target),
                            request_id=request.request_id,working_id=wid,slot=slot,
                            channel=texture.get('channel','RGB'),sha256=rid,size_bytes=len(blob),
                            width=width,height=height)
                        item['paths'][rid]=str(path)
                        patches[slot]=TexturePatch(slot,PatchAction.SET,TextureUse(slot,rid,texture.get('channel','RGB'),0,
                            texture_color_space(slot),source_locator=path.name))
                        effective=json.loads(self._resolved(item,wid,state).properties_json or '{}')
                        if slot=='opacity' and effective.get('alpha_mode','OPAQUE')=='OPAQUE':
                            properties['alpha_mode']='BLEND'
                        if slot=='emissive' and not any(effective.get('emissive_factor',[0,0,0])):
                            options=properties.setdefault('slots',{}).setdefault('emissive',{})
                            options.setdefault('value',1)
                            options.setdefault('color',[1,1,1])
                    elif action=='channel':
                        use=next((t for t in self._resolved(item,wid,state).textures if t.slot==slot),None)
                        channels.append(slot+':'+str(texture.get('channel','')))
                        if use is None:raise ValueError('Add a texture before selecting its channel')
                        patches[slot]=TexturePatch(slot,PatchAction.SET,replace(use,channels=texture['channel']))
                    else:raise ValueError('未知的贴图操作')
                mats[wid]=replace(m,texture_patches=tuple(patches.values()),property_patch_json=json.dumps(properties))
            self._note_edit(request,state,'patch',
                slots=touched[:8],slots_truncated=len(touched)>8,
                texture_actions=sorted(set(textured))[:8],
                channels=sorted(set(channels))[:8])
        elif operation==MaterialOperation.PASTE:
            payload=parameters['material_clipboard']
            if payload.get('project_id')!=self.owner.repository.context.project_id:
                raise ValueError('该剪贴板快照不属于当前工程，请先将源材质导入当前工程')
            token=payload['snapshot_token'];snapshot=self._clipboard.get(token)
            if not snapshot:raise ValueError('复制的材质快照已失效')
            item['snapshots'][token]=snapshot['definition'];item['paths'].update(snapshot['paths'])
            item['resources'].update(snapshot['resources'])
            for wid in request.working_ids:mats[wid]=replace(mats[wid],base_snapshot_ref=token,texture_patches=(),property_patch_json='{}')
            self._note_edit(request,state,'paste',snapshot=token[:16],
                resource_count=len(snapshot.get('resources') or {}))
        elif operation==MaterialOperation.MAKE_UNIQUE:
            clones={}
            for bid in request.binding_ids:
                if bid not in assignment:raise ValueError('该部位没有可复制的材质')
                source=mats[assignment[bid]]
                if source.working_id not in request.working_ids:raise ValueError('Binding is outside the selected material')
                wid=clones.setdefault(source.working_id,'w-'+uuid.uuid4().hex)
                mats[wid]=replace(source,working_id=wid,display_name=source.display_name+' 副本');assignment[bid]=wid
            self._note_edit(request,state,'make_unique',
                clone_count=len(clones),rebound_bindings=len(request.binding_ids))
        elif operation==MaterialOperation.RESTORE_MATERIAL:
            for wid in request.working_ids:
                mats[wid]=replace(mats[wid],base_snapshot_ref=None,texture_patches=(),property_patch_json='{}')
            self._note_edit(request,state,'restore_material')
        elif operation==MaterialOperation.RESTORE_PREVIEW:
            baseline=self._preview_baselines.get(request.target.model_id)
            if baseline is None:raise ValueError('Preview baseline is unavailable')
            original=baseline['state']
            if replace(original.target,state_revision=state.target.state_revision)!=state.target:
                raise ValueError('Model source changed; refusing to restore an old preview baseline')
            for field in ('snapshots','paths','resources'):
                item[field].update(copy.deepcopy(baseline[field]))
            return replace(original,target=state.target)
        elif operation==MaterialOperation.RESTORE_MODEL:
            self._note_edit(request,state,'restore_model',binding_count=0)
            return self._initial(item['catalog'],state.target)
        else:raise ValueError('该材质操作尚未接入')
        return replace(state,materials=tuple(mats.values()),assignments=tuple(MaterialAssignment(k,v) for k,v in assignment.items()))

    def preview(self,request,draft_generation):
        def work(job):
            item=self._load(request.target)
            self._check_revision(request,item)
            draft=self._edit(item,request)
            if job in self._cancelled:return
            snapshot=copy.deepcopy(item)
            def draw(_job):
                if job in self._cancelled:return
                self._render(snapshot,draft,request.request_id,show=False,draft_generation=draft_generation)
                if job not in self._cancelled:
                    self._publish(kind='preview',preview=MaterialRenderReceipt(request.request_id,request.target,draft_generation,uuid.uuid4().hex))
            self._schedule_draw(request.target,draw,replace_key=('scene',request.target.model_id))
        return self._queue('preview',request.target,work,request,draft_generation)

    def _check_revision(self,request,item):
        current=item['state'].target
        # Only state_revision may lag: a request built while an earlier commit
        # was still queued must replay onto the current state, not fail with
        # "version changed". Session/model/source/catalog must still match.
        if replace(request.target,state_revision=current.state_revision)!=current:
            raise ValueError('材质版本已变化，请刷新后重试')

    def reset_adjustments(self,target,working_ids=()):
        """Selection boundary; serialize behind edits and use their final revision."""
        request=MaterialEditRequest(uuid.uuid4().hex,'selection-reset',target,
            MaterialOperation.PATCH,working_ids=tuple(working_ids),
            parameters_json=json.dumps({'patch':{'reset_adjustments':True}}))
        return self.commit(request)

    def commit(self,request):
        if request.target.model_id in self._closing_previews and request.operation!=MaterialOperation.RESTORE_PREVIEW:
            raise ValueError('Preview is closing; wait for its original parameters to be restored')
        def work(job, request=request):
            item=self._load(request.target)
            self.owner._require_write()
            parameters=json.loads(request.parameters_json or '{}')
            resetting=parameters.get('patch')=={'reset_adjustments':True}
            if resetting:
                request=replace(request,target=item['state'].target,
                    working_ids=request.working_ids or tuple(m.working_id for m in item['state'].materials))
                if self._edit(copy.deepcopy(item),request)==item['state']:
                    return
            signature=hashlib.sha256(json.dumps(asdict(request),sort_keys=True).encode()).hexdigest()
            previous=self.owner.repository.get_payload(request.target.model_id)
            receipt=(previous.get('multimaterial') or {}).get('receipts',{}).get(request.request_id)
            if receipt:
                if receipt['signature']!=signature:raise ValueError('同一请求 ID 的内容不同')
                self._publish(kind='commit',receipt=MaterialCommitReceipt(request.request_id,
                    MaterialTarget(**receipt['before']),MaterialTarget(**receipt['after']),receipt['command_id']))
                return
            self._check_revision(request,item)
            # Command preparation owns private registries. Failure/cancellation
            # must not leak draft snapshots or paths into the loaded model.
            committed_item=item
            item=copy.deepcopy(item)
            from .history import load_history, append_history, move_history
            history=load_history(previous.get('multimaterial') or {},item['state'].target,
                reset_source=request.operation not in (MaterialOperation.UNDO,MaterialOperation.REDO))
            if request.operation in (MaterialOperation.UNDO,MaterialOperation.REDO):
                after_state,history=move_history(history,item['state'],request.operation.value)
            else:
                after_state=self._edit(item,request)
                if request.operation==MaterialOperation.RESTORE_PREVIEW and after_state==item['state']:
                    self._preview_baselines.pop(request.target.model_id,None)
                    self._publish(kind='catalog',target=item['state'].target,catalog=item['catalog'],state=item['state'])
                    return
                history=append_history(history,item['state'],after_state,request.gesture_id)
            if job in self._cancelled:return
            # File decoding is validated by _edit. Rendering capability is
            # independent: missing UVs or an unavailable preview cannot block
            # durable texture management. Refresh happens after commit below.
            after_target=replace(item['state'].target,state_revision=item['state'].target.state_revision+1)
            after_state=replace(after_state,target=after_target)
            block=copy.deepcopy(previous.get('multimaterial') or {})
            block['history']=history
            from .resource_plan import plan_resources
            plan=plan_resources(
                (self._resolved(item,m.working_id,after_state) for m in after_state.materials),
                item['resources'],item['paths'])
            files=list(plan.files)
            catalog_file=self._root/(uuid.uuid4().hex+'.json')
            catalog_file.write_text(json.dumps(catalog_to_payload(item['catalog']),ensure_ascii=False),encoding='utf-8')
            add_catalog=not block.get('catalog_ref')
            if add_catalog:files.append((catalog_file,'model-material-catalog.json'))
            result={}
            def transaction(db,stored):
                from .commit_transaction import write_material_commit
                committed=write_material_commit(db,
                    model_id=request.target.model_id, expected_revision=previous['record_revision'],
                    previous_block=block, state=after_state,
                    initial_state=self._initial(item['catalog'],after_target),
                    snapshots=item['snapshots'], resources=item['resources'], resource_plan=plan,
                    stored=stored, add_catalog=add_catalog, request=request, signature=signature)
                result.update(before=committed.before,after=committed.after,
                    registry=committed.registry,command_id=committed.command_id)
            try:self.owner.repository.resources.put_batch(files,commit=transaction,cancel=lambda:self._closed or job in self._cancelled)
            except Exception:
                if request.target.model_id in self._native_ready and not self._closed:
                    snapshot=copy.deepcopy(item)
                    self._schedule_draw(item['state'].target,lambda _:self._render(snapshot,snapshot['state'],request.request_id,show=False),replace_key=('scene',request.target.model_id))
                raise
            finally:catalog_file.unlink(missing_ok=True)
            item.update(state=after_state,resources=result['registry'],payload=result['after'])
            committed_item.update(item)
            item=committed_item
            def acknowledged():
                with audit_stage('model','scene_ack',**asdict(after_target),request_id=request.request_id):
                    self.owner._on_commit(result['before'],result['after'])
                self._publish(kind='commit',receipt=MaterialCommitReceipt(request.request_id,request.target,after_target,result['command_id']),catalog=item['catalog'],state=after_state)
                if request.operation==MaterialOperation.RESTORE_PREVIEW:
                    self._publish(kind='catalog',target=after_target,catalog=item['catalog'],state=after_state)
                elif resetting:
                    self._publish(kind='catalog',target=after_target,catalog=item['catalog'],state=after_state)
            self._post(acknowledged)
            if self.cover_controller:self.render_cover(request.target.model_id)
            record_model_event('material.commit.completed',**asdict(after_target),request_id=request.request_id,
                gesture_id=request.gesture_id,material_id=','.join(request.working_ids),command_id=result['command_id'])
            if request.operation==MaterialOperation.RESTORE_PREVIEW:
                self._preview_baselines.pop(request.target.model_id,None)
                return
            snapshot=copy.deepcopy(item)
            def draw(_job):
                if self._models.get(after_target.model_id,{}).get('state') != after_state:return
                self._render(snapshot,after_state,request.request_id,show=False)
            self._schedule_draw(after_target,draw,replace_key=('scene',after_target.model_id),request_id=request.request_id)
        return self._queue('commit',request.target,work,request)

    def _schedule_draw(self,target,draw,replace_key,request_id=None):
        context=capture_context()
        def run():
            if self._closed:return
            try:
                draw(None)
                if request_id is not None:
                    self._publish(kind='runtime_completed',target=target,request_id=request_id)
            except Exception as error:
                self._publish(kind='runtime_failed',target=target,request_id=request_id,reason=str(error))
                record_model_event('material.install.failed',**asdict(target),request_id=request_id or '',reason=str(error)[:300])
        return self._render_pool.submit(run_bound,context,run,replace_key=replace_key)

    def open_model(self,record):
        target=self.target_for(record)
        def opened(job):
            item=self._load(target)
            baseline=self._preview_baselines.get(target.model_id)
            if baseline is None or replace(baseline['state'].target,
                    state_revision=item['state'].target.state_revision)!=item['state'].target:
                self._preview_baselines[target.model_id]=copy.deepcopy(item)
            self._render(item,item['state'],job,show=True)
        return self._queue('preview',target,opened)

    def cover_status(self,model_id):
        future=self._covers.get(model_id)
        if model_id in self._cover_pending:return 'pending'
        if future is not None and not future.done():return 'pending'
        failed=self._failed_covers.get(model_id)
        if failed is not None:
            record=self.owner.get(model_id)
            if record is not None and self.target_for(record)==failed:return 'failed'
            self._failed_covers.pop(model_id,None)
        return 'idle'

    def render_cover(self,model_id,camera=None):
        # Explicit requests may retry; automatic fill consults cover_status.
        explicit_camera = camera is not None
        self._failed_covers.pop(model_id,None)
        if not hasattr(self, '_cover_cameras'):self._cover_cameras={}
        if camera is not None:self._cover_cameras[model_id]=camera
        camera=self._cover_cameras.get(model_id) or (
            self.owner.repository.get_payload(model_id).get('thumbnail') or {}).get('camera')
        previous=self._covers.get(model_id)
        if previous and not previous.done():
            # A commit during rendering invalidates that frame. Remember to
            # render the latest revision instead of dropping the request.
            self._cover_pending.add(model_id)
            if explicit_camera:
                if not hasattr(self,'_explicit_cover_pending'):self._explicit_cover_pending=set()
                self._explicit_cover_pending.add(model_id)
            library_event('model','cover','coalesced',model_id=model_id,reason='latest_revision_requested')
            return 'material-cover-'+model_id
        record=self.owner.get(model_id)
        target=self.target_for(record)
        def work():
            try:
                item=self._load(target);state=item['state']
                identity=ModelRequestIdentity(target.project_session,target.open_epoch,model_id,
                    uuid.uuid4().hex,target.source_revision,state.target.state_revision,0)
                self._remember_native_trace(identity.request_id)
                request=ModelJobRequest(identity,ModelJobKind.THUMBNAIL,item['path'])
                capture=dict(action='cover',camera=camera,output=str(self._root/(identity.request_id+'.png')))
                if explicit_camera:
                    # Reuse the visible scene instead of allocating a second
                    # full model and texture set in the cover helper.
                    controller=self.controller
                    capture['expected_scene']={key:asdict(identity)[key] for key in
                        ('project_session','open_epoch','model_id','source_revision','appearance_revision')}
                    payloads=(capture,)
                else:
                    from .presentation import material_artifact
                    artifact=material_artifact(item,state,self._root,bundled_runtime.assimp_path(),
                        {m.working_id:self._resolved(item,m.working_id,state) for m in state.materials})
                    controller=self.cover_controller
                    capture['release_scene']=True
                    payloads=(dict(action='load',artifact_ref=str(artifact)),capture)
                library_event('model','cover','started',**asdict(target),
                    route='visible_scene' if explicit_camera else 'background_scene')
                for payload in payloads:
                    job=controller.submit(request,kind='material-cover',payload=payload)
                    result=controller.wait(job,timeout=150)
                    if not result or result.get('state')!='completed':
                        raise ValueError((result or {}).get('error_message') or 'Model cover rendering failed')
                    controller.forget(job)
                image=Path(result['output_path']).read_bytes()
                self._native_traces.pop(identity.request_id,None)
                if len(image)>4*1024*1024:raise ValueError('Model cover exceeds preview budget')
                def install():
                    if self._closed or not self.owner._can_write():
                        library_event('model','cover','rejected',**asdict(state.target),reason='closed_or_not_writable')
                        return
                    before=self.owner.repository.get_payload(model_id)
                    current=self.target_for(self.owner.get(model_id))
                    if current!=state.target:
                        library_event('model','cover','stale',**asdict(state.target),
                            received_generation=current.state_revision,reason='state_changed_before_install')
                        return
                    digest=hashlib.sha256(image).hexdigest()
                    root=self.owner.repository.cover_cache_root
                    root.mkdir(parents=True,exist_ok=True)
                    destination=root/(digest+'.png')
                    if not destination.exists():destination.write_bytes(image)
                    from .browser_material import RENDERER_VERSION
                    thumbnail=dict(cache_key=digest,state='ready',renderer=RENDERER_VERSION,source_revision=target.source_revision,
                        material_state_revision=state.target.state_revision)
                    if camera:thumbnail['camera']=camera
                    after=self.owner.repository.update_cache(model_id,expected_revision=before['record_revision'],thumbnail=thumbnail)
                    self.owner._on_commit(before,after)
                    library_event('model','cover','installed',**asdict(state.target),sha256=digest,size_bytes=len(image))
                    self.owner._publish(dict(kind='thumbnail',job_id='material-cover-'+model_id,
                        model_id=model_id,state='completed'))
                self._post(install)
            except Exception as error:
                message=str(error)
                self._failed_covers[model_id]=target
                library_event('model','cover','failed',**asdict(target),reason=message,error_type=type(error).__name__)
                self._post(lambda:self.owner._publish(dict(kind='thumbnail',job_id='material-cover-'+model_id,
                    model_id=model_id,state='failed',error_message=message)))
        future=self._cover_pool.submit(run_bound,capture_context(),work)
        self._covers[model_id]=future
        def finished(_future):
            def follow_up():
                if model_id not in self._cover_pending:return
                self._cover_pending.discard(model_id)
                explicit=model_id in getattr(self,'_explicit_cover_pending',set())
                if explicit:self._explicit_cover_pending.discard(model_id)
                if not self._closed:self.render_cover(model_id,self._cover_cameras.get(model_id) if explicit else None)
            self._post(follow_up)
        future.add_done_callback(finished)
        return 'material-cover-'+model_id

    @observed('model','prepare_native_install')
    def _render(self,item,state,request_id,show=False,draft_generation=0):
        from .presentation import material_artifact
        # The helper needs about a second and a half to load VTK. Starting it
        # before the artifact is written makes that cost overlap the writing
        # instead of following it: the first preview of a session used to
        # wait for the start-up after the artifact was already on disk.
        self.controller.warm()
        with audit_stage('model','resolve_preview_materials',request_id=request_id,
                         model_id=state.target.model_id):
            resolved = {m.working_id:self._resolved(item,m.working_id,state) for m in state.materials}
        with audit_stage('model','write_preview_artifact',request_id=request_id,
                         model_id=state.target.model_id):
            artifact=material_artifact(item,state,self._root,bundled_runtime.assimp_path(),resolved)
        identity=ModelRequestIdentity(state.target.project_session,state.target.open_epoch,state.target.model_id,
            request_id,state.target.source_revision,state.target.state_revision,0)
        request=ModelJobRequest(identity,ModelJobKind.PREVIEW,item['path'])
        self._remember_native_trace(request_id)
        job=self.controller.submit(request,kind='material-preview',payload=dict(action='open' if show else 'load',
            artifact_ref=str(artifact),show=show,draft_generation=draft_generation,owner_window_id=self.owner._owner_window_id,
            model_name=item['payload'].get('name','模型')))
        self._native_running[request_id]=job
        try:
            with audit_stage('model','wait_preview_backend',request_id=request_id,
                             model_id=state.target.model_id):
                result=self.controller.wait(job,timeout=150)
        finally:
            self._native_running.pop(request_id,None)
            self._native_traces.pop(request_id,None)
        if not result or result.get('state')!='completed':raise ValueError((result or {}).get('error_message','模型预览没有完成'))
        first=state.target.model_id not in self._native_ready
        self._native_ready.add(state.target.model_id);self._active=state.target
        # A comma-joined id list gets cut by the log's string cap, so a
        # multi-material edit ends up with neither count nor membership. Record
        # the count plus a stable digest of the sorted membership instead.
        working_ids=sorted(m.working_id for m in state.materials)
        # Which *bindings* this edit touched. Count + stable digest keeps the
        # answer bounded; the sample is capped so a 1000-part model does not
        # blow up one event.
        binding_ids=sorted(a.binding_id for a in getattr(state,'assignments',()) or ())
        record_model_event('material.install.completed',**asdict(state.target),request_id=request_id,
            draft_generation=draft_generation,material_id=','.join(working_ids),
            material_count=len(working_ids),
            material_set_digest='sha256:'+hashlib.sha256('\n'.join(working_ids).encode('utf-8')).hexdigest()[:16],
            material_sets_truncated=len(working_ids)>8,
            material_set_sample=working_ids[:8],
            binding_count=len(binding_ids),
            binding_set_digest=(('sha256:'+hashlib.sha256('\n'.join(binding_ids).encode('utf-8')).hexdigest()[:16]) if binding_ids else ''),
            binding_sets_truncated=len(binding_ids)>8,
            binding_set_sample=binding_ids[:8])
        if first:self._publish(kind='catalog',target=state.target,catalog=item['catalog'],state=item['state'])
        self.controller.forget(job)

    def highlight(self,target,binding_ids):
        from prism.material_preview_profile import preview_profile
        deadline = time.monotonic() + preview_profile()['selectionDurationMs'] / 1000
        record_model_event('material.edit.highlight',
            project_session=target.project_session, open_epoch=target.open_epoch,
            model_id=target.model_id, source_revision=target.source_revision,
            state_revision=target.state_revision,
            binding_count=len(binding_ids), binding_ids=list(binding_ids)[:8],
            bindings_truncated=len(binding_ids)>8, state='requested')
        def work(job):
            item=self._load(target)
            if self._active is None or self._active.model_id!=target.model_id:
                self._render(item,item['state'],job,show=False)
            identity=ModelRequestIdentity(target.project_session,target.open_epoch,target.model_id,job,target.source_revision,target.state_revision,0)
            native_job=self.controller.submit(ModelJobRequest(identity,ModelJobKind.PREVIEW,item['path']),kind='material-highlight',
                payload=dict(action='highlight',binding_ids=list(binding_ids),highlight_deadline=deadline))
            result=self.controller.wait(native_job,timeout=30)
            self.controller.forget(native_job)
            if not result or result.get('state')!='completed':raise ValueError('Material highlight failed')
        return self._queue('preview',target,work)

    def thumbnail(self,target,key):
        def work(job):
            from .browser_material import prepare_material
            item=self._load(target)
            if key.startswith('working:'):
                definition=self._resolved(item,key.removeprefix('working:'))
            else:
                definition=self._normalise(next(d for d in item['catalog'].definitions
                    if d.material_id==key.removeprefix('definition:')))
            spec=prepare_material(definition,item['paths'],self._root)
            # A thumbnail uses UV0 on the sphere while retaining the source model's UV set.
            spec=copy.deepcopy(spec)
            for image in spec['descriptors'].values():image['texcoord']=0
            # The ball is 128 pixels wide.  The original picture of a 4K
            # texture costs about four hundred milliseconds to decode and
            # 160 MB of pixels, and none of that can be seen in a 128 pixel
            # ball, so the small copy is painted instead.
            from urllib.parse import urlparse
            from urllib.request import url2pathname
            for slot,uri in list(spec['maps'].items()):
                parsed=urlparse(uri)
                small=self._small_texture(url2pathname(('//'+parsed.netloc if parsed.netloc else '')+parsed.path))
                if small:spec['maps'][slot]=Path(small).resolve().as_uri()
            output=self._root/(hashlib.sha256(json.dumps(spec,sort_keys=True).encode()).hexdigest()+'.sphere.png')
            if not output.exists():
                controller=self.cover_controller or self.controller
                identity=ModelRequestIdentity(target.project_session,target.open_epoch,target.model_id,job,
                    target.source_revision,target.state_revision,0)
                self._remember_native_trace(job)
                native_job=controller.submit(ModelJobRequest(identity,ModelJobKind.PREVIEW,item['path']),
                    kind='material-thumbnail',payload=dict(action='thumbnail',material=spec,output=str(output)))
                result=controller.wait(native_job,timeout=60)
                controller.forget(native_job)
                self._native_traces.pop(job,None)
                if not result or result.get('state')!='completed':raise ValueError('Material thumbnail failed')
            if job not in self._cancelled:
                self._publish(kind='thumbnail',target=target,key=key,image=output.read_bytes())
        self._queue('preview',target,work,lane=self._thumbnail_pool)
        return None

    def texture_thumbnail(self,target,working_id,slot):
        """Paint the 64x64 channel icon of every texture of one material.

        The inspector asks slot by slot, but the pictures behind the slots
        are the same heavy originals: one 8K texture costs about two seconds
        to decode, so a job per slot filled the queue with dozens of decodes
        and the last icon arrived twelve seconds after the click.  One job
        now paints all the slots of the material at once, it runs on its own
        lane so a native render cannot hold it up, and a newer material
        supersedes the one that is still decoding instead of queueing
        behind it.
        """
        with self._lock:
            previous=self._texture_jobs.pop(target.model_id,None)
        if previous is not None:
            self.supersede(previous)
        def work(job):
            try:
                self._paint_texture_icons(job,target,working_id)
            finally:
                with self._lock:
                    if self._texture_jobs.get(target.model_id)==job:
                        self._texture_jobs.pop(target.model_id,None)
        job=self._queue('preview',target,work,lane=self._texture_pool,
                        replace_key=('texture',target.model_id))
        with self._lock:
            self._texture_jobs[target.model_id]=job
        return job

    def _paint_texture_icons(self,job,target,working_id):
        """Decode and publish the channel icon of every texture of a material."""
        item=self._load(target)
        definition=self._resolved(item,working_id)
        for use in definition.textures:
            if job in self._cancelled:return
            path=item['paths'].get(use.resource_id)
            if not path:continue
            key=(use.resource_id,use.channels)
            blob=self._texture_icons.get(key)
            if blob is None:
                blob=self._texture_icon(path,key)
                if blob is None:continue
                self._texture_icons[key]=blob
            self._publish(kind='texture_image',target=target,working_id=working_id,
                slot=use.slot,resource_id=use.resource_id,data=blob)

    def _small_texture(self,path,*,create=True):
        """A small copy of one picture, kept in the cache folder.

        The row preview of the material list is a 128 pixel ball and the
        channel icon is 64 pixels: the original resolution of a 4K texture
        can never be seen in either of them.  Decoding that original costs
        about four hundred milliseconds and 160 MB of pixels, so the small
        copy is written once and read from then on, by every row, every
        model and every session that shows the picture.
        """
        if not path:return None
        source=Path(path)
        digest=hashlib.sha256(str(source).encode()).hexdigest()
        destination=self._root/(digest+'.small.png')
        try:
            if destination.exists():return str(destination)
        except OSError:pass
        if not create:return None
        from PIL import Image
        from prism.material_textures import display_rgba
        try:
            with Image.open(source) as image:
                image=display_rgba(image)
                image.thumbnail((512,512),Image.Resampling.LANCZOS)
                from .presentation import _publish_cache_file
                _publish_cache_file(destination,lambda staged:image.save(staged,format='PNG'))
        except Exception:  # noqa: BLE001 - the original is still usable
            return None
        return str(destination)

    def _texture_icon(self,path,key):
        """The 64x64 channel icon of one texture, kept next to the texture.

        The original is decoded once and the small icon is written beside
        it, so the next session reads a few kilobytes instead of decoding
        an 8K texture again.  The icon lands in the material work folder,
        which is the project's own cache for everything unpacked from a
        model bundle.
        """
        resource_id,channels=key
        icon=self._root/('%s_%s.icon.png' % (resource_id.replace(':','_'),channels))
        try:
            if icon.exists():return icon.read_bytes()
        except OSError:pass
        from PIL import Image
        # A small copy of the picture is enough for a 64 pixel icon, and it
        # is what the row preview paints with as well.
        source=self._small_texture(path,create=False) or path
        with Image.open(source) as image:
            image=image.convert('RGBA')
            if channels in ('R','G','B','A'):image=image.getchannel(channels).convert('RGB')
            image.thumbnail((64,64));stream=io.BytesIO();image.save(stream,format='PNG');blob=stream.getvalue()
        try:
            from .presentation import _publish_cache_file
            _publish_cache_file(icon,lambda staged:staged.write_bytes(blob))
        except Exception:pass
        return blob

    def retry_committed_preview(self,target,request_id):
        """Render persisted state only; retry never submits an edit or save."""
        def work(job):
            try:
                item=self._load(target)
                if item['state'].target != target:
                    raise ValueError('Model changed; retry the current saved revision')
                self._render(copy.deepcopy(item),item['state'],request_id,show=False)
                self._publish(kind='runtime_completed',target=target,request_id=request_id)
            except Exception as error:
                self._publish(kind='runtime_failed',target=target,request_id=request_id,reason=str(error))
                record_model_event('material.install.failed',**asdict(target),request_id=request_id,reason=str(error)[:300])
        return self._queue('refresh',target,work)

    def restore(self,target):
        return self._queue('preview',target,lambda job:self._render(self._load(target),self._load(target)['state'],job))

    def supersede(self,job_id):
        # New intent supersedes an old frame, not the committed scene.
        # Let the one running native call finish; skip pending old draws.
        self._cancelled.add(job_id)
        library_event('model','job','superseded',job_id=job_id)

    def cancel(self,job_id):
        library_event('model','job','cancel_requested',job_id=job_id)
        self._cancelled.add(job_id)
        request=self._job_requests.get(job_id)
        if request:
            request_id,target,kind=request
            native=self._native_running.get(request_id)
            if native:self.controller.cancel(native)
            if kind=='preview' and not self._closed:self.restore(target)
    def release(self,lease_id):pass  # Preparation artifacts belong to the model session, not a frame.

    def native_event(self,event,**fields):
        if self._closed:return
        identity=fields.get('identity') or {}
        payload=dict(identity) if isinstance(identity,dict) else {}
        payload.update(fields)
        payload.setdefault('project_session',self.owner.repository.context.project_session)
        payload.setdefault('open_epoch',self.owner.repository.context.open_epoch)
        if 'appearance_revision' in payload:payload.setdefault('state_revision',payload['appearance_revision'])
        context=getattr(self,'_native_traces',{}).get(payload.get('request_id'))
        if context:run_bound(context,record_model_event,event,**payload)
        else:record_model_event(event,**payload)
        if fields.get('native_role')=='cover':return
        if event=='material.cover_requested':
            model_id=payload.get('model_id')
            if model_id:self._post(lambda:self.render_cover(model_id,fields.get('camera')))
            return
        if event=='material.preview_closed':
            # A normal close releases native handles, not the validated source
            # mapping. The next edit reloads in the worker via _render().
            self._active=None
            for model_id,baseline in list(self._preview_baselines.items()):
                current=self.owner.get(model_id)
                if current is None:continue
                target=self.target_for(current)
                self._native_ready.discard(model_id)
                if self.owner._readonly:
                    self._preview_baselines.pop(model_id,None)
                    continue
                self._closing_previews.add(model_id)
                self._publish(kind='preview_closed',target=target)
                from .contracts import MaterialEditRequest
                request=MaterialEditRequest(uuid.uuid4().hex,'preview-close',target,MaterialOperation.RESTORE_PREVIEW)
                handle=self.commit(request)
                self._jobs[handle].add_done_callback(lambda _future,mid=model_id:self._closing_previews.discard(mid))
            return
        if event=='native_worker_lost':
            self._native_ready.clear()
            active=self._active;self._active=None
            if active and active.model_id in self._models:
                item=self._models[active.model_id]
                self._publish(kind='catalog',target=item['state'].target,catalog=item['catalog'],state=item['state'])
            return
        if event=='material.picked' and self._active:
            target=self._active;identity=fields.get('identity') or {}
            expected=dict(project_session=target.project_session,open_epoch=target.open_epoch,
                model_id=target.model_id,source_revision=target.source_revision,appearance_revision=target.state_revision)
            if any(identity.get(key)!=value for key,value in expected.items()):return
            self._post(lambda:self.owner._material_pick(target,fields.get('binding_id','')))

    def close(self):
        self._closed=True;self._listeners.clear();self._pool.shutdown(wait=False,cancel_futures=True)
        self._cover_pool.shutdown(wait=False,cancel_futures=True)
        self._render_pool.shutdown(wait=False,cancel_futures=True)
        self._texture_pool.shutdown(wait=False,cancel_futures=True)
        self._thumbnail_pool.shutdown(wait=False,cancel_futures=True)
        if self.cover_controller:self.cover_controller.shutdown()
        self.controller.shutdown()
        self._pool.shutdown(wait=True,cancel_futures=True)
        self._cover_pool.shutdown(wait=True,cancel_futures=True)
        self._render_pool.shutdown(wait=True,cancel_futures=True)
        self._texture_pool.shutdown(wait=True,cancel_futures=True)
        self._thumbnail_pool.shutdown(wait=True,cancel_futures=True)
