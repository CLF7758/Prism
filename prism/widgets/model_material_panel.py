"""C0/C1/C4: the multi-material container of one model.

What lives here: the view state of one model's internal materials, the two
independent selections (material vs. binding site), the drafted requests and
the status vocabulary. What does not live here: parsing, storage, rendering and
the native runtime. The panel never reads an original, never imports F3D and
never renders a frame itself.

The panel is driven through a duck-typed port (``MaterialPanelPort`` below).
Everything is disabled - with a stated reason - while the port is missing, so a
scaffolded build can never look like a working editor:

    capabilities(target, working_ids=()) -> MaterialCapabilityReport
    inspect(target) -> job id                 # result: publish_catalog()
    preview(request, draft_generation) -> job id   # result: publish_preview()
    commit(request) -> job id                 # result: publish_commit()
    cancel(job_id) / release(lease_id)
    highlight(target, binding_ids)            # temporary, never a commit
    thumbnail(target, key)                    # optional; rows stay text only

A returned job id is never shown as success: only a commit receipt is.
"""
import json
import uuid
from dataclasses import dataclass, replace
from prism.library_observability import observed, event as library_event

from PyQt6 import QtCore, QtWidgets
from PyQt6.QtCore import Qt

from prism.model_library.multimaterial.contracts import (
    MaterialCapabilityReport, MaterialCommitReceipt, MaterialEditRequest,
    MaterialOperation, MaterialRenderReceipt, MaterialTarget,
    PreparedMaterialView)
from prism.widgets.model_material_editor import MaterialEditor, build_material_view
from prism.widgets.model_material_list import (
    PHASE_FAILED, PHASE_INSPECTING, PHASE_NO_MODEL, PHASE_UNPARSED, PHASE_READY,
    ROW_UNASSIGNED, ROW_WORKING, MaterialListView, build_rows, site_label, source_definition,
    sites_for, slot_label, summarize)
from prism.widgets.model_material_picker import (
    MODE_SURFACE, MaterialPicker, PickerSelection)

#: Which words the panel is allowed to use for each phase.
PHASE_TEXT = {
    PHASE_NO_MODEL: '选择模型后显示它内部的材质',
    PHASE_UNPARSED: '材质清单尚未解析',
    PHASE_INSPECTING: '正在解析模型内的材质…',
    PHASE_FAILED: '材质解析失败',
    PHASE_READY: '材质清单已解析',
}

#: Bumped whenever the command payload shape below changes. The payload lives in
#: ONE place (``ModelMaterialPanel._command_parameters``) and is explicitly
#: provisional: AI 1 owns the shared command contract, so this is a draft that
#: can be swapped without touching the action code.
COMMAND_CONTRACT_DRAFT = 1

#: How many material thumbnails stay decoded at once. The request budget in the
#: list model bounds *reads*; this bounds *residency*. Oldest decode is dropped
#: first, so a 1000-row scroll cannot keep 1000 pixmaps alive.
MAX_RESIDENT_THUMBNAILS = 64

#: Texture states a backend may report per slot. Declared here so the UI never
#: has to guess "empty resource id" == "missing file".
TEXTURE_OK = 'ok'
TEXTURE_MISSING = 'missing'          # declared, but the file/resource is gone
TEXTURE_UNLOADED = 'unloaded'        # not decoded yet (still a real texture)
TEXTURE_NONE = 'none'                # the material really has no such map
TEXTURE_FAILED = 'failed'            # decode/IO failed
TEXTURE_UNKNOWN = 'unknown'          # the backend said nothing


from prism.model_library.multimaterial.edit_session import PendingPreview, PendingCommit, PreviewSession, CommitLedger


@dataclass(frozen=True)
class ThumbnailImage:
    """A material preview as a Qt-free service may return it.

    Bytes plus identity, never a QPixmap: decoding belongs to the UI thread, and
    the caller decides what to keep.
    """

    data: bytes | None = None
    digest: str = ''
    width: int | None = None
    height: int | None = None
    state: str = TEXTURE_OK


