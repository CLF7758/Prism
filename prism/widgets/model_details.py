"""Model inspector. Reuses the existing texture controls; no second slot system.

The panel shows what the project actually knows about one model: its format,
the size of the original bytes, the statistics the backend reported together
with the basis those statistics were measured on, the dependency files and the
existing tag / note fields.

Two rules shape the presentation:

* **Unknown is not zero.** A missing statistic reads "未取得" (not obtained),
  and it keeps the basis it would have been measured on, so the same number
  is never shown with two different meanings.
* **A cell count is not a triangle count.** The native backend may report
  cells, polygons or triangles. The panel shows the backend's own number and
  names the basis; it never converts one into another.
"""
import base64

from PyQt6 import QtCore, QtGui, QtWidgets
from PyQt6.QtCore import Qt

from prism.material_textures import SLOTS
from prism.model_library import appearance as appearance_adapter
from prism.model_library.contracts import ModelAppearance, ModelCapabilities
from prism.widgets.material_slots import MaterialSlot

#: How long the metadata fields wait before they are submitted.
METADATA_DEBOUNCE_MS = 600

#: Text used whenever a value is genuinely unknown. Distinct from the
#: em dash, which means "no model is selected at all".
NOT_OBTAINED = '未取得'


def _decode_data_url(data_url):
    """Decode a cached image data URL into bytes, or return ``None``."""
    if not isinstance(data_url, str) or ',' not in data_url:
        return None
    header, _, payload = data_url.partition(',')
    try:
        if 'base64' in header:
            return base64.b64decode(payload)
        from urllib.parse import unquote_to_bytes
        return unquote_to_bytes(payload)
    except (ValueError, TypeError):
        return None


