"""Project material records, folder navigation and per-folder view state."""
import copy
import tempfile
import uuid
import re
import os
from collections import OrderedDict
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
from prism.performance_trace import measured, record as trace_record
from prism.material_cache import MaterialCache, MaterialThumbnailCache
from prism.item_lifecycle import is_deleted


class MaterialLibrary(QtWidgets.QWidget):
    folder_requested = QtCore.pyqtSignal(object)
    create_folder_requested = QtCore.pyqtSignal(object)

    def __init__(self, window):
        super().__init__(window)
        self.window_host = window
        self.folder_id = None
        self.selected_id = None
        self._bound_service = None
        self._views = {}
        self._flushing = False
        self._live_edit = False
        self.setObjectName('materialLibrary')
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.details = MaterialDetails()
        self._jobs = set()
        self._worker_pool = QtCore.QThreadPool(self)
        self._worker_pool.setMaxThreadCount(2)
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
        self._adjustment_default = None
        self._drag_busy = False
        self._drag_active = False
        self._auto_timer = QtCore.QTimer(self)
        self._auto_timer.setSingleShot(True)
        self._auto_timer.setInterval(120)
        self._auto_timer.timeout.connect(self.update_preview)
        self.details.slot_file_requested.connect(self.import_slot)
        self.details.slot_channel_requested.connect(self.change_channel)
        self.details.slot_clear_requested.connect(self.clear_slot)
        self.details.slot_reuse_requested.connect(self.reuse_slot)
        self.details.slot_options_requested.connect(self.change_slot_options)
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
        self.materials = MaterialCards(self)
        self.materials.setObjectName('materialLibraryRecords')
        self.materials.selection_changed.connect(self._selection_changed)
        self.materials.open_requested.connect(self.open_preview)
        self.materials.export_requested.connect(self.export_record)
        self.materials.batch_export_requested.connect(self.export_selected)
        self.materials.scroll_changed.connect(self._remember_view)
        self.materials.thumbnail_requested.connect(self._load_card_thumbnail)
        self.materials.textures_drag_requested.connect(self.drag_textures)
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
        self._thumbnail_cache = MaterialThumbnailCache()
        self._adjustment_default = None
        self._icon_cache.clear()
        self._render_cache.clear()
        self._bound_service = None
        self.selected_id = None
        self.details.show_record(None)
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

    def open_folder(self, folder_id=None):
        self._sync_project()
        self.flush_details()
        self._remember_view()
        if folder_id is not None:
            self.records._validate_folder(folder_id)
        if self.folder_id != folder_id:
            self._reset_preview_adjustments(self.selected_id)
            self.folder_id = folder_id
            self._restore_view()
        self.refresh()

    def reveal_material(self, record_id, *, from_search=False):
        """Shared navigation target for both existing project-search surfaces."""
        record = self.records.get(record_id)
        if not record or record.get('deletedAt') is not None or not self.records._folder_visible(record.get('folderId')):
            return False
        folder_id = record.get('folderId')
        if folder_id is not None and self.service._by_id(folder_id) is None:
            folder_id = None
        self.window_host.open_material_library(folder_id, from_search=from_search)
        self.search.clear()
        self._selection_changed(record_id)
        self.materials.reveal_record(record_id)
        return True

    def load_from_scene(self):
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
        self.flush_details()
        if record_id != self.selected_id:
            self._reset_preview_adjustments(self.selected_id)
        self.selected_id = record_id
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
        thumbnail = self._thumbnail(record) if record else None
        self.details.show_record(record, self._folder_path(record.get('folderId')) if record else '', thumbnail)
        if record:
            self.details.export_button.setEnabled(bool(record.get('files')) or len(self.materials.selected_ids)>1)
            self._slot_icons(record)
            key = (id(self.records), record['id'], render_signature(record))
            if key != self._auto_key:
                self._auto_key = key
                self._auto_timer.start()
        else:
            self._auto_timer.stop()
            self._auto_key = None

    def showEvent(self, event):
        super().showEvent(event)
        self._show_details()

    def hideEvent(self, event):
        self._auto_timer.stop()
        super().hideEvent(event)

    def _task(self, function, callback, service=None, record_id=None, finally_callback=None, obsolete=None, kind='operation'):
        service = service or self.records
        record_id = record_id or self.selected_id
        job = MaterialTask(function, obsolete)
        task_id = uuid.uuid4().hex[:12]
        trace_record('material_task_queued', task=task_id, material=record_id, task_kind=kind, jobs=len(self._jobs)+1)
        job.task_id = task_id
        self._jobs.add(job)
        def current():
            return not is_deleted(self) and self.records is service and service.get(record_id) is not None
        def done(value):
            trace_record('material_task_result', task=task_id, material=record_id, current=current())
            if current():
                try:
                    callback(value)
                except Exception as exc:
                    error(str(exc))
        def error(message):
            trace_record('material_task_error', task=task_id, material=record_id, message=message)
            if current() and self.selected_id == record_id:
                self.details.operation_status.setText('操作失败：' + message)
        job.signals.result.connect(done)
        job.signals.error.connect(error)
        def finished():
            self._jobs.discard(job)
            trace_record('material_task_finished', task=task_id, remaining=len(self._jobs))
            if finally_callback and not is_deleted(self):
                finally_callback()
        job.signals.finished.connect(finished)
        self._worker_pool.start(job)

    def import_slot(self, slot, path):
        self.flush_details()
        service, record_id = self.records, self.selected_id
        if not service.get(record_id):
            return
        request = uuid.uuid4().hex
        key = (id(service), record_id, slot)
        self._slot_requests[key] = request
        self.details.operation_status.setText('正在收存原始贴图…')
        def commit(prepared):
            if self._slot_requests.get(key) != request:
                return
            service.configure(record_id, slot=slot, prepared=prepared, channel='RGB')
            if self.selected_id == record_id:
                self.details.operation_status.setText('原文件已完整收存，请检查下面的通道选择。')
        self._task(lambda: prepare_texture(path), commit, service, record_id, kind='original_import')

    def change_channel(self, slot, channel):
        if self.records.get(self.selected_id):
            self.records.configure(self.selected_id, slot=slot, channel=channel)

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
        current = self.records.get(record_id)
        if not current:
            return
        props = current.get('preview', {}).get('properties', {})
        if not props.get('slots') and not any(key in props for key in ('flipX', 'flipY', 'alphaCutoff')):
            return
        self._prepare_token = None
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
        if not self.isVisible():
            return
        self._open_render(False)

    def open_preview(self, record_id=None):
        if isinstance(record_id, str) and self.records.get(record_id):
            self._selection_changed(record_id)
        self._open_render(True)

    @measured('materials.open_render')
    def _open_render(self, large):
        self.flush_details()
        record = self.records.get(self.selected_id)
        if not record:
            return
        service = self.records
        record_id = record['id']
        if not large:
            self._auto_timer.stop()
            self._auto_key = (id(service), record_id, render_signature(record))
        token = uuid.uuid4().hex
        if not large:
            self._prepare_token = token
        attachments = self.window_host.view.scene.attachments
        self.details.operation_status.setText('正在准备渲染，原文件保持不变…')
        self.details.operation_status.show()
        def show(payload):
            if render_signature(service.get(record_id) or {}) != payload['signature']:
                return
            if not large:
                if self.selected_id != record_id or self._prepare_token != token or not self.isVisible():
                    return
                for dialog in list(self._dialogs):
                    if getattr(dialog, 'material_service', None) is service and getattr(dialog, 'material_id', None) == record_id:
                        dialog.material_viewer.load_material(payload, uuid.uuid4().hex)
                viewer = self.details.inline_render
                self.details.preview.hide()
                viewer.show()
                # Replace the previous request's callbacks without retaining records.
                try:
                    viewer.completed.disconnect()
                    viewer.failed.disconnect()
                except TypeError:
                    pass
                def inline_done(signature, blob):
                    if self.records is service and service.save_preview(record_id, signature, blob):
                        if self.selected_id == record_id:
                            self.details.operation_status.clear()
                            self.details.operation_status.hide()
                            self.details.preview_status.setText('自动更新 · 已同步到材质卡片')
                            self.details.render_button.hide()
                props = payload['properties']
                mode_label = {'opaque':'不透明', 'masked':'透明裁切', 'translucent':'半透明'}[props['blendMode']]
                prefix = '自动：' if record.get('preview', {}).get('properties', {}).get('blendMode', 'auto') == 'auto' else '手动覆盖：'
                self.details.auto_mode.setText(prefix + mode_label)
                self.details.auto_mode.setToolTip(props.get('autoBlendReason', ''))
                viewer.completed.connect(inline_done)
                def updated(signature):
                    if self.selected_id == record_id:
                        self.details.preview_status.setText('实时预览 · 正在同步卡片')
                        if self.details.operation_status.text().startswith('正在准备渲染'):
                            self.details.operation_status.clear()
                            self.details.operation_status.hide()
                try:
                    viewer.updated.disconnect()
                except TypeError:
                    pass
                viewer.updated.connect(updated)
                def failed(error):
                    if self.selected_id == record_id:
                        self.details.operation_status.setText('渲染失败：' + error)
                        self.details.operation_status.show()
                        self.details.render_button.show()
                        viewer.hide()
                        self.details.preview.show()
                viewer.failed.connect(failed)
                viewer.load_material(payload, token)
                return
            dialog = QtWidgets.QDialog(self)
            dialog.setWindowTitle(record['name'] + ' · 材质预览')
            dialog.resize(1000 if large else 620, 780 if large else 560)
            layout = QtWidgets.QVBoxLayout(dialog)
            toolbar = QtWidgets.QHBoxLayout()
            modes = QtWidgets.QComboBox()
            modes.addItem('完整材质', 'full')
            for key, title in SLOTS.items():
                modes.addItem(title, key)
            toolbar.addWidget(modes)
            front = QtWidgets.QPushButton('回到正面')
            toolbar.addWidget(front)
            toolbar.addStretch(1)
            close = QtWidgets.QPushButton('返回材质库')
            toolbar.addWidget(close)
            layout.addLayout(toolbar)
            viewer = MaterialRender(dialog)
            viewer.capture_enabled = False
            dialog.material_service = service
            dialog.material_id = record_id
            dialog.material_viewer = viewer
            layout.addWidget(viewer, 1)
            state = QtWidgets.QLabel('正在生成预览…')
            state.setWordWrap(True)
            layout.addWidget(state)
            viewer.load_material(payload, token)
            modes.currentIndexChanged.connect(lambda: viewer.view_channel(modes.currentData()))
            shapes = QtWidgets.QComboBox()
            for text, key in [('平面','plane'),('球体','sphere'),('圆柱体','cylinder'),('立方体','box')]:
                shapes.addItem(text,key)
            toolbar.insertWidget(1, shapes)
            shapes.currentIndexChanged.connect(lambda: viewer.set_shape(shapes.currentData()))
            front.clicked.connect(viewer.reset_camera)
            close.clicked.connect(dialog.close)
            def complete(signature, blob):
                if self.records is service and render_signature(service.get(record_id) or {}) == signature:
                    state.setText('已同步最新配置。拖动旋转，滚轮缩放；通道查看不修改材质。')
                else:
                    state.setText('配置已改变，此预览未覆盖新配置。')
            viewer.completed.connect(complete)
            viewer.updated.connect(lambda signature: complete(signature,b''))
            viewer.failed.connect(lambda message: state.setText('渲染失败：' + message))
            dialog.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
            self._dialogs.append(dialog)
            dialog.destroyed.connect(lambda: self._dialogs.remove(dialog) if dialog in self._dialogs else None)
            dialog.destroyed.connect(lambda _object=None: self._prune_render_cache())
            dialog.show()
            self.details.operation_status.clear()
        cache_key = (id(service), record_id, texture_signature(record))
        cached = self._render_cache.get(cache_key)
        if cached:
            self._render_cache.move_to_end(cache_key)
        trace_record('material_prepare', material=record_id, token=token, cached=bool(cached), large=large)
        if cached:
            payload = copy.deepcopy(cached)
            old = payload['properties']
            payload['properties'] = resolved_properties(record.get('preview', {}).get('properties', {}),
                payload['maps'], old['sourceAutoBlendMode'], old.get('sourceAutoBlendReason', ''))
            payload['signature'] = render_signature(record)
            show(payload)
        else:
            temp = tempfile.TemporaryDirectory(prefix='prism-material-render-')
            self._render_directories.append(temp)
            self._preparing_directories.add(temp.name)
            def prepared(payload):
                self._preparing_directories.discard(temp.name)
                self._render_cache[cache_key] = payload
                show(payload)
            def finished():
                self._preparing_directories.discard(temp.name)
                self._prune_render_cache()
            self._task(lambda: prepare_render(record, attachments, temp.name), prepared, service, record_id,
                       finally_callback=finished,
                       obsolete=None if large else lambda: self._prepare_token != token or self.selected_id != record_id,
                       kind='render_inputs')

    def _prune_render_cache(self):
        # Keep a bounded working set and files used by open inspectors/dialogs.
        from urllib.parse import unquote, urlparse
        while len(self._render_cache) > 16:
            self._render_cache.popitem(last=False)
        payloads = list(self._render_cache.values()) + [self.details.inline_render.payload]
        payloads += [d.material_viewer.payload for d in self._dialogs if hasattr(d, 'material_viewer')]
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
        if self._drag_busy or self._drag_active:
            return
        self.flush_details()
        service = self.records
        records = [record for record in service.list_records() if record['id'] in record_ids]
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

    def export_selected(self, record_ids):
        self.flush_details()
        records = [record for record in self.records.list_records()
                   if record['id'] in record_ids and record.get('files')]
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
        state = getattr(self, '_restore_scroll', {})
        state = state if isinstance(state, dict) else {}
        scroll = self.materials.verticalScrollBar().value()
        folder_scroll = self.folders.verticalScrollBar().value()
        folder_item = self.folders.currentItem()
        folder_selected = (folder_item.data(Qt.ItemDataRole.UserRole)
                           if folder_item else state.get('selectedFolderId'))
        self.folders.clear()
        for child in self.service.listChildren('material', self.folder_id):
            item = QtWidgets.QListWidgetItem(icon('folder'), child['title'])
            item.setData(Qt.ItemDataRole.UserRole, child['id'])
            item.setSizeHint(QtCore.QSize(0, 40))
            self.folders.addItem(item)
            if child['id'] == folder_selected:
                self.folders.setCurrentItem(item)
        self.folder_toggle.setText(f'子文件夹 · {self.folders.count()}')
        self.folder_toggle.setVisible(self.folders.count() > 0)
        self.folders.setVisible(self.folder_toggle.isChecked() and self.folders.count() > 0)
        records = self.records.list_records(self.folder_id, self.search.text())
        if self.selected_id and not any(record['id'] == self.selected_id for record in records):
            self._reset_preview_adjustments(self.selected_id)
        self.materials.set_context((id(self.records), self.folder_id, self.search.text()))
        self.materials.set_records(
            records, self.selected_id,
            self._card_thumbnail)
        self.selected_id = self.materials.selected_id
        self.materials.setVisible(bool(records))
        self.folders.doItemsLayout()
        def saved_scroll(key, fallback):
            value = state.get(key, fallback)
            return max(0, value) if isinstance(value, int) else fallback
        self.materials.verticalScrollBar().setValue(saved_scroll('scroll', scroll))
        self.folders.verticalScrollBar().setValue(saved_scroll('folderScroll', folder_scroll))
        self._restore_scroll = {}
        self.scope_label.setText(('当前文件夹及子文件夹' if node else '全部材质') + f' · {len(records)} 条')
        self.materials.setVisible(bool(records))
        self.empty_state.setVisible(not records)
        self.empty_title.setText('没有找到匹配的材质' if self.search.text().strip() else
                                 '这个文件夹还没有内容' if node else
                                 '还没有材质记录' if self.folders.count() else '材质库还没有内容')
        self._show_details()
        self._remember_view()

    def _go_up(self):
        node = self.service._by_id(self.folder_id)
        self.folder_requested.emit(node.get('parentId') if node else None)

    def _thumbnail(self, record):
        if record.get('preview', {}).get('discardedAdjustments'):
            return None
        return self._thumbnail_cache.get(record, self.window_host.view.scene.attachments, material_thumbnail)

    def _card_thumbnail(self, record):
        attachments = self.window_host.view.scene.attachments
        peek = getattr(attachments, 'peek', None)
        key = record.get('preview', {}).get('thumbnailAttachmentId')
        if peek is not None and peek(key) is None:
            return None
        return self._thumbnail(record)

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
        def done(image):
            current = self.records.get(record_id)
            if current and current.get('preview', {}).get('signature') == key[2] and image:
                self.materials.update_thumbnail(current, image)
        self._task(lambda: material_thumbnail(record, attachments), done, record_id=record_id,
                   finally_callback=lambda: self._thumbnail_pending.discard(key),
                   obsolete=lambda: self.materials._context != context, kind='card_thumbnail')

    def open_performance_logs(self):
        from PyQt6.QtGui import QDesktopServices
        from prism.config import logfile_name
        directory = Path(logfile_name()).parent
        trace_record('diagnostic_folder_opened', directory=str(directory))
        QDesktopServices.openUrl(QtCore.QUrl.fromLocalFile(str(directory)))
