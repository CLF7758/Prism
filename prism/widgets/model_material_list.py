"""C1: the material list of ONE model.

Qt-free derivation plus one virtualizing list model. This module never parses a
model, never opens the database and never imports F3D: the panel hands it an
already-inspected ``MaterialCatalog`` and ``MaterialState``.

Three rules shape it:

* **An unknown count is not zero.** ``None`` stays ``None``; only a resolved
  catalog may print "0 处".
* **Identity is a working id.** Two working materials may carry the same
  display name. Rows are keyed by ``working_id`` / ``binding_id`` so a rename
  never re-points a row at another material.
* **No per-row engine and no per-row thumbnail read.** The model asks for a
  thumbnail only when a row is actually painted, and only inside a small
  budget (``MAX_PREFETCH_THUMBNAILS``).
"""
import json
from dataclasses import dataclass, replace

from PyQt6 import QtCore, QtGui, QtWidgets

from prism.material_textures import SLOTS
from prism.model_library.multimaterial.contracts import PatchAction

# ----------------------------------------------------------------------
# Vocabulary
# ----------------------------------------------------------------------
#: Panel phases. "unknown" is the state before anything was asked.
PHASE_NO_MODEL = 'no_model'
PHASE_UNPARSED = 'unparsed'
PHASE_INSPECTING = 'inspecting'
PHASE_FAILED = 'failed'
PHASE_READY = 'ready'

#: What a row stands for. Groups are listed in this order.
ROW_WORKING = 'working'          # a working material that is used somewhere
ROW_UNUSED = 'unused'            # a definition no binding site uses
ROW_UNASSIGNED = 'unassigned'    # binding sites without any assignment
ROW_UNRESOLVED = 'unresolved'    # a source entry the parser could not resolve

#: Per-row condition. ``STATUS_EDITABLE`` is the only "nothing is wrong" one.
STATUS_EDITABLE = 'editable'
STATUS_MISSING_TEXTURE = 'missing_texture'
STATUS_PARTIAL = 'partial'
STATUS_UNRESOLVED = 'unresolved'
STATUS_UNUSED = 'unused'
STATUS_UNKNOWN = 'unknown'
STATUS_TEXTURE_UNKNOWN = 'texture_unknown'

STATUS_TEXT = {
    STATUS_EDITABLE: '可编辑',
    STATUS_MISSING_TEXTURE: '缺图',
    STATUS_PARTIAL: '部分支持',
    STATUS_UNRESOLVED: '未解析',
    STATUS_UNUSED: '未使用',
    STATUS_UNKNOWN: '数量未取得',
    STATUS_TEXTURE_UNKNOWN: '贴图状态未取得',
}

#: Thumbnails in flight at once. A 1000-row catalog must not schedule 1000 reads.
MAX_PREFETCH_THUMBNAILS = 8

#: Mime type of a material-library drag. The library must publish this type for
#: a drop to be recognised; anything else is ignored rather than applied to the
#: whole model.
MATERIAL_MIME = 'application/x-prism-material'

#: The payload the existing material library already publishes when a card is
#: dragged (see ``prism.widgets.library_transfer``). Reusing it means a library
#: card can be dropped on a material row without changing the library side.
LIBRARY_MIME = 'application/x-prism-library-assets'


def slot_label(key):
    """The existing slot caption; the panel has no second naming scheme."""
    return SLOTS.get(key, key)


def site_count_text(count):
    """Render a site count, keeping "unknown" distinguishable from zero."""
    return '—' if count is None else f'{count:,} 处'


def status_text(status):
    return STATUS_TEXT.get(status, '数量未取得')


