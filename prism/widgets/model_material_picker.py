"""C3: choose a material from the existing library without leaving the model.

The picker reuses Prism's own card surface, tag search and cached thumbnails.
It is deliberately not a "long list of names": the user sees the same cards as
in the material library, filtered by the same text/tag search, inside the model
context.

Two rules make the workflow safe:

* **Preview is temporary.** Selecting a card asks for a preview draft. Only the
  explicit "应用" button commits, and closing the dialog cancels the draft.
* **Surface replace is the default.** It keeps transparency / cut-out, two
  sidedness, SSS, displace and the geometry assignment, and states that. Full
  replacement is an advanced, explicitly described option.

This module never reads a material file, never renders and never saves.
"""
import json
from dataclasses import dataclass

from PyQt6 import QtCore, QtWidgets

from prism.material_textures import SLOTS
from prism.model_library.multimaterial.contracts import MaterialOperation
from prism.widgets.model_material_list import MATERIAL_MIME

MODE_SURFACE = MaterialOperation.REPLACE_SURFACE.value
MODE_FULL = MaterialOperation.REPLACE_FULL.value

#: Channels a surface replacement owns.
SURFACE_CHANNELS = ('颜色', '法线', '粗糙度', '金属度', 'AO', '自发光与数值')

#: What a surface replacement keeps, and why the user can trust it.
PRESERVED = ('透明 / 镂空', '双面', 'SSS', '置换', '几何分配')

#: Two promises the picker must state: a card sphere is not the model, and an
#: applied material is a snapshot rather than a live link to the library.
SNAPSHOT_NOTE = ('应用后模型保存这份材质的快照：之后修改或删除库材质不会改变已应用的模型，'
                 '模型里只保留来源与版本来历。卡片上的材质球不等于模型本体，'
                 '最终请以模型预览确认。')


@dataclass(frozen=True)
class LibraryEntry:
    """One material-library card, as the picker needs it.

    ``snapshot_token`` is the frozen identity of the library content (a version
    or digest). Preview and apply must both carry it so the two commands can
    only ever describe the same bytes; without it the picker refuses to apply.
    """

    entry_id: str
    name: str
    tags: tuple = ()
    thumbnail: str | None = None
    channels: tuple = ()
    version: str = ''
    resolution: str = ''
    file_count: int | None = None
    snapshot_token: str = ''

    def matches(self, text):
        text = (text or '').strip().lower()
        if not text:
            return True
        haystack = ' '.join((self.name or '', ' '.join(self.tags or ()))).lower()
        return all(word in haystack for word in text.split())

    def tags_text(self):
        return '、'.join(self.tags or ())


@dataclass(frozen=True)
class PickerSelection:
    """What the user picked, in which mode, frozen at which library version."""

    entry_id: str
    mode: str = MODE_SURFACE
    snapshot_token: str = ''

    @property
    def operation(self):
        return MaterialOperation(self.mode)

    @property
    def frozen(self):
        """The library content this selection names can be re-fetched exactly."""
        return bool(self.snapshot_token)

    def parameters(self):
        """Operation parameters only; targets travel in working_ids.

        Which channels a surface replacement keeps is a **service** rule, not a
        UI deduction: the panel states what it promises on screen, but it does
        not decide it by reading a channel name.
        """
        return {'entry_id': self.entry_id, 'mode': self.mode,
                'snapshot_token': self.snapshot_token}


