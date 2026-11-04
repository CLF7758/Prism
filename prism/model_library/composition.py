"""Desktop wiring. The bridge is the only thread allowed to touch the scene."""
import copy
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from PyQt6 import QtCore, QtWidgets

from .events import EVENT_PROJECT_MODEL_APPLIED, record_model_event
from .service import ModelLibraryService


class ModelLibraryComposition(QtCore.QObject):
    delivered = QtCore.pyqtSignal(object)

    def __init__(self, window):
        super().__init__(window)
        self.window = window
        self.service = None
        self._scope = None
        self._generation = 0
        #: Services that were closed but whose lease working copies may not
        #: be back on the disk yet; see `_track_closed` and `wait_closed`.
        self._closed_services = []
        #: Cached project identity; see `_key`.
        self._key_source = None
        self._key_value = None
        self._pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix='model-bind')
        self.delivered.connect(self._deliver)
        window.view.project_open_finished.connect(self.invalidate)
        details = window.model_library.details
        details.texture_requested.connect(self.attach_texture)
        details.material_apply_requested.connect(self.apply_material)
        tree = window.view.category_panel.resource_tree
        tree.open_model_folder_requested.connect(window.open_model_library)
        tree.library_drop_handler = self.move_library_assets

    def move_library_assets(self, kind, ids, folder_id):
        view = self.window.view
        from prism.fileio import is_readonly_project
        if is_readonly_project(view.scene, view.filename):
            return False
        if not self._can_write(self._key()) or getattr(view, '_upgrade_preview_readonly', False):
            return False
        folders = view.category_panel.resource_service
        try:
            folders._validate_parent(kind, folder_id)
            if kind == 'material':
                library = self.window.material_library
                if library.store_to_scene() is False:
                    return False
                library.records.move_records(ids, folder_id)
                view.mark_content_dirty()
                library.refresh()
            else:
                library = self.window.model_library
                if self.service is None or not library.store_to_scene():
                    return False
                self.service.patch_metadata_batch(ids, {'folder_id': folder_id})
                library.refresh()
            record_model_event('assets_moved', asset_kind=kind, count=len(ids), folder_id=folder_id)
            return True
        except Exception as error:
            self.window.statusBar().showMessage('移动失败：' + str(error), 6000)
            record_model_event('assets_move_failed', asset_kind=kind, error=str(error)[:200])
            return False

    def _key(self):
        """Identity of the project the model library is currently bound to.

        ``Path.resolve()`` touches the filesystem.  This key is asked for by
        every capability check and every model event, and the model library
        asks once per rendered row — the same path was resolved thousands of
        times and the repeated resolution became a 703 ms stall on the GUI
        thread.  The resolved path only changes when the project does, so it
        is cached against the raw source that produced it.
        """
        view = self.window.view
        path = getattr(view, 'filename', None)
        source = (path or '', getattr(view.scene, 'project_id', ''),
                  getattr(view, '_trace_open_epoch', 0))
        if source != self._key_source:
            self._key_source = source
            self._key_value = (str(Path(source[0]).resolve()) if source[0] else '',
                               source[1], source[2])
        return self._key_value

    def post(self, callback):
        try:
            self.delivered.emit(callback)
        except RuntimeError:
            pass

    @QtCore.pyqtSlot(object)
    def _deliver(self, callback):
        try:
            callback()
        except Exception as error:
            record_model_event('composition_failed', error=str(error)[:300])
            self.window.model_library._report_failure('模型库', error)

    def invalidate(self, *_):
        if self._scope != self._key():
            self._generation += 1
            old, self.service = self.service, None
            self._scope = None
            if old is not None:
                old.close()
                self._track_closed(old)
            if getattr(self.window.model_library, 'initialized', True):
                self.window.model_library.set_service(ModelLibraryService())

    def ensure(self):
        key = self._key()
        if key == self._scope:
            return
        self.invalidate()
        if not key[0]:
            # The panel is built on first use. Touching `status` on the host
            # would build it right here, inside the click that opened the
            # library - that is where the 1.7 second freeze came from. The
            # message is therefore only written when the panel already exists.
            if getattr(self.window.model_library, 'initialized', False):
                self.window.model_library.status.setText('请先保存工程，再导入模型')
            return
        from prism import fileio
        readonly = fileio.is_readonly_project(self.window.view.scene, key[0])
        needs_attachment_index = isinstance(self.window.view.scene.attachments, dict)
        self._scope = key
        generation = self._generation
        def prepare():
            from .project_storage import open_model_context, connect, ensure_model_tables
            from .repository import ModelRepositoryAdapter
            from .importer import ModelImporter
            from .exporter import ModelOriginalExporter
            from .bundles import ModelBundleStore, reclaim_stale_sessions
            from .native_jobs import RuntimeConfig, NativeModelJobController
            from .babylon_backend import BabylonBackend
            from .native_protocol import WORKER_NAME_PREVIEW
            context = open_model_context(key[0], project_id=key[1], open_epoch=key[2], writable=not readonly)
            if not readonly:
                with connect(context.metadata_path) as db:
                    ensure_model_tables(db)
            cache_base = Path(QtCore.QStandardPaths.writableLocation(
                QtCore.QStandardPaths.StandardLocation.CacheLocation)) / 'model-library'
            # Reclaim the disk of sessions that are no longer alive.  Every
            # open used to leave its own directory behind - 188 directories
            # and 3.81 GB on the machine this was measured on - because the
            # release of a closing session runs on a daemon thread that the
            # process does not wait for.  The reclaim runs in the background
            # too: opening the library must never wait for the disk.
            threading.Thread(target=reclaim_stale_sessions,
                             args=(cache_base, context.project_session),
                             daemon=True, name='model-cache-reclaim').start()
            repo = ModelRepositoryAdapter(context, cover_cache_root=cache_base / 'covers',
                material_cache_root=cache_base / 'material-work')
            cache = cache_base / context.project_session
            config = RuntimeConfig(cache_root=cache, cancel_grace_s=1.0, shutdown_timeout_s=1.0,
                worker_target='prism.components.material_preview', worker_worker_args=('--headless',),
                worker_name=WORKER_NAME_PREVIEW)
            def native_event(event, _role='cover', **fields):
                fields.setdefault('project_session', context.project_session)
                service = self.service
                material = getattr(service, 'multimaterial', None)
                if (callable(getattr(material, 'native_event', None)) and service.repository.context.project_session == context.project_session):
                    material.native_event(event, native_role=_role, **fields)
                else:
                    record_model_event(event, native_role=_role, **fields)
            render = NativeModelJobController(config, native_event)
            preview_config = RuntimeConfig(cache_root=cache / 'preview', cancel_grace_s=1.0,
                worker_target='prism.components.material_preview', worker_worker_args=(),
                worker_name=WORKER_NAME_PREVIEW, idle_exit_s=0.0, shutdown_timeout_s=1.0, unused_exit_s=180.0)
            def preview_event(event, **fields):
                native_event(event, _role='preview', **fields)
                self.post(lambda: self._preview_event(key, event, fields))
            preview = NativeModelJobController(preview_config, preview_event)
            backend = BabylonBackend(config, render)
            attachment_index = []
            if needs_attachment_index:
                with connect(context.metadata_path, readonly=True) as db:
                    attachment_index = db.execute('SELECT id,name,mime FROM attachments').fetchall()
            return dict(repository=repo, importer=ModelImporter(context, repo),
                exporter=ModelOriginalExporter(context, repo),
                bundles=ModelBundleStore(context, repo, root=str(cache / 'bundles')),
                backend=backend, preview_controller=preview, readonly=readonly,
                attachment_index=attachment_index)
        future = self._pool.submit(prepare)
        def prepared(result):
            try:
                parts = result.result()
            except Exception as error:
                message = str(error)
                self.post(lambda: self._bind_failed(key, generation, message))
                return
            self.post(lambda: self._bind(key, generation, parts))
        future.add_done_callback(prepared)

    def _bind_failed(self, key, generation, message):
        if generation == self._generation and key == self._key():
            self._scope = None
            self.window.model_library._report_failure('模型库接入', message)

    def _bind(self, key, generation, parts):
        if generation != self._generation or key != self._key():
            # No native helper has started; there is nothing to stop here.
            return
        attachments = self.window.view.scene.attachments
        attachment_index = parts.pop('attachment_index', [])
        if isinstance(attachments, dict):
            from prism.fileio.attachments_store import StoredAttachments, sqlite_attachment_loader
            attachments = StoredAttachments(sqlite_attachment_loader(key[0]), entries=attachments)
            for attachment_id, name, mime in attachment_index:
                attachments._meta.setdefault(attachment_id, (name, mime))
            self.window.view.scene.attachments = attachments
        service = ModelLibraryService(**parts, post=self.post,
            owner_window_id=int(self.window.winId()),
            folder_creator=lambda parent_id: self.window._create_resource_item('model', 'folder', parent_id),
            on_commit=lambda before, after: self._commit(key, before, after),
            can_write=lambda: self._can_write(key),
            read_texture=lambda binding: self._read_texture(attachments, binding),
            publish_event=lambda data: self._event(key, data))
        self.service = service
        from .multimaterial.application import ProjectMaterialService
        from .multimaterial.bundled_runtime import available
        if available():
            # Reuse the two existing lanes, with one shared Babylon implementation.
            service.multimaterial = ProjectMaterialService(service, service.preview_controller, service.backend.controller)
            def warm_when_usable():
                # The count reads the project database, which may still be
                # locked by the open that just finished; it must not run on
                # the GUI thread, and a helper that is not needed is worse
                # than one that starts late.
                try:
                    has_models = service.repository.count() > 0
                except Exception as error:  # noqa: BLE001 - reported, not raised
                    record_model_event('model.helper_warm_skipped',
                                       reason=str(error)[:200])
                    return
                if has_models:
                    service.multimaterial.warm_native()
            try:
                service._pool.submit(warm_when_usable)
            except RuntimeError:
                pass  # The service was closed again before the helper could start.
            service._material_pick = lambda target, bid: self.window.model_library.details.material_panel.publish_pick_result(target, bid)
        self.window.model_library.set_service(service)
        if callable(getattr(service.multimaterial, 'freeze_library_record', None)):
            from prism.widgets.model_material_picker import SceneMaterialLibrarySource
            self.window.model_library.details.material_panel.set_library_source(
                SceneMaterialLibrarySource(lambda:self.window.view.scene,
                    service.multimaterial.freeze_library_record,
                    service.repository.resources.read_all))
        service.probe_native()

    def _can_write(self, key):
        view = self.window.view
        worker = getattr(view, 'worker', None)
        opening = getattr(view, '_opening_operation', None)
        session = getattr(view.scene, '_partial_project_session', None)
        model_domain_loaded = not (
            (getattr(view.scene, '_partial_project_source', None) and session is None)
            or (session is not None and session._domains is None))
        return (key == self._key() and not getattr(view, '_save_preparing', False)
                and model_domain_loaded
                and getattr(view, '_save_operation', None) is None
                and not (worker is not None and worker.isRunning())
                and not (opening is not None and not opening.terminal)
                and getattr(view, '_storage_operation', None) is None)

    #: Which picture a job is about. `kind` alone cannot tell a model cover from
    #: an interactive preview or a texture-slot thumbnail.
    _SUBJECT_BY_KIND = {
        'thumbnail': 'model_cover',
        'preview': 'interactive_preview',
        'texture': 'texture_slot',
    }

    #: The lifecycle names the contract declares. `_event` used to flatten all
    #: of them into one `service_job` event, so the stage lived only in a field
    #: and REQUIRED_FIELDS for these names never ran.
    _LIFECYCLE_STATES = frozenset({
        'queued', 'started', 'source_prepared', 'native_loaded', 'rendered',
        'output_written', 'installed', 'completed', 'failed', 'cancelled', 'stale',
    })

    def _event(self, key, data):
        if key != self._key():
            return
        payload = dict(data)
        subject = self._SUBJECT_BY_KIND.get(str(payload.get('kind') or ''))
        if subject and not payload.get('subject'):
            payload['subject'] = subject
        record_model_event('service_job', **payload)
        # Also record the stage under its declared name, so it can be searched
        # by event and so its required fields are actually checked. The
        # aggregated `service_job` line stays for existing queries.
        state = str(payload.get('state') or '')
        if state in self._LIFECYCLE_STATES and payload.get('kind') != 'capabilities':
            record_model_event(state, **payload)
        if data.get('kind') == 'capabilities':
            library = self.window.model_library
            library.details.set_capabilities(self.service.capabilities, self.service.appearance_slots)
            library.search.setEnabled(self.service.capabilities.browse)
            library._update_buttons()
            library.refresh()
        else:
            self.window.model_library.publish_job_event(data)

    def _preview_event(self, key, name, fields):
        if key == self._key() and self.service is not None:
            self.service.preview_event(name, fields)

    def _commit(self, key, before, after):
        if key != self._key():
            raise RuntimeError('模型提交属于已关闭的项目会话')
        view = self.window.view
        scene = view.scene
        records = scene.model_records
        index = next((i for i, record in enumerate(records) if record.get('id') == after['id']), None)
        current = records[index] if index is not None else None
        if current != before:
            raise RuntimeError('模型界面有更新，拒绝覆盖')
        session = getattr(scene, '_partial_project_session', None)
        if session is not None:
            session.acknowledge_model_commit(before, after)
        if index is None:
            records.append(copy.deepcopy(after))
        else:
            records[index] = copy.deepcopy(after)
        take_receipts = getattr(self.service.repository, 'take_catalog_receipts', None)
        receipts = take_receipts() if take_receipts is not None else ()
        if receipts:
            view._refresh_catalog_after_save(str(self.service.repository.context.metadata_path),
                                            receipts=receipts, reason='model_metadata_committed')
        searchable_fields = ('name', 'notes', 'tags', 'format', 'files', 'folder_id', 'deleted_at')
        if before is None or any(before.get(field) != after.get(field) for field in searchable_fields):
            view.category_panel.search_content_changed()
            view.category_panel.resource_records_changed.emit({'kind': 'model', 'id': after['id']})
        elif (before or {}).get('thumbnail') != after.get('thumbnail'):
            view.category_panel.resource_records_changed.emit(
                {'kind':'model','id':after['id'],'operation':'preview'})
        from .project_storage import register_model_attachments
        register_model_attachments(scene, after)
        details = self.window.model_library.details
        displayed = getattr(details, '_record', None)
        if displayed is not None and displayed.model_id == after['id']:
            from .project_storage import decode_record
            # Advance the committed revision without replacing live note or
            # slider drafts. Those controls still own their pending edits.
            details._record = decode_record(after)
        # These commands are already durable, and must not clear dirty state
        # belonging to another editor. Explicit project Save remains available.
        # Historic name kept for existing queries; the new name states what it
        # actually proves (the scene copy was replaced, and the record was
        # already durable before this point).
        record_model_event(EVENT_PROJECT_MODEL_APPLIED, model_id=after['id'],
                           model_count=1,
                           record_revision=after['record_revision'],
                           durable=True, state='completed')
        record_model_event('model_commit_confirmed', model_id=after['id'],
                           record_revision=after['record_revision'])

    @staticmethod
    def _read_texture(attachments, binding):
        attachment_id = binding.get('attachmentId', binding.get('attachment_id'))
        if attachment_id is None:
            raise ValueError('贴图没有受管理的原件引用')
        result = attachments.read_uncached(int(attachment_id))
        if result is None or result[2] is None:
            raise ValueError('原始贴图缺失')
        return result[2]

    def attach_texture(self, slot, path):
        service = self.service
        library = self.window.model_library
        if service is None or not service.capabilities.edit_appearance:
            return
        if not library.store_to_scene():
            return
        if service.appearance_slots.get(slot):
            library._report_failure('贴图', service.appearance_slots[slot])
            return
        # Texture preparation follows the material library's managed byte
        # channel, retaining original bytes. The slot binding is a snapshot.
        model_id = library.selection.current
        if model_id:
            try:
                service.attach_texture(model_id, slot, path)
            except Exception as error:
                library._report_failure('贴图导入', error)

    def apply_material(self):
        service = self.service
        if service is None:
            return
        records = [record for record in self.window.view.scene.material_records
                   if record.get('deletedAt') is None]
        if not records:
            self.window.model_library._report_failure('应用材质', '材质库中没有可用材质')
            return
        labels = [f"{i + 1}. {record.get('name', '')}" for i, record in enumerate(records)]
        selected, ok = QtWidgets.QInputDialog.getItem(self.window, '应用已有材质',
            '选择材质（模型中保存独立快照）', labels, editable=False)
        if ok:
            complete = self.window.model_library.apply_material(records[labels.index(selected)])
            if not complete:
                QtWidgets.QMessageBox.information(self.window, '材质部分应用',
                    '已应用支持的通道。法线覆盖、SSS、置换尚未验证，未执行；原因见贴图槽位说明。')

    def close(self):
        self._generation += 1
        if self.service is not None:
            self.service.close()
            self._track_closed(self.service)
        self._pool.shutdown(wait=False, cancel_futures=True)

    def _track_closed(self, service):
        """Remember a service whose lease working copies are being given back.

        ``ModelLibraryService.close`` releases them on a background thread,
        because it must not join inside a GUI callback.  A service that is
        forgotten - by opening another project, for instance - would leave its
        working copies on the disk when the process ends.  The list only holds
        the ones whose thread is still running.
        """
        if service is None:
            return
        self._closed_services = [item for item in self._closed_services
                                 if not item.wait_closed(0)]
        self._closed_services.append(service)

    def wait_closed(self, timeout=None):
        """Wait until every closed service gave its working copies back.

        The end of the process waits for this: a service whose close thread
        is still running when the process ends leaves its lease working copies
        behind - 32.7 MB per model of the 100k project, measured.  Returns
        whether all of them are done.
        """
        deadline = None if timeout is None else time.monotonic() + timeout
        done = True
        for service in list(self._closed_services):
            remaining = None if deadline is None else max(0.0, deadline - time.monotonic())
            if not service.wait_closed(remaining):
                done = False
        self._closed_services = [item for item in self._closed_services
                                 if not item.wait_closed(0)]
        return done