@dataclass(frozen=True)
class MaterialRow:
    """One line of the material list."""

    key: str
    kind: str
    name: str
    site_count: int | None
    working_id: str | None = None
    definition_id: str | None = None
    status: str = STATUS_UNKNOWN
    detail: str = ''
    missing_slots: tuple[str, ...] = ()
    unresolved_texture_slots: tuple[str, ...] = ()
    unsupported: tuple[str, ...] = ()
    modified: bool = False
    thumbnail_key: str | None = None
    thumbnail_revision: int = 0

    @property
    def used(self):
        return self.kind == ROW_WORKING

    @property
    def selectable(self):
        """Only a real working material can be the target of an edit."""
        return self.kind == ROW_WORKING and bool(self.working_id)

    def badges(self):
        """The short condition words shown after the name."""
        words = []
        if self.modified:
            words.append('已修改')
        if self.missing_slots:
            words.append('缺图 ' + '、'.join(slot_label(slot) for slot in self.missing_slots))
        if self.unresolved_texture_slots:
            words.append('贴图状态未取得 '
                         + '、'.join(slot_label(slot)
                                     for slot in self.unresolved_texture_slots))
        if self.unsupported:
            words.append('部分支持 ' + '、'.join(self.unsupported))
        if self.status == STATUS_UNRESOLVED:
            words.append('未解析')
        if self.kind == ROW_UNUSED:
            words.append('未使用')
        return tuple(words)

    def impact_text(self):
        """The one sentence a user needs before editing this row."""
        if self.kind == ROW_UNASSIGNED:
            return f'未指定材质的部位：{site_count_text(self.site_count)}'
        if self.kind == ROW_UNRESOLVED:
            return '这一项尚未解析，使用处数量未取得'
        if self.kind == ROW_UNUSED:
            return '这一材质未被任何部位使用，编辑它不影响模型'
        if self.site_count is None:
            return f'当前材质「{self.name}」，使用处数量未取得'
        if self.site_count == 1:
            return f'当前材质「{self.name}」，影响 1 处'
        return f'当前材质「{self.name}」，影响 {self.site_count:,} 处'

    def tooltip(self):
        lines = [self.name, self.impact_text(), '状态：' + status_text(self.status)]
        if self.detail:
            lines.append(self.detail)
        if self.missing_slots:
            lines.append('缺图槽位：' + '、'.join(slot_label(s) for s in self.missing_slots))
        if self.unresolved_texture_slots:
            lines.append('贴图状态未取得的槽位：'
                         + '、'.join(slot_label(s) for s in self.unresolved_texture_slots))
        if self.unsupported:
            lines.append('后端未支持的通道：' + '、'.join(self.unsupported))
        return '\n'.join(lines)


# ----------------------------------------------------------------------
# Derivation from the contracts
# ----------------------------------------------------------------------
#: Texture conditions the backend may report per slot. "no map" and "the file is
#: gone" are different facts, and neither is "the backend said nothing".
TEXTURE_OK = 'ok'
TEXTURE_MISSING = 'missing'
TEXTURE_UNLOADED = 'unloaded'
TEXTURE_NONE = 'none'
TEXTURE_FAILED = 'failed'
TEXTURE_UNKNOWN = 'unknown'

TEXTURE_STATES = (TEXTURE_OK, TEXTURE_MISSING, TEXTURE_UNLOADED, TEXTURE_NONE,
                  TEXTURE_FAILED, TEXTURE_UNKNOWN)

#: Conditions that make a row "not fully usable" and need their own word.
TEXTURE_PROBLEMS = {TEXTURE_MISSING: 'missing', TEXTURE_FAILED: 'failed',
                    TEXTURE_UNKNOWN: 'unknown'}


def _definitions(catalog):
    return {item.material_id: item for item in (getattr(catalog, 'definitions', ()) or ())}


def source_definition(working, catalog):
    """The *source* definition of a working material, if the catalog carries it.

    A resolved library snapshot (``base_snapshot_ref``) is deliberately NOT
    looked up here: it is not a source definition, it belongs to the backend's
    resolved base view. When only a snapshot exists this returns ``None`` and the
    row says the base is unknown instead of reading a snapshot out of the source
    catalog.
    """
    source = getattr(working, 'source_material_id', None)
    if not source:
        return None
    if getattr(working, 'base_snapshot_ref', None):
        return None
    return _definitions(catalog).get(source)


