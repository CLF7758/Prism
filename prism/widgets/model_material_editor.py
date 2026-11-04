"""C2: edit ONE working material with the existing slot controls.

The editor owns no parameter rules of its own. Every number, channel name and
range comes from :mod:`prism.material_parameters` / :mod:`prism.material_textures`
through the very same ``MaterialSlot`` the single-material inspector uses, so
there is exactly one colour-space / channel / parameter definition in Prism.

What this module adds is the part the old full-model area cannot express:

* **three states per slot** - inherit, set, explicitly cleared. A cleared slot
  is not "inherit"; a slot whose image is missing is not "value 0".
* **provenance per slot** - original, resolved library snapshot, or a local
  override. A pure-numeric material stays legal.
* **one gesture, one commit** - a slider drag previews many times (increasing
  draft generation) and commits once. Project-history integration must
  separately provide undo; one commit alone does not implement it.

The editor never parses, never saves and never reads an original file: it emits
intents, the panel turns them into contract requests.
"""
import json
import uuid
from dataclasses import dataclass, field

from PyQt6 import QtCore, QtWidgets

from prism.material_textures import SLOTS
from prism.model_library.multimaterial.contracts import PatchAction

#: The three texture states of one slot. They are never collapsed into two.
STATE_INHERIT = 'inherit'
STATE_SET = 'set'
STATE_CLEAR = 'clear'

STATE_LABELS = (
    (STATE_INHERIT, '继承', '取消本地覆盖：重新使用原始/快照里的贴图与数值'),
    (STATE_SET, '设置贴图', '只为这一个材质指定一张贴图（本地覆盖）'),
    (STATE_CLEAR, '明确清除', '明确移除贴图，且不再继承旧图；数值仍然有效'),
)

#: Words used for the per-slot provenance line.
SOURCE_ORIGINAL = '原始'
SOURCE_SNAPSHOT = '库快照'
SOURCE_LOCAL = '本地覆盖'
SOURCE_CLEARED = '已明确清除'
SOURCE_UNKNOWN = '原始值未取得'


@dataclass(frozen=True)
class SlotView:
    """Everything the UI may show about one slot of one working material."""

    key: str
    state: str
    source_label: str
    binding: dict
    supported: bool = True
    reason: str = ''
    numeric_override: bool = False
    base_known: bool = True
    texture: object = None


@dataclass(frozen=True)
class MaterialView:
    """The single effective state the editor renders."""

    name: str
    source_note: str = ''
    properties: dict = field(default_factory=dict)
    slots: tuple[SlotView, ...] = ()
    unknown_base: bool = False
    alpha_channels: tuple[str, ...] = ()

    def slot(self, key):
        return next((view for view in self.slots if view.key == key), None)


def _properties(definition):
    payload = getattr(definition, 'properties_json', '{}') or '{}'
    try:
        parsed = json.loads(payload)
    except (TypeError, ValueError):
        return {'slots': {}}
    if not isinstance(parsed, dict):
        return {'slots': {}}
    slots = parsed.get('slots')
    parsed['slots'] = dict(slots) if isinstance(slots, dict) else {}
    return parsed


def _patch_properties(working):
    payload = getattr(working, 'property_patch_json', '{}') or '{}'
    try:
        parsed = json.loads(payload)
    except (TypeError, ValueError):
        return {'slots': {}}
    if not isinstance(parsed, dict):
        return {'slots': {}}
    slots = parsed.get('slots')
    parsed['slots'] = dict(slots) if isinstance(slots, dict) else {}
    return parsed


def merged_properties(definition, working):
    """Base slot options with the local numeric override applied on top."""
    base = _properties(definition)
    patch = _patch_properties(working)
    slots = dict(base.get('slots') or {})
    for key, options in (patch.get('slots') or {}).items():
        if not isinstance(options, dict):
            continue
        current = dict(slots.get(key) or {})
        current.update(options)
        slots[key] = current
    return {'slots': slots}