class MaterialPicker(QtWidgets.QDialog):
    """Modal chooser; the caller owns the request/commit transactions."""

    #: A card was focused. Payload is a ``PickerSelection`` (temporary preview).
    preview_requested = QtCore.pyqtSignal(object)
    #: The user pressed 应用. Payload is a ``PickerSelection``.
    apply_requested = QtCore.pyqtSignal(object)
    #: The dialog closed without applying. Payload is the last selection or None.
    cancelled = QtCore.pyqtSignal(object)
    #: The user asked for a card preview image; payload is the entry id.
    thumbnail_requested = QtCore.pyqtSignal(str)

    def __init__(self, parent=None, *, card_factory=None):
        super().__init__(parent)
        self.setObjectName('modelMaterialPicker')
        self.setWindowTitle('选择材质')
        self.resize(760, 560)
        self._entries = ()
        self._visible = ()
        self._selected = None
        self._blocked_reason = ''
        self._alpha_slots = ()
        self._full_changes = ()
        self._source = None
        self._thumbnails = {}
        self._card_factory = card_factory
        self._cards = None
        layout = QtWidgets.QVBoxLayout(self)
        layout.setContentsMargins(16, 14, 16, 12)
        layout.setSpacing(8)
        self.target = QtWidgets.QLabel('')
        self.target.setProperty('uiRole', 'inspectorTitle')
        self.target.setWordWrap(True)
        layout.addWidget(self.target)
        self.scope = QtWidgets.QLabel('')
        self.scope.setProperty('uiRole', 'secondary')
        self.scope.setWordWrap(True)
        layout.addWidget(self.scope)
        search_row = QtWidgets.QHBoxLayout()
        self.search = QtWidgets.QLineEdit()
        self.search.setPlaceholderText('按名称或标签筛选材质')
        self.search.setClearButtonEnabled(True)
        self.search.textChanged.connect(self._refilter)
        search_row.addWidget(self.search, 1)
        layout.addLayout(search_row)
        self.tag_row = QtWidgets.QHBoxLayout()
        self.tag_row.setSpacing(4)
        self.tag_buttons = {}
        layout.addLayout(self.tag_row)
        self.hint = QtWidgets.QLabel('')
        self.hint.setProperty('uiRole', 'secondarySmall')
        self.hint.setWordWrap(True)
        layout.addWidget(self.hint)
        self.cards_host = QtWidgets.QFrame()
        self.cards_layout = QtWidgets.QVBoxLayout(self.cards_host)
        self.cards_layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self.cards_host, 1)
        mode_group = QtWidgets.QGroupBox('替换方式')
        mode_layout = QtWidgets.QVBoxLayout(mode_group)
        self.surface_mode = QtWidgets.QRadioButton('替换表面（默认）')
        self.surface_mode.setToolTip('替换 ' + '、'.join(SURFACE_CHANNELS))
        self.surface_mode.setChecked(True)
        self.full_mode = QtWidgets.QRadioButton('替换完整材质（高级）')
        self.full_mode.setToolTip('完整采用新材质定义与它的默认值')
        mode_layout.addWidget(self.surface_mode)
        self.surface_mode.toggled.connect(self._mode_changed)
        self.mode_note = QtWidgets.QLabel('')
        self.mode_note.setProperty('uiRole', 'secondary')
        self.mode_note.setWordWrap(True)
        mode_layout.addWidget(self.mode_note)
        mode_layout.addWidget(self.full_mode)
        self.full_note = QtWidgets.QLabel('')
        self.full_note.setProperty('uiRole', 'secondary')
        self.full_note.setWordWrap(True)
        self.full_note.hide()
        mode_layout.addWidget(self.full_note)
        layout.addWidget(mode_group)
        self.alpha_note = QtWidgets.QLabel('')
        self.alpha_note.setProperty('uiRole', 'secondary')
        self.alpha_note.setWordWrap(True)
        self.alpha_note.hide()
        layout.addWidget(self.alpha_note)
        self.blocked_note = QtWidgets.QLabel('')
        self.blocked_note.setProperty('uiRole', 'secondary')
        self.blocked_note.setWordWrap(True)
        self.blocked_note.hide()
        layout.addWidget(self.blocked_note)
        self.frozen_note = QtWidgets.QLabel('')
        self.frozen_note.setProperty('uiRole', 'secondary')
        self.frozen_note.setWordWrap(True)
        self.frozen_note.hide()
        layout.addWidget(self.frozen_note)
        buttons = QtWidgets.QHBoxLayout()
        self.status = QtWidgets.QLabel('')
        self.status.setProperty('uiRole', 'secondary')
        buttons.addWidget(self.status, 1)
        self.apply = QtWidgets.QPushButton('应用')
        self.apply.setEnabled(False)
        self.apply.clicked.connect(self._apply)
        self.cancel = QtWidgets.QPushButton('取消')
        self.cancel.clicked.connect(self.reject)
        buttons.addWidget(self.cancel)
        buttons.addWidget(self.apply)
        layout.addLayout(buttons)
        self._update_mode_note()

    # ------------------------------------------------------------------
    # Target and contents
    # ------------------------------------------------------------------
    def set_target(self, *, name='', impact='', alpha_slots=(), blocked_reason='',
                   full_changes=()):
        """Describe the material being replaced. Every limit is stated."""
        self._alpha_slots = tuple(alpha_slots or ())
        self._blocked_reason = blocked_reason or ''
        self.target.setText(f'为「{name}」选择材质' if name else '选择材质')
        self.scope.setText(impact)
        self._full_changes = tuple(full_changes or ())
        self.alpha_note.setVisible(bool(self._alpha_slots))
        if self._alpha_slots:
            self.alpha_note.setText(
                '旧' + '、'.join(self._alpha_slots)
                + '的贴图带 A 通道（镂空）。界面只负责说明：是否保留由服务端的替换规则决定，'
                  '界面不按通道名自行推断，也不会把 A 通道当成可覆盖的 RGB。')
        self.blocked_note.setVisible(bool(self._blocked_reason))
        self.blocked_note.setText(self._blocked_reason)
        self._refresh_apply()
        self._update_mode_note()

    def set_entries(self, entries, source=None):
        """Install the library cards. Never reads a file to build them.

        ``source`` is optional: when it can also yield the raw material-library
        records, the existing card record path (``set_records``) is used instead
        of a second presentation format.
        """
        self._entries = tuple(entries or ())
        self._source = source
        self._rebuild_tags()
        self._refilter()

    def entries(self):
        return self._entries

    def visible_entries(self):
        return self._visible

    def selected(self):
        if self._selected is None:
            return None
        entry = next((item for item in self._entries
                      if item.entry_id == self._selected), None)
        token = entry.snapshot_token if entry is not None else ''
        return PickerSelection(self._selected, self.mode(), token)

    def mode(self):
        return MODE_FULL if self.full_mode.isChecked() else MODE_SURFACE

    def _apply_reason(self):
        """Why 应用 is unavailable right now, or an empty string."""
        if self._blocked_reason:
            return self._blocked_reason
        selection = self.selected()
        if selection is None:
            return '先在卡片里选择一个材质'
        if not selection.frozen:
            return ('材质库没有提供版本或摘要令牌：预览与应用无法保证是同一份内容，'
                    '已禁用应用')
        return ''

    def _rebuild_tags(self):
        while self.tag_row.count():
            item = self.tag_row.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.deleteLater()
        self.tag_buttons = {}
        tags = []
        for entry in self._entries:
            for tag in entry.tags or ():
                if tag not in tags:
                    tags.append(tag)
        for tag in tags[:12]:
            button = QtWidgets.QToolButton()
            button.setText(tag)
            button.setCheckable(True)
            button.setToolTip('把这一标签加入筛选')
            button.toggled.connect(self._tag_toggled)
            self.tag_buttons[tag] = button
            self.tag_row.addWidget(button)
        if not tags:
            self.tag_row.addWidget(QtWidgets.QLabel('材质库还没有标签'))
        self.tag_row.addStretch(1)

    def _tag_toggled(self, _checked):
        active = [tag for tag, button in self.tag_buttons.items() if button.isChecked()]
        self.search.setText(' '.join(active))

    def _refilter(self, *_):
        self._visible = tuple(entry for entry in self._entries
                             if entry.matches(self.search.text()))
        if self._selected is not None and self._selected not in {
                entry.entry_id for entry in self._visible}:
            self._selected = None
        self._install_cards()
        self._update_hint()
        self._refresh_apply()

    def _update_hint(self):
        if not self._entries:
            self.hint.setText('材质库中没有可用材质（不是“搜索无结果”）')
        elif not self._visible:
            self.hint.setText('没有符合筛选条件的材质')
        else:
            self.hint.setText(f'{len(self._visible)} / {len(self._entries)} 个材质')

    def _refresh_apply(self):
        reason = self._apply_reason()
        self.apply.setEnabled(not reason)
        self.apply.setToolTip(reason or '应用这一个材质（一次事务）')
        self.frozen_note.setVisible(bool(reason) and not self._blocked_reason
                                    and bool(self._selected))
        self.frozen_note.setText(reason)

    def _install_cards(self):
        if self._cards is None:
            if not self._visible:
                return
            self._cards = self._make_cards()
            self.cards_layout.addWidget(self._cards)
        records = self._records_for_visible()
        install_records = getattr(self._cards, 'set_records', None)
        if records is not None and callable(install_records):
            # Reuse the material library's own record path: it already knows the
            # source label, version label, resolution and placeholder rules.
            install_records(records, selected_id=self._selected,
                            thumbnail_for=self._thumbnail_for_record)
            return
        assets = []
        for entry in self._visible:
            assets.append(dict(
                id=entry.entry_id, name=entry.name, tags=list(entry.tags or ()),
                thumbnail=entry.thumbnail or '', thumbnailPending=not entry.thumbnail,
                sourceLabel='材质库', versionLabel=entry.version,
                resolution=entry.resolution, fileCount=entry.file_count or 0,
                maps=[{'channel': channel} for channel in (entry.channels or ())]))
        self._cards.set_display_assets(
            assets, selected_id=self._selected, selected_ids=(), bounded=False)

    def _records_for_visible(self):
        """Raw library records for the visible entries, when the source has them."""
        getter = getattr(self._source, 'records', None) if self._source is not None else None
        if not callable(getter):
            return None
        try:
            records = {record.get('id'): record for record in (getter() or ())}
        except Exception:
            return None
        visible = [records.get(entry.entry_id) for entry in self._visible]
        if any(record is None for record in visible):
            return None
        return visible

    def _thumbnail_for_record(self, record):
        entry_id = record.get('id') if isinstance(record, dict) else None
        cached = self._thumbnails.get(entry_id)
        if cached:
            return cached
        # The shared card surface requests images for visible cards only.
        # Reading every image here blocks opening a large library.
        return None

    def _make_cards(self):
        factory = self._card_factory
        if factory is None:
            from prism.widgets.material_cards import MaterialCards
            factory = MaterialCards
        cards = factory(self)
        select = getattr(cards, 'selection_toggled', None)
        if select is not None:
            select.connect(self._card_focused)
        else:  # a test double may only expose selection_changed
            cards.selection_changed.connect(self._card_focused)
        thumbnail = getattr(cards, 'thumbnail_requested', None)
        if thumbnail is not None:
            thumbnail.connect(self.thumbnail_requested.emit)
        return cards

    def _card_focused(self, entry_id, *_):
        if not entry_id:
            return
        if entry_id not in {entry.entry_id for entry in self._visible}:
            return
        self._selected = entry_id
        self._refresh_apply()
        selection = self.selected()
        if self._blocked_reason:
            self.status.setText('预览不可用：' + self._blocked_reason)
            return
        self.status.setText('预览中（未提交）')
        self.preview_requested.emit(selection)

    def _mode_changed(self, *_):
        self._update_mode_note()
        selection = self.selected()
        if selection is not None and not self._blocked_reason:
            self.status.setText('预览中（未提交）')
            self.preview_requested.emit(selection)

    def _update_mode_note(self):
        self.full_note.setVisible(self.mode() == MODE_FULL)
        if self.mode() == MODE_FULL:
            changes = self._full_changes or ('颜色、法线、粗糙度、金属度、AO、自发光',
                                             '透明 / 双面 / SSS / 置换的数值将采用新材质定义')
            self.mode_note.setText('会保留：几何分配。会采用新材质自身的默认值。'
                                   + '\n' + SNAPSHOT_NOTE)
            self.full_note.setText('完整替换会改变：' + '；'.join(changes))
        else:
            self.mode_note.setText(
                '保留：' + '、'.join(PRESERVED) + '。替换：' + '、'.join(SURFACE_CHANNELS) + '。'
                + '\n' + SNAPSHOT_NOTE)

    def set_status(self, text, kind='secondary'):
        self.status.setText(text)
        self.status.setProperty('uiRole', kind)

    def set_thumbnails(self, images):
        """Install cached card images; ``images`` maps entry id to a data URL."""
        for entry_id, data_url in (images or {}).items():
            self._thumbnails[entry_id] = data_url
        if self._cards is None:
            return
        for entry_id, data_url in (images or {}).items():
            entry = next((item for item in self._visible if item.entry_id == entry_id), None)
            if entry is None:
                continue
            self._cards.update_thumbnail(
                {'id': entry_id, 'name': entry.name, 'tags': list(entry.tags or ())}, data_url)

    # ------------------------------------------------------------------
    # Applying and closing
    # ------------------------------------------------------------------
    def _note_picker(self, event, **fields):
        try:
            from prism.performance_trace import record
            record(event, picker_id=id(self), **fields)
        except Exception:                          # noqa: BLE001 - logging only
            pass

    def _apply(self):
        # Record first: reading dialog state can fail on a partially built
        # picker, and a logging call must never be gated behind that.
        selected = getattr(self, '_selected', None)
        entries = list(getattr(self, '_entries', ()) or ())
        visible = list(getattr(self, '_visible', ()) or ())
        entry = next((item for item in entries
                      if getattr(item, 'entry_id', None) == selected), None)
        self._note_picker('material_picker.applied',
                          selected=selected or '',
                          name=(getattr(entry, 'name', '') if entry else ''),
                          entries=len(entries), visible=len(visible),
                          blocked=bool(getattr(self, '_blocked_reason', '')))
        selection = self.selected()
        reason = self._apply_reason()
        if selection is None or reason:
            self.status.setText(reason or '未选择材质')
            return
        self.apply_requested.emit(selection)
        self.accept()

    def reject(self):
        # Closing never commits: the only commit path is the 应用 button.
        selection = self.selected()
        super().reject()
        self.cancelled.emit(selection)

    def closeEvent(self, event):
        super().closeEvent(event)
        if not self.result():
            self.cancelled.emit(self.selected())

    @staticmethod
    def mime_data(entry_id):
        """The drag payload a card must publish for an explicit material drop."""
        mime = QtCore.QMimeData()
        mime.setData(MATERIAL_MIME, json.dumps({'entry_id': entry_id}).encode('utf-8'))
        return mime