class ModelDetails(QtWidgets.QWidget):
    open_requested = QtCore.pyqtSignal()
    export_requested = QtCore.pyqtSignal()
    apply_material_requested = QtCore.pyqtSignal()
    #: The user changed a slot value. Payload is the detached appearance.
    appearance_changed = QtCore.pyqtSignal(object)
    #: The user asked for a texture in a slot. Payload is (slot key, path).
    texture_requested = QtCore.pyqtSignal(str, str)
    #: The user asked to reuse an already attached original for a slot.
    reuse_requested = QtCore.pyqtSignal(str)
    #: The user cleared a slot binding.
    slot_cleared = QtCore.pyqtSignal(str)
    #: Metadata edit submitted. Payload is (tags, notes, expected revision).
    metadata_changed = QtCore.pyqtSignal(object, str, object)
    rename_requested = QtCore.pyqtSignal(str, str)
    #: The user asked to apply a material from the material library.
    material_apply_requested = QtCore.pyqtSignal()

    def __init__(self, parent=None, *, legacy_slots=True):
        from .widget_construction import finish_construction
        finish_construction(self._construct(parent, legacy_slots=legacy_slots))

    def _construct(self, parent=None, *, legacy_slots=True):
        from .widget_construction import widget_steps
        super().__init__(parent)
        self.setObjectName('modelDetails')
        self._record = None
        self._capabilities = ModelCapabilities()
        self._appearance = ModelAppearance()
        self._session = appearance_adapter.AppearanceSession()
        self._slot_policy = appearance_adapter.resolve_slot_policy()
        self._cover_pending = False
        self._metadata_timer = QtCore.QTimer(self)
        self._metadata_timer.setSingleShot(True)
        self._metadata_timer.setInterval(METADATA_DEBOUNCE_MS)
        self._metadata_timer.timeout.connect(self._submit_metadata)
        self._pending_tags = None
        self._pending_notes = None

        layout = QtWidgets.QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        scroll = QtWidgets.QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QtWidgets.QFrame.Shape.NoFrame)
        layout.addWidget(scroll)
        body = QtWidgets.QWidget()
        scroll.setWidget(body)
        form = QtWidgets.QVBoxLayout(body)
        form.setContentsMargins(16, 18, 16, 16)
        form.setSpacing(12)
        self.title = QtWidgets.QLineEdit()
        self.title.setPlaceholderText('模型名称')
        self.title.setMaxLength(160)
        self.title.setReadOnly(True)
        self.title.editingFinished.connect(self._rename_finished)
        form.addWidget(self.title)
        self.preview = QtWidgets.QLabel('模型封面\n选择模型后显示当前材质效果')
        self.preview.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.preview.setMinimumHeight(190)
        self.preview.setStyleSheet(
            'background:#202023;border:1px solid #35353b;border-radius:8px;color:#929ba8;')
        self.preview.setParent(self)
        self.preview.hide()
        self.preview_status = QtWidgets.QLabel('预览尚未接入')
        self.preview_status.setProperty('uiRole', 'secondary')
        self.preview_status.setParent(self)
        self.preview_status.hide()
        self.open_button = QtWidgets.QPushButton('打开大预览')
        self.export_button = QtWidgets.QPushButton('导出模型与贴图')
        for button in (self.open_button, self.export_button):
            button.setEnabled(False)
            button.setToolTip('此功能尚未接入')
            button.setParent(self)
            button.hide()
        self.open_button.clicked.connect(self.open_requested.emit)
        self.export_button.clicked.connect(self.export_requested.emit)
        self.sections = QtWidgets.QTabWidget()
        form.addWidget(self.sections, 1)
        slot_page = QtWidgets.QWidget()
        self.sections.addTab(slot_page, '材质')
        slot_layout = QtWidgets.QVBoxLayout(slot_page)
        from .model_material_panel import ModelMaterialPanel
        yield
        self.material_panel = yield from widget_steps(ModelMaterialPanel, slot_page)
        slot_layout.addWidget(self.material_panel, 1)
        legacy_notice = QtWidgets.QLabel('现有全模型外观（兼容区域，作用于整个模型）')
        legacy_notice.setWordWrap(True)
        legacy_notice.setProperty('uiRole', 'secondary')
        slot_layout.addWidget(legacy_notice)
        self.legacy_notice = legacy_notice
        self._multimaterial_owned = False
        self.apply_button = QtWidgets.QPushButton('应用已有材质')
        self.apply_button.setEnabled(False)
        self.apply_button.setToolTip('等待模型外观适配与项目保存接入')
        self.apply_button.clicked.connect(self.material_apply_requested.emit)
        slot_layout.addWidget(self.apply_button)
        self.slot_notice = QtWidgets.QLabel('')
        self.slot_notice.setWordWrap(True)
        self.slot_notice.setProperty('uiRole', 'secondary')
        self.slot_notice.hide()
        slot_layout.addWidget(self.slot_notice)
        self.slots = {}
        self._legacy_slot_layout = slot_layout
        if legacy_slots:
            self._ensure_legacy_slots()
        info_page = QtWidgets.QWidget()
        self.sections.addTab(info_page, '版本与文件')
        yield
        info_layout = QtWidgets.QVBoxLayout(info_page)
        info_form = QtWidgets.QFormLayout()
        self.info = {}
        for key, label in (
            ('format', '格式'), ('size', '原件大小'), ('triangles', '三角形'),
            ('vertices', '顶点'), ('basis', '统计口径'), ('revision', '原件版本'),
            ('dependencies', '依赖状态'),
        ):
            value = QtWidgets.QLabel('—')
            value.setWordWrap(True)
            self.info[key] = value
            info_form.addRow(label, value)
            yield
        info_layout.addLayout(info_form)
        info_layout.addWidget(QtWidgets.QLabel('模型与依赖文件'))
        self.files = QtWidgets.QListWidget()
        self.files.setMinimumHeight(100)
        self.files.setEnabled(False)
        info_layout.addWidget(self.files)
        hint = QtWidgets.QLabel('导出仅取回原件；库内贴图配置随项目保存。')
        hint.setWordWrap(True)
        hint.setProperty('uiRole', 'secondary')
        info_layout.addWidget(hint)
        info_layout.addStretch()
        metadata = QtWidgets.QFormLayout()
        self.tags = QtWidgets.QLineEdit()
        self.tags.setPlaceholderText('使用现有项目标签系统')
        self.tags.setReadOnly(True)
        self.notes = QtWidgets.QPlainTextEdit()
        self.notes.setPlaceholderText('模型备注')
        self.notes.setMaximumHeight(110)
        self.notes.setReadOnly(True)
        metadata.addRow('标签', self.tags)
        metadata.addRow('备注', self.notes)
        info_layout.insertLayout(info_layout.count() - 1, metadata)
        self.metadata_status = QtWidgets.QLabel('')
        self.metadata_status.setProperty('uiRole', 'secondary')
        self.metadata_status.hide()
        info_layout.insertWidget(info_layout.count() - 1, self.metadata_status)
        self.tags.textEdited.connect(self._tags_edited)
        self.notes.textChanged.connect(self._notes_edited)

    # ------------------------------------------------------------------
    # Loading
    # ------------------------------------------------------------------
    def show_summary(self, summary=None):
        """Presentation only. Full files/notes require repository.get later."""
        self._record = None
        if summary is None or self.material_panel.model_id != summary.model_id:
            self.material_panel.finish_edit()
            self.material_panel.reset(summary.model_id if summary else None)
        self._session = appearance_adapter.AppearanceSession()
        self._appearance = ModelAppearance()
        self.title.setText(summary.name if summary else '模型资料')
        self.tags.setText(', '.join(summary.tags) if summary else '')
        self.notes.blockSignals(True)
        self.notes.clear()
        self.notes.blockSignals(False)
        self._pending_tags = None
        self._pending_notes = None
        self._metadata_timer.stop()
        # A summary is not an editable record, including while an async
        # metadata read is pending. Reapply the existing capability policy.
        self.set_capabilities(self._capabilities, self._slot_policy)
        for label in self.info.values():
            label.setText('—')
        self.files.clear()
        self.files.setEnabled(False)
        self.preview.clear()
        self.preview.setText('模型封面\n选择模型后显示当前材质效果')
        self.preview_status.setText('请选择模型' if summary is None else '封面尚未生成')
        self._cover_pending = False
        if summary is None:
            return
        self.info['format'].setText(summary.format.upper())
        self.info['size'].setText(
            f'{summary.size_bytes:,} 字节' if summary.size_bytes is not None else NOT_OBTAINED)
        for key in ('triangles', 'vertices'):
            value = getattr(summary.statistics, key)
            self.info[key].setText(f'{value:,}' if value is not None else NOT_OBTAINED)
        self.info['basis'].setText(summary.statistics.basis)

    def show_record(self, record, cached_cover=None, capabilities=None):
        """Show a complete model record.

        ``record`` must be the full ``ModelRecord`` from the repository. A
        card summary is never enough: the appearance, the dependency files
        and the note text only exist on the record.
        """
        self._flush_metadata()
        self._record = record
        if record is None or self.material_panel.model_id != record.model_id:
            self.material_panel.finish_edit()
            self.material_panel.reset(record.model_id if record else None)
        if capabilities is not None:
            self.set_capabilities(capabilities)
        else:
            self.set_capabilities(self._capabilities, self._slot_policy)
        if record is None:
            self.show_summary(None)
            return
        self.title.setText(record.name)
        self.tags.setText(', '.join(record.tags))
        self.notes.blockSignals(True)
        self.notes.setPlainText(record.notes)
        self.notes.blockSignals(False)
        self._pending_tags = None
        self._pending_notes = None
        self.info['format'].setText((record.format or '').upper() or NOT_OBTAINED)
        self.info['revision'].setText(str(record.source_revision))
        # The size of the original bytes is the size of the *main* file. A
        # dependency does not stand in for it, and a missing number is not 0.
        main = next((entry for entry in record.files
                     if entry.file_id == record.main_file_id), None)
        self.info['size'].setText(
            f'{main.size_bytes:,} 字节' if main is not None and main.size_bytes
            else NOT_OBTAINED)
        statistics = record.statistics
        self.info['triangles'].setText(
            f'{statistics.triangles:,}' if statistics.triangles is not None else NOT_OBTAINED)
        self.info['vertices'].setText(
            f'{statistics.vertices:,}' if statistics.vertices is not None else NOT_OBTAINED)
        # The basis travels with the numbers. A backend that reports cells
        # must not have its cell count presented as a triangle count.
        self.info['basis'].setText(statistics.basis or NOT_OBTAINED)
        self.info['dependencies'].setText(self._dependency_text(record))
        self.files.clear()
        for entry in record.files:
            size = f'{entry.size_bytes:,} 字节' if entry.size_bytes else NOT_OBTAINED
            name = entry.original_reference if entry.missing else entry.relative_path
            state = '缺失' if entry.missing else entry.role
            self.files.addItem(f'{name} · {state} · {size}')
        self.files.setEnabled(True)
        self._appearance = appearance_adapter.appearance_from_record(record)
        self._session = appearance_adapter.AppearanceSession(
            self._appearance, supported=self._slot_policy)
        self._load_slots()
        self._show_cover(cached_cover)

    def _rename_finished(self):
        record = self._record
        if record is not None and self.title.text() != record.name:
            self.rename_requested.emit(record.model_id, self.title.text())

    def _ensure_legacy_slots(self):
        if self.slots:
            return
        for key, title in SLOTS.items():
            slot = MaterialSlot(key, title)
            slot.setEnabled(False)
            slot.setToolTip('复用现有贴图控件；模型后端能力尚未接入')
            self.slots[key] = slot
            self._legacy_slot_layout.addWidget(slot)
            slot.options_requested.connect(self._slot_options)
            slot.channel_requested.connect(self._slot_channel)
            slot.clear_requested.connect(self._slot_cleared)
            slot.reuse_requested.connect(self.reuse_requested.emit)
            slot.file_requested.connect(self.texture_requested.emit)

    def set_multimaterial_owned(self, owned):
        self._multimaterial_owned = bool(owned)
        for widget in (self.legacy_notice, self.apply_button, self.slot_notice, *self.slots.values()):
            widget.setVisible(not owned)
        self.set_capabilities(self._capabilities, self._slot_policy)

    def set_capabilities(self, capabilities, slot_policy=None):
        """Apply the real capability set; unavailable operations stay disabled.

        ``slot_policy`` carries the slots the native runtime has verified. It
        is a plain mapping of slot key to blocking reason; an empty reason
        means the slot is usable.
        """
        self._capabilities = capabilities or ModelCapabilities()
        self.title.setReadOnly(not self._capabilities.edit_metadata or self._record is None)
        policy = appearance_adapter.resolve_slot_policy(
            slot_policy if slot_policy is not None
            else getattr(self._capabilities, 'appearance_slots', None))
        self._slot_policy = policy
        editable = (self._record is not None and
                    bool(getattr(self._capabilities, 'edit_appearance', False)) and not self._multimaterial_owned)
        if editable:
            self._ensure_legacy_slots()
        reasons = []
        for key, slot in self.slots.items():
            reason = policy.get(key, '')
            if reason:
                slot.setEnabled(False)
                slot.setToolTip(reason)
                reasons.append(f'{SLOTS[key]}：{reason}')
            elif not editable:
                slot.setEnabled(False)
                slot.setToolTip('模型外观保存尚未接入；控件保持只读')
            else:
                slot.setEnabled(True)
                slot.setToolTip('复用现有贴图控件')
        if reasons:
            self.slot_notice.setText('以下通道在原生运行时未验证，已禁用：'
                                     + '；'.join(reasons))
            self.slot_notice.show()
        else:
            self.slot_notice.hide()
        self.apply_button.setEnabled(editable and self._record is not None)
        if self._multimaterial_owned:
            self.slot_notice.hide()
        self.apply_button.setToolTip(
            '复用材质库中的材质配置' if editable else '等待模型外观适配与项目保存接入')
        metadata_editable = (self._record is not None and
                             bool(getattr(self._capabilities, 'edit_metadata', False)))
        self.tags.setReadOnly(not metadata_editable)
        self.notes.setReadOnly(not metadata_editable)
        if not metadata_editable:
            self.tags.setToolTip('模型元数据保存尚未接入；字段保持只读')
            self.notes.setToolTip('模型元数据保存尚未接入；字段保持只读')
        else:
            self.tags.setToolTip('')
            self.notes.setToolTip('')

    def _load_slots(self):
        if self._record is not None and not self._multimaterial_owned:
            self._ensure_legacy_slots()
        for key, slot in self.slots.items():
            # ``MaterialSlot.show_binding`` expects a mapping: the material
            # library passes ``textures.get(key, {})`` for an empty slot. Pass
            # the same empty mapping here instead of ``None`` so a model
            # without a texture in this channel does not crash the inspector.
            binding = appearance_adapter.slot_binding(self._appearance, key) or {}
            properties = appearance_adapter.appearance_properties(self._appearance)
            slot.show_binding(binding, properties)

    def _show_cover(self, cached_cover):
        blob = _decode_data_url(cached_cover)
        if blob:
            image = QtGui.QImage.fromData(blob)
            if not image.isNull():
                pixmap = QtGui.QPixmap.fromImage(image)
                self.preview.setPixmap(pixmap.scaled(
                    self.preview.width() or 320, self.preview.height() or 190,
                    Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation))
                self.preview.setText('')
                if not self._cover_pending:
                    self.preview_status.setText('模型封面')
                return
        self.preview.setPixmap(QtGui.QPixmap())
        self.preview.setText('模型封面\n封面尚未生成')
        if not self._cover_pending:
            self.preview_status.setText('封面尚未生成')

    def set_cover_pending(self, pending, message='封面更新中'):
        """Mark the cover as being regenerated.

        The old cover stays visible until the new revision arrives, but it is
        labelled, so the user is never shown a stale cover as if it were the
        current one.
        """
        self._cover_pending = bool(pending)
        if pending:
            self.preview_status.setText(message)
        else:
            self.preview_status.setText('封面已更新')

    def _dependency_text(self, record):
        files = record.files
        if not files:
            return '无依赖文件'
        missing = [entry for entry in files
                   if getattr(entry, 'role', '') == 'missing'
                   or getattr(entry, 'missing', False)]
        if missing:
            return f'{len(files)} 个文件，{len(missing)} 个缺失'
        return f'{len(files)} 个文件齐全'

    # ------------------------------------------------------------------
    # Appearance
    # ------------------------------------------------------------------
    def collect_appearance(self):
        """Return a detached snapshot of the slot controls.

        The result holds no widget and no live dictionary, so it can be
        handed to a background job or written to the project unchanged.
        """
        pairs = [(key, widget) for key, widget in self.slots.items()]
        return appearance_adapter.collect_appearance(
            pairs, self._appearance,
            appearance_adapter.appearance_properties(self._appearance))

    def commit_appearance(self):
        """Close the current edit group and return its one revision.

        A slider drag publishes dozens of patches. They belong to *one* user
        action, so they become one appearance revision here: undo then has
        one entry per real edit instead of one per pixel (C4.6).
        """
        self._appearance = self._session.commit()
        return appearance_adapter.detached_appearance(self._appearance)

    def _slot_options(self, key, patch):
        if not self._capabilities.edit_appearance:
            return
        if not self._session.patch(key, patch):
            reason = self._session.block_reason(key)
            self.preview_status.setText(reason or '该槽位不可编辑')
            return
        self._appearance = self._session.working
        self.appearance_changed.emit(appearance_adapter.detached_appearance(self._appearance))

    def _slot_channel(self, key, channel):
        if not self._capabilities.edit_appearance:
            return
        if not self._session.set_channel(key, channel):
            return
        self._appearance = self._session.working
        self.appearance_changed.emit(appearance_adapter.detached_appearance(self._appearance))

    def _slot_cleared(self, key):
        if not self._capabilities.edit_appearance:
            return
        if not self._session.set_binding(key, None):
            return
        self._appearance = self._session.working
        self.appearance_changed.emit(appearance_adapter.detached_appearance(self._appearance))
        self.slot_cleared.emit(key)

    def apply_material_record(self, material_record):
        """Adopt a material-library configuration as a detached snapshot."""
        if not self._capabilities.edit_appearance:
            return False
        complete = self._session.apply_material(material_record)
        self._appearance = self._session.working
        self._load_slots()
        self.appearance_changed.emit(appearance_adapter.detached_appearance(self._appearance))
        return complete

    # ------------------------------------------------------------------
    # Metadata
    # ------------------------------------------------------------------
    def _tags_edited(self, text):
        self._pending_tags = text
        self._metadata_timer.start()

    def _notes_edited(self):
        if self.notes.signalsBlocked():
            return
        self._pending_notes = self.notes.toPlainText()
        self._metadata_timer.start()

    def _submit_metadata(self):
        if self._record is None:
            return
        if self._pending_tags is None and self._pending_notes is None:
            return
        tags = self._pending_tags if self._pending_tags is not None else \
            ', '.join(self._record.tags)
        notes = self._pending_notes if self._pending_notes is not None else self._record.notes
        expected = self._record.record_revision
        self._pending_tags = None
        self._pending_notes = None
        self.metadata_changed.emit(tags, notes, expected)

    def _flush_metadata(self):
        """Submit a pending edit before the selection moves on."""
        if self._metadata_timer.isActive():
            self._metadata_timer.stop()
        self._submit_metadata()

    def set_metadata_feedback(self, text, ok=True):
        self.metadata_status.setText(text)
        self.metadata_status.setVisible(bool(text))
        self.metadata_status.setProperty('uiRole', 'secondary' if ok else 'error')