def texture_state(texture):
    """The declared condition of one texture, never guessed from a path."""
    for name in ('state', 'texture_state', 'status'):
        value = getattr(texture, name, None)
        if isinstance(value, str) and value in TEXTURE_STATES:
            return value
    if not getattr(texture, 'resource_id', '') and not getattr(texture, 'source_locator', ''):
        # No image was ever declared for this slot: that is "no map", not a
        # missing file.
        return TEXTURE_NONE
    # A locator exists but the backend said nothing about its state.
    return TEXTURE_UNKNOWN


def texture_states(definition):
    """``{slot: state}`` for one definition, using only declared facts."""
    if definition is None:
        return {}
    return {texture.slot: texture_state(texture)
            for texture in (getattr(definition, 'textures', ()) or ())}


def missing_slots(definition):
    """Slots whose declared image is genuinely gone (not "no map")."""
    states = texture_states(definition)
    return tuple(slot for slot, state in states.items()
                 if state in (TEXTURE_MISSING, TEXTURE_FAILED))


def unresolved_texture_slots(definition):
    """Slots whose texture state the backend did not report."""
    states = texture_states(definition)
    return tuple(slot for slot, state in states.items() if state == TEXTURE_UNKNOWN)


def _patched_slots(working):
    return {patch.slot for patch in (getattr(working, 'texture_patches', ()) or ())
            if patch.action != PatchAction.INHERIT}


def is_modified(working):
    """A working material carries a local change (textures, numbers or base)."""
    if _patched_slots(working):
        return True
    if getattr(working, 'base_snapshot_ref', None):
        return True
    payload = getattr(working, 'property_patch_json', '{}') or '{}'
    try:
        parsed = json.loads(payload)
    except (TypeError, ValueError):
        return True
    return bool(parsed.get('slots')) if isinstance(parsed, dict) else True


def _row_status(definition, missing, unsupported, working, unresolved=()):
    if definition is None and not getattr(working, 'base_snapshot_ref', None):
        return STATUS_UNKNOWN, '原始材质定义未取得，继承值不可核对'
    if unsupported:
        return STATUS_PARTIAL, '这些通道后端未支持：' + '、'.join(unsupported)
    if missing:
        states = texture_states(definition)
        words = []
        for slot in missing:
            words.append(slot_label(slot) + ('（解码失败）' if states.get(slot) == TEXTURE_FAILED
                                              else '（贴图文件缺失）'))
        return STATUS_MISSING_TEXTURE, ('缺图：' + '、'.join(words)
                                        + '；材质其余数值仍可编辑')
    if unresolved:
        return STATUS_TEXTURE_UNKNOWN, ('贴图状态未取得：'
                                        + '、'.join(slot_label(s) for s in unresolved)
                                        + '；后端未报告，界面不猜测是否缺图')
    return STATUS_EDITABLE, ''


def _working_row(working, catalog, sites):
    definition = source_definition(working, catalog)
    missing = missing_slots(definition)
    unsupported = tuple(getattr(definition, 'unsupported', ()) or ())
    unresolved_textures = unresolved_texture_slots(definition)
    status, detail = _row_status(definition, missing, unsupported, working,
                                 unresolved_textures)
    return MaterialRow(
        key='working:' + working.working_id,
        kind=ROW_WORKING,
        name=working.display_name or '未命名材质',
        site_count=len(sites),
        working_id=working.working_id,
        definition_id=(definition.material_id if definition is not None
                       else working.base_snapshot_ref),
        status=status,
        detail=detail,
        missing_slots=missing,
        unresolved_texture_slots=unresolved_textures,
        unsupported=unsupported,
        modified=is_modified(working),
        thumbnail_key=('working:' + working.working_id),
    )