def _binding_of(texture):
    if texture is None:
        return {}
    locator = getattr(texture, 'source_locator', '') or getattr(texture, 'resource_id', '')
    # The catalog does not carry pixel sizes. The existing slot control needs
    # the two keys to render, so they are marked unknown rather than filled
    # with a number the UI would then be claiming as a measurement.
    binding = {'name': locator or '贴图原件', 'channel': getattr(texture, 'channels', '') or 'RGB',
               'resourceId': getattr(texture, 'resource_id', ''),
               'width': '未取得', 'height': '未取得'}
    if getattr(texture, 'color_space', ''):
        binding['colorSpace'] = texture.color_space
    if getattr(texture, 'texcoord', 0):
        binding['texcoord'] = texture.texcoord
    return binding


def build_material_view(definition, working, *, supported=None, base_kind='original'):
    """Build the effective view of one working material.

    ``definition`` is the base material **as the backend resolved it** (an
    original definition or a resolved snapshot). It is never derived here: the
    panel asks the base source and hands the answer over.

    ``base_kind`` is ``'original'``, ``'snapshot'`` or ``'unknown'``. It only
    chooses the provenance word; it never changes the values.
    """
    blocked = dict(supported or {})
    base_properties = _properties(definition)
    patches = {}
    for patch in (getattr(working, 'texture_patches', ()) or ()):
        patches[patch.slot] = patch
    base_textures = {texture.slot: texture for texture in
                     (getattr(definition, 'textures', ()) or ())}
    patched = _patch_properties(working)
    properties = merged_properties(definition, working)
    base_known = definition is not None and base_kind != 'unknown'
    source_word = SOURCE_SNAPSHOT if base_kind == 'snapshot' else SOURCE_ORIGINAL
    views = []
    for key in SLOTS:
        patch = patches.get(key)
        if patch is not None and patch.action == PatchAction.CLEAR:
            state, texture = STATE_CLEAR, None
        elif patch is not None and patch.action == PatchAction.SET:
            state, texture = STATE_SET, patch.value
        else:
            state, texture = STATE_INHERIT, base_textures.get(key)
        overridden_numbers = key in (patched.get('slots') or {})
        if state == STATE_CLEAR:
            label = SOURCE_CLEARED
        elif state == STATE_SET:
            label = SOURCE_LOCAL
        elif not base_known:
            label = SOURCE_UNKNOWN
        else:
            label = source_word
        if overridden_numbers and state != STATE_SET:
            label += '（数值覆盖）'
        reason = blocked.get(key, '')
        views.append(SlotView(
            key=key,
            state=state,
            source_label=label,
            binding=_binding_of(texture),
            supported=not reason,
            reason=reason,
            numeric_override=overridden_numbers,
            base_known=base_known,
            texture=texture,
        ))
    alpha = _alpha_channels(base_textures, patches, base_properties)
    note_source = {'snapshot': '继承自材质库快照（由后端解析）',
                   'original': '继承自模型原材质'}.get(
                       base_kind, '基础材质未取得，继承值不可核对')
    return MaterialView(
        name=getattr(working, 'display_name', '') or '未命名材质',
        source_note=note_source if base_known else '原始定义未取得，继承值不可核对',
        properties=properties,
        slots=tuple(views),
        unknown_base=not base_known,
        alpha_channels=alpha,
    )


def _alpha_channels(base_textures, patches, base_properties):
    """Slots whose colour image also carries the alpha / cut-out channel.

    Replacing such a colour image with an RGB-only one would silently change
    the silhouette, so the picker has to state that the old alpha survives.
    Only declarations are read here; no image is decoded.
    """
    channels = []
    for key, texture in base_textures.items():
        if key not in ('basecolor', 'opacity'):
            continue
        declared = (getattr(texture, 'channels', '') or '').upper()
        if 'A' in declared.split('+') or declared == 'A':
            channels.append(key)
    opacity = base_textures.get('opacity')
    if opacity is not None and opacity.slot not in channels:
        channels.append(opacity.slot)
    return tuple(channels)


