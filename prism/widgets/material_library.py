"""Project material records, folder navigation and per-folder view state."""
import copy
import tempfile
import uuid
import re
import os
from collections import OrderedDict, deque
from pathlib import Path

from PyQt6 import QtCore, QtGui, QtWidgets
from PyQt6.QtCore import Qt

from prism.widgets import components
from prism.widgets.components.icons import icon
from prism.widgets.material_details import MaterialDetails
from prism.widgets.material_cards import MaterialCards, material_thumbnail
from prism.material_textures import (SLOTS, prepare_texture, selected_image, png_bytes,
                                     prepare_render, export_originals, render_signature,
                                     texture_signature, resolved_properties, transform_image)
from prism.material_textures import read_original
from prism.widgets.material_task import MaterialTask
from prism.widgets.material_render import MaterialRender
from prism.performance_trace import measured, operation, record as trace_record
from prism.material_cache import MaterialCache, MaterialThumbnailCache
from prism.item_lifecycle import is_deleted
from prism.library_observability import observed, event as library_event, run_bound


class MaterialLibrary(QtWidgets.QWidget):
    folder_requested = QtCore.pyqtSignal(object)
    create_folder_requested = QtCore.pyqtSignal(object)
    #: 卡片面就绪：网页加载完，第一批卡片可以推进来了。
    content_ready = QtCore.pyqtSignal()

    def __init__(self, window, *, details_parent=None, parent=None):
        from .widget_construction import finish_construction
        finish_construction(self._construct(window, details_parent=details_parent,
                                            parent=parent))

    def _construct(self, window, *, details_parent=None, parent=None):
        from .widget_construction import widget_steps
        # The Qt parent and the window host are two different things.  The
        # page belongs to the tab that hosts it, not to the window: while the
        # library is built during the pre-warm, every widget it creates is a
        # child of its host.  With the window as the parent each of those
        # widgets invalidated the **window's** layout, and the window
        # repainted itself -- canvas and side panels included -- in the
        # middle of the open the user just asked for.  That is the flash.
        super().__init__(window if parent is None else parent)
        self.window_host = window
        self.folder_id = None
        self.selected_id = None
        self._bound_service = None
        self._loaded_revision = None
        self._content_job = None
        self._shown_details = None
        self._views = {}
        self._flushing = False
        self._live_edit = False
        self.setObjectName('materialLibrary')
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.details = yield from widget_steps(MaterialDetails, details_parent)
        self._jobs = set()
        self._worker_pool = QtCore.QThreadPool(self)
        self._worker_pool.setMaxThreadCount(2)
        self._card_reads = deque()
        self._card_read_running = False
        self._slot_requests = {}
        self._icon_cache = MaterialCache(512, 8 * 1024 * 1024)
        self._thumbnail_cache = MaterialThumbnailCache()
        self._thumbnail_pending = set()
        self._icon_pending = set()
        self._render_directories = []
        self._preparing_directories = set()
        self._dialogs = []
        self._render_cache = OrderedDict()
        self._auto_key = None
        self._prepare_token = None
        self._inline_request = None
        self._inline_preparation = None
        self._inline_pending = None
        self._preview_generation = 0
        self._preview_state = 'idle'
        self.details.inline_render.request_updated.connect(self._inline_updated)
        self.details.inline_render.request_completed.connect(self._inline_completed)
        self.details.inline_render.request_failed.connect(self._inline_failed)
        self._adjustment_default = None
        self._drag_busy = False
        self._drag_active = False
        from .preview_updates import PreviewUpdates
        self._auto_timer = PreviewUpdates(self)
        self._auto_timer.setSingleShot(True)
        self._auto_timer.setInterval(120)
        self._auto_timer.timeout.connect(self.update_preview)
        self.details.slot_file_requested.connect(self.import_slot)
        self.details.slot_channel_requested.connect(self.change_channel)
        self.details.slot_clear_requested.connect(self.clear_slot)
        self.details.slot_reuse_requested.connect(self.reuse_slot)
        self.details.slot_options_requested.connect(self.change_slot_options)
        for slot in self.details.slots.values():
            slot.slider.sliderReleased.connect(self.update_preview)
        self.details.settings_requested.connect(self.save_settings)
        self.details.render_requested.connect(self.update_preview)
        self.details.open_requested.connect(self.open_preview)
        self.details.export_requested.connect(self.export_textures)
        self._edit_timer = QtCore.QTimer(self)
        self._edit_timer.setSingleShot(True)
        self._edit_timer.setInterval(500)
        self._edit_timer.timeout.connect(self.flush_details)
        self.details.edited.connect(self._details_edited)
        self.details.tag_edit.textEdited.connect(self._details_edited)
        self.details.name.editingFinished.connect(self.flush_details)
        layout = QtWidgets.QVBoxLayout(self)
        layout.setContentsMargins(24, 20, 24, 20)
        layout.setSpacing(16)
        heading = QtWidgets.QHBoxLayout()
        self.back_button = components.Button('返回上一级')
        self.back_button.setIcon(icon('folder'))
        self.back_button.clicked.connect(self._go_up)
        heading.addWidget(self.back_button)
        self.title = QtWidgets.QLabel('材质库')
        self.title.setObjectName('materialLibraryTitle')
        self.title.setTextFormat(Qt.TextFormat.PlainText)
        heading.addWidget(self.title, 1)
        self.new_material_button = components.Button('新建材质')
        self.new_material_button.setIcon(icon('plus'))
        self.new_material_button.clicked.connect(self.create_material)
        heading.addWidget(self.new_material_button)
        self.create_button = components.Button('新建文件夹')
        self.create_button.setIcon(icon('folder'))
        self.create_button.clicked.connect(
            lambda: self.create_folder_requested.emit(self.folder_id))
        heading.addWidget(self.create_button)
        # Search is the first row; folder navigation lives in the left tree.
        self.back_button.hide()
        self.title.hide()
        self.breadcrumb = QtWidgets.QLabel('材质库')
        self.breadcrumb.setTextFormat(Qt.TextFormat.PlainText)
        self.breadcrumb.setWordWrap(True)
        self.breadcrumb.setProperty('uiRole', 'secondary')
        self.breadcrumb.hide()
        self.search = QtWidgets.QLineEdit()
        self.search.setClearButtonEnabled(True)
        self.search.setPlaceholderText('按名称或说明查找材质（全部材质）')
        self.search.textChanged.connect(self._search_changed)
        layout.addWidget(self.search)
        self.folders = QtWidgets.QListWidget()
        self.folders.setObjectName('materialLibraryFolders')
        self.folders.setMaximumHeight(120)
        self.folders.setSpacing(6)
        self.folders.itemActivated.connect(
            lambda item: self.folder_requested.emit(item.data(Qt.ItemDataRole.UserRole)))
        self.folder_toggle = components.Button('子文件夹')
        self.folder_toggle.setCheckable(True)
        self.folder_toggle.setChecked(False)
        self.folder_toggle.toggled.connect(
            lambda checked: self.folders.setVisible(checked and self.folders.count() > 0))
        layout.addWidget(self.folder_toggle)
        layout.addWidget(self.folders)
        self.folders.hide()
        self.scope_label = QtWidgets.QLabel()
        self.scope_label.setProperty('uiRole', 'secondary')
        tools = QtWidgets.QHBoxLayout()
        tools.addWidget(self.scope_label, 1)
        tools.addWidget(self.new_material_button)
        tools.addWidget(self.create_button)
        self.grid_button = QtWidgets.QToolButton()
        self.grid_button.setText('网格')
        self.grid_button.setCheckable(True)
        self.grid_button.setChecked(True)
        self.list_button = QtWidgets.QToolButton()
        self.list_button.setText('列表')
        self.list_button.setCheckable(True)
        self._display_modes = QtWidgets.QButtonGroup(self)
        for button in (self.grid_button, self.list_button):
            self._display_modes.addButton(button)
            button.clicked.connect(self._set_card_layout)
            tools.addWidget(button)
        tools.addWidget(QtWidgets.QLabel('卡片大小'))
        self.card_size = QtWidgets.QSlider(Qt.Orientation.Horizontal)
        self.card_size.setRange(160, 320)
        self.card_size.setValue(220)
        self.card_size.setMaximumWidth(90)
        self.card_size.valueChanged.connect(self._set_card_layout)
        tools.addWidget(self.card_size)
        self.logs_button = components.Button('性能日志')
        self.logs_button.clicked.connect(self.open_performance_logs)
        tools.addWidget(self.logs_button)
        layout.addLayout(tools)
        yield
        self.materials = yield from widget_steps(MaterialCards, self)
        self.materials.setObjectName('materialLibraryRecords')
        self.materials.content_ready.connect(self._surface_ready)
        self.materials.selection_changed.connect(self._selection_changed)
        self.materials.open_requested.connect(self.open_preview)
        self.materials.export_requested.connect(self.export_record)
        self.materials.batch_export_requested.connect(self.export_selected)
        self.materials.scroll_changed.connect(self._remember_view)
        self.materials.thumbnail_requested.connect(self._load_card_thumbnail)
        self.materials.textures_drag_requested.connect(self.drag_textures)
        self.materials.rename_requested.connect(self.rename_record)
        self.materials.rename_committed.connect(self.rename_record)
        self.materials.card_size_requested.connect(self.card_size.setValue)
        self.materials.setToolTip('拖动选中材质到左侧文件夹整理；Alt＋拖动可拖出原始贴图')
        self.details.tag_requested.connect(self.search.setText)
        layout.addWidget(self.materials, 1)
        self.empty_state = QtWidgets.QWidget()
        empty_layout = QtWidgets.QVBoxLayout(self.empty_state)
        empty_layout.addStretch(1)
        self.empty_title = QtWidgets.QLabel('材质库还没有内容')
        self.empty_title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        empty_layout.addWidget(self.empty_title)
        hint = QtWidgets.QLabel('新建记录可先整理名称和说明。未添加贴图的记录会保留为草稿。')
        hint.setAlignment(Qt.AlignmentFlag.AlignCenter)
        hint.setWordWrap(True)
        hint.setProperty('uiRole', 'secondary')
        empty_layout.addWidget(hint)
        empty_layout.addStretch(2)
        layout.addWidget(self.empty_state, 1)
        panel = window.view.category_panel
        panel.resource_nodes_changed.connect(self.refresh)
        panel.material_records_changed.connect(self.refresh)

    @property
    def service(self):
        return self.window_host.view.category_panel.resource_service

    @property
    def records(self):
        return self.window_host.view.category_panel.material_service

    def _view_key(self):
        return self.folder_id or '__root__'

    def _remember_view(self):
        if self._bound_service is not self.records:
            return
        self._views[self._view_key()] = {
            'selectedId': self.selected_id, 'search': self.search.text(),
            'scroll': self.materials.verticalScrollBar().value(),
            'folderScroll': self.folders.verticalScrollBar().value(),
            'selectedFolderId': (self.folders.currentItem().data(Qt.ItemDataRole.UserRole)
                                 if self.folders.currentItem() else None),
        }
        self.window_host.view.scene.material_library_state = {
            'folderId': self.folder_id, 'views': copy.deepcopy(self._views),
            'viewMode': 'list' if self.list_button.isChecked() else 'grid',
            'cardSize': self.card_size.value()}

    def _restore_view(self):
        state = self._views.get(self._view_key(), {})
        state = state if isinstance(state, dict) else {}
        selected_id = state.get('selectedId')
        self.selected_id = selected_id if isinstance(selected_id, str) else None
        query = state.get('search', '')
        with QtCore.QSignalBlocker(self.search):
            self.search.setText(query if isinstance(query, str) else '')
        self._restore_scroll = state

    def _sync_project(self):
        if self._bound_service is self.records:
            return
        self._edit_timer.stop()
        self._invalidate_inline_preview()
        self._auto_key = None
        self._thumbnail_cache = MaterialThumbnailCache()
        self._adjustment_default = None
        self._icon_cache.clear()
        self._render_cache.clear()
        self._bound_service = None
        self.selected_id = None
        self.details.show_record(None)
        self._shown_details = None
        self._bound_service = self.records
        state = getattr(self.window_host.view.scene, 'material_library_state', {})
        state = state if isinstance(state, dict) else {}
        folder_id = state.get('folderId')
        self.folder_id = folder_id if isinstance(folder_id, str) else None
        self._views = copy.deepcopy(state.get('views', {}))
        if not isinstance(self._views, dict):
            self._views = {}
        mode = state.get('viewMode', 'grid')
        size = state.get('cardSize', 220)
        with QtCore.QSignalBlocker(self.grid_button), QtCore.QSignalBlocker(self.list_button), QtCore.QSignalBlocker(self.card_size):
            (self.list_button if mode == 'list' else self.grid_button).setChecked(True)
            self.card_size.setValue(size if isinstance(size, int) else 220)
        self.materials.set_layout(mode, self.card_size.value())
        self._restore_view()
        self._show_details()

    def is_content_ready(self):
        """Whether the card surface can already show the loaded records.

        Before the web document is loaded the surface is a blank rectangle, so
        a host that shows the page too early shows an empty panel first and the
        cards afterwards - that is the flash the user sees.
        """
        job = self._content_job
        return bool(self.materials.ready and (job is None or (job.done and job.error is None)))

    def open_folder(self, folder_id=None):
        from .widget_construction import finish_construction
        self._cancel_content_job()
        finish_construction(self._open_folder_steps(folder_id))

    def open_folder_cooperatively(self, folder_id=None):
        """Navigation entry: bounded preparation using the normal refresh path."""
        from .widget_construction import WidgetConstruction
        self._cancel_content_job()
        job = WidgetConstruction(self._open_folder_steps(folder_id, cooperative=True), self)
        self._content_job = job
        job.finished.connect(lambda result: self._content_finished(job, result))
        job.failed.connect(lambda error: self._content_failed(job, error))
        job.expedite()
        job.start()
        return job

    def _cancel_content_job(self):
        job = self._content_job
        if job is not None and not job.done:
            job.cancel()
        if job is not None:
            job.deleteLater()
        self._content_job = None

    def _content_finished(self, job, result):
        if self._content_job is not job:
            return
        self._content_job = None
        job.deleteLater()
        if result is False:
            # Source changed between batches. Re-query the current identity;
            # the discarded staging list has never been installed.
            self._sync_project()
            self.open_folder_cooperatively(self.folder_id)
        elif self.materials.ready:
            self._surface_ready()

    def _content_failed(self, job, error):
        if self._content_job is not job:
            return
        self.materials.hide()
        self.empty_title.setText('材质加载失败，请重新打开材质库')
        self.empty_state.show()
        trace_record('material_library_load_failed', error_type=type(error).__name__)
        # Wake the host with an explicit failure state, not a silent timeout.
        self.content_ready.emit()

    def _open_folder_steps(self, folder_id=None, *, cooperative=False):
        with operation('material.open_folder', folder_id=folder_id):
            with operation('material.open_folder.sync_project'):
                self._sync_project()
            scene, records = self.window_host.view.scene, self.records
            project_id = scene.project_id
            yield
            if (self.window_host.view.scene is not scene or scene.project_id != project_id
                    or self.records is not records):
                return False
            with operation('material.open_folder.flush_details'):
                self.flush_details()
            with operation('material.open_folder.remember_view'):
                self._remember_view()
            if folder_id is not None:
                self.records._validate_folder(folder_id)
            if self.folder_id != folder_id:
                self._reset_preview_adjustments(self.selected_id)
                self.folder_id = folder_id
                with operation('material.open_folder.restore_view'):
                    self._restore_view()
            with operation('material.open_folder.load_from_scene'):
                revision = self._content_revision()
                if revision != self._loaded_revision:
                    return (yield from self._refresh_steps(cooperative=cooperative))
            return True

    def _content_revision(self):
        return (id(self.records), self.records.revision, self.service.revision,
                self.folder_id, self.search.text())

    def reveal_material(self, record_id, *, from_search=False):
        """Shared navigation target for both existing project-search surfaces."""
        service = self.records
        project_id = self.window_host.view.scene.project_id

        def visible_record():
            if (is_deleted(self) or self.window_host.view.scene.project_id != project_id
                    or self.records is not service):
                return None
            record = service.get(record_id)
            if (not record or record.get('deletedAt') is not None
                    or not service._folder_visible(record.get('folderId'))):
                return None
            return record

        if visible_record() is None:
            return False

        def reveal():
            # The outgoing editor may complete later. Re-resolve both project
            # ownership and location before opening AND selecting the target.
            record = visible_record()
            if record is None:
                return
            folder_id = record.get('folderId')
            if folder_id is not None and self.service._by_id(folder_id) is None:
                folder_id = None
            self.window_host.open_material_library(folder_id, from_search=from_search)
            if self.window_host.tabs.currentWidget() is not getattr(self, '_tab_host', self) or visible_record() is None:
                return
            self.search.clear()
            self._selection_changed(record_id)
            self.materials.reveal_record(record_id)
            # 「最近使用」记一笔：账本在用户目录里，不写工程文件。
            self.window_host.view.category_panel.record_resource_usage('material', record_id,
                                        record.get('name', ''))

        if self.window_host._navigation_is_committed():
            reveal()
        else:
            self.window_host.request_navigation(reveal)
        return True

    def load_from_scene(self):
        self._sync_project()
        revision = (id(self.records), self.records.revision, self.service.revision,
                    self.folder_id, self.search.text())
        if revision != self._loaded_revision:
            self.refresh()

    def store_to_scene(self):
        # Loading may already have replaced the project's owned record list.
        if (self._bound_service is self.records
                and self.records.records is self.window_host.view.scene.material_records):
            self.flush_details()
            self._reset_preview_adjustments(self.selected_id)
            self._show_details()
            self._remember_view()

    def flush_details(self):
        if getattr(self.window_host.view, '_upgrade_preview_readonly', False):
            return
        if (self._flushing or self._bound_service is not self.records
                or self.records.records is not self.window_host.view.scene.material_records):
            return
        self._edit_timer.stop()
        record = self.records.get(self.selected_id)
        if record is None or record.get('deletedAt') is not None:
            return
        self._flushing = True
        try:
            name = self.details.name.text().strip()
            if not name:
                name = record['name']
                with QtCore.QSignalBlocker(self.details.name):
                    self.details.name.setText(name)
            self.records.update(record['id'], name=name,
                                notes=self.details.notes.toPlainText())
            self.save_settings()
        finally:
            self._flushing = False

    def _details_edited(self):
        if getattr(self.window_host.view, '_upgrade_preview_readonly', False):
            return
        self.window_host.view.mark_content_dirty()
        self._edit_timer.start()

    def create_material(self):
        self._sync_project()
        self.flush_details()
        self._reset_preview_adjustments(self.selected_id)
        with QtCore.QSignalBlocker(self.search):
            self.search.clear()
        record = self.records.create(self.folder_id)
        self.selected_id = record['id']
        self.refresh()
        self.details.name.setFocus()
        self.details.name.selectAll()

    def _search_changed(self):
        self.flush_details()
        self.materials.set_scroll_position(0)
        self.refresh()

    @measured('materials.select')
    def _selection_changed(self, record_id):
        previous = self.selected_id
        self.flush_details()
        if record_id != self.selected_id:
            self._reset_preview_adjustments(self.selected_id)
        trace_record('material_library_selection', material=record_id or '',
                     previous=previous,
                     multi=len(getattr(self.materials, 'selected_ids', ()) or ()))
        self.selected_id = record_id
        if record_id != previous:
            self._reset_preview_adjustments(record_id)
        if record_id and record_id != previous:
            record = next((r for r in self.records.records if r['id'] == record_id), {})
            self.window_host.view.category_panel.record_resource_usage('material', record_id, record.get('name', ''))
        # Selection changes the inspector, not the whole material catalogue.
        self.materials.show_selection(record_id)
        self._show_details()
        self._remember_view()

    def _set_card_layout(self, *_args):
        mode = 'list' if self.list_button.isChecked() else 'grid'
        self.materials.set_layout(mode, self.card_size.value())
        self._remember_view()

    def _folder_path(self, folder_id):
        titles = []
        current = self.service._by_id(folder_id)
        while current:
            titles.append(current['title'])
            current = self.service._by_id(current.get('parentId'))
        return ' / '.join(reversed(titles)) or '未分类'

    def _show_details(self):
        record = self.records.get(self.selected_id)
        viewer = self.details.inline_render
        request = self._inline_request
        if ((request and (request['service'] is not self.records or not record
                          or request['material'] != record['id']))
                or (viewer.payload and (not record or viewer.payload.get('recordId') != record['id']))):
            self._invalidate_inline_preview()
        thumbnail = self._thumbnail(record) if record else None
        location = self._folder_path(record.get('folderId')) if record else ''
        # Entry synchronization, refresh and Qt's first show all reach here.
        # The record is already a detached copy: retain that value as the
        # comparison basis rather than installing identical controls thrice.
        shown = (id(self.records), record, location, thumbnail)
        changed = shown != self._shown_details
        if changed:
            self.details.show_record(record, location, thumbnail)
            self._shown_details = shown
        if record is not None and thumbnail is None:
            # Not resident yet: fetch it in the background and come back.
            self._load_detail_thumbnail(record['id'])
        if record:
            self.details.export_button.setEnabled(bool(record.get('files')) or len(self.materials.selected_ids)>1)
            if changed:
                self._slot_icons(record)
            if changed or self._auto_key is None:
                key = (id(self.records), record['id'], render_signature(record))
                if key != self._auto_key:
                    self._auto_key = key
                    self._auto_timer.start()
        else:
            self._auto_timer.stop()
            self._auto_key = None

    def showEvent(self, event):
        super().showEvent(event)
        if not getattr(self, '_preserve_editor_on_show', False):
            self._show_details()
        trace_record('material_library_opened',
                     records=len(getattr(self.records, 'records', None) or ()),
                     folder=self.folder_id or '',
                     searching=bool(self.search.text().strip()),
                     generation=getattr(self.materials, 'generation', None))

    def hideEvent(self, event):
        self._auto_timer.stop()
        self._auto_key = None
        self._invalidate_inline_preview()
        self._prune_render_cache()
        trace_record('material_library_closed',
                     selected=self.selected_id or '',
                     pending_reads=len(getattr(self, '_thumbnail_pending', ()) or ()))
        super().hideEvent(event)

    def _task(self, function, callback, service=None, record_id=None, finally_callback=None, obsolete=None, kind='operation', error_callback=None):
        service = service or self.records
        record_id = record_id or self.selected_id
        job = MaterialTask(function, obsolete)
        priority = {'card_thumbnail': -10, 'slot_icon': 5, 'detail_thumbnail': 10,
                    'render_inputs': 20, 'original_import': 20}.get(kind, 0)
        task_id = uuid.uuid4().hex[:12]
        trace_record('material_task_queued', task=task_id, material=record_id, task_kind=kind,
                     jobs=len(self._jobs)+1, queue_priority=priority)
        job.task_id = task_id
        self._jobs.add(job)
        def current():
            return (not is_deleted(self) and self.records is service
                    and service.get(record_id) is not None
                    and (obsolete is None or not obsolete()))
        def done(value):
            is_current = current()
            trace_record('material_task_result', task=task_id, material=record_id, current=is_current)
            if is_current:
                try:
                    callback(value)
                except Exception as exc:
                    error(str(exc))
            else:
                library_event('material','task_result','stale',job_id=task_id,
                              material_id=record_id,reason='project_material_or_request_changed')
        def error(message):
            trace_record('material_task_error', task=task_id, material=record_id, message=message)
            if current():
                if error_callback:
                    error_callback(message)
                elif self.selected_id == record_id:
                    self.details.operation_status.setText('操作失败：' + message)
        job.signals.result.connect(lambda value:run_bound(job.trace_context,done,value))
        job.signals.error.connect(lambda message:run_bound(job.trace_context,error,message))
        def finished():
            self._jobs.discard(job)
            if kind == 'card_thumbnail':
                self._card_read_running = False
                if not is_deleted(self):QtCore.QTimer.singleShot(0, self._dispatch_card_read)
            trace_record('material_task_finished', task=task_id, remaining=len(self._jobs))
            if finally_callback and not is_deleted(self):
                finally_callback()
        job.signals.finished.connect(lambda:run_bound(job.trace_context,finished))
        if kind == 'card_thumbnail':
            self._card_reads.append((job, priority))
            self._dispatch_card_read()
        elif priority:
            self._worker_pool.start(job, priority)
        else:
            self._worker_pool.start(job)

    def _dispatch_card_read(self):
        # One decorative read leaves the other existing worker available for
        # selected-slot icons and preview preparation; priorities cannot preempt
        # two card decodes that have already started.
        if is_deleted(self) or self._card_read_running or not self._card_reads:
            return
        job, priority = self._card_reads.popleft()
        self._card_read_running = True
        self._worker_pool.start(job, priority)

    @observed('material','ui_texture')
    def import_slot(self, slot, path):
        self.flush_details()
        service, record_id = self.records, self.selected_id
        if not service.get(record_id):
            library_event('material','ui_texture','rejected',material_id=record_id,slot=slot,reason='no_current_material')
            return
        request = uuid.uuid4().hex
        key = (id(service), record_id, slot)
        self._slot_requests[key] = request
        self.details.operation_status.setText('正在收存原始贴图…')
        def commit(prepared):
            if self._slot_requests.get(key) != request:
                library_event('material','texture_commit','stale',material_id=record_id,slot=slot,
                              request_id=request,reason='newer_slot_request')
                return
            service.configure(record_id, slot=slot, prepared=prepared, channel='RGB')
            if self.selected_id == record_id:
                self.details.operation_status.setText('原文件已完整收存，请检查下面的通道选择。')
        self._task(lambda: prepare_texture(path), commit, service, record_id, kind='original_import')

    @observed('material','ui_channel')
    def change_channel(self, slot, channel):
        if self.records.get(self.selected_id):
            self.records.configure(self.selected_id, slot=slot, channel=channel)

    @observed('material','ui_clear')
    def clear_slot(self, slot):
        self._slot_requests.pop((id(self.records), self.selected_id, slot), None)
        self.records.configure(self.selected_id, slot=slot)

    def reuse_slot(self, slot):
        record = self.records.get(self.selected_id)
        if not record:
            return
        files = record.get('files', [])
        if not files:
            self.details.operation_status.setText('请先拖入一张原始贴图。')
            return
        labels = [f"{f['name']} · {f['sha256'][:8]}" for f in files]
        label, ok = QtWidgets.QInputDialog.getItem(self, '复用原始贴图', '选择本材质已收存的图片', labels, 0, False)
        if ok:
            file = files[labels.index(label)]
            entry = self.window_host.view.scene.attachments.get(file['attachmentId'])
            if entry and isinstance(entry[2], bytes):
                self.records.configure(record['id'], slot=slot, prepared=(file, entry[2]), channel='RGB')

    def save_settings(self):
        if getattr(self.window_host.view, '_upgrade_preview_readonly', False):
            return
        if self._bound_service is not self.records or not self.records.get(self.selected_id):
            return
        current = self.records.get(self.selected_id)
        props = current.get('preview', {}).get('properties', {})
        blend = self.details.blend_mode.currentData()
        normal = self.details.normal_convention.currentData()
        patch = dict(flipX=self.details.flip_x.isChecked(), flipY=self.details.flip_y.isChecked(),
                     alphaCutoff=self.details.alpha_cutoff.value(),
                     ignoreBaseAlpha=self.details.ignore_base_alpha.isChecked())
        tags = list(dict.fromkeys(t.strip() for t in re.split('[,，]', self.details.tag_edit.text()) if t.strip()))
        # The inspector is where values are actually edited; the commit happens
        # downstream, so without this the log never says which field the user
        # touched -- only that "something" was written.
        try:
            changed = []
            if props.get('blendMode', 'auto') != blend:
                changed.append('blendMode')
            if props.get('normalConvention', 'DirectX') != normal:
                changed.append('normalConvention')
            for key, value in patch.items():
                if props.get(key, {'flipX': False, 'flipY': False,
                                   'alphaCutoff': .5,
                                   'ignoreBaseAlpha': False}[key]) != value:
                    changed.append(key)
            trace_record('material_settings_edited',
                               material=self.selected_id or '',
                               blend_mode=str(blend),
                               normal_convention=str(normal),
                               alpha_cutoff=float(patch['alphaCutoff']),
                               tag_count=len(tags),
                               changed=sorted(changed)[:8],
                               changed_truncated=len(changed) > 8,
                               state='edited')
        except Exception:                          # noqa: BLE001 - logging only
            pass

        defaults = dict(flipX=False, flipY=False, alphaCutoff=.5, ignoreBaseAlpha=False)
        if (props.get('blendMode', 'auto') == blend and props.get('normalConvention', 'DirectX') == normal
                and all(props.get(key, defaults[key]) == value for key, value in patch.items())
                and current.get('tags', []) == tags):
            return
        self.records.configure(self.selected_id,
            blend_mode=blend, normal_convention=normal, properties_patch=patch, tags=tags)

    def change_slot_options(self, slot, patch):
        if self.records.get(self.selected_id):
            if self._adjustment_default is None:
                current = self.records.get(self.selected_id)
                preview = current.get('preview', {})
                attachment = self.window_host.view.scene.attachments.get(preview.get('thumbnailAttachmentId'))
                default = copy.deepcopy(current)
                props = default.setdefault('preview', {}).setdefault('properties', {})
                props.pop('slots', None)
                for key in ('flipX', 'flipY', 'alphaCutoff'):
                    props.pop(key, None)
                blob = attachment[2] if attachment and preview.get('signature') == render_signature(default) else None
                self._adjustment_default = (id(self.records), current['id'], blob)
            self._live_edit = True
            try:
                record = self.records.configure(self.selected_id, slot_settings=(slot, patch))
            finally:
                self._live_edit = False
            if any(key in patch for key in ('enabled','invert')):
                self._slot_icons(record)

    def _reset_preview_adjustments(self, record_id):
        if getattr(self.window_host.view, '_upgrade_preview_readonly', False):
            return
        current = self.records.get(record_id)
        if not current:
            return
        props = current.get('preview', {}).get('properties', {})
        if not props.get('slots') and not any(key in props for key in ('flipX', 'flipY', 'alphaCutoff')):
            return
        self._invalidate_inline_preview()
        self._auto_timer.stop()
        self._live_edit = True
        try:
            reset = self.records.configure(record_id, reset_adjustments=True)
        finally:
            self._live_edit = False
        saved = self._adjustment_default
        self._adjustment_default = None
        if saved and saved[:2] == (id(self.records), record_id) and saved[2]:
            self.records.save_preview(record_id, render_signature(reset), saved[2])
        else:
            # Never display a thumbnail of discarded temporary adjustments.
            placeholder = dict(reset, preview={})
            self.materials.update_thumbnail(placeholder, None)
        for dialog in list(self._dialogs):
            if (not is_deleted(dialog) and dialog.isVisible()
                    and dialog.material_service is self.records and dialog.material_id == record_id):
                request = getattr(dialog, 'material_request', None)
                if request and request['signature'] != render_signature(reset):
                    self._open_render(True, dialog=dialog)
        trace_record('material_adjustments_reset', material=record_id)

    def _slot_icons(self, record):
        properties = record.get('preview', {}).get('properties', {})
        for slot, binding in record.get('textures', {}).items():
            if slot not in self.details.slots or not isinstance(binding, dict) or 'sha256' not in binding:
                continue
            key = (binding['sha256'], binding['channel'], slot, texture_signature(record))
            if key in self._icon_cache:
                self.details.slots[slot].show_channel_image(self._icon_cache[key])
                continue
            pending = (id(self.records), record['id'], slot, key)
            if pending in self._icon_pending:
                continue
            attachments = self.window_host.view.scene.attachments
            if binding['attachmentId'] not in attachments:
                continue
            record_id = record['id']
            self._icon_pending.add(pending)
            def show(data, slot=slot, key=key, record_id=record_id, pending=pending):
                self._icon_pending.discard(pending)
                if data is None:
                    return
                self._icon_cache[key] = data
                current = self.records.get(self.selected_id)
                b = current.get('textures', {}).get(slot, {}) if current else {}
                if self.selected_id == record_id and current and (b.get('sha256'), b.get('channel'), slot, texture_signature(current)) == key:
                    self.details.slots[slot].show_channel_image(data)
            def decode(b=binding, slot=slot, properties=properties, attachments=attachments):
                entry = read_original(attachments, b['attachmentId'])
                if not entry or not isinstance(entry[2], bytes):
                    return None
                return png_bytes(transform_image(selected_image(entry[2], b['channel'], 64), slot, properties))
            self._task(decode,
                       show, record_id=record_id,
                       finally_callback=lambda pending=pending: self._icon_pending.discard(pending),
                       obsolete=lambda record_id=record_id: self.selected_id != record_id, kind='slot_icon')

    def update_preview(self):
        if getattr(self.window_host.view, '_upgrade_preview_readonly', False):
            return
        if not self.isVisible():
            return
        self._open_render(False)

    @observed('material','ui_preview')
    def open_preview(self, record_id=None):
        if getattr(self.window_host.view, '_upgrade_preview_readonly', False):
            return
        if isinstance(record_id, str) and self.records.get(record_id):
            self._selection_changed(record_id)
        self._open_render(True)

    def _invalidate_inline_preview(self):
        if self._inline_request:
            self._inline_request['valid'] = False
        self._inline_request = None
        self._inline_pending = None
        self._prepare_token = None
        self._preview_state = 'idle'
        self.details.operation_status.clear()
        self.details.inline_render.release_material()
        self.details.set_live_preview(False)

    def _new_preview_request(self, service, record, viewer):
        self._preview_generation += 1
        return dict(service=service, material=record['id'], viewer=viewer,
                    signature=render_signature(record), token=uuid.uuid4().hex,
                    generation=self._preview_generation, valid=True, state='idle')

    def _preview_request_current(self, request, *, inline=False, signature=None):
        if (not request or not request['valid'] or is_deleted(self)
                or self.records is not request['service'] or is_deleted(request['viewer'])):
            return False
        record = request['service'].get(request['material'])
        if not record or render_signature(record) != request['signature']:
            return False
        if signature is not None and signature != request['signature']:
            return False
        return (not inline or (request is self._inline_request and self.isVisible()
                              and self.selected_id == request['material']
                              and self._prepare_token == request['token']))

    def _set_inline_state(self, request, state, message=''):
        if not self._preview_request_current(request, inline=True):
            return
        previous = request.get('status_text', '')
        request['state'] = state
        self._preview_state = state
        if state == 'preparing':
            message = '正在准备渲染，原文件保持不变…'
        elif state == 'updating':
            message = '正在更新预览…'
        elif state == 'error':
            message = '渲染失败：' + message
        elif self.details.operation_status.text() not in (previous, ''):
            # A newer import/export message owns the label; capture must not erase it.
            message = self.details.operation_status.text()
        request['status_text'] = message
        self.details.operation_status.setText(message)
        self.details.render_button.setVisible(state == 'error')
        trace_record('material_preview_state', token=request['token'],
                     material=request['material'], generation=request['generation'],
                     viewer_id=request['viewer'].viewer_id,
                     state=state, tab_y=self.details.sections.mapTo(self.details, QtCore.QPoint()).y(),
                     header_height=self.details.header.height())

    def _inline_updated(self, token, signature):
        request = self._inline_request
        if not self._preview_request_current(request, inline=True, signature=signature) or token != request['token']:
            return
        if request['state'] == 'error':
            return
        self._set_inline_state(request, 'ready')
        self.details.preview_status.setText('实时预览 · 正在同步卡片')

    def _inline_completed(self, token, signature, blob):
        request = self._inline_request
        if not self._preview_request_current(request, inline=True, signature=signature) or token != request['token']:
            library_event('material','cover_result','stale',signature=signature,reason='request_or_project_changed')
            return
        if request['state'] == 'error' or request.get('captured'):
            return
        request['captured'] = True
        if request['service'].save_preview(request['material'], signature, blob):
            # save_preview emits service signals synchronously; recheck ownership.
            if self._preview_request_current(request, inline=True, signature=signature):
                self._set_inline_state(request, 'ready')
                self.details.preview_status.setText('自动更新 · 已同步到材质卡片')

    def _inline_failed(self, token, message):
        request = self._inline_request
        if not self._preview_request_current(request, inline=True) or token != request['token']:
            return
        self._set_inline_state(request, 'error', message)
        self.details.set_live_preview(False)

    @staticmethod
    def _payload_for_record(cached, record):
        from prism.material_textures import payload_for_record
        return payload_for_record(cached, record)

    def _install_inline_payload(self, request, payload):
        if not self._preview_request_current(request, inline=True, signature=payload['signature']):
            return
        self.details.set_live_preview(True)
        props = payload['properties']
        mode_label = {'opaque': '不透明', 'masked': '透明裁切', 'translucent': '半透明'}[props['blendMode']]
        record = request['service'].get(request['material'])
        prefix = '自动：' if record.get('preview', {}).get('properties', {}).get('blendMode', 'auto') == 'auto' else '手动覆盖：'
        self.details.auto_mode.setText(prefix + mode_label)
        self.details.auto_mode.setToolTip(props.get('autoBlendReason', ''))
        self._set_inline_state(request, 'updating')
        for dialog in list(self._dialogs):
            if (not is_deleted(dialog) and getattr(dialog, 'material_service', None) is request['service']
                    and getattr(dialog, 'material_id', None) == request['material']):
                self._load_dialog_payload(dialog, payload)
        request['viewer'].load_material(payload, request['token'])

    def _load_dialog_payload(self, dialog, payload, request=None):
        if is_deleted(dialog) or self.records is not dialog.material_service:
            return
        record = dialog.material_service.get(dialog.material_id)
        if not record or render_signature(record) != payload['signature']:
            return
        old = getattr(dialog, 'material_request', None)
        if request is not None:
            if request is not old or not self._preview_request_current(request, signature=payload['signature']):
                return
        else:
            if old:
                old['valid'] = False
            request = self._new_preview_request(dialog.material_service, record, dialog.material_viewer)
        dialog.material_request = request
        request['state'] = 'updating'
        dialog.material_state.setText('正在更新预览…')
        dialog.material_viewer.load_material(payload, request['token'])

    def _create_preview_dialog(self, record, service):
        dialog = QtWidgets.QDialog(self)
        dialog.setWindowTitle(record['name'] + ' · 材质预览')
        dialog.resize(1000, 780)
        layout = QtWidgets.QVBoxLayout(dialog)
        toolbar = QtWidgets.QHBoxLayout()
        modes = QtWidgets.QComboBox()
        modes.addItem('完整材质', 'full')
        for key, title in SLOTS.items():
            modes.addItem(title, key)
        toolbar.addWidget(modes)
        shapes = QtWidgets.QComboBox()
        for text, key in [('平面', 'plane'), ('球体', 'sphere'), ('圆柱体', 'cylinder'), ('立方体', 'box')]:
            shapes.addItem(text, key)
        toolbar.addWidget(shapes)
        front = QtWidgets.QPushButton('回到正面')
        toolbar.addWidget(front)
        toolbar.addStretch(1)
        close = QtWidgets.QPushButton('返回材质库')
        toolbar.addWidget(close)
        layout.addLayout(toolbar)
        viewer = MaterialRender(dialog)
        viewer.capture_enabled = False
        dialog.material_service = service
        dialog.material_id = record['id']
        dialog.material_viewer = viewer
        dialog.material_preparation = None
        layout.addWidget(viewer, 1)
        dialog.material_state = QtWidgets.QLabel('正在准备预览…')
        dialog.material_state.setTextFormat(Qt.TextFormat.PlainText)
        dialog.material_state.setWordWrap(True)
        layout.addWidget(dialog.material_state)
        modes.currentIndexChanged.connect(lambda: viewer.view_channel(modes.currentData()))
        shapes.currentIndexChanged.connect(lambda: viewer.set_shape(shapes.currentData()))
        front.clicked.connect(viewer.reset_camera)
        close.clicked.connect(dialog.close)
        def current(token, signature=None):
            request = getattr(dialog, 'material_request', None)
            return (not is_deleted(dialog) and self._preview_request_current(request, signature=signature)
                    and token == request['token'])
        def updated(token, signature):
            if current(token, signature):
                dialog.material_request['state'] = 'ready'
                dialog.material_state.setText('已同步最新配置。拖动旋转，滚轮缩放；通道查看不修改材质。')
        def failed(token, message):
            if current(token):
                dialog.material_request['state'] = 'error'
                dialog.material_state.setText('渲染失败：' + message)
        viewer.request_updated.connect(updated)
        viewer.request_failed.connect(failed)
        dialog.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        self._dialogs.append(dialog)
        def destroyed(_object=None):
            request = getattr(dialog, 'material_request', None)
            if request:
                request['valid'] = False
            if dialog in self._dialogs:
                self._dialogs.remove(dialog)
            if not is_deleted(self):
                self._prune_render_cache()
        dialog.destroyed.connect(destroyed)
        dialog.show()
        return dialog

    @measured('materials.open_render')
    def _open_render(self, large, dialog=None):
        if dialog is not None:
            # Resume this viewer's material, independently of the inspector selection.
            if (not large or is_deleted(dialog) or not dialog.isVisible()
                    or dialog not in self._dialogs or self.records is not dialog.material_service):
                return
            service = dialog.material_service
            record = service.get(dialog.material_id)
        else:
            self.flush_details()
            service = self.records
            record = service.get(self.selected_id)
        if not record or (not large and not self.isVisible()):
            return
        record_id = record['id']
        if large and dialog is None:
            dialog = self._create_preview_dialog(record, service)
        viewer = dialog.material_viewer if large else self.details.inline_render
        request = self._new_preview_request(service, record, viewer)
        if large:
            old = getattr(dialog, 'material_request', None)
            if old:
                old['valid'] = False
            dialog.material_request = request
            if dialog.material_preparation is not None:
                request['state'] = 'preparing'
                dialog.material_state.setText('正在准备预览…')
                return  # The executing preparation will advance this latest intent.
        else:
            self._auto_timer.stop()
            self._auto_key = (id(service), record_id, request['signature'])
            if self._inline_request:
                self._inline_request['valid'] = False
            self._inline_request = request
            self._prepare_token = request['token']
            if self._inline_preparation is not None:
                # A texture preparation cannot be interrupted; retain only the latest intent.
                self._inline_pending = request
                self._set_inline_state(request, 'preparing')
                return
        cache_key = (id(service), record_id, texture_signature(record))
        cached = self._render_cache.get(cache_key)
        trace_record('material_prepare', material=record_id, token=request['token'],
                     cached=bool(cached), large=large, generation=request['generation'],
                     viewer_id=viewer.viewer_id)
        def current():
            return (self._preview_request_current(request, inline=not large)
                    and (not large or (not is_deleted(dialog) and dialog.isVisible()
                                       and dialog.material_request is request)))
        def show(payload):
            if not current() or payload['signature'] != request['signature']:
                library_event('material','prepared_payload','stale',material_id=record_id,
                              generation=request['generation'],reason='identity_or_signature_changed')
                return
            if large:
                self._load_dialog_payload(dialog, payload, request=request)
            else:
                self._install_inline_payload(request, payload)
        if cached:
            self._render_cache.move_to_end(cache_key)
            show(self._payload_for_record(cached, record))
            return
        if large:
            request['state'] = 'preparing'
            dialog.material_state.setText('正在准备预览…')
            dialog.material_preparation = request
        else:
            self._set_inline_state(request, 'preparing')
            self._inline_preparation = request
        temp = tempfile.TemporaryDirectory(prefix='prism-material-render-')
        self._render_directories.append(temp)
        self._preparing_directories.add(temp.name)
        attachments = self.window_host.view.scene.attachments
        def prepared(payload):
            self._preparing_directories.discard(temp.name)
            # Cache is keyed by immutable source/transforms, not current UI parameters.
            self._render_cache[cache_key] = payload
            show(payload)
        def failed(message):
            if current():
                if large:
                    request['state'] = 'error'
                    dialog.material_state.setText('渲染失败：' + message)
                else:
                    self._inline_failed(request['token'], message)
        def finished():
            self._preparing_directories.discard(temp.name)
            if not large and self._inline_preparation is request:
                self._inline_preparation = None
                pending = self._inline_pending
                self._inline_pending = None
                if self._preview_request_current(pending, inline=True):
                    self._open_render(False)
            elif (large and not is_deleted(dialog)
                  and dialog.material_preparation is request):
                dialog.material_preparation = None
                owner = getattr(dialog, 'material_request', None)
                if (dialog.isVisible() and self.records is service
                        and owner and owner['valid']):
                    latest = service.get(record_id)
                    waiting = owner is not request and owner['state'] == 'preparing'
                    stale = latest and render_signature(latest) != owner['signature']
                    if latest and (waiting or stale):
                        # Reconcile the current owner, not the obsolete worker's request.
                        # Selection may reset temporary values after an inline sync.
                        self._open_render(True, dialog=dialog)
            self._prune_render_cache()
        self._task(lambda: prepare_render(record, attachments, temp.name), prepared, service, record_id,
                   finally_callback=finished, kind='render_inputs', error_callback=failed)

    def _prune_render_cache(self):
        # Keep a bounded working set and files used by open inspectors/dialogs.
        from urllib.parse import unquote, urlparse
        while len(self._render_cache) > 16:
            self._render_cache.popitem(last=False)
        payloads = list(self._render_cache.values())
        viewers = [self.details.inline_render]
        viewers += [d.material_viewer for d in self._dialogs
                    if not is_deleted(d) and hasattr(d, 'material_viewer')]
        for viewer in viewers:
            payloads.append(viewer.payload)
            pending = getattr(viewer, '_pending', None)
            if pending is not None:
                payloads.append(pending.payload)
        used = set(self._preparing_directories)
        for payload in payloads:
            for url in (payload or {}).get('maps', {}).values():
                raw = unquote(urlparse(url).path)
                path = Path(raw.lstrip('/') if os.name == 'nt' else raw)
                used.add(str(path.parent))
        retained = []
        for directory in self._render_directories:
            if directory.name in used:
                retained.append(directory)
            else:
                directory.cleanup()
        self._render_directories = retained

    def export_textures(self):
        if len(self.materials.selected_ids)>1:
            self.export_selected(self.materials.selected_ids)
            return
        self.flush_details()
        record = self.records.get(self.selected_id)
        if not record:
            return
        parent = QtWidgets.QFileDialog.getExistingDirectory(self, '选择导出目录（将新建独立文件夹）')
        if not parent:
            return
        root = Path(parent) / ('PrismMaterial_' + record['id'][:8] + '_' + uuid.uuid4().hex[:8])
        attachments = self.window_host.view.scene.attachments
        self._task(lambda: export_originals(record, attachments, root),
                   lambda paths: self.details.operation_status.setText(f'已导出 {len(paths)} 份原文件：{root}'))

    def drag_textures(self, record_ids):
        if not QtWidgets.QApplication.keyboardModifiers() & Qt.KeyboardModifier.AltModifier:
            if getattr(self.window_host.view, '_upgrade_preview_readonly', False):
                return
            if self.store_to_scene() is False:
                return
            from .library_transfer import start_library_drag
            start_library_drag(self.materials, self.service, 'material', record_ids)
            return
        self._drag_external_textures(record_ids)

    def rename_record(self, record_id, name=None):
        from prism.fileio import is_readonly_project
        view = self.window_host.view
        if is_readonly_project(view.scene, getattr(view, 'filename', None)):
            return
        if getattr(self.window_host.view, '_upgrade_preview_readonly', False):
            return
        if self.store_to_scene() is False:
            return
        record = self.records.get(record_id)
        if not record:
            return
        ok = True
        if name is None:
            name, ok = QtWidgets.QInputDialog.getText(self, '重命名材质', '名称：', text=record['name'])
        if ok:
            try:
                self.records.update(record_id, name=name, notes=record.get('notes', ''))
                trace_record('material_renamed', material_id=record_id)
                self.refresh()
            except ValueError as error:
                self.details.operation_status.setText(str(error))

    def _drag_external_textures(self, record_ids):
        if getattr(self.window_host.view, '_upgrade_preview_readonly', False):
            return
        if self._drag_busy or self._drag_active:
            return
        self.flush_details()
        service = self.records
        records = service.list_records(record_ids=record_ids)
        if not records:
            return
        from prism.material_transfer import prepare_texture_transfer, transfer_directory
        self._drag_busy = True
        root = transfer_directory()
        attachments = self.window_host.view.scene.attachments
        self.details.operation_status.setText('正在准备源贴图，请保持左键按住后拖到 UE 内容浏览器…')
        self.details.operation_status.show()
        trace_record('material_drag_requested', materials=len(records))
        gesture = self.materials.mouse_sequence
        def ready(result):
            if service is not self.records:
                return
            if (self.materials.mouse_sequence != gesture
                    or not QtWidgets.QApplication.mouseButtons() & Qt.MouseButton.LeftButton):
                if self.selected_id in record_ids:
                    self.details.operation_status.setText('源贴图已准备好，再次按住卡片拖动即可。')
                trace_record('material_drag_released_before_ready', textures=len(result['paths']))
                return
            self._execute_texture_drag(result)
        self._task(lambda: prepare_texture_transfer(records,attachments,root), ready,
                   service=service, record_id=records[0]['id'],
                   finally_callback=lambda: setattr(self,'_drag_busy',False), kind='external_texture_drag')

    def _execute_texture_drag(self, result):
        from prism.external_drag import start_file_drag
        image = QtGui.QPixmap(180,44)
        image.fill(QtGui.QColor('#242428'))
        painter = QtGui.QPainter(image)
        painter.setPen(QtGui.QColor('#eeeeee'))
        painter.drawText(image.rect(),Qt.AlignmentFlag.AlignCenter,f"{len(result['paths'])} 张原始贴图")
        painter.end()
        self._drag_active = True
        try:
            action = start_file_drag(self.materials,result['paths'],image)
        finally:
            self._drag_active = False
        trace_record('material_drag_finished', action=action.name, textures=len(result['paths']))
        text = f"已交给目标应用：{len(result['paths'])} 张源贴图。" if action == Qt.DropAction.CopyAction else '拖动已取消，或目标应用未接收。'
        if result['renamed']:
            text += f" {result['renamed']} 个重名文件使用了防冲突名称，字节未改变。"
        self.details.operation_status.setText(text)
        self.details.operation_status.show()

    def export_record(self, record_id):
        if self.records.get(record_id):
            self.materials.selected_ids = {record_id}
            self._selection_changed(record_id)
            self.export_textures()

    @observed('material','ui_export')
    def export_selected(self, record_ids):
        self.flush_details()
        records = [record for record in self.records.list_records(record_ids=record_ids)
                   if record.get('files')]
        if not records:
            self.details.operation_status.setText('选中的材质没有已收存的原始贴图。')
            self.details.operation_status.show()
            return
        parent=QtWidgets.QFileDialog.getExistingDirectory(self,'选择批量导出目录')
        if not parent:
            return
        root=Path(parent)/('PrismMaterials_'+uuid.uuid4().hex[:10])
        attachments=self.window_host.view.scene.attachments
        def export():
            count=0
            for record in records:
                name=''.join('_' if c in '<>:"/\\|?*' or ord(c)<32 else c for c in record['name']).strip(' .')[:70]
                destination=root/(name+'_'+uuid.uuid5(uuid.NAMESPACE_OID,record['id']).hex[:8])
                count+=len(export_originals(record,attachments,destination))
            return count
        def done(count):
            self.details.operation_status.setText(f'已导出 {len(records)} 个材质，{count} 份原文件：{root}')
            self.details.operation_status.show()
        self._task(export,done)

    @measured('materials.refresh')
    def refresh(self, *_args):
        from .widget_construction import finish_construction
        change = _args[0] if _args and isinstance(_args[0], dict) else {}
        preview_only = (change.get('operation') == 'preview' and self._bound_service is self.records)
        if not preview_only:
            self._cancel_content_job()
        finish_construction(self._refresh_steps(*_args))
        if self.is_content_ready():
            self.content_ready.emit()

    def _refresh_steps(self, *_args, cooperative=False):
        change = _args[0] if _args and isinstance(_args[0], dict) else {}
        if (change.get('operation') == 'preview' and self._bound_service is self.records):
            updated = self.records.get(change.get('id'))
            if updated:
                self.materials.update_thumbnail(updated, self._thumbnail(updated))
            return
        if self._live_edit:
            record = self.records.get(self.selected_id)
            if record:
                self._auto_key = (id(self.records),record['id'],render_signature(record))
                # Throttle to the newest value each frame; restarting a
                # debounce timer here freezes the preview throughout a drag.
                if not self._auto_timer.isActive():
                    self._auto_timer.start(33)
            return
        self._sync_project()
        if self._edit_timer.isActive() and not self._flushing:
            self.flush_details()
        node = self.service._by_id(self.folder_id)
        if self.folder_id is not None and (
                node is None or node.get('deletedAt') is not None
                or node.get('section') != 'material'):
            self.folder_id = None
            node = None
            self._restore_view()
        self.title.setText(node['title'] if node else '材质库')
        path = self._folder_path(self.folder_id) if node else ''
        self.breadcrumb.setText('材质库' + (' / ' + path if path else ''))
        self.back_button.hide()
        self.search.setPlaceholderText('按名称或说明查找材质（当前文件夹）' if node else
                                       '按名称或说明查找材质（全部材质）')
        revision = self._content_revision()
        scene = self.window_host.view.scene
        service, project_id = self.service, scene.project_id
        def valid():
            return (scene is self.window_host.view.scene
                    and scene.project_id == project_id and service is self.service
                    and revision == self._content_revision())
        state = getattr(self, '_restore_scroll', {})
        state = state if isinstance(state, dict) else {}
        scroll = self.materials.verticalScrollBar().value()
        folder_scroll = self.folders.verticalScrollBar().value()
        folder_item = self.folders.currentItem()
        folder_selected = (folder_item.data(Qt.ItemDataRole.UserRole)
                           if folder_item else state.get('selectedFolderId'))
        self.folders.clear()
        for index, child in enumerate(self.service.listChildren('material', self.folder_id), 1):
            item = QtWidgets.QListWidgetItem(icon('folder'), child['title'])
            item.setData(Qt.ItemDataRole.UserRole, child['id'])
            item.setSizeHint(QtCore.QSize(0, 40))
            self.folders.addItem(item)
            if child['id'] == folder_selected:
                self.folders.setCurrentItem(item)
            if index % 64 == 0:
                yield
                if not valid():
                    return False
        self.folder_toggle.setText(f'子文件夹 · {self.folders.count()}')
        self.folder_toggle.setVisible(self.folders.count() > 0)
        self.folders.setVisible(self.folder_toggle.isChecked() and self.folders.count() > 0)
        records = []
        for batch in self.records.iter_card_record_batches(self.folder_id, self.search.text()):
            records.extend(batch)
            yield
            if not valid():
                return False
        if self.selected_id and not any(record['id'] == self.selected_id for record in records):
            self._reset_preview_adjustments(self.selected_id)
        self.materials.set_context((id(self.records), self.folder_id, self.search.text()))
        if cooperative:
            installed = yield from self.materials.set_records_steps(
                records, self.selected_id, self._card_thumbnail, valid=valid)
            if installed is False:
                return False
        else:
            self.materials.set_records(records, self.selected_id, self._card_thumbnail)
        self.selected_id = self.materials.selected_id
        self.materials.setVisible(bool(records))
        self.folders.doItemsLayout()
        def saved_scroll(key, fallback):
            value = state.get(key, fallback)
            return max(0, value) if isinstance(value, int) else fallback
        self.materials.verticalScrollBar().setValue(saved_scroll('scroll', scroll))
        self.folders.verticalScrollBar().setValue(saved_scroll('folderScroll', folder_scroll))
        self._restore_scroll = {}
        trace_record('material_library_query', folder=self.folder_id or '',
                     searching=bool(self.search.text().strip()),
                     records=len(records),
                     with_thumbnail=sum(1 for record in records
                                        if record.get('preview', {}).get('thumbnailAttachmentId')),
                     empty=not records,
                     subfolders=self.folders.count())
        self.scope_label.setText(('当前文件夹及子文件夹' if node else '全部材质') + f' · {len(records)} 条')
        # 卡片面要等网页文档加载完才有东西可显示。在那之前显示的是
        # "正在加载"，不是一块空白矩形——空白矩形就是用户说的那一下"闪"。
        surface_ready = self.materials.ready
        self.materials.setVisible(bool(records) and surface_ready)
        self.empty_state.setVisible(not records or not surface_ready)
        if records and not surface_ready:
            self.empty_title.setText('正在加载材质…')
        else:
            self.empty_title.setText('没有找到匹配的材质' if self.search.text().strip() else
                                     '这个文件夹还没有内容' if node else
                                     '还没有材质记录' if self.folders.count() else '材质库还没有内容')
        self._show_details()
        self._remember_view()
        self._loaded_revision = (id(self.records), self.records.revision, self.service.revision,
                                 self.folder_id, self.search.text())
        return True

    def _go_up(self):
        node = self.service._by_id(self.folder_id)
        self.folder_requested.emit(node.get('parentId') if node else None)

    def _thumbnail(self, record):
        """The inspector's picture, but only if it is already resident.

        The GUI thread must not read the original bytes here: doing that
        blocked the window for as long as the attachment took to load
        (606 ms on the 100k project) while the user was only switching
        pages.  An uncached picture is loaded by the background loader and
        the inspector is refreshed when it arrives - the same contract the
        card surface already uses.
        """
        if record.get('preview', {}).get('discardedAdjustments'):
            return None
        return self._thumbnail_cache.peek(
            record, self.window_host.view.scene.attachments)

    def _card_thumbnail(self, record):
        """The card's picture, or None while it still has to be loaded.

        A card never decodes on the GUI thread. An uncached picture stays
        pending; the card surface asks for the ones it actually shows, and
        they arrive from the background loader.
        """
        if record.get('preview', {}).get('discardedAdjustments'):
            # The adjustments were discarded, so the old picture must not
            # come back -- not even from the cache.
            return None
        return self._thumbnail_cache.peek(
            record, self.window_host.view.scene.attachments)

    def _load_card_thumbnail(self, record_id):
        record = self.records.get(record_id)
        if not record:
            return
        preview = record.get('preview', {})
        key = (id(self.records), record_id, preview.get('signature'), preview.get('thumbnailAttachmentId'))
        if key in self._thumbnail_pending:
            return
        self._thumbnail_pending.add(key)
        context = self.materials._context
        attachments = self.window_host.view.scene.attachments
        # The cache is the one that was current when the card asked. It is
        # replaced when the project changes, and the result is then stale
        # anyway -- but the bytes it holds stay valid, because the key is the
        # attachment's own content.
        cache = self._thumbnail_cache
        def done(image):
            current = self.records.get(record_id)
            if current and current.get('preview', {}).get('signature') == key[2] and image:
                self.materials.update_thumbnail(current, image)
        self._task(lambda: cache.get(record, attachments, material_thumbnail), done,
                   record_id=record_id,
                   finally_callback=lambda: self._thumbnail_pending.discard(key),
                   obsolete=lambda: self.materials._context != context, kind='card_thumbnail')

    def _load_detail_thumbnail(self, record_id):
        """Fetch the inspector picture off the GUI thread, then refresh it.

        The card surface asks for its own pictures through the same loader,
        so a picture the cards already pulled is found resident and this
        request is skipped entirely.
        """
        record = self.records.get(record_id)
        if not record:
            return
        preview = record.get('preview', {})
        key = (id(self.records), record_id, preview.get('signature'),
               preview.get('thumbnailAttachmentId'))
        if key in self._thumbnail_pending:
            return
        self._thumbnail_pending.add(key)
        attachments = self.window_host.view.scene.attachments
        cache = self._thumbnail_cache
        context = self.materials._context

        def done(image):
            if not image or self.selected_id != record_id:
                return
            # 只在图真的查得到的时候才刷新详情。附件的驻留缓存是有上限的，
            # 万一它刚被挤掉，这里再刷一次就变成"排队→再排队"的死循环。
            current = self.records.get(record_id)
            if current is None or self._thumbnail(current) is None:
                return
            self._show_details()

        self._task(lambda: cache.get(record, attachments, material_thumbnail), done,
                   record_id=record_id,
                   finally_callback=lambda: self._thumbnail_pending.discard(key),
                   obsolete=lambda: self.materials._context != context,
                   kind='detail_thumbnail')

    def _surface_ready(self):
        """The card surface finished loading: show the cards, drop the
        loading state.

        Until the web document is there the panel shows "正在加载材质…".
        A blank rectangle in the middle of the window is what the user
        called the flash, so the cards only become visible once they can
        actually show something.
        """
        try:
            if not self.records:
                return
        except (AttributeError, RuntimeError):
            return
        if not self.is_content_ready():
            return
        populated = bool(self.materials.assets)
        self.materials.setVisible(populated)
        self.empty_state.setVisible(not populated)
        self.content_ready.emit()

    def open_performance_logs(self):
        from PyQt6.QtGui import QDesktopServices
        from prism.config import logfile_name
        directory = Path(logfile_name()).parent
        trace_record('diagnostic_folder_opened', directory=str(directory))
        QDesktopServices.openUrl(QtCore.QUrl.fromLocalFile(str(directory)))