def build_rows(catalog=None, state=None):
    """Turn one inspected catalog + state into list rows.

    ``catalog is None and state is None`` means "nothing was inspected yet";
    the caller shows a phase message instead of an empty library.
    """
    if catalog is None and state is None:
        return ()
    definitions = _definitions(catalog)
    unresolved = tuple(getattr(catalog, 'unresolved', ()) or ())
    rows = []
    if state is None:
        # The catalog arrived without an assignment state: the definition list
        # is real, the usage counts are not.
        for definition in sorted(definitions.values(), key=lambda item: (item.name.lower(),
                                                                        item.material_id)):
            missing = missing_slots(definition)
            unsupported = tuple(getattr(definition, 'unsupported', ()) or ())
            rows.append(MaterialRow(
                key='definition:' + definition.material_id,
                kind=ROW_UNUSED,
                name=definition.name or '未命名材质',
                site_count=None,
                definition_id=definition.material_id,
                status=STATUS_UNKNOWN,
                detail='分配尚未取得，无法确定这一材质用在哪里',
                missing_slots=missing,
                unresolved_texture_slots=unresolved_texture_slots(definition),
                unsupported=unsupported,
                thumbnail_key='definition:' + definition.material_id))
        rows.extend(_unresolved_rows(unresolved))
        return tuple(rows)

    assignments = tuple(getattr(state, 'assignments', ()) or ())
    by_working = {}
    assigned = set()
    for assignment in assignments:
        assigned.add(assignment.binding_id)
        by_working.setdefault(assignment.working_id, []).append(assignment.binding_id)
    used_definition_ids = set()
    workings = tuple(getattr(state, 'materials', ()) or ())
    snapshot_ids = {working.base_snapshot_ref for working in workings
                    if working.base_snapshot_ref}
    source_ids = {working.source_material_id for working in workings
                  if working.source_material_id}
    for working in workings:
        sites = by_working.get(working.working_id, [])
        row = _working_row(working, catalog, sites)
        used_definition_ids.add(row.definition_id)
        rows.append(row)
    # Cards represent working slots only; source definitions remain internal.
    unassigned = _unassigned_sites(catalog, assigned)
    if unassigned:
        rows.append(MaterialRow(
            key='unassigned',
            kind=ROW_UNASSIGNED,
            name='未指定材质',
            site_count=len(unassigned),
            status=STATUS_UNKNOWN,
            detail='这些部位在原件里没有材质分配：' + '、'.join(unassigned[:5])
                   + ('…' if len(unassigned) > 5 else ''),
        ))
    rows.extend(_unresolved_rows(unresolved))
    revision = getattr(getattr(state, 'target', None), 'state_revision', 0)
    return tuple(replace(row, thumbnail_revision=revision) for row in rows)


def _unresolved_rows(unresolved):
    return [MaterialRow(key='unresolved:' + str(key), kind=ROW_UNRESOLVED,
                        name=str(key) or '未命名源条目', site_count=None,
                        status=STATUS_UNRESOLVED,
                        detail='解析器没有解析出这一项；它不是“0 个材质”')
            for key in unresolved]


def _unassigned_sites(catalog, assigned_ids):
    if catalog is None:
        return ()
    labels = []
    for site in getattr(catalog, 'binding_sites', ()) or ():
        if site.binding_id in assigned_ids:
            continue
        if site.original_material_id:
            # The source told us which material it wants, but no assignment
            # exists. That is a missing assignment, not an unassigned site.
            continue
        labels.append(site_label(site))
    return tuple(labels)


def site_label(site):
    """A stable, human-readable name for one binding site."""
    node = getattr(site, 'node_path', '') or ''
    geometry = getattr(site, 'geometry_key', '') or ''
    primitive = getattr(site, 'primitive_key', '') or ''
    parts = [part for part in (node, geometry, primitive) if part]
    return ' / '.join(parts) or getattr(site, 'binding_id', '') or '未命名部位'