class SlotStateBar(QtWidgets.QWidget):
    """The three-state switch above one slot editor."""

    state_requested = QtCore.pyqtSignal(str)

    def __init__(self, key, parent=None):
        super().__init__(parent)
        self.key = key
        layout = QtWidgets.QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(4)
        if key == 'basecolor':
            layout.addWidget(QtWidgets.QLabel('状态'))
        self.buttons = {}
        self.group = QtWidgets.QButtonGroup(self)
        self.group.setExclusive(True)
        for state, label, tip in STATE_LABELS:
            button = QtWidgets.QToolButton()
            button.setText(label)
            button.setToolTip(tip)
            button.setCheckable(True)
            button.clicked.connect(lambda _checked=False, value=state: self._request(value))
            self.group.addButton(button)
            self.buttons[state] = button
            layout.addWidget(button)
        self.source = QtWidgets.QLabel('')
        self.source.setProperty('uiRole', 'secondarySmall')
        self.source.setSizePolicy(QtWidgets.QSizePolicy.Policy.Ignored,
                                  QtWidgets.QSizePolicy.Policy.Preferred)
        layout.addWidget(self.source, 1)

    def _request(self, state):
        # Re-clicking the active state must not send an empty request.
        if self._state == state:
            return
        self.state_requested.emit(state)

    def show_state(self, state, source_label):
        self._state = state
        button = self.buttons.get(state)
        if button is not None and not button.isChecked():
            button.setChecked(True)
        self.source.setText(source_label)
        self.source.setToolTip(source_label)

    def state(self):
        return getattr(self, '_state', STATE_INHERIT)