class ModelMaterialPanel(QtWidgets.QWidget):
    """Internal material list + one existing-slot editor + library picker."""

    #: Observers of the material selection. Payload is a working id.
    material_selected = QtCore.pyqtSignal(str)
    #: Temporary highlight of every site of the selected material.
    highlight_requested = QtCore.pyqtSignal(object)
    #: The user asked for one step of project history (Ctrl+Z outside text).
    undo_requested = QtCore.pyqtSignal(object)
    #: Local (per-material) editing really became available / unavailable.
    local_editing_changed = QtCore.pyqtSignal(bool)

    @property
    def _preview(self):
        return self._preview_session.current

    @_preview.setter
    def _preview(self, value):
        self._preview_session.current = value

    @property
    def _lease_id(self):
        return self._preview_session.lease_id

    @_lease_id.setter
    def _lease_id(self, value):
        self._preview_session.lease_id = value

    def __init__(self, parent=None):
        from .widget_construction import finish_construction
        finish_construction(self._construct(parent))

    def _construct(self, parent=None):
        from .widget_construction import widget_steps
        super().__init__(parent)
        self.setObjectName('modelMaterialPanel')
        self.port = None
        self.library_source = None
        self.base_source = None
        self.picker_factory = None
        self.model_id = None
        self.capabilities = MaterialCapabilityReport()
        self._identity = None
        self._target = None
        self._complete = False
        self._phase = PHASE_NO_MODEL
        self._catalog = None
        self._state = None
        self._rows = ()
        self._error = ''
        self._selected_working_id = None
        self._adjusted_working_ids = set()
        self._reset_on_inspect = True
        self._selected_binding_id = None
        self._site_ids = ()
        self._material_view = None
        self._inspect_generation = 0
        self._draft_generation = 0
        self._preview_session = PreviewSession()
        self._committed_preview = None
        from .preview_updates import PreviewUpdates
        self._preview_updates = PreviewUpdates(self)
        self._preview_updates.ready.connect(self._dispatch_slot_preview)
        self._lease_id = None                # renderer lease, released once
        self._commit_ledger = CommitLedger()
        self._commits = self._commit_ledger.pending
        self._commit_order = self._commit_ledger.order
        self._confirmed_commits = self._commit_ledger.confirmed
        self._retryable = None               # the latest failed PendingCommit
        self._candidates = ()                # ambiguous pick candidates
        self._subscription_id = None
        self._thumbnail_order = []
        self._stale_publications = 0
        self._picker = None
        self._picker_thumbnails = {}

        layout = QtWidgets.QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(6)
        self.editor_frame = QtWidgets.QWidget(self)
        self.heading = QtWidgets.QLabel('模型材质 · 未选择模型')
        self.heading.setProperty('uiRole', 'inspectorSection')
        layout.addWidget(self.heading)
        self.status = QtWidgets.QLabel(PHASE_TEXT[PHASE_NO_MODEL])
        self.status.setProperty('uiRole', 'secondary')
        self.status.setWordWrap(True)
        layout.addWidget(self.status)
        self.materials = MaterialListView(self)
        self.materials.row_selected.connect(self._material_row_selected)
        self.materials.copy_requested.connect(self.copy_material)
        self.materials.paste_requested.connect(self.paste_material)
        self.materials.rename_requested.connect(self.rename_material)
        self.materials.retry_requested.connect(self._retry_commit)
        self.materials.material_dropped.connect(self._row_dropped)
        self.materials.material_model().thumbnail_requested.connect(self._thumbnail_requested)
        layout.addWidget(self.materials)
        self.empty_hint = QtWidgets.QLabel('')
        self.empty_hint.setProperty('uiRole', 'secondary')
        self.empty_hint.setWordWrap(True)
        self.empty_hint.hide()
        layout.addWidget(self.empty_hint)
        self.scope = QtWidgets.QLabel('')
        self.scope.setProperty('uiRole', 'secondary')
        self.scope.setWordWrap(True)
        layout.addWidget(self.scope)
        grid = QtWidgets.QGridLayout()
        grid.setSpacing(4)
        self.actions = {}
        for index, (key, label, tip) in enumerate((
                ('replace', '从材质库替换', '在模型上下文里选中材质，先预览再应用'),
                ('unique', '仅此部件独立', '先选中一处使用部位；只把这一处重绑到独立副本'),
                ('restore', '恢复此材质原样', '只恢复这一个材质，不影响其他材质'),
                ('restore_model', '恢复全模型原分配', '恢复整个模型的原始材质分配'))):
            button = QtWidgets.QPushButton(label)
            button.setText({'replace': '材质库', 'unique': '独立此部位',
                            'restore': '恢复此材质', 'restore_model': '全部恢复'}[key])
            button.setEnabled(False)
            button.setToolTip(tip)
            # 2x2: a four-button row overflows the narrow inspector column and
            # clips the last label; two short rows never do.
            if key == 'replace':
                grid.addWidget(button, 0, 0)
            else:
                button.setParent(self)
                button.hide()
            self.actions[key] = button
        self.retry_preview_button = QtWidgets.QPushButton('重试预览')
        self.retry_preview_button.setObjectName('retryModelMaterialPreview')
        self.retry_preview_button.hide()
        self.retry_preview_button.clicked.connect(self.retry_committed_preview)
        grid.addWidget(self.retry_preview_button, 1, 0)
        self.actions['replace'].clicked.connect(self.open_picker)
        self.actions['unique'].clicked.connect(self.make_unique)
        self.actions['restore'].clicked.connect(self.restore_material)
        self.actions['restore_model'].clicked.connect(self.restore_model)
        self.action_reasons = {key: button.toolTip() for key, button in self.actions.items()}
        self.sites = QtWidgets.QListWidget()
        self.sites.setObjectName('modelMaterialSites')
        self.sites.setMinimumHeight(70)
        self.sites.setMaximumHeight(120)
        self.sites.setEnabled(False)
        self.sites.setToolTip('选择一处使用部位；选择材质不等于选择部位')
        self.sites.currentRowChanged.connect(self._site_row_changed)
        self.site_label = QtWidgets.QLabel('使用部位 · 先选择一个材质')
        self.site_label.setProperty('uiRole', 'secondarySmall')
        layout.addWidget(self.site_label)
        layout.addWidget(self.sites)
        yield
        self.editor = yield from widget_steps(MaterialEditor, self.editor_frame)
        self.editor.preview_requested.connect(self._option_previewed)
        self.editor.commit_requested.connect(self._option_committed)
        self.editor.texture_requested.connect(self._texture_requested)
        self.editor.reuse_requested.connect(self._reuse_requested)
        self.editor.retry_requested.connect(self._retry_commit)
        self.editor.local_editing_changed.connect(self.local_editing_changed.emit)
        feedback = QtWidgets.QVBoxLayout()
        feedback.setSpacing(2)
        self.preview_status = QtWidgets.QLabel('预览：无')
        self.preview_status.setProperty('uiRole', 'secondarySmall')
        self.preview_status.setWordWrap(True)
        feedback.addWidget(self.preview_status)
        self.save_status = QtWidgets.QLabel('保存：无待保存修改')
        self.save_status.setProperty('uiRole', 'secondarySmall')
        self.save_status.setWordWrap(True)
        feedback.addWidget(self.save_status)
        self.cover_status = QtWidgets.QLabel('')
        self.cover_status.setProperty('uiRole', 'secondarySmall')
        self.cover_status.setWordWrap(True)
        self.cover_status.hide()
        feedback.addWidget(self.cover_status)
        self.export_notice = QtWidgets.QLabel(
            '原件导出不包含 Prism 外观修改；修改只保存在本工程内。')
        self.export_notice.setProperty('uiRole', 'secondarySmall')
        self.export_notice.setWordWrap(True)
        self.export_notice.hide()
        feedback.addWidget(self.export_notice)
        layout.addLayout(feedback)
        self.legacy_notice = QtWidgets.QLabel('')
        self.legacy_notice.setProperty('uiRole', 'secondarySmall')
        self.legacy_notice.setWordWrap(True)
        self.legacy_notice.hide()
        layout.addWidget(self.legacy_notice)
        # Two stable regions: model materials above, the selected material's
        # editor below. Technical binding paths are available on demand.
        while layout.count():
            layout.takeAt(0)
        self.catalog_frame = QtWidgets.QGroupBox('', self)
        catalog_layout = QtWidgets.QVBoxLayout(self.catalog_frame)
        for widget in (self.heading,self.status,self.materials,self.empty_hint):
            catalog_layout.addWidget(widget)
        self.heading.hide()
        self.status.hide()
        editor_layout = QtWidgets.QVBoxLayout(self.editor_frame)
        editor_layout.setContentsMargins(0, 0, 0, 0)
        editor_layout.setSpacing(12)
        editor_layout.addLayout(grid)
        self.parts_toggle = QtWidgets.QToolButton()
        self.parts_toggle.setText('查看使用部位')
        self.parts_toggle.setCheckable(True)
        self.parts_toggle.toggled.connect(self.sites.setVisible)
        editor_layout.addWidget(self.parts_toggle)
        self.parts_toggle.hide()
        editor_layout.addWidget(self.sites)
        self.sites.hide();self.site_label.hide();self.scope.hide()
        editor_layout.addWidget(self.editor,1)
        self._hidden_feedback = QtWidgets.QWidget(self)
        self._hidden_feedback.setLayout(feedback)
        self._hidden_feedback.hide()
        for label in (self.preview_status,self.save_status,self.cover_status,self.export_notice,self.legacy_notice):
            label.hide()
        self.legacy_notice.setParent(self._hidden_feedback)
        layout.addWidget(self.catalog_frame)
        layout.addWidget(self.editor_frame,1)
        self.editor.source_note.hide()
        self.editor.impact.hide()
        self.editor.title.hide()
        self.editor.notice.hide()
        app = QtWidgets.QApplication.instance()
        if app is not None:
            app.installEventFilter(self)

    # ------------------------------------------------------------------
    # Wiring
    # ------------------------------------------------------------------
    def set_port(self, port):
        """Attach the controller, at most one subscription at a time.

        The composition root calls this from several places (page load, project
        rebind, capability refresh). Re-attaching must not install a second
        callback: with two subscriptions every preview and every receipt would
        be delivered twice.
        """
        if port is self.port:
            return
        self.detach_port()
        self.port = port
        self._reset_on_inspect = True
        if port is None:
            return
        subscribe = getattr(port, 'subscribe', None)
        if callable(subscribe):
            self._subscription_id = subscribe(self._on_port_event)
        if self._complete:
            self.inspect()

    def detach_port(self):
        """Drop the controller and its subscription; nothing may arrive after."""
        port = self.port
        self._adjusted_working_ids.clear()
        self.cancel_preview('Port detached')
        self.release_lease()
        self.port = None
        if port is not None:
            unsubscribe = getattr(port, 'unsubscribe', None)
            if callable(unsubscribe) and self._subscription_id is not None:
                try:
                    unsubscribe(self._subscription_id)
                except Exception as error:
                    self._error = '退订失败：' + str(error)[:120]
        self._subscription_id = None
        self.cancel_preview('端口已解除')
        self.release_lease()

    def subscription_id(self):
        return self._subscription_id

    def _on_port_event(self, event):
        """One event from an optional service subscription (GUI thread)."""
        if not isinstance(event, dict):
            return False
        kind = event.get('kind')
        target = event.get('target')
        if kind=='preview_closed':
            if not self._same_model(target):return False
            self.cancel_preview('预览已关闭，恢复打开前参数',superseded=True)
            self.editor.discard_gesture()
            return True
        if kind=='texture_image':
            if not self._accepts(target) or event.get('working_id')!=self._selected_working_id:return False
            widget=self.editor.slots.get(event.get('slot'))
            if widget is None or widget._binding.get('resourceId')!=event.get('resource_id'):return False
            widget.show_channel_image(event['data'])
            return True
        if kind == 'catalog':
            return self.publish_catalog(target, event.get('catalog'), event.get('state'))
        if kind == 'failed':
            return self.publish_failed(target, event.get('reason'))
        if kind in ('runtime_failed', 'runtime_completed'):
            if not self._accepts(target):return False
            request_id = event.get('request_id')
            if request_id is not None and self._committed_preview != (target, request_id):
                self._stale_publications += 1
                return False
            if kind == 'runtime_completed':
                if request_id is None:return False
                self.preview_status.setText('修改已保存，预览已更新')
                self.retry_preview_button.hide()
                return True
            prefix = '修改已保存，预览更新失败：' if request_id is not None else '预览失败：'
            self.preview_status.setText(prefix + str(event.get('reason') or '未提供原因'))
            if request_id is not None:
                self.retry_preview_button.setToolTip(self.preview_status.text())
                self.retry_preview_button.setEnabled(callable(getattr(self.port,'retry_committed_preview',None)))
                self.retry_preview_button.show()
            return True
        if kind == 'preview':
            return self.publish_preview(event.get('preview'))
        if kind == 'preview_failed':
            return self.publish_preview_failed(
                target, event.get('reason'), request_id=event.get('request_id'),
                draft_generation=event.get('draft_generation'))
        if kind == 'commit':
            receipt=event.get('receipt')
            accepted=self.publish_commit(receipt)
            if accepted and event.get('catalog') is not None and event.get('state') is not None:
                self.publish_catalog(receipt.after,event['catalog'],event['state'])
            return accepted
        if kind == 'commit_failed':
            return self.publish_commit_failed(
                target, event.get('reason'), request_id=event.get('request_id'))
        if kind == 'cover':
            return self.publish_cover_updated(target)
        if kind == 'thumbnail':
            return self.publish_thumbnail(target, event.get('key'), event.get('image'))
        return False

    def set_library_source(self, source):
        """Attach the material-library card source used by the picker."""
        self.library_source = source

    def set_base_source(self, source):
        """Attach the resolved base-material view (AI 1 owns it).

        The UI must not derive inheritance itself: a library snapshot is not a
        source definition and must not be looked up inside the source catalog.
        ``source.bases(target, catalog, state)`` returns
        ``{working_id: base_definition}``; without it the editor shows
        "原始值未取得" instead of computing a base on its own.
        """
        self.base_source = source
        row = self._current_row()
        if row is not None:
            self._show_material(row)

    def set_capabilities(self, capabilities):
        self.capabilities = capabilities or MaterialCapabilityReport()
        self._refresh_actions()
        row = self._current_row()
        if row is not None:
            self._show_material(row)
            self._fill_sites(row.working_id)
        self._refresh_text()

    # ------------------------------------------------------------------
    # Session identity
    # ------------------------------------------------------------------
    def reset(self, identity=None):
        """Forget everything that belongs to the previous model/session.

        Compared by full identity later: a bare model id is not enough to
        accept an asynchronous result, and the revisions must match too.

        A new identity means the old edit can no longer be completed: its draft
        is dropped, its preview is cancelled and its lease is released. That is
        deliberately different from switching the material inside one model,
        which finishes the edit instead (see ``select_material``).
        """
        next_model = identity.model_id if isinstance(identity, MaterialTarget) else (str(identity) if identity else None)
        if next_model != self.model_id:
            self._reset_adjustments()
            self._reset_on_inspect = True
        self.discard_edit()
        self._identity = identity
        if isinstance(identity, MaterialTarget):
            self._target = identity
            self._complete = True
            self.model_id = identity.model_id
        else:
            self._target = None
            self._complete = False
            self.model_id = str(identity) if identity else None
        self._phase = PHASE_NO_MODEL if identity in (None, '') else PHASE_UNPARSED
        self._catalog = None
        self._state = None
        self._rows = ()
        self._error = ''
        self.capabilities = MaterialCapabilityReport() if not self._complete else self.capabilities
        self._selected_working_id = None
        self._selected_binding_id = None
        self._site_ids = ()
        self._material_view = None
        self._inspect_generation += 1
        self._draft_generation = 0
        self._commits.clear()
        self._commit_order.clear()
        self._retryable = None
        self._candidates = ()
        self.materials.clear_rows()
        self.sites.clear()
        self.sites.setEnabled(False)
        self.site_label.setText('使用部位 · 先选择一个材质')
        self.editor.clear()
        self.local_editing_changed.emit(False)
        self.scope.setText('')
        self.empty_hint.hide()
        self.preview_status.setText('预览：无')
        self._committed_preview = None
        self.retry_preview_button.hide()
        self.save_status.setText('保存：无待保存修改')
        self.cover_status.hide()
        self.export_notice.hide()
        self.legacy_notice.hide()
        self._refresh_text()
        self._refresh_actions()
        if self._complete and self.port is not None:
            self.inspect()

    def identity(self):
        return self._identity

    def target(self):
        return self._target

    def phase(self):
        return self._phase

    # ------------------------------------------------------------------
    # Text
    # ------------------------------------------------------------------
    def _refresh_text(self):
        if self._identity in (None, ''):
            self.heading.setText('模型材质 · 未选择模型')
            self.status.setText(PHASE_TEXT[PHASE_NO_MODEL])
        elif not self._complete:
            self.heading.setText('模型材质 · 尚未解析')
            self.status.setText('缺少工程会话与修订信息，无法确认材质清单；'
                                '数量保持未知，不显示 0。')
        elif self._phase == PHASE_FAILED:
            self.heading.setText('模型材质 · 解析失败')
            self.status.setText('解析失败：' + (self._error or '未提供原因')
                                + '（失败不等于“模型没有材质”）')
        elif self._phase == PHASE_INSPECTING:
            self.heading.setText('模型材质 · 解析中')
            self.status.setText(PHASE_TEXT[PHASE_INSPECTING])
        elif self._phase == PHASE_READY:
            used = [row for row in self._rows if row.kind == ROW_WORKING]
            self.heading.setText('模型材质 · ' + summarize(self._rows))
            self.status.setText(self._ready_text(used))
        else:
            self.heading.setText('模型材质 · 尚未解析')
            if self.port is None:
                self.status.setText('多材质服务未接入：材质列表不可用（这不是“没有材质”）。')
            else:
                self.status.setText(PHASE_TEXT[PHASE_UNPARSED])

    def _ready_text(self, used):
        if not used:
            extra = [row for row in self._rows if row.kind != ROW_WORKING]
            if extra:
                return ('模型内没有正在使用的材质；未解析条目与未指定组单独列在下方。')
            return '解析完成，但模型内没有可用的材质定义。'
        unknown = [row for row in used if row.site_count is None]
        text = summarize(self._rows)
        if unknown:
            text += '。有材质的使用处数量未取得'
        if self._catalog and not any(d.textures for d in self._catalog.definitions):
            text += '。文件未声明贴图，可从材质库选择或手动添加。'
        return text

    # ------------------------------------------------------------------
    # Inspection results
    # ------------------------------------------------------------------
    def inspect(self):
        """Ask the port for the catalog of the current target."""
        if not self._complete:
            self._refresh_text()
            return
        if self.port is None:
            self._refresh_text()
            return
        if self.capabilities is not None and not self.capabilities.read_catalog:
            report = self._call('capabilities', self._target, ()) if callable(
                getattr(self.port, 'capabilities', None)) else None
            if report is not None:
                self.capabilities = report
            if not self.capabilities.read_catalog:
                self._phase = PHASE_UNPARSED
                self._error = self.capabilities.reason or '后端未确认可读取材质清单'
                self._refresh_text()
                self._refresh_actions()
                return
        self._inspect_generation += 1
        self._phase = PHASE_INSPECTING
        self._error = ''
        self._refresh_text()
        if self._reset_on_inspect:
            if self.capabilities.edit_material and callable(getattr(self.port, 'reset_adjustments', None)):
                self._call('reset_adjustments', self._target)
            self._reset_on_inspect = False
        job = self._call('inspect', self._target)
        if job is None:
            self._phase = PHASE_FAILED
            self._error = self._error or '解析任务未能启动'
            self._refresh_text()
            self._refresh_actions()

    @observed('model','ui_catalog_result')
    def publish_catalog(self, target, catalog, state=None):
        """Accept one inspection result. Stale identities are dropped.

        The result is matched by model, then adopted **forward**: the service
        may replay a commit and report a newer ``state_revision`` than the panel
        last knew, and that newer target becomes the panel's own so the next
        edit is built against it. A result for an older revision is dropped.

        A result arriving while a commit is still unconfirmed must not overwrite
        the user's draft: the backend revision it describes does not contain that
        commit yet. The list and the status still refresh; the editor keeps the
        draft so the next commit carries it forward.
        """
        if not self._same_model(target):
            self._stale_publications += 1
            library_event('model', 'catalog_result', 'stale', reason='model_identity_changed',
                          model_id=target.model_id, state_revision=target.state_revision)
            return False
        if target.state_revision < self._target.state_revision:
            self._stale_publications += 1
            library_event('model', 'catalog_result', 'stale', reason='older_state_revision',
                          model_id=target.model_id, state_revision=target.state_revision,
                          expected_state_revision=self._target.state_revision)
            return False
        if target.state_revision >= self._target.state_revision:
            self._target = target
            self._identity = target
        self._catalog = catalog
        self._state = state
        self._phase = PHASE_READY
        self._error = ''
        self._rows = build_rows(catalog, state)
        self.materials.install(self._rows)
        self.empty_hint.setVisible(not self._rows)
        if not self._rows:
            self.empty_hint.setText('解析完成，但这一结果里没有任何材质条目。')
        report = self._call('capabilities', target, ()) if callable(
            getattr(self.port, 'capabilities', None)) else None
        if report is not None:
            self.capabilities = report
        self._restore_selection()
        if self._selected_working_id is None:
            first = next((row for row in self._rows
                          if row.working_id and row.selectable), None)
            if first is not None:
                self._automatic_selection = True
                try:
                    self.select_material(first.working_id)
                finally:
                    self._automatic_selection = False
        self._refresh_text()
        self._refresh_actions()
        self._refresh_modification_notice()
        self._refresh_save_status()
        return True

    def publish_failed(self, target, reason):
        """An inspection failed. This is not an empty library."""
        if not self._accepts(target):
            return False
        self._phase = PHASE_FAILED
        self._error = str(reason or '')
        self._catalog = None
        self._state = None
        self._rows = ()
        self.materials.clear_rows()
        self.empty_hint.setVisible(True)
        self.empty_hint.setText('解析失败，材质列表不可用：' + (self._error or '未提供原因'))
        self.editor.clear()
        self._selected_working_id = None
        self._selected_binding_id = None
        self.sites.clear()
        self._refresh_text()
        self._refresh_actions()
        return True

    def publish_thumbnail(self, target, key, image):
        """A material preview arrived; stale keys and stale sessions are ignored.

        ``image`` is bytes, a mapping, or a :class:`ThumbnailImage` - never a
        QPixmap from the service: decoding stays in this thread.
        """
        if not self._accepts(target):
            return False
        payload = self._thumbnail_payload(image)
        if payload is None:
            return False
        accepted = self.materials.material_model().set_thumbnail(key, payload.data)
        if accepted:
            self._remember_thumbnail(key)
        return accepted

    @staticmethod
    def _thumbnail_payload(image):
        if isinstance(image, ThumbnailImage):
            return image if image.data else None
        if isinstance(image, (bytes, bytearray)):
            return ThumbnailImage(data=bytes(image))
        if isinstance(image, dict):
            data = image.get('data') or image.get('bytes')
            if not data:
                return None
            return ThumbnailImage(data=bytes(data), digest=str(image.get('digest', '')),
                                  width=image.get('width'), height=image.get('height'),
                                  state=str(image.get('state', TEXTURE_OK)))
        return None

    def _remember_thumbnail(self, key):
        if key in self._thumbnail_order:
            self._thumbnail_order.remove(key)
        self._thumbnail_order.append(key)
        while len(self._thumbnail_order) > MAX_RESIDENT_THUMBNAILS:
            oldest = self._thumbnail_order.pop(0)
            self.materials.material_model().drop_thumbnail(oldest)

    def resident_thumbnails(self):
        return tuple(self._thumbnail_order)

    def publish_pick_result(self, target, binding_id, candidates=()):
        """A large-preview pick, resolved through the full assignment map.

        Picking in the model preview is not restricted to the material that
        happens to be selected: the stable binding id is looked up in the whole
        assignment table, the material is switched first and the site selected
        second. Ambiguous hits (transparent overlap) are never guessed.
        """
        if not self._accepts(target):
            return False
        options = tuple(dict.fromkeys([binding_id] if binding_id else [])) + tuple(
            candidate for candidate in candidates or () if candidate != binding_id)
        if len(options) > 1:
            self._candidates = options
            self.site_label.setText(
                f'拾取命中 {len(options)} 个重叠候选，未自动选择；请在“使用部位”里明确指定')
            return False
        self._candidates = ()
        if not options:
            return False
        picked = options[0]
        site = next((item for item in (getattr(self._catalog, 'binding_sites', ()) or ())
                     if item.binding_id == picked), None)
        if site is None:
            self.site_label.setText('拾取结果已过期：部位不在当前清单里，未做任何改变')
            return False
        working_id = self._working_for_binding(picked)
        if working_id is None:
            # A site the source never assigned: select the unassigned group so
            # the user sees where the pick landed, but do not invent a material.
            if any(row.kind == ROW_UNASSIGNED for row in self._rows):
                self.materials.clear_selection()
                self._select_material(None)
                self.site_label.setText(
                    f'部位 {site_label(site)} 没有材质分配，已定位到“未指定材质”组')
                return True
            self.site_label.setText('拾取到的部位没有材质分配')
            return False
        if working_id != self._selected_working_id:
            if not self.select_material(working_id):
                return False
        return self.select_site(picked)

    def _working_for_binding(self, binding_id):
        if not binding_id or self._state is None:
            return None
        for assignment in (getattr(self._state, 'assignments', ()) or ()):
            if assignment.binding_id == binding_id:
                return assignment.working_id
        return None

    def _matches(self, target):
        """Full identity match: session, epoch, model and every revision."""
        return bool(self._complete and self._target is not None
                    and target == self._target)

    def _same_model(self, target):
        """Session + model match, ignoring revisions.

        The service now replays requests whose ``state_revision`` lags behind a
        queued commit, so a result may arrive carrying an older revision than
        the one the panel has already advanced to. It still belongs to the same
        model, so the panel must not drop it on a revision mismatch.
        """
        if not self._complete or self._target is None or target is None:
            return False
        return (target.project_session == self._target.project_session
                and target.open_epoch == self._target.open_epoch
                and target.model_id == self._target.model_id)

    def _accepts(self, target):
        if self._matches(target):
            return True
        self._stale_publications += 1
        return False

    def stale_publications(self):
        return self._stale_publications

    # ------------------------------------------------------------------
    # Read-only view accessors (the wiring and tests read, never write)
    # ------------------------------------------------------------------
    def rows(self):
        return self._rows

    def material_row(self, working_id):
        return next((row for row in self._rows if row.working_id == working_id), None)

    def material_view(self):
        return self._material_view

    def site_ids(self):
        return self._site_ids

    def candidates(self):
        """Ambiguous pick candidates awaiting an explicit choice."""
        return self._candidates

    # ------------------------------------------------------------------
    # Material selection
    # ------------------------------------------------------------------
    def _current_row(self):
        for row in self._rows:
            if row.working_id and row.working_id == self._selected_working_id:
                return row
        return None

    def _material_row_selected(self, row):
        self.finish_edit()
        if row is None or not row.selectable:
            self._select_material(None, row)
            return
        self._select_material(row.working_id, row)

    @observed('model','ui_material_selection')
    def select_material(self, working_id):
        """Select a material by id (used by tests and by the wiring)."""
        row = next((item for item in self._rows if item.working_id == working_id), None)
        if row is None:
            return False
        if working_id == self._selected_working_id:
            self._select_material(working_id, row)
            return True
        self.materials.select_key(row.key)
        return True

    def _select_material(self, working_id, row=None):
        if working_id == self._selected_working_id and working_id is not None:
            if not getattr(self, '_automatic_selection', False):
                self._highlight_current_material()
            self._refresh_actions()
            return
        # Switching material inside one model FINISHES the edit (the gesture is
        # committed); only a new model/project discards it (see reset()).
        self.finish_edit()
        self.cancel_preview('已切换材质')
        self.release_lease()
        self._selected_working_id = working_id
        self._selected_binding_id = None
        self._material_view = None
        if working_id is None:
            self.editor.clear()
            self.scope.setText('')
            self.sites.clear()
            self.sites.setEnabled(False)
            self.site_label.setText('使用部位 · 先选择一个材质')
            self._site_ids = ()
            self._refresh_actions()
            return
        self._show_material(row or self._current_row())
        self._fill_sites(working_id)
        if not getattr(self, '_automatic_selection', False):
            self._highlight_current_material()
        self.material_selected.emit(working_id)
        self._refresh_actions()

    def _highlight_current_material(self):
        self.highlight_requested.emit((self._target, self._site_ids))
        self._call('highlight', self._target, self._site_ids)

    def _show_material(self, row):
        if row is None:
            self._material_view = None
            self.editor.clear()
            self.scope.setText('')
            return
        working = self._working(row.working_id)
        definition, base_kind = self._base_view(working)
        view = build_material_view(definition, working,
                                   supported=self._slot_support(),
                                   base_kind=base_kind)
        self._material_view = view
        editable = bool(self.capabilities.edit_material)
        reason = '' if editable else (self.capabilities.reason or '后端未确认可编辑材质')
        self.scope.setText(row.impact_text())
        self.editor.show_material(view, impact_text=row.impact_text(),
                                  editable=editable, reason=reason)
        getter=getattr(self.port,'texture_thumbnail',None)
        if callable(getter):
            for slot_view in view.slots:
                if slot_view.binding:getter(self._target,row.working_id,slot_view.key)

    def _base_view(self, working):
        """``(definition, kind)`` for one working material, resolved elsewhere.

        A library snapshot is not a source definition: it must not be looked up
        inside ``MaterialCatalog.definitions``. The panel asks the injected base
        source (AI 1 owns it) instead of deriving inheritance itself.
        """
        if working is None:
            return None, 'unknown'
        kind = 'snapshot' if getattr(working, 'base_snapshot_ref', None) else 'original'
        source = self.base_source
        getter = getattr(source, 'bases', None) if source is not None else None
        if callable(getter):
            try:
                bases = getter(self._target, self._catalog, self._state) or {}
            except Exception as error:
                self._error = '基础材质视图不可用：' + str(error)[:120]
                return None, 'unknown'
            definition = bases.get(working.working_id)
            if definition is None:
                return None, 'unknown'
            return definition, kind
        definition = source_definition(working, self._catalog)
        if definition is None:
            return None, 'unknown'
        return definition, kind

    def _working(self, working_id):
        for working in (getattr(self._state, 'materials', ()) or ()):
            if working.working_id == working_id:
                return working
        return None

    def _slot_support(self):
        # Storage/editing is independent of the renderer's effect support.
        # Read-only/session restrictions are applied by _show_material.
        return {}

    def _fill_sites(self, working_id):
        self.sites.blockSignals(True)
        self.sites.clear()
        found = sites_for(self._state, self._catalog, working_id)
        self._site_ids = tuple(site.binding_id for site in found)
        self.parts_toggle.setText(f'查看使用部位（{len(found)} 处）')
        for site in found:
            item = QtWidgets.QListWidgetItem(site_label(site))
            item.setToolTip(f'使用点 {site.binding_id}')
            item.setData(Qt.ItemDataRole.UserRole, site.binding_id)
            self.sites.addItem(item)
        self.sites.blockSignals(False)
        self.sites.setEnabled(bool(found) and bool(self.capabilities.edit_material))
        if not found:
            self.site_label.setText('使用部位 · 这一材质的使用点未取得')
        elif len(found) == 1:
            self.site_label.setText(f'使用部位 · {len(found)} 处（选中后才能“仅此部件独立”）')
        else:
            self.site_label.setText(
                f'使用部位 · {len(found)} 处共享这一材质；选中一处才能只改那一处')

    def _site_row_changed(self, position):
        if position < 0 or position >= len(self._site_ids):
            self._selected_binding_id = None
        else:
            self._selected_binding_id = self._site_ids[position]
        self._refresh_actions()
        self._refresh_text()

    def select_site(self, binding_id):
        if binding_id is None:
            self.sites.setCurrentRow(-1)
            return True
        if binding_id not in self._site_ids:
            return False
        self.sites.setCurrentRow(self._site_ids.index(binding_id))
        return True

    @property
    def selected_working_id(self):
        return self._selected_working_id

    @property
    def selected_binding_id(self):
        return self._selected_binding_id

    def _restore_selection(self):
        """Keep the selection only if the same working material still exists."""
        if self._selected_working_id is None:
            self._refresh_actions()
            return
        row = self._current_row()
        if row is None:
            self._selected_working_id = None
            self._selected_binding_id = None
            self.editor.clear()
            self.sites.clear()
            self._site_ids = ()
            self.sites.setEnabled(False)
            self.scope.setText('')
            self.site_label.setText('使用部位 · 选中的材质已不在模型里')
            self.materials.clear_selection()
            return
        previous_site = self._selected_binding_id
        if self._draft_must_survive():
            # The backend revision behind this result does not contain the
            # unconfirmed commit yet. Rebuilding the controls from it would look
            # like the user's edit was silently reverted.
            self._refresh_actions()
            self._refresh_modification_notice()
            return
        self._show_material(row)
        self._fill_sites(row.working_id)
        if previous_site in self._site_ids:
            self.select_site(previous_site)
        self._refresh_modification_notice()

    def _draft_must_survive(self):
        """A draft outlives a catalog refresh while a commit is unconfirmed."""
        if self._pending_commit() is not None:
            return True
        return bool(self.editor.draft_failure())

    def _refresh_modification_notice(self):
        modified = any(row.modified for row in self._rows)
        self.export_notice.setVisible(modified)

    # ------------------------------------------------------------------
    # Actions
    # ------------------------------------------------------------------
    def _refresh_actions(self):
        row = self._current_row()
        reasons = self._action_reasons(row)
        for key, button in self.actions.items():
            reason = reasons.get(key, '')
            button.setEnabled(not reason)
            button.setToolTip(reason or self._action_tip(key))
        self.action_reasons = reasons

    def _action_tip(self, key):
        return {
            'replace': '在模型上下文里选择材质，先预览再应用（一个事务）',
            'unique': '只把选中的这一处部位重绑到独立副本',
            'restore': '只恢复这一个材质，不影响其他材质',
            'restore_model': '恢复整个模型的原始材质分配',
        }[key]

    def _action_reasons(self, row):
        reasons = {}
        report = self.capabilities or MaterialCapabilityReport()
        if self.port is None:
            reason = '多材质服务未接入（端口未接入）'
            return {key: reason for key in self.actions}
        if not self._complete:
            reason = '需要完整的工程会话与修订信息'
            return {key: reason for key in self.actions}
        if self._phase != PHASE_READY:
            reason = '材质清单尚未解析完成'
            return {key: reason for key in self.actions}
        if row is None:
            reasons['replace'] = '先选择一个正在使用的材质'
            reasons['unique'] = '先选择一个正在使用的材质'
            reasons['restore'] = '先选择一个正在使用的材质'
        else:
            if not report.edit_material:
                reasons['replace'] = report.reason or '后端未确认可编辑材质'
                reasons['restore'] = report.reason or '后端未确认可编辑材质'
            if self.library_source is None:
                reasons['replace'] = '材质库未接入'
            if not row.modified:
                reasons['restore'] = '这一材质没有本地修改'
            if not self._site_ids:
                reasons['unique'] = '当前材质没有可独立的使用部位'
            elif not report.assign_material:
                reasons['unique'] = report.reason or '后端未确认可重绑材质分配'
        if not any(item.modified for item in self._rows):
            reasons['restore_model'] = '模型没有本地材质修改'
        elif not report.edit_material:
            reasons['restore_model'] = report.reason or '后端未确认可编辑材质'
        return reasons

    def _commit(self, request, note='保存中'):
        """Submit one command and keep it pending until *its own* receipt.

        Several commits may be in flight (a slider gesture on A, then a library
        replacement on B). Each is tracked by request id, so a receipt can only
        confirm the command it belongs to.
        """
        job = self._call('commit', request)
        if job is not None and request.operation in (
                MaterialOperation.REPLACE_SURFACE, MaterialOperation.REPLACE_FULL, MaterialOperation.PASTE):
            self._adjusted_working_ids.update(request.working_ids)
        if job is None:
            pending = PendingCommit(request=request, note=note,
                                    failed_reason=self._error or '提交任务未能启动')
            self._commit_ledger.register(pending)
            self._retryable = pending
            self._refresh_save_status()
            self.editor.show_commit_failed(pending.failed_reason)
            return None
        pending = PendingCommit(request=request, note=note, job_id=job)
        self._commit_ledger.register(pending)
        if self._retryable is not None and self._retryable.request_id == pending.request_id:
            self._retryable = None
        self._refresh_save_status()
        return job

    def _pending_commit(self):
        """The newest commit still waiting for a receipt, if any."""
        return self._commit_ledger.newest_waiting()

    def _refresh_save_status(self):
        """State the truth about every in-flight commit, newest last."""
        self.materials.retry_available = self._retryable is not None
        pending = [self._commits[request_id] for request_id in self._commit_order
                   if request_id in self._commits]
        if not pending:
            self.save_status.setText('保存：无待保存修改')
            return
        waiting = [item for item in pending if not item.failed]
        failed = [item for item in pending if item.failed]
        parts = []
        if waiting:
            newest = waiting[-1]
            parts.append(f'保存中：{newest.note}（任务 {newest.job_id}）；等待保存回执')
            if len(waiting) > 1:
                parts.append(f'另有 {len(waiting) - 1} 条提交在等待回执')
        if failed:
            parts.append('保存失败：' + '；'.join(item.failed_reason for item in failed)
                         + '（草稿保留）')
        self.save_status.setText('；'.join(parts))

    def pending_commits(self):
        return tuple(self._commits[request_id] for request_id in self._commit_order
                     if request_id in self._commits)

    def _request(self, operation, *, working_ids=(), binding_ids=(),
                 parameters=None, gesture_id=''):
        """Build one command.

        Targets travel in ``working_ids`` / ``binding_ids``; everything else is
        an operation parameter built by :meth:`_command_parameters`, which is the
        single place to swap when AI 1's shared command contract lands.
        """
        return MaterialEditRequest(
            request_id=uuid.uuid4().hex,
            gesture_id=gesture_id or uuid.uuid4().hex,
            target=self._target,
            operation=operation,
            working_ids=tuple(working_ids),
            binding_ids=tuple(binding_ids),
            parameters_json=json.dumps(self._command_parameters(parameters),
                                       ensure_ascii=False))

    @staticmethod
    def _command_parameters(parameters):
        """Wrap operation parameters with the draft contract version.

        Provisional on purpose: AI 1 owns the shared schema, so the version tag
        makes an old payload identifiable instead of silently reinterpreted.
        """
        payload = dict(parameters or {})
        payload.setdefault('command_contract', COMMAND_CONTRACT_DRAFT)
        return payload

    def rename_material(self, row, name=None):
        if row is None or not row.working_id or not self.capabilities.edit_material:
            return
        if name is None:
            from .inline_name import edit_name
            edit_name(self.materials,self.materials.currentIndex(),row.name,
                      lambda value:self.rename_material(row,value))
            return
        name=name.strip()
        if not name or name==row.name:return
        request = self._request(MaterialOperation.PATCH,
            working_ids=(row.working_id,),
            parameters={'patch': {'display_name': name}})
        return self._commit(request, note='重命名材质')

    def make_unique(self):
        row = self._current_row()
        if row is None or not row.working_id:
            self._refresh_actions()
            return None
        if self.action_reasons.get('unique'):
            self.save_status.setText('无法独立：' + self.action_reasons['unique'])
            return None
        if self._selected_binding_id is None:
            sites = tuple(sites_for(self._state, self._catalog, row.working_id))
            if not sites:
                return None
            labels = [f'{index + 1}. {site_label(site)}'
                      for index, site in enumerate(sites)]
            selected, accepted = QtWidgets.QInputDialog.getItem(
                self, '独立此部位', '选择要独立使用材质的部位', labels, 0, False)
            if not accepted:
                return None
            self.select_site(sites[labels.index(selected)].binding_id)
            if self._selected_binding_id is None:
                return None
        request = self._request(MaterialOperation.MAKE_UNIQUE,
                                working_ids=(row.working_id,),
                                binding_ids=(self._selected_binding_id,))
        return self._commit(request, note='已请求仅此部件独立')

    def restore_material(self):
        row = self._current_row()
        if row is None or not row.working_id or self.action_reasons.get('restore'):
            return None
        request = self._request(MaterialOperation.RESTORE_MATERIAL,
                                working_ids=(row.working_id,))
        return self._commit(request, note='已请求恢复此材质')

    def restore_model(self):
        if self.action_reasons.get('restore_model'):
            return None
        request = self._request(MaterialOperation.RESTORE_MODEL)
        return self._commit(request, note='已请求恢复全模型原分配')

    # ------------------------------------------------------------------
    # Slot edits
    # ------------------------------------------------------------------
    def _option_previewed(self, slot, patch, gesture_id, generation):
        row = self._current_row()
        if row is None or not row.working_id or not self.capabilities.edit_material:
            return
        request = self._request(MaterialOperation.PATCH,
                                working_ids=(row.working_id,),
                                parameters={'patch': {'slots': {slot: patch}}},
                                gesture_id=gesture_id)
        self._draft_generation = generation
        # Invalidate the previous token immediately, while coalescing backend
        # work until the next shared preview tick.
        self.cancel_preview(superseded=True)
        self._preview = PendingPreview(request_id=request.request_id,
            draft_generation=generation, working_id=row.working_id, kind='slot')
        self._preview_updates.submit((request, generation, row.working_id, slot))

    def _dispatch_slot_preview(self, intent):
        request, generation, working_id, slot = intent
        if (self._preview is None or self._preview.request_id != request.request_id
                or not self._same_model(request.target)):
            return
        job = self._start_preview(request, generation, kind='slot', working_id=working_id)
        if job is None:
            self.preview_status.setText('预览失败：预览服务未接入（草稿保留）')
            return
        self.preview_status.setText(f'预览中：{slot_label(slot)} · 草稿 {generation}（未提交）')

    def _option_committed(self, slot, patch, gesture_id, generation):
        row = self._current_row()
        if row is None or not row.working_id or not self.capabilities.edit_material:
            return
        if any(key in patch for key in ('value', 'factor', 'color')):
            self._adjusted_working_ids.add(row.working_id)
        self._preview_updates.flush()
        self._log_ui('material.ui_edit', row.working_id, slot=slot,
                     gesture_id=gesture_id)
        request = self._request(MaterialOperation.PATCH,
                                working_ids=(row.working_id,),
                                parameters={'patch': {'slots': {slot: patch}}},
                                gesture_id=gesture_id)
        self._commit(request, note=f'已提交 {slot_label(slot)} 修改')

    def _reset_adjustments(self, working_id=None):
        changed = self._adjusted_working_ids
        ids = (working_id,) if working_id in changed else tuple(changed) if working_id is None else ()
        reset = getattr(self.port, 'reset_adjustments', None)
        if ids and self._target is not None and callable(reset):
            self._call('reset_adjustments', self._target, () if working_id is None else ids)
        changed.difference_update(ids)

    @observed('model','ui_texture')
    def _texture_requested(self, slot, action, detail):
        row = self._current_row()
        if row is None or not row.working_id or not self.capabilities.edit_material:
            library_event('model','ui_texture','rejected',slot=slot,action=action,
                          reason='no_editable_material_or_native_capability')
            return
        if action not in ('inherit', 'set', 'clear', 'channel'):
            return
        if action=='set' and not (detail or {}).get('path'):
            library_event('model','ui_texture','rejected',slot=slot,action=action,reason='no_file_selected')
            return
        self._log_ui('material.ui_texture_action', row.working_id, slot=slot,
                     action=action)
        parameters = {'patch': {'texture': dict(detail or {}, slot=slot, action=action)}}
        request = self._request(MaterialOperation.PATCH,
                                working_ids=(row.working_id,),
                                parameters=parameters)
        self._draft_generation += 1
        self._start_preview(request, self._draft_generation, kind='texture',
                            working_id=row.working_id)
        self.preview_status.setText(
            f'预览中（{slot_label(slot)} 贴图变更 · 代次 {self._draft_generation}，未提交）')
        # A texture change is one discrete action: one preview, one commit.
        self._commit(request, note=f'已提交 {slot_label(slot)} 贴图变更')

    def _log_ui(self, event, working_id, **fields):
        """One UI-side action event; failures here must never break editing."""
        try:
            from prism.model_library.events import record_model_event
            payload = {}
            if self._target is not None:
                payload.update(model_id=self._target.model_id,
                               project_session=self._target.project_session,
                               source_revision=self._target.source_revision,
                               state_revision=self._target.state_revision)
            record_model_event(event, working_id=working_id, **payload, **fields)
        except Exception:  # noqa: BLE001 - logging only
            pass

    def _start_preview(self, request, generation, *, kind='', working_id=None):
        """Register the only acceptable preview, then ask for it."""
        self.cancel_preview(superseded=True)
        self.release_lease()
        job = self._call('preview', request, generation)
        self._preview = PendingPreview(request_id=request.request_id,
                                      draft_generation=generation, job_id=job,
                                      working_id=working_id, kind=kind)
        return job

    def _reuse_requested(self, slot):
        self.preview_status.setText(f'{slot_label(slot)}：复用原始贴图需要后端提供候选列表')

    def _retry_commit(self):
        """Retry the failed draft with the same immutable request.

        Reusing the exact request id keeps the retry idempotent: if the original
        commit actually landed but its receipt was lost, the service replays the
        stored receipt instead of applying the edit twice. The service also now
        replays a stale ``state_revision`` onto the current state, so the same
        request succeeds without changing its identity.
        """
        pending = self._retryable
        if pending is None or not pending.failed:
            return
        self._commits.pop(pending.request_id, None)
        if pending.request_id in self._commit_order:
            self._commit_order.remove(pending.request_id)
        self._retryable = None
        self._commit(pending.request, note='已重新提交草稿')

    # ------------------------------------------------------------------
    # Library picker (C3)
    # ------------------------------------------------------------------
    @observed('model','ui_copy')
    def copy_material(self, row=None):
        row = row or self._current_row()
        if row is None or not row.working_id or self._target is None:
            return
        payload = self._call('copy_material', self._target, row.working_id)
        if payload is None:
            self.status.setText('复制失败：' + self._error)
            return
        mime = QtCore.QMimeData()
        mime.setData('application/x-prism-model-material',
                     json.dumps(payload, ensure_ascii=False).encode('utf-8'))
        QtWidgets.QApplication.clipboard().setMimeData(mime)
        self.status.setText('材质已复制；在目标材质球上右键粘贴。')

    @observed('model','ui_paste')
    def paste_material(self, row=None):
        row = row or self._current_row()
        if row is None or not row.working_id or not self.capabilities.edit_material:
            return
        try:
            raw = QtWidgets.QApplication.clipboard().mimeData().data(
                'application/x-prism-model-material')
            payload = json.loads(bytes(raw).decode('utf-8'))
        except (ValueError, UnicodeError):
            self.status.setText('剪贴板中没有可用的模型材质')
            return
        self.select_material(row.working_id)
        request = self._request(MaterialOperation.PASTE, working_ids=(row.working_id,),
                                parameters={'material_clipboard': payload})
        self._commit(request, note='粘贴材质副本')

    def build_picker(self):
        """Create the picker without blocking; the wiring may exec() it."""
        row = self._current_row()
        if row is None or not row.working_id:
            return None
        if self.library_source is None:
            return None
        factory = self.picker_factory or MaterialPicker
        picker = factory(self)
        self._picker = picker
        self._picker_thumbnails = {}
        # Connect before installing cards: asking for a card image happens while
        # the card surface is being filled.
        picker.preview_requested.connect(self._library_preview)
        picker.apply_requested.connect(self._library_apply)
        picker.cancelled.connect(self._library_cancelled)
        picker.thumbnail_requested.connect(self._picker_thumbnail_requested)
        view = self._material_view
        blocked = ''
        if not self.capabilities.edit_material:
            blocked = self.capabilities.reason or '后端未确认可编辑材质'
        picker.set_target(
            name=row.name, impact=row.impact_text(),
            alpha_slots=getattr(view, 'alpha_channels', ()) if view else (),
            blocked_reason=blocked)
        entries = tuple(self.library_source.entries() or ())
        picker.set_entries(entries, source=self.library_source)
        return picker

    def open_picker(self):
        picker = self.build_picker()
        if picker is None:
            self.save_status.setText('无法打开材质库选择器：先选择一个材质，并接入材质库')
            return None
        picker.exec()
        return picker

    def _library_preview(self, selection):
        row = self._current_row()
        if row is None or not row.working_id or not isinstance(selection, PickerSelection):
            return None
        if not selection.frozen:
            self.preview_status.setText(
                '无法预览：材质库没有提供版本/摘要令牌，预览与应用不能保证是同一份内容')
            return None
        request = self._request(
            selection.operation, working_ids=(row.working_id,),
            parameters={'replace': selection.parameters()})
        self._draft_generation += 1
        job = self._start_preview(request, self._draft_generation, kind='library',
                                 working_id=row.working_id)
        self.preview_status.setText(
            f'预览中（材质库 · {selection.mode} · 代次 {self._draft_generation}，未提交）')
        if job is None:
            self.preview_status.setText('预览失败：预览任务未能启动（未提交）')
        return job

    def _library_apply(self, selection):
        row = self._current_row()
        if row is None or not row.working_id or not isinstance(selection, PickerSelection):
            return None
        if not selection.frozen:
            self.save_status.setText(
                '保存失败：材质库没有提供版本/摘要令牌，未提交（预览与应用无法保证一致）')
            return None
        request = self._request(
            selection.operation, working_ids=(row.working_id,),
            parameters={'replace': selection.parameters()})
        return self._commit(request, note='已提交材质库替换')

    def _library_cancelled(self, selection):
        if self._preview is None:
            return
        # Cancelling is not "the picture went back": ask for the committed state.
        self.restore_committed_preview()

    def _picker_thumbnail_requested(self, entry_id):
        source = self.library_source
        if entry_id in self._picker_thumbnails:
            if self._picker is not None:self._picker.set_thumbnails({entry_id:self._picker_thumbnails[entry_id]})
            return
        if source is None:
            return
        getter = getattr(source, 'thumbnail', None)
        if not callable(getter):
            return
        try:
            data_url = getter(entry_id)
        except Exception as error:  # a broken source must not kill the dialog
            self.preview_status.setText('封面读取失败：' + str(error)[:120])
            return
        if not data_url:
            return
        self._picker_thumbnails[entry_id] = data_url
        if self._picker is not None:
            self._picker.set_thumbnails({entry_id: data_url})

    def _row_dropped(self, row_key, entry_id):
        """A library card dropped on an explicit material row previews there."""
        row = next((item for item in self._rows if item.key == row_key), None)
        if row is None or not row.selectable:
            return False
        self.select_material(row.working_id)
        if not row.working_id:
            return False
        selection = self.selection_for(entry_id)
        if not selection.frozen:
            self.preview_status.setText(
                '无法预览：材质库没有提供版本/摘要令牌，拖放未生效')
            return False
        self._library_preview(selection)
        return True

    def selection_for(self, entry_id, mode=MODE_SURFACE):
        """A frozen selection for one library entry (token from the source)."""
        entries = ()
        if self.library_source is not None:
            try:
                entries = tuple(self.library_source.entries() or ())
            except Exception as error:
                self._error = '材质库条目读取失败：' + str(error)[:120]
                entries = ()
        entry = next((item for item in entries
                      if getattr(item, 'entry_id', None) == entry_id), None)
        token = getattr(entry, 'snapshot_token', '') if entry is not None else ''
        return PickerSelection(entry_id, mode, token)

    # ------------------------------------------------------------------
    # Preview / commit results
    # ------------------------------------------------------------------
    def _preview_accepts(self, prepared):
        """A preview result is only installable for the current request+draft.

        Out-of-order, duplicated or post-cancel deliveries are refused. A
        refused ``PreparedMaterialView`` still carries a lease, which is handed
        back here so the renderer never keeps a stale view alive.

        The target is matched by model, not by full revision: the service may
        have replayed the preview onto a newer state than the request named.
        """
        reason = self._preview_session.rejection_reason(prepared, self._target)
        if reason is None:
            return True
        self._stale_publications += 1
        self._release_stale_lease(prepared)
        current = self._preview
        library_event('model','preview_result','stale',reason=reason,
            request_id=getattr(prepared,'request_id',None),
            received_generation=getattr(prepared,'draft_generation',None),
            expected_request_id=current.request_id if current else None,
            expected_generation=current.draft_generation if current else None)
        return False

    def _release_stale_lease(self, prepared):
        lease = getattr(prepared, 'lease_id', None) if isinstance(
            prepared, PreparedMaterialView) else None
        if lease:
            self._call('release', lease)

    @observed('model','ui_preview_result')
    def publish_preview(self, prepared, receipt=None):
        """A prepared view or a rendered frame arrived (still uncommitted)."""
        if not self._preview_accepts(prepared):
            return False
        current = self._preview
        if isinstance(prepared, PreparedMaterialView):
            if self._lease_id and self._lease_id != prepared.lease_id:
                self.release_lease()
            self._lease_id = prepared.lease_id
            self._preview = replace(current, lease_id=prepared.lease_id)
            self.preview_status.setText(
                f'预览已准备（代次 {prepared.draft_generation}，未提交）')
            return True
        if isinstance(prepared, MaterialRenderReceipt):
            self.preview_status.setText(
                f'预览已更新（代次 {prepared.draft_generation}，未提交）')
            return True
        return False

    @observed('model','ui_preview_failure')
    def publish_preview_failed(self, target, reason, request_id=None,
                               draft_generation=None):
        """Only the current preview may report failure."""
        if not self._same_model(target):
            self._stale_publications += 1
            return False
        current = self._preview
        if current is None:
            self._stale_publications += 1
            return False
        if request_id is not None and request_id != current.request_id:
            self._stale_publications += 1
            return False
        if draft_generation is not None and draft_generation != current.draft_generation:
            self._stale_publications += 1
            return False
        self.preview_status.setText('预览失败：' + (str(reason) or '渲染未确认') + '（未提交）')
        self.editor.show_preview_failed(str(reason or ''))
        return True

    @observed('model','ui_commit_receipt')
    def publish_commit(self, receipt):
        """Confirm exactly the command this receipt belongs to.

        Rules that keep a second edit alive while the first is confirming:
        * a receipt only confirms its own ``request_id``;
        * a duplicate receipt changes nothing and is not reported as new;
        * a receipt never clears a newer draft, its failure flag or its retry;
        * only the last outstanding commit may move the save line to 已保存.
        """
        if not isinstance(receipt, MaterialCommitReceipt):
            return False
        if not self._same_model(receipt.after):
            self._stale_publications += 1
            return False
        request_id = getattr(receipt, 'request_id', None)
        disposition, pending = self._commit_ledger.acknowledge(request_id)
        if disposition == 'duplicate':
            return True
        if disposition == 'unknown':
            self._stale_publications += 1
            return False
        if (self._committed_preview is None
                or replace(receipt.after, state_revision=self._committed_preview[0].state_revision) != self._committed_preview[0]
                or receipt.after.state_revision >= self._committed_preview[0].state_revision):
            self._committed_preview = (receipt.after, request_id)
        self.retry_preview_button.hide()
        self._retryable = None
        if pending.request_id == self._last_preview_request_id():
            self.cancel_preview('已提交', superseded=True)
            self.release_lease()
        self._refresh_save_status()
        if not self._commits:
            self.save_status.setText(f'已保存（命令 {receipt.command_id}）；封面待更新')
            self.editor.show_committed('已保存')
        else:
            # A newer edit is still outstanding: keep its draft and its retry.
            newest = self._pending_commit()
            self.save_status.setText(
                f'已保存（命令 {receipt.command_id}）；'
                f'另有 {len(self._commits)} 条提交仍在等待回执'
                + (f'（{newest.note}）' if newest is not None else ''))
        self.cover_status.setText('封面待更新：正式封面只采用最新的完整已提交状态。')
        self.cover_status.show()
        if self._identity is not None and self._complete:
            # Advance only forward: with replay, receipts may arrive while the
            # panel already moved on to an even newer revision.
            if receipt.after.state_revision >= self._target.state_revision:
                self._target = receipt.after
                self._identity = receipt.after
                self.inspect()
        return True

    @observed('model','ui_commit_failure')
    def publish_commit_failed(self, target, reason, request_id=None):
        """Fail exactly one command; matched by request id, not revision."""
        if not self._same_model(target):
            self._stale_publications += 1
            return False
        pending = self._commits.get(request_id) if request_id else None
        if request_id is not None and pending is None:
            # Confirmed already, or never submitted here: nothing to fail.
            self._stale_publications += 1
            return False
        if pending is None:
            newest = self._pending_commit()
            if newest is None:
                self._stale_publications += 1
                return False
            pending = newest
        failed = replace(pending, failed_reason=str(reason or '未提供原因'))
        self._commits[failed.request_id] = failed
        self._retryable = failed
        self._refresh_save_status()
        if self._pending_commit() is None:
            self.editor.show_commit_failed(failed.failed_reason)
        # Refresh the revision so the visible 重试 button rebuilds the request
        # against the current state instead of the stale one it just failed on.
        self.inspect()
        return True

    def _last_preview_request_id(self):
        return self._preview.request_id if self._preview is not None else None

    def publish_cover_updated(self, target):
        if not self._accepts(target):
            return False
        self.cover_status.setText('封面已更新')
        return True

    def _thumbnail_requested(self, request):
        getter = getattr(self.port, 'thumbnail', None) if self.port is not None else None
        if not callable(getter) or self._target is None:
            return
        try:
            data = getter(self._target, request.key)
        except Exception as error:
            self.preview_status.setText('材质小预览读取失败：' + str(error)[:120])
            return
        if data:
            self.publish_thumbnail(self._target, request.key, data)

    # ------------------------------------------------------------------
    # Jobs, drafts and lifecycle
    #
    # Five different user actions, five different meanings. They are NOT one
    # method: finishing an edit submits it, discarding an edit throws it away.
    # ------------------------------------------------------------------
    def _call(self, name, *args, **kwargs):
        port = self.port
        method = getattr(port, name, None) if port is not None else None
        if not callable(method):
            self._error = f'{name} 端口未接入'
            library_event('model','ui_port','rejected',action=name,reason='port_unavailable')
            return None
        try:
            return method(*args, **kwargs)
        except Exception as error:
            self._error = str(error)[:200]
            library_event('model','ui_port','failed',action=name,error_type=type(error).__name__,reason=str(error))
            return None

    def finish_edit(self):
        """Complete the pending gesture (switching material/model keeps it)."""
        self.editor.flush_gesture()

    def discard_edit(self):
        """Throw the draft away. Only a new identity or an explicit cancel."""
        self.editor.clear()
        self.editor.local_editing_changed.emit(False)
        self.cancel_preview('草稿已丢弃')
        self.release_lease()

    def cancel_preview(self, reason='', *, superseded=False):
        """Invalidate the local preview first, then cancel the job by job id.

        Order matters: clearing the token before calling the port means a frame
        that arrives during the cancel call is already stale. ``cancel`` only
        ever receives the job id the contract defines - never a request id. The
        lease is not dropped here: the caller releases it explicitly (or asks
        for the committed look first).
        """
        self._preview_updates.stop()
        preview = self._preview_session.take_current()
        if preview is not None and preview.job_id:
            if superseded:
                # Replacing intent must not invoke cancel's restore semantics.
                # Older adapters can finish; their invalidated results are rejected.
                if callable(getattr(self.port, 'supersede', None)):
                    self._call('supersede', preview.job_id)
            else:
                self._call('cancel', preview.job_id)
        if reason:
            self.preview_status.setText(f'草稿预览已取消（{reason}）；保存状态见下方')

    def release_lease(self):
        """Hand the renderer lease back exactly once."""
        lease = self._preview_session.take_lease()
        if lease:
            self._call('release', lease)

    def retry_committed_preview(self):
        if not self._complete or self._target is None or not callable(getattr(self.port,'retry_committed_preview',None)):
            return None
        target = self._target
        request_id = uuid.uuid4().hex
        self._committed_preview = (target, request_id)
        self.retry_preview_button.setEnabled(False)
        self.retry_preview_button.setToolTip('修改已保存，正在重试预览')
        self.preview_status.setText('修改已保存，正在重试预览')
        job = self._call('retry_committed_preview', target, request_id)
        if job is None:
            self.retry_preview_button.setEnabled(True)
            self.retry_preview_button.setToolTip('重试未启动；修改仍已保存')
        return job

    def restore_committed_preview(self):
        """Ask for a frame of the latest *committed* state.

        Cancel is not "put the picture back": the renderer keeps showing the
        last frame it installed until it receives a new one. If the port has no
        restore entry point the panel says so instead of implying the screen
        already matches the project.
        """
        self.cancel_preview('恢复已提交外观')
        self.release_lease()
        if not self._complete or self.port is None:
            return None
        restore = getattr(self.port, 'restore', None)
        if not callable(restore):
            self.preview_status.setText(
                '预览已取消；后端未提供“恢复已提交外观”端口，画面可能仍显示草稿帧')
            return None
        job = self._call('restore', self._target)
        if job is None:
            self.preview_status.setText('恢复已提交外观失败：任务未能启动')
            return None
        self.preview_status.setText(f'正在恢复已提交外观（任务 {job}）')
        return job

    def close_picker(self):
        """Closing the library picker: drop its draft, restore the real look."""
        if self._picker is not None:
            picker, self._picker = self._picker, None
            picker.close()
        return self.restore_committed_preview()

    def shutdown(self):
        """Closing the panel or the program: drop drafts, stop jobs, free leases."""
        self.discard_edit()
        for pending in list(self._commits.values()):
            if pending.job_id:
                self._call('cancel', pending.job_id)
        self._commits.clear()
        self._commit_order.clear()
        self._retryable = None
        self._picker = None
        self._material_view = None
        self._thumbnail_order.clear()

    def closeEvent(self, event):
        self.shutdown()
        super().closeEvent(event)

    # ------------------------------------------------------------------
    # Keyboard
    # ------------------------------------------------------------------
    def eventFilter(self, watched, event):
        """Ctrl+Z goes to the text field first, then to project history.

        Delete is swallowed while the focus is inside this panel so that it can
        never delete a model or an original file from here. Like the tracing
        filter, this one must never raise: an unexpected event class is skipped.
        """
        try:
            return self._filter_event(watched, event)
        except Exception:  # noqa: BLE001 - a filter must not crash the app
            return False

    def _filter_event(self, watched, event):
        if event.type() != QtCore.QEvent.Type.ShortcutOverride:
            return False
        if not self._focus_inside():
            return False
        key = event.key()
        modifiers = event.modifiers()
        ctrl = bool(modifiers & Qt.KeyboardModifier.ControlModifier)
        if key == Qt.Key.Key_Delete:
            event.accept()
            return True
        if ctrl and key in (Qt.Key.Key_Z, Qt.Key.Key_Y):
            # Accept the override so no canvas/model undo action claims it.
            # A focused text control still gets the real key press and undoes
            # its own text; otherwise the panel asks for one project step.
            event.accept()
            return True
        return False

    def keyPressEvent(self, event):
        ctrl = bool(event.modifiers() & Qt.KeyboardModifier.ControlModifier)
        if event.key() == Qt.Key.Key_Delete:
            event.accept()
            return
        if ctrl and event.key() in (Qt.Key.Key_Z, Qt.Key.Key_Y):
            if event.key() == Qt.Key.Key_Y or event.modifiers() & Qt.KeyboardModifier.ShiftModifier:
                self.request_redo()
            else:
                self.request_undo()
            event.accept()
            return
        super().keyPressEvent(event)

    def request_undo(self):
        """One project-history step; never a canvas or model delete."""
        self.finish_edit()
        self.undo_requested.emit(self._target)
        if self._complete and self.capabilities.edit_material and self.port is not None:
            self._commit(self._request(MaterialOperation.UNDO), note='正在撤销材质修改')

    def request_redo(self):
        self.finish_edit()
        if self._complete and self.capabilities.edit_material and self.port is not None:
            self._commit(self._request(MaterialOperation.REDO), note='正在重做材质修改')

    def _focus_inside(self):
        app = QtWidgets.QApplication.instance()
        focused = app.focusWidget() if app is not None else None
        if focused is None:
            return False
        return focused is self or self.isAncestorOf(focused)