def sites_for(state, catalog, working_id):
    """The binding sites of one working material, in catalog order."""
    if state is None or not working_id:
        return ()
    assigned = {assignment.binding_id for assignment in
                (getattr(state, 'assignments', ()) or ())
                if assignment.working_id == working_id}
    ordered = []
    for site in ((getattr(catalog, 'binding_sites', ()) or ())
                 if catalog is not None else ()):
        if site.binding_id in assigned:
            ordered.append(site)
    return tuple(ordered)


def summarize(rows):
    """Heading text for the list: used materials, not definitions."""
    used = [row for row in rows if row.kind == ROW_WORKING]
    if not used:
        return '未取得材质列表'
    known = [row.site_count for row in used if row.site_count is not None]
    if len(known) != len(used):
        return f'{len(used):,} 个材质 · 部分使用处数量未取得'
    return f'{len(used):,} 个材质 · 共影响 {sum(known):,} 处'


# ----------------------------------------------------------------------
# Thumbnails
# ----------------------------------------------------------------------
@dataclass(frozen=True)
class ThumbnailRequest:
    """Ask the owner for one small preview; ``key`` is the stable row key."""

    key: str
    generation: int


class MaterialListModel(QtCore.QAbstractListModel):
    """Virtualized rows. One model, no widget per material."""

    thumbnail_requested = QtCore.pyqtSignal(object)

    RowRole = int(QtCore.Qt.ItemDataRole.UserRole) + 1

    def __init__(self, parent=None):
        super().__init__(parent)
        self._rows = ()
        self._generation = 0
        self._thumbs = {}
        self._pending = set()

    # -- contents ------------------------------------------------------
    def set_rows(self, rows):
        rows = tuple(rows or ())
        old_revisions = {row.thumbnail_key:row.thumbnail_revision for row in self._rows}
        changed = {row.thumbnail_key for row in rows
                   if old_revisions.get(row.thumbnail_key)!=row.thumbnail_revision}
        self.beginResetModel()
        self._rows = rows
        self._generation += 1
        valid = {row.thumbnail_key for row in rows if row.thumbnail_key}
        self._thumbs = {key: value for key, value in self._thumbs.items() if key in valid and key not in changed}
        self._pending = {key for key in self._pending if key in valid and key not in changed}
        self.endResetModel()

    def rows(self):
        return self._rows

    def generation(self):
        return self._generation

    def row_at(self, index):
        if not isinstance(index, QtCore.QModelIndex):
            return None
        if not index.isValid() or not 0 <= index.row() < len(self._rows):
            return None
        return self._rows[index.row()]

    def row_for_key(self, key):
        return next((row for row in self._rows if row.key == key), None)

    def index_for_key(self, key):
        for position, row in enumerate(self._rows):
            if row.key == key:
                return self.index(position, 0)
        return QtCore.QModelIndex()

    # -- Qt model API --------------------------------------------------
    def rowCount(self, parent=QtCore.QModelIndex()):
        return 0 if parent.isValid() else len(self._rows)

    def data(self, index, role=QtCore.Qt.ItemDataRole.DisplayRole):
        row = self.row_at(index)
        if row is None:
            return None
        if role in (QtCore.Qt.ItemDataRole.DisplayRole, self.RowRole):
            return row if role == self.RowRole else _display_text(row)
        if role == QtCore.Qt.ItemDataRole.ToolTipRole:
            return row.tooltip()
        if role == QtCore.Qt.ItemDataRole.DecorationRole:
            return self._decoration(row)
        if role == QtCore.Qt.ItemDataRole.ForegroundRole:
            return None
        return None

    def flags(self, index):
        base = super().flags(index)
        row = self.row_at(index)
        if row is None:
            return base
        if not row.selectable:
            return QtCore.Qt.ItemFlag.ItemIsEnabled
        return base | QtCore.Qt.ItemFlag.ItemIsSelectable

    def _decoration(self, row):
        key = row.thumbnail_key
        if not key:
            return None
        cached = self._thumbs.get(key)
        if cached is not None:
            return cached
        if key in self._pending or len(self._pending) >= MAX_PREFETCH_THUMBNAILS:
            return None
        # Only a painted row asks for an image, so 1000 rows cost one page of
        # reads instead of 1000 upstream calls.
        self._pending.add(key)
        self.thumbnail_requested.emit(ThumbnailRequest(key, self._generation))
        return None

    # -- thumbnails ----------------------------------------------------
    def pending_thumbnails(self):
        return frozenset(self._pending)

    def set_thumbnail(self, key, image, generation=None):
        """Install one preview. Stale or unknown keys are dropped silently."""
        if generation is not None and generation != self._generation:
            return False
        self._pending.discard(key)
        if not any(row.thumbnail_key == key for row in self._rows):
            return False
        pixmap = image
        if isinstance(image, (bytes, bytearray)):
            pixmap = QtGui.QPixmap()
            if not pixmap.loadFromData(bytes(image)):
                return False
        self._thumbs[key] = pixmap
        index = next((position for position, row in enumerate(self._rows)
                      if row.thumbnail_key == key), None)
        if index is not None:
            self.dataChanged.emit(self.index(index, 0), self.index(index, 0),
                                  [QtCore.Qt.ItemDataRole.DecorationRole])
        return True

    def thumbnail(self, key):
        return self._thumbs.get(key)

    def drop_thumbnail(self, key):
        """Release one decoded preview (a residency budget, not a read budget)."""
        if self._thumbs.pop(key, None) is None:
            return False
        index = next((position for position, row in enumerate(self._rows)
                      if row.thumbnail_key == key), None)
        if index is not None:
            self.dataChanged.emit(self.index(index, 0), self.index(index, 0),
                                  [QtCore.Qt.ItemDataRole.DecorationRole])
        return True

    def resident_count(self):
        return len(self._thumbs)