class MaterialEditor(QtWidgets.QWidget):
    """Reuses ``MaterialSlot`` for one working material."""

    #: Continuous option change. Payload: (slot, patch, gesture_id, generation).
    preview_requested = QtCore.pyqtSignal(object, object, str, int)
    #: The gesture ended. Payload: (slot, merged patch, gesture_id, generation).
    commit_requested = QtCore.pyqtSignal(object, object, str, int)
    #: Discrete texture intent. Payload: (slot, action, detail).
    texture_requested = QtCore.pyqtSignal(object, str, object)
    #: The user asked to reuse an original of this material in a slot.
    reuse_requested = QtCore.pyqtSignal(str)
    #: The user asked to retry the last failed commit without losing the draft.
    retry_requested = QtCore.pyqtSignal()
    #: This editor now really edits a local material (legacy area must fold).
    local_editing_changed = QtCore.pyqtSignal(bool)

    def __init__(self, parent=None):
        from .widget_construction import finish_construction
        finish_construction(self._construct(parent))

    def _construct(self, parent=None):
        from .widget_construction import widget_steps
        super().__init__(parent)
        from prism.widgets.material_slots import MaterialSlot, configure_material_slot_column
        self.setObjectName('modelMaterialEditor')
        self._view = None
        self._editable = False
        self._gesture = None
        self._generation = 0
        self._slider_active = False
        self._draft_failure = ''
        layout = QtWidgets.QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(6)
        heading = QtWidgets.QHBoxLayout()
        self.title = QtWidgets.QLabel('未选择材质')
        self.title.setProperty('uiRole', 'inspectorTitle')
        heading.addWidget(self.title, 1)
        layout.addLayout(heading)
        self.impact = QtWidgets.QLabel('')
        self.impact.setProperty('uiRole', 'secondary')
        self.impact.setWordWrap(True)
        layout.addWidget(self.impact)
        self.source_note = QtWidgets.QLabel('')
        self.source_note.setProperty('uiRole', 'secondarySmall')
        self.source_note.setWordWrap(True)
        layout.addWidget(self.source_note)
        self.notice = QtWidgets.QLabel('')
        self.notice.setProperty('uiRole', 'secondary')
        self.notice.setWordWrap(True)
        layout.addWidget(self.notice)
        self.bars = {}
        self.slots = {}
        scroll = QtWidgets.QScrollArea(self)
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(QtCore.Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        scroll.setFrameShape(QtWidgets.QFrame.Shape.NoFrame)
        body = QtWidgets.QWidget()
        scroll.setWidget(body)
        layout.addWidget(scroll, 1)
        body_layout = QtWidgets.QVBoxLayout(body)
        body_layout.setContentsMargins(0, 0, 0, 0)
        configure_material_slot_column(body_layout)
        yield
        for key, title in SLOTS.items():
            bar = SlotStateBar(key)
            bar.state_requested.connect(
                lambda state, slot=key: self.texture_requested.emit(slot, state, {}))
            self.bars[key] = bar
            body_layout.addWidget(bar)
            bar.hide()
            slot = yield from widget_steps(MaterialSlot, key, title, body)
            slot.setEnabled(False)
            self.slots[key] = slot
            slot.filename.setMinimumWidth(0)
            slot.filename.setSizePolicy(QtWidgets.QSizePolicy.Policy.Ignored,QtWidgets.QSizePolicy.Policy.Preferred)
            slot.channel.setMinimumWidth(0)
            body_layout.addWidget(slot)
            slot.options_requested.connect(self._options_changed)
            slot.file_requested.connect(
                lambda slot_key, path: self.texture_requested.emit(slot_key, 'set', {'path': path}))
            slot.channel_requested.connect(
                lambda slot_key, channel: self.texture_requested.emit(
                    slot_key, 'channel', {'channel': channel}))
            slot.clear_requested.connect(
                lambda slot_key: self.texture_requested.emit(slot_key, STATE_CLEAR, {}))
            slot.reuse_requested.connect(self.reuse_requested.emit)
            slot.slider.sliderPressed.connect(self._slider_pressed)
            slot.slider.sliderReleased.connect(self._slider_released)
            slot.amount.editingFinished.connect(self._editing_finished)
            yield
        draft = QtWidgets.QHBoxLayout()
        self.draft_status = QtWidgets.QLabel('')
        self.draft_status.setProperty('uiRole', 'secondary')
        self.draft_status.setWordWrap(True)
        draft.addWidget(self.draft_status, 1)
        self.retry = QtWidgets.QToolButton()
        self.retry.setText('重试')
        self.retry.setToolTip('用当前草稿重新提交；不会跳回旧值')
        self.retry.clicked.connect(self.retry_requested.emit)
        self.retry.hide()
        draft.addWidget(self.retry)
        self._hidden_draft = QtWidgets.QWidget(self)
        self._hidden_draft.setLayout(draft)
        self._hidden_draft.hide()

    # ------------------------------------------------------------------
    # Presenting one material
    # ------------------------------------------------------------------
    def clear(self):
        self._view = None
        self._editable = False
        self._gesture = None
        self.title.setText('未选择材质')
        self.impact.setText('')
        self.source_note.setText('')
        self.notice.setText('')
        self.draft_status.setText('')
        self.retry.hide()
        for key, bar in self.bars.items():
            bar.show_state(STATE_INHERIT, '')
            self.slots[key].setEnabled(False)
            self.slots[key].setToolTip('')

    def show_material(self, view, *, impact_text='', editable=False, reason=''):
        """Render one effective state. The values are never re-derived here."""
        self._view = view
        self._gesture = None
        self._slider_active = False
        if view is None:
            self.clear()
            self.local_editing_changed.emit(False)
            return
        self.title.setText(view.name)
        self.impact.setText(impact_text or '')
        self.source_note.setText(view.source_note)
        reasons = []
        self._editable = bool(editable) and not reason
        for slot_view in view.slots:
            bar = self.bars[slot_view.key]
            bar.show_state(slot_view.state, slot_view.source_label)
            slot = self.slots[slot_view.key]
            slot.show_binding(slot_view.binding, view.properties)
            self._describe_texture(slot, slot_view)
            usable = self._editable and slot_view.supported
            slot.setEnabled(usable)
            bar.setEnabled(self._editable)
            for button in bar.buttons.values():
                button.setEnabled(self._editable)
            if not slot_view.supported:
                slot.setToolTip(slot_view.reason)
                bar.setToolTip(slot_view.reason)
                reasons.append(f'{SLOTS[slot_view.key]}：{slot_view.reason}')
            elif reason:
                slot.setToolTip(reason)
                bar.setToolTip(reason)
            else:
                slot.setToolTip('复用现有贴图控件；只作用于这一个材质')
                bar.setToolTip('')
            slot.invert.setEnabled(usable)
        notes = []
        if reason:
            notes.append(reason)
        if reasons:
            notes.append('以下通道后端未支持，已禁用：' + '；'.join(reasons))
        if view.unknown_base:
            notes.append('原始定义未取得：继承值不可核对，仍可设置本地覆盖')
        self.notice.setText('\n'.join(notes))
        self.notice.hide();self.title.hide();self.impact.hide();self.source_note.hide()
        self.notice.setToolTip('\n'.join(notes))
        if editable and reasons:
            self.notice.setText('部分通道暂不可用，悬停查看原因；其余槽位可以编辑。')
        if self._draft_failure:
            self.draft_status.setText(self._draft_failure)
            self.retry.show()
        else:
            self.draft_status.setText('')
            self.retry.hide()
        self.local_editing_changed.emit(self._editable)

    def _describe_texture(self, slot, slot_view):
        """State what this slot's image is, without claiming a resolution.

        The existing control fills its tooltip with pixel dimensions; the
        multi-material catalog does not measure them, so the panel replaces the
        tooltip instead of showing a measurement it never took.
        """
        if slot_view.state == STATE_CLEAR:
            text = '贴图已明确清除；数值仍然有效，也不再回退到旧图'
        elif slot_view.state == STATE_INHERIT:
            text = '继承贴图（' + slot_view.source_label + '）；数值未被覆盖'
        elif slot_view.binding:
            text = '本地覆盖贴图：' + str(slot_view.binding.get('name', ''))
        else:
            text = '本地覆盖：贴图缺失'
        if slot_view.numeric_override:
            text += '\n数值为本地覆盖'
        text += '\n分辨率由后端报告，界面不猜测；只作用于这一个材质'
        slot.filename.setToolTip(text)
        slot.drop.setToolTip(text)

    def show_committed(self, text='已保存'):
        """A commit receipt arrived; the draft is no longer a draft."""
        self._draft_failure = ''
        self.draft_status.setText(text)
        self.retry.hide()

    def show_commit_failed(self, reason):
        """Keep the user's values on screen and say the commit failed.

        Reverting the controls to the stored values here would tell the user
        the old numbers are what they now have, which they are not.
        """
        self._draft_failure = '未保存（草稿保留）：' + (reason or '提交失败')
        self.draft_status.setText(self._draft_failure)
        self.retry.show()

    def show_preview_failed(self, reason):
        self.draft_status.setText('预览失败：' + (reason or '渲染未确认') + '（草稿保留）')
        self.retry.show()

    def draft_failure(self):
        return self._draft_failure

    # ------------------------------------------------------------------
    # Gestures
    # ------------------------------------------------------------------
    def _options_changed(self, key, patch):
        if not self._editable or self._view is None:
            return
        slot_view = self._view.slot(key)
        if slot_view is not None and not slot_view.supported:
            return
        gesture = self._gesture
        if gesture is None or gesture['slot'] != key:
            self._commit_gesture()
            gesture = {'id': uuid.uuid4().hex, 'slot': key, 'patch': {}}
            self._gesture = gesture
        gesture['patch'].update(patch)
        self._generation += 1
        self.preview_requested.emit(key, dict(gesture['patch']), gesture['id'], self._generation)
        if not self._slider_active:
            self._commit_gesture()

    def _slider_pressed(self):
        self._slider_active = True
        self._gesture = None

    def _slider_released(self):
        self._slider_active = False
        self._commit_gesture()

    def _editing_finished(self):
        if not self._slider_active:
            self._commit_gesture()

    def _commit_gesture(self):
        gesture, self._gesture = self._gesture, None
        if gesture and gesture['patch']:
            self.commit_requested.emit(gesture['slot'], dict(gesture['patch']),
                                       gesture['id'], self._generation)

    def flush_gesture(self):
        """Submit the pending gesture before the target changes."""
        self._slider_active = False
        self._commit_gesture()

    def discard_gesture(self):
        self._slider_active=False
        self._gesture=None
        self._draft_failure=''
        self.retry.hide()

    def gesture_generation(self):
        return self._generation

    def editable(self):
        return self._editable
