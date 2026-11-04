"""Project-scoped composition of storage, jobs and native processes.

No Qt/native imports here. The desktop owner supplies a GUI-thread dispatcher
and a receipt callback; an accepted job is never mistaken for a saved model.
"""
import copy
import hashlib
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict
from pathlib import Path
import threading
import time
from typing import Optional
import uuid

import os

from .contracts import (ModelCapabilities, ModelFeatureUnavailable, ModelJobKind,
                        ModelJobRequest, ModelQuery, ModelRequestIdentity)
from .events import (EVENT_APPEARANCE_SNAPSHOT, EVENT_COMMIT_APPLY_FAILED,
                     EVENT_COMMIT_DURABLE,
                     EVENT_THUMBNAIL_CALCULATED, EVENT_THUMBNAIL_REQUEST_SKIPPED,
                     EVENT_THUMBNAIL_REQUESTED,
                     EVENT_PROJECT_MODEL_APPLIED, EVENT_THUMBNAIL_CACHE_WRITTEN,
                     record_model_event)


class ModelLibraryService:
    def __init__(self, repository=None, *, importer=None, exporter=None,
                 bundles=None, backend=None, preview_controller=None,
                 publish_event=None, post=None, on_commit=None,
                 can_write=None, read_texture=None, readonly=False, folder_creator=None,
                 owner_window_id=0):
        self._repository = repository
        from .multimaterial import MultiMaterialService
        self.multimaterial = MultiMaterialService()
        self.importer, self.exporter = importer, exporter
        self.bundles, self.backend = bundles, backend
        self.preview_controller = preview_controller
        self._publish = publish_event or (lambda event: None)
        self._post = post or (lambda call: call())
        self._on_commit = on_commit
        self._can_write = can_write or (lambda: True)
        self._read_texture = read_texture
        self._readonly = readonly
        self._folder_creator = folder_creator
        self._owner_window_id = int(owner_window_id or 0)
        self._closed = False
        self._caps = ModelCapabilities()
        self._pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix='model-service')
        self._imports = set()
        self._preparing = set()
        self._leases = {}
        self._running_native = {}
        self._cancelled = set()
        self._lock = threading.RLock()
        self._close_thread: Optional[threading.Thread] = None
        if importer is not None:
            importer._publish = self._storage_event
        if exporter is not None:
            exporter._publish = self._storage_event
        # One line per bound service: opening or re-opening a project is the
        # anchor every later model event belongs to.
        try:
            context = getattr(repository, 'context', None)
            record_model_event('model_project_bound',
                               project_session=str(getattr(context, 'session_token', '') or ''),
                               project_id=str(getattr(context, 'project_id', '') or ''),
                               open_epoch=int(getattr(context, 'open_epoch', 0) or 0),
                               has_importer=importer is not None,
                               has_exporter=exporter is not None,
                               state='bound')
        except Exception:                              # noqa: BLE001 - logging only
            pass

    @property
    def repository(self):
        return self._repository

    @property
    def pending_writes(self):
        return bool(self._imports)

    @property
    def capabilities(self):
        writable = (not self._readonly and self._on_commit is not None
                    and not self._closed and self._repository is not None and self._can_write())
        shared_preview = callable(getattr(self.multimaterial, 'open_model', None))
        return ModelCapabilities(
            browse=self._repository is not None and not self._closed,
            import_models=writable and self.importer is not None,
            export_originals=self.exporter is not None and not self._closed,
            edit_metadata=writable,
            create_folders=writable and self._folder_creator is not None,
            edit_appearance=writable and self._caps.edit_appearance,
            preview=not self._closed and (shared_preview or (
                self._caps.preview and self.preview_controller is not None)),
            wireframe=self._caps.wireframe,
            backend_version=self._caps.backend_version)

    @property
    def appearance_slots(self):
        from .appearance import UNVERIFIED_SLOTS
        # SSS/displacement are not implemented by the pinned native runtime.
        # Missing tangents remain a per-model native limitation.
        return dict(UNVERIFIED_SLOTS, normal=(
            '后端只验证了含切线模型的法线覆盖；逐模型切线检查尚未接入，暂不开放法线编辑'))

    def query(self, query: ModelQuery):
        if not self.capabilities.browse:
            raise ModelFeatureUnavailable('模型数据服务尚未接入')
        return self._repository.query(query)

    def get(self, model_id):
        if not self.capabilities.browse:
            raise ModelFeatureUnavailable('模型数据服务尚未接入')
        return self._repository.get(model_id)

    def _require_write(self):
        if self._readonly or self._closed or self._on_commit is None or not self._can_write():
            raise ModelFeatureUnavailable('工程只读、正在保存或项目会话已失效')

    def import_paths(self, paths, *, folder_id=None, metadata=None):
        self._require_write()
        if self.importer is None:
            raise ModelFeatureUnavailable('模型导入尚未接入')
        # The importer may complete before returning. GUI dispatch queues its
        # result until this set has been installed.
        options = {'folder_id': folder_id}
        if metadata is not None:
            options['metadata'] = metadata
        job_id = self.importer.import_paths(tuple(paths), **options)
        self._imports.add(job_id)
        return job_id

    def export_originals(self, model_ids, destination):
        if self._closed or self.exporter is None:
            raise ModelFeatureUnavailable('模型原件导出尚未接入')
        return self.exporter.export_originals(tuple(model_ids), destination)

    def prepare_transfer(self, model_ids, directory=None):
        if not self.capabilities.export_originals:
            raise ModelFeatureUnavailable('模型原件拖出尚未接通')
        from .transfer import prepare_model_transfer
        return prepare_model_transfer(self, model_ids, directory)

    def _storage_event(self, event):
        self._post(lambda: self._install_storage_event(event))

    def _install_storage_event(self, event):
        if self._closed:
            return
        data = asdict(event)
        kind = data.get('kind', '')
        job_id = data.get('job_id', '')
        if kind == 'import_completed':
            result = data['result']
            try:
                self._on_commit(None, result['payload'])
            except Exception as error:
                self._publish(dict(kind='import', job_id=job_id, state='failed',
                                   error_message='原件已写入，但界面确认失败，请重新打开工程：' + str(error)))
                self._readonly = True
                return
            self._publish(dict(kind='import', job_id=job_id, state='completed',
                               model_id=result['model_id'], missing=result['missing']))
            if self._caps.preview:
                self.render_cover(result['model_id'])
        elif kind == 'import_failed':
            failure = data.get('failure') or {}
            self._publish(dict(kind='import', job_id=job_id, state='failed',
                               error_code=failure.get('error_code') or 'import_failed',
                               source_name=os.path.basename(str(failure.get('source') or '')),
                               error_message=failure.get('message', '')))
        elif kind == 'phase':
            if data.get('job_kind') == 'import' and data.get('state') in ('completed', 'failed', 'cancelled'):
                self._imports.discard(job_id)
            # Per-model completion above carries missing dependencies; don't
            # overwrite it with a second anonymous batch success.
            if data.get('state') != 'completed':
                self._publish(dict(data, kind=data.get('job_kind', 'import')))
        elif kind == 'export_completed':
            result = data['result']
            failed = [item for item in result['items'] if item['state'] != 'completed']
            missing = [name for item in result['items'] for name in item.get('missing', [])]
            self._publish(dict(kind='export_originals', job_id=job_id,
                               state=result['state'], missing=failed + missing))
        elif kind == 'export_failed':
            self._publish(dict(kind='export_originals', job_id=job_id, state='failed',
                               error_code=data.get('error_code') or 'export_failed',
                               error_message=data.get('message', '')))

    def _commit_payload(self, before, payload, *, derived=False):
        self._require_write()
        if derived:
            after = self._repository.update_cache(payload['model_id'],
                expected_revision=before['record_revision'], thumbnail=payload['thumbnail'],
                statistics=payload.get('statistics'))
        else:
            payload['updated_at'] = time.strftime('%Y-%m-%dT%H:%M:%S')
            self._repository.update(payload, expected_revision=before['record_revision'])
            after = self._repository.get_payload(payload['model_id'])
        # The record is already durable at this point; syncing the scene is a
        # different step that can fail on its own.  Logging them as one event is
        # how "committed" and "visible in the project" got confused.
        record_model_event(EVENT_COMMIT_DURABLE, model_id=payload['model_id'],
                           model_count=1,
                           record_revision=(after or {}).get('record_revision'),
                           derived=bool(derived), state='completed')
        try:
            self._on_commit(before, after)
        except Exception as error:
            self._readonly = True
            record_model_event(EVENT_COMMIT_APPLY_FAILED,
                               model_id=payload['model_id'], model_count=1,
                               error_code=type(error).__name__,
                               durable=True, readonly_after=True, state='failed')
            raise
        return self.get(payload['model_id'])

    def create_folder(self, parent_id=None):
        self._require_write()
        if self._folder_creator is None:
            raise ModelFeatureUnavailable('文件夹服务未接入')
        return self._folder_creator(parent_id)

    def patch_metadata_batch(self, model_ids, patch):
        self._require_write()
        changes = []
        for model_id in dict.fromkeys(model_ids):
            before = self._repository.get_payload(model_id)
            if before is None:
                raise KeyError(model_id)
            changes.append((model_id, int(before['record_revision']), patch))
        pairs = self._repository.patch_batch(changes)
        # Same split as _commit_payload: the rows are durable now, syncing the
        # scene is a separate step that can fail on its own.
        touched = [before.get('model_id') for before, _after in pairs]
        record_model_event(EVENT_COMMIT_DURABLE, path='batch',
                           model_count=len(touched),
                           model_ids=touched[:8],
                           ids_truncated=len(touched) > 8, state='completed')
        try:
            for before, after in pairs:
                self._on_commit(before, after)
        except Exception as error:
            self._readonly = True
            record_model_event(EVENT_COMMIT_APPLY_FAILED, path='batch',
                               model_count=len(touched),
                               model_ids=touched[:8],
                               error_code=type(error).__name__,
                               durable=True, readonly_after=True, state='failed')
            raise
        record_model_event(EVENT_PROJECT_MODEL_APPLIED, path='batch',
                           model_count=len(touched),
                           model_ids=touched[:8], durable=True, state='completed')
        return pairs

    def rename(self, model_id, name):
        if not isinstance(name, str) or not name.strip() or len(name.strip()) > 160:
            raise ValueError('名称须为 1–160 字')
        return self.patch_metadata_batch((model_id,), {'name': name.strip()})

    def update_metadata(self, model_id, *, tags, notes, expected_revision):
        self._require_write()
        before = self._repository.get_payload(model_id)
        if before is None:
            raise KeyError(model_id)
        if before['record_revision'] != expected_revision:
            from .project_storage import ModelRevisionConflict
            raise ModelRevisionConflict(model_id, expected_revision, before['record_revision'])
        payload = copy.deepcopy(before)
        payload['tags'] = list(dict.fromkeys(part.strip() for part in
            (tags.split(',') if isinstance(tags, str) else tags) if part.strip()))
        payload['notes'] = str(notes)
        return self._commit_payload(before, payload)

    def update_appearance(self, model_id, appearance):
        self._require_write()
        if callable(getattr(self.multimaterial, 'target_for', None)):
            raise ModelFeatureUnavailable('Select a model material before editing its slots')
        if not self.capabilities.edit_appearance:
            raise ModelFeatureUnavailable('模型贴图保存尚未接入')
        from .appearance import appearance_payload
        for slot in appearance.bindings:
            if self.appearance_slots.get(slot):
                raise ModelFeatureUnavailable(self.appearance_slots[slot])
        before = self._repository.get_payload(model_id)
        if before is None:
            raise KeyError(model_id)
        previous = before.get('appearance') or {}
        if int(appearance.revision) != int(previous.get('revision', 0)) + 1:
            raise ValueError('模型外观版本已变化，请重新选择模型')
        payload = copy.deepcopy(before)
        payload['appearance'] = appearance_payload(appearance)
        record_model_event(EVENT_APPEARANCE_SNAPSHOT, model_id=model_id,
                           revision=int(getattr(appearance, 'revision', 0) or 0),
                           slot_count=len(getattr(appearance, 'bindings', {}) or {}),
                           payload_bytes=len(json.dumps(payload['appearance'],
                                                        ensure_ascii=False)),
                           state='captured')
        payload.setdefault('thumbnail', {})['state'] = 'stale'
        self._commit_payload(before, payload)
        handle = self.render_cover(model_id)
        active = getattr(self, '_active_preview', None)
        if active is not None and active.model_id == model_id:
            self.open_preview(model_id)
        return handle

    def attach_texture(self, model_id, slot, path):
        self._require_write()
        if callable(getattr(self.multimaterial, 'target_for', None)):
            raise ModelFeatureUnavailable('Select a model material before attaching a texture')
        if not self.capabilities.edit_appearance or self.appearance_slots.get(slot):
            raise ModelFeatureUnavailable(self.appearance_slots.get(slot) or '外观尚未接入')
        record = self.get(model_id)
        if record is None:
            raise KeyError(model_id)
        before_revision = record.record_revision
        handle = 'texture-' + uuid.uuid4().hex
        self._imports.add(handle)
        def run():
            try:
                from prism.material_textures import prepare_texture
                info, blob = prepare_texture(path)
                resource = self._repository.resources.put_file(path)
                if resource.sha256 != info['sha256']:
                    raise ValueError('贴图在导入期间改变')
                binding = dict(info, attachmentId=resource.attachment_id, channel='RGB')
                def install():
                    try:
                        if self._closed:
                            return
                        current = self.get(model_id)
                        if current.record_revision != before_revision:
                            raise ValueError('贴图准备期间模型已编辑，请重试')
                        from dataclasses import replace
                        bindings = dict(current.appearance.bindings)
                        bindings[slot] = binding
                        self.update_appearance(model_id, replace(current.appearance,
                            revision=current.appearance.revision + 1, bindings=bindings))
                        self._publish(dict(kind='texture', job_id=handle, state='completed', model_id=model_id))
                    except Exception as error:
                        self._publish(dict(kind='texture', job_id=handle, state='failed', error_message=str(error)))
                    finally:
                        self._imports.discard(handle)
                self._post(install)
            except Exception as error:
                message = str(error)
                def failed():
                    self._imports.discard(handle)
                    if not self._closed:
                        self._publish(dict(kind='texture', job_id=handle, state='failed', error_message=message))
                self._post(failed)
        self._pool.submit(run)
        return handle

    def probe_native(self):
        if self.backend is None:
            return
        def probe():
            caps = self.backend.capabilities
            def install():
                if not self._closed:
                    self._caps = caps
                    self._publish(dict(kind='capabilities', state='completed'))
            self._post(install)
        self._pool.submit(probe)

    def _identity(self, record):
        context = self._repository.context
        metadata = self._repository.get_payload(record.model_id) or {}
        camera_revision = int((metadata.get('thumbnail') or {}).get('camera_revision', 0))
        return ModelRequestIdentity(context.project_session, context.open_epoch,
            record.model_id, uuid.uuid4().hex, record.source_revision,
            record.appearance.revision, camera_revision)

    def render_cover(self, model_id):
        via = 'multimaterial' if getattr(
            self.multimaterial, 'cover_controller', None) is not None else 'native'
        record_model_event(EVENT_THUMBNAIL_REQUESTED, model_id=model_id,
                           subject='model_cover', via=via, state='requested')
        if via == 'multimaterial':
            return self.multimaterial.render_cover(model_id)
        return self._native_job(model_id, preview=False)

    def ensure_covers(self, summaries):
        # A cover that never appears used to leave no trace at all: the early
        # returns were silent.  Each one now says why it did nothing.
        if not self._caps.preview:
            record_model_event(EVENT_THUMBNAIL_REQUEST_SKIPPED,
                               subject='model_cover', reason='preview_capability_off',
                               model_count=len(summaries), state='skipped')
            return
        if self._readonly:
            record_model_event(EVENT_THUMBNAIL_REQUEST_SKIPPED,
                               subject='model_cover', reason='readonly',
                               model_count=len(summaries), state='skipped')
            return
        if self._closed:
            record_model_event(EVENT_THUMBNAIL_REQUEST_SKIPPED,
                               subject='model_cover', reason='closed',
                               model_count=len(summaries), state='skipped')
            return
        visible = {item.model_id for item in summaries}
        cancelled = 0
        for key, future in list(getattr(self, '_futures', {}).items()):
            if not key[1] and key[0] not in visible:
                future.cancel()
                cancelled += 1
        requested = 0
        ready = 0
        in_flight = 0
        for item in summaries:
            cover_status = getattr(self.multimaterial, 'cover_status', None)
            status = cover_status(item.model_id) if callable(cover_status) else 'idle'
            if status == 'pending':
                in_flight += 1
                continue
            if status == 'failed':
                record_model_event(EVENT_THUMBNAIL_REQUEST_SKIPPED, subject='model_cover',
                                   model_id=item.model_id, reason='same_revision_failed', state='skipped')
                continue
            key = (item.model_id, False)
            future = getattr(self, '_futures', {}).get(key)
            from .multimaterial.browser_material import RENDERER_VERSION
            needs_renderer = (self._caps.backend_version.startswith('Babylon')
                              and getattr(item, 'thumbnail_renderer', '') != RENDERER_VERSION)
            if (not item.thumbnail_data_url or item.thumbnail_state != 'ready' or needs_renderer) and (
                    future is None or future.done()):
                self.render_cover(item.model_id)
                requested += 1
            elif future is not None and not future.done():
                in_flight += 1
            else:
                ready += 1
        record_model_event(EVENT_THUMBNAIL_CALCULATED, subject='model_cover',
                           model_count=len(summaries), requested=requested,
                           already_ready=ready, in_flight=in_flight,
                           cancelled_off_screen=cancelled, state='planned')

    def open_preview(self, model_id):
        if self._closed:
            raise ModelFeatureUnavailable('模型预览会话已关闭')
        if callable(getattr(self.multimaterial, 'open_model', None)):
            record = self.get(model_id)
            if record is None:
                raise ValueError('Model no longer exists')
            return self.multimaterial.open_model(record)
        if not self.capabilities.preview:
            raise ModelFeatureUnavailable('模型交互预览尚未接入或渲染运行时不可用')
        return self._native_job(model_id, preview=True)

    def preview_event(self, name, fields):
        if self._closed:
            return
        # Every event the native preview window sends is a fact about that
        # window; previously only two of them were consumed and the rest were
        # dropped without trace, so "点了没反应" had nothing to look at.
        payload = dict(fields or {})
        record_model_event('material_preview.event', event_name=str(name),
                           binding_id=str(payload.get('binding_id') or ''),
                           has_camera=bool(payload.get('camera')),
                           identity_model=str((payload.get('identity') or {}).get('model_id') or ''),
                           state='received')
        if name == 'material.picked':
            record_model_event('material_preview.picked',
                               binding_id=str(payload.get('binding_id') or ''),
                               state='picked')
        if name == 'camera_changed':
            self._preview_camera = copy.deepcopy(fields.get('camera') or {})
        elif name == 'cover_requested':
            self.capture_preview_cover()

    def capture_preview_cover(self):
        identity = getattr(self, '_active_preview', None)
        if identity is None or self._closed or self._readonly:
            return
        from dataclasses import replace
        current = self._repository.get_payload(identity.model_id) or {}
        camera_base = int((current.get('thumbnail') or {}).get('camera_revision', 0))
        identity = replace(identity, request_id=uuid.uuid4().hex,
            camera_revision=max(getattr(self, '_camera_revision', 0), camera_base) + 1)
        self._camera_revision = identity.camera_revision
        camera = copy.deepcopy(getattr(self, '_preview_camera', {}))
        request = ModelJobRequest(identity, ModelJobKind.THUMBNAIL, '')
        controller = self.preview_controller
        def capture():
            try:
                job_id = controller.submit(request, kind='thumbnail', payload=dict(action='cover'))
                result = controller.wait(job_id, 60)
                output = self.backend.output_path(result or {})
                if output is None:
                    raise RuntimeError('预览封面没有通过校验')
                image = Path(output).read_bytes()
                if len(image) > 4 * 1024 * 1024:
                    raise ValueError('封面超过单图预算')
                self._post(lambda: self._install_cover(identity, identity.request_id,
                                                       image, None, camera))
                controller.forget(job_id, discard_output=True)
            except Exception as error:
                message = str(error)
                self._post(lambda: self._notify_if_current(identity, dict(kind='thumbnail',
                    job_id=identity.request_id, state='failed', error_message=message)))
        self._pool.submit(capture)

    def _native_job(self, model_id, *, preview):
        if self._closed or self.backend is None or self.bundles is None:
            raise ModelFeatureUnavailable('模型原生后端尚未接入')
        record = self.get(model_id)
        if record is None:
            raise KeyError(model_id)
        identity = self._identity(record)
        handle = identity.request_id
        kind = 'preview' if preview else 'thumbnail'
        self._publish(dict(kind=kind, job_id=handle, state='queued'))
        # Collapse pending preparations too; don't build one directory for
        # every slider revision waiting for the two workers.
        key = (model_id, preview)
        with self._lock:
            latest = getattr(self, '_latest', {})
            superseded = latest.get(key)
            latest[key] = handle
            self._latest = latest
        if superseded:
            self._publish(dict(kind=kind, job_id=superseded, state='stale'))
        def run():
            bundle = None
            retain = False
            controller = None
            job_id = None
            with self._lock:
                self._preparing.add(handle)
            try:
                if self._closed or self._latest.get(key) != handle:
                    return
                bundle = self.bundles.prepare(model_id, record.source_revision,
                    appearance_revision=record.appearance.revision)
                if self._closed or self._latest.get(key) != handle:
                    return
                path = str(Path(bundle.package_root) / bundle.main_relative_path)
                request = ModelJobRequest(identity, ModelJobKind.PREVIEW if preview
                                          else ModelJobKind.THUMBNAIL, path)
                snapshot = self.backend.parameter_snapshot(request, record.appearance,
                                                           self._read_texture)
                controller = self.preview_controller if preview else self.backend.controller
                payload = self.backend._payload(request, snapshot=snapshot)
                metadata = self._repository.get_payload(model_id) or {}
                camera = (metadata.get('thumbnail') or {}).get('camera')
                if preview and getattr(self, '_active_preview', None) is not None and self._active_preview.model_id == model_id:
                    camera = copy.deepcopy(getattr(self, '_preview_camera', camera))
                if camera:
                    payload['camera'] = camera
                if preview:
                    payload.update(action='open', prepared_bundle=asdict(bundle),
                                   owner_window_id=self._owner_window_id, model_name=record.name)
                job_id = controller.submit(request, kind=kind, payload=payload)
                self._running_native[handle] = (controller, job_id)
                if handle in self._cancelled:
                    controller.cancel(job_id)
                outcome = controller.wait(job_id, timeout=150)
                state = controller.state(job_id)
                if state in ('queued', 'running'):
                    controller.cancel(job_id)
                    raise TimeoutError('原生模型任务超时')
                if not outcome:
                    raise RuntimeError('原生模型任务结束：' + state)
                if preview:
                    # A window may continue reading dependencies until its
                    # process stops. Its lease cannot end at open's response.
                    self._leases[bundle.lease_id] = bundle
                    retain = True
                    # First-frame acknowledgement proves that the old scene
                    # no longer reads its dependencies. Keep one active lease.
                    for lease_id in list(self._leases):
                        if lease_id != bundle.lease_id:
                            self.bundles.release(lease_id)
                            self._leases.pop(lease_id, None)
                    self._active_preview = identity
                    self._post(lambda: self._notify_if_current(identity, dict(
                        kind=kind, job_id=handle, state='completed')))
                else:
                    output = self.backend.output_path(outcome)
                    image = Path(output).read_bytes() if output else None
                    if not image or len(image) > 4 * 1024 * 1024:
                        raise ValueError('模型封面缺失或超过单图预算')
                    stats = asdict(self.backend.statistics_from_result(outcome))
                    cover = image
                    self._post(lambda: self._install_cover(identity, handle, cover, stats, camera))
                controller.forget(job_id, discard_output=not preview)
            except Exception as error:
                message = str(error)
                if handle not in self._cancelled:
                    self._post(lambda: self._notify_if_current(identity, dict(
                        kind=kind, job_id=handle, state='failed', error_message=message)))
            finally:
                with self._lock:
                    self._preparing.discard(handle)
                if bundle is not None and not retain:
                    if controller is None or job_id is None or controller.wait_for_read_release(job_id):
                        self.bundles.release(bundle.lease_id)
                self._running_native.pop(handle, None)
        with self._lock:
            pending = getattr(self, '_futures', {})
            previous = pending.get(key)
            if previous is not None:
                previous.cancel()
            pending[key] = self._pool.submit(run)
            self._futures = pending
            def forget(future):
                with self._lock:
                    if self._futures.get(key) is future:
                        self._futures.pop(key, None)
                    if self._latest.get(key) == handle:
                        self._latest.pop(key, None)
            pending[key].add_done_callback(forget)
        return handle

    def _notify_if_current(self, identity, event):
        if not self._closed:
            self._publish(event)

    def _install_cover(self, identity, handle, cover, stats, camera=None):
        if self._closed or handle in self._cancelled:
            return
        before = self._repository.get_payload(identity.model_id)
        if (before is None or before.get('source_revision') != identity.source_revision or
                (before.get('appearance') or {}).get('revision', 0) != identity.appearance_revision or
                int((before.get('thumbnail') or {}).get('camera_revision', 0)) > identity.camera_revision):
            self._publish(dict(kind='thumbnail', job_id=handle, state='stale'))
            return
        try:
            payload = copy.deepcopy(before)
            cache_root = self._repository.cover_cache_root
            if cache_root is None:
                raise ModelFeatureUnavailable('封面缓存未接入')
            cache_root.mkdir(parents=True, exist_ok=True)
            digest = hashlib.sha256(cover).hexdigest()
            output = cache_root / (digest + '.png')
            cache_hit = output.exists()
            if not cache_hit:
                temporary = cache_root / (digest + '.' + handle + '.tmp')
                temporary.write_bytes(cover)
                temporary.replace(output)
            from .multimaterial.browser_material import RENDERER_VERSION
            payload['thumbnail'] = dict(cache_key=digest, state='ready', renderer=RENDERER_VERSION,
                source_revision=identity.source_revision, appearance_revision=identity.appearance_revision,
                camera_revision=identity.camera_revision, camera=camera or {})
            if stats is not None:
                payload['statistics'] = stats
            # The bytes are on disk now; whether a card shows them is a later,
            # separate fact (recorded by the widget when it installs the image).
            record_model_event(EVENT_THUMBNAIL_CACHE_WRITTEN,
                               model_id=identity.model_id, job_id=handle,
                               subject='model_cover', cache_key=digest,
                               cache_hit=cache_hit,
                               bytes_written=0 if cache_hit else len(cover),
                               state='completed')
            self._commit_payload(before, payload, derived=True)
            self._publish(dict(kind='thumbnail', job_id=handle, state='completed', model_id=identity.model_id))
        except Exception as error:
            self._publish(dict(kind='thumbnail', job_id=handle, state='failed', error_message=str(error)))

    def cancel(self, job_id):
        self._cancelled.add(job_id)
        with self._lock:
            key = next((key for key, handle in getattr(self, '_latest', {}).items()
                        if handle == job_id), None)
            if key is not None:
                self._latest.pop(key, None)
        if key is not None:
            future = getattr(self, '_futures', {}).get(key)
            if future is not None:
                future.cancel()
            self._publish(dict(kind='preview' if key[1] else 'thumbnail',
                               job_id=job_id, state='cancelled'))
        native = self._running_native.get(job_id)
        if native is not None:
            native[0].cancel(native[1])
        for owner in (self.importer, self.exporter):
            if owner is not None:
                owner.cancel(job_id)

    def close(self):
        if self._closed:
            return
        self._closed = True
        self._pool.shutdown(wait=False, cancel_futures=True)
        context = getattr(self._repository, 'context', None)
        if context is None:
            return
        def stop():
            material_close = getattr(self.multimaterial, 'close', None)
            if callable(material_close):
                material_close()
            for owner in (self.importer, self.exporter):
                if owner is not None:
                    owner.close_project(context.project_session)
            for controller in (getattr(self.backend, 'controller', None), self.preview_controller):
                if controller is not None:
                    controller.close_project(context.project_session)
                    controller.shutdown()
            # Native reads and bundle preparation have stopped before leases
            # can be reclaimed. No join runs inside a GUI callback.
            while self._preparing:
                threading.Event().wait(.05)
            if self.bundles is not None:
                self.bundles.release_all()
        thread = threading.Thread(target=stop, daemon=True, name='model-close')
        self._close_thread = thread
        thread.start()

    def wait_closed(self, timeout: Optional[float] = None) -> bool:
        """Wait for the close thread that gives the lease working copies back.

        The close path runs on a daemon thread, because it must not join
        inside a GUI callback.  A process that ends before that thread
        finishes leaves the copies on the disk - the machine this was
        measured on held 640 MB of them - so the owner of the process end
        waits for it here.  Returns whether the thread is done.
        """
        thread = self._close_thread
        if thread is None:
            return True
        thread.join(timeout)
        return not thread.is_alive()