def _display_text(row):
    parts = [row.name, site_count_text(row.site_count), status_text(row.status)]
    badges = row.badges()
    if badges:
        parts.append('·'.join(badges))
    return '  ·  '.join(parts)


class MaterialSphereDelegate(QtWidgets.QStyledItemDelegate):
    """Same dark asset-card language, at inspector size; no per-card widgets."""
    def paint(self, painter, option, index):
        row = index.data(MaterialListModel.RowRole)
        if row is None:
            return
        painter.save()
        rect = option.rect.adjusted(3, 3, -3, -3)
        selected = bool(option.state & QtWidgets.QStyle.StateFlag.State_Selected)
        painter.setPen(QtGui.QColor('#82b6ff' if selected else '#3b3d44'))
        painter.setBrush(QtGui.QColor('#303238' if selected else '#24262b'))
        painter.drawRoundedRect(rect, 7, 7)
        picture = QtCore.QRect(rect.x()+8, rect.y()+6, rect.width()-16, 64)
        icon = index.data(QtCore.Qt.ItemDataRole.DecorationRole)
        if isinstance(icon, QtGui.QPixmap) and not icon.isNull():
            pixmap = icon.scaled(picture.size(), QtCore.Qt.AspectRatioMode.KeepAspectRatio,
                                 QtCore.Qt.TransformationMode.SmoothTransformation)
            painter.drawPixmap(picture.center().x()-pixmap.width()//2,
                               picture.center().y()-pixmap.height()//2, pixmap)
        else:
            painter.setPen(QtGui.QColor('#777d89'))
            painter.drawText(picture, QtCore.Qt.AlignmentFlag.AlignCenter, '预览待生成')
        painter.setPen(QtGui.QColor('#e7e9ed'))
        text = painter.fontMetrics().elidedText(row.name, QtCore.Qt.TextElideMode.ElideRight,
                                                rect.width()-12)
        painter.drawText(rect.adjusted(6, 72, -6, 0), QtCore.Qt.AlignmentFlag.AlignHCenter, text)
        painter.restore()

    def sizeHint(self, option, index):
        view = self.parent()
        return view.gridSize() if isinstance(view, QtWidgets.QListView) else QtCore.QSize(96, 104)


class MaterialListView(QtWidgets.QListView):
    """The list itself: dense rows, one selection, explicit drop targets.

    The model is installed only when real rows exist. A view that shows an
    empty model would look like "this model has no materials", which is not
    what "not inspected yet" means.
    """

    #: A library card was dropped on an explicit material row.
    material_dropped = QtCore.pyqtSignal(object, object)
    #: One row was selected (payload is a ``MaterialRow``) or the list cleared.
    row_selected = QtCore.pyqtSignal(object)
    copy_requested = QtCore.pyqtSignal(object)
    paste_requested = QtCore.pyqtSignal(object)
    retry_requested = QtCore.pyqtSignal()
    rename_requested = QtCore.pyqtSignal(object)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName('modelMaterialList')
        self.setSelectionMode(QtWidgets.QAbstractItemView.SelectionMode.SingleSelection)
        self.setEditTriggers(QtWidgets.QAbstractItemView.EditTrigger.NoEditTriggers)
        self.setUniformItemSizes(True)
        self.setVerticalScrollMode(QtWidgets.QAbstractItemView.ScrollMode.ScrollPerItem)
        self.setViewMode(QtWidgets.QListView.ViewMode.IconMode)
        self.setResizeMode(QtWidgets.QListView.ResizeMode.Adjust)
        self.setMovement(QtWidgets.QListView.Movement.Static)
        self.setWrapping(True)
        self.setSpacing(0)
        self.setFlow(QtWidgets.QListView.Flow.LeftToRight)
        self.setVerticalScrollBarPolicy(QtCore.Qt.ScrollBarPolicy.ScrollBarAlwaysOn)
        self.setGridSize(QtCore.QSize(112, 118))
        self.setItemDelegate(MaterialSphereDelegate(self))
        self.setMaximumHeight(258)
        self.setContextMenuPolicy(QtCore.Qt.ContextMenuPolicy.CustomContextMenu)
        self.customContextMenuRequested.connect(self._material_menu)
        self.doubleClicked.connect(lambda index: self.rename_requested.emit(
            self._model.row_at(index)))
        self.setAlternatingRowColors(False)
        self.setWordWrap(False)
        self.setMinimumHeight(110)
        self.setAcceptDrops(True)
        self.viewport().setAcceptDrops(True)
        self._model = MaterialListModel(self)
        self.retry_available = False
        self.setEnabled(False)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._sync_grid()
        QtCore.QTimer.singleShot(0, self._sync_grid)

    def _sync_grid(self):
        size = QtCore.QSize(max(1, (self.viewport().width() - 2) // 3), 104)
        if self.gridSize() != size:
            self.setGridSize(size)

    def keyPressEvent(self, event):
        if event.key() == QtCore.Qt.Key.Key_F2:
            self.rename_requested.emit(self.selected_row())
            event.accept()
            return
        super().keyPressEvent(event)

    def material_model(self):
        return self._model

    def _material_menu(self, position):
        row = self._model.row_at(self.indexAt(position))
        if row is None or not row.selectable:
            return
        self.select_key(row.key)
        menu = QtWidgets.QMenu(self)
        menu.addAction('复制材质', lambda: self.copy_requested.emit(row))
        paste = menu.addAction('粘贴材质', lambda: self.paste_requested.emit(row))
        paste.setEnabled(QtWidgets.QApplication.clipboard().mimeData().hasFormat(
            'application/x-prism-model-material'))
        if self.retry_available:
            menu.addAction('重试未保存的修改',self.retry_requested.emit)
        menu.exec(self.viewport().mapToGlobal(position))

    def install(self, rows):
        """Install real rows and connect the selection exactly once."""
        if self.model() is not self._model:
            self._model.set_rows(rows)
            self.setModel(self._model)
            self.selectionModel().selectionChanged.connect(self._selection_changed)
        else:
            previous=self._model.row_at(self.currentIndex())
            selected_key=previous.key if previous else None
            blocker=QtCore.QSignalBlocker(self.selectionModel())
            self._model.set_rows(rows)
            if selected_key:
                self.select_key(selected_key)
            del blocker
        self.setEnabled(bool(rows))
        if not rows:
            self.row_selected.emit(None)

    def clear_rows(self):
        """Forget the rows entirely: unknown is not an empty library."""
        self._model.set_rows(())
        self.setModel(None)
        self.setEnabled(False)
        self.row_selected.emit(None)

    def selected_row(self):
        model = self.model()
        if model is None or self.selectionModel() is None:
            return None
        indexes = self.selectionModel().selectedIndexes()
        return self._model.row_at(indexes[0]) if indexes else None

    def select_key(self, key):
        index = self._model.index_for_key(key)
        if index.isValid():
            self.setCurrentIndex(index)
            return True
        return False

    def clear_selection(self):
        if self.selectionModel():
            self.selectionModel().clearSelection()
            self.row_selected.emit(None)

    def _selection_changed(self, *_):
        self.row_selected.emit(self.selected_row())

    # -- drag and drop -------------------------------------------------
    def _entry_id(self, event):
        """One library material id from a drop, or ``None``.

        Two payloads are accepted: this panel's own minimal one and the existing
        library drag (``application/x-prism-library-assets``), which the material
        library already publishes. Nothing else is interpreted: a batch of ids
        with no explicit row must not turn into "apply to every material".
        """
        mime = event.mimeData()
        if mime.hasFormat(MATERIAL_MIME):
            try:
                payload = json.loads(bytes(mime.data(MATERIAL_MIME)).decode('utf-8'))
            except (TypeError, ValueError, UnicodeDecodeError):
                return None
            if isinstance(payload, dict):
                payload = payload.get('entry_id')
            if isinstance(payload, list) and len(payload) == 1:
                payload = payload[0]
            return payload if isinstance(payload, str) and payload else None
        if mime.hasFormat(LIBRARY_MIME):
            try:
                payload = json.loads(bytes(mime.data(LIBRARY_MIME)).decode('utf-8'))
            except (TypeError, ValueError, UnicodeDecodeError):
                return None
            if not isinstance(payload, dict) or payload.get('kind') != 'material':
                return None
            ids = payload.get('ids')
            if not isinstance(ids, list) or len(ids) != 1:
                return None
            return ids[0] if isinstance(ids[0], str) and ids[0] else None
        return None

    def dragEnterEvent(self, event):
        row = self._model.row_at(self.indexAt(self._position(event)))
        if self._entry_id(event) and row is not None and row.selectable:
            # A copy into this material: the source library keeps its record.
            event.setDropAction(QtCore.Qt.DropAction.CopyAction)
            event.accept()
            return
        # No explicit row: never fall back to "apply to the whole model".
        event.ignore()

    def dragMoveEvent(self, event):
        self.dragEnterEvent(event)

    def dropEvent(self, event):
        row = self._model.row_at(self.indexAt(self._position(event)))
        entry_id = self._entry_id(event)
        if not entry_id or row is None or not row.selectable:
            event.ignore()
            return
        event.setDropAction(QtCore.Qt.DropAction.CopyAction)
        event.accept()
        self.material_dropped.emit(row.key, entry_id)

    @staticmethod
    def _position(event):
        point = event.position()
        return point.toPoint() if hasattr(point, 'toPoint') else point