class SceneMaterialLibrarySource:
    """Adapter: the project's material records → picker entries.

    Reads the in-memory project records only. It never opens the database and
    never reads a texture file: card images come from the existing cached
    attachment helper the material library already uses.

    ``scene_getter`` may be a callable so a long-lived source always sees the
    current project instead of a closed one.
    """

    def __init__(self, scene_getter, snapshotter=None, thumbnail_reader=None):
        self._scene_getter = scene_getter
        self._snapshotter = snapshotter
        self._thumbnail_reader = thumbnail_reader
        self._resolved = {}

    def _scene(self):
        getter = self._scene_getter
        return getter() if callable(getter) else getter

    def _records(self):
        scene = self._scene()
        return [record for record in (getattr(scene, 'material_records', ()) or ())
                if isinstance(record, dict) and record.get('deletedAt') is None]

    def records(self):
        return tuple(self._records())

    def entries(self):
        from prism.widgets.material_cards import material_version_label
        entries = []
        for record in sorted(self._records(),
                             key=lambda item: (item.get('name') or '').lower()):
            textures = record.get('textures') or {}
            entries.append(LibraryEntry(
                entry_id=record.get('id') or '',
                name=record.get('name') or '',
                tags=tuple(record.get('tags') or ()),
                channels=tuple(key for key in textures if key in SLOTS),
                version=material_version_label(record),
                file_count=len(record.get('files') or textures) or None,
                # A frozen version/digest is mandatory for preview == apply. The
                # library record is the only honest source; when it has none the
                # picker refuses to apply instead of guessing.
                snapshot_token=str(self._snapshotter(record) if self._snapshotter else record.get('content_digest')
                                   or record.get('digest') or '')))
        return tuple(entry for entry in entries if entry.entry_id)

    def thumbnail(self, entry_id):
        record = next((item for item in self._records()
                       if item.get('id') == entry_id), None)
        if record is None:
            return None
        from prism.widgets.material_cards import material_thumbnail
        scene = self._scene()
        attachments=getattr(scene,'attachments',None)
        reader=getattr(attachments,'read_uncached',None)
        attachment_id=(record.get('preview') or {}).get('thumbnailAttachmentId')
        if callable(reader) and isinstance(attachment_id,int):
            entry=reader(attachment_id)
            if entry is not None and entry[2] is not None:return material_thumbnail(record,{attachment_id:entry})
        if self._thumbnail_reader is not None and isinstance(attachment_id,int):
            blob=self._thumbnail_reader(attachment_id)
            if blob:return material_thumbnail(record,{attachment_id:('preview.png','image/png',blob)})
        return material_thumbnail(record,attachments or {})
