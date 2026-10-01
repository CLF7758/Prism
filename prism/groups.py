"""Persistent media groups without changing member transforms or ownership."""
import uuid

from PyQt6 import QtCore, QtGui, QtWidgets
from PyQt6.QtCore import Qt

from prism import commands
from prism.i18n import _


class ChangeGroup(QtGui.QUndoCommand):
    def __init__(self, scene, items, group_id, note='', label=_('Group')):
        super().__init__(label)
        self.scene = scene
        self.items = list(items)
        self.before = [(getattr(i, '_group_id', None),
                        getattr(i, '_group_note', '')) for i in self.items]
        self.after = [(group_id, note) for i in self.items]

    def _apply(self, values):
        for item, (group_id, note) in zip(self.items, values):
            item._group_id, item._group_note = group_id, note
        self.scene.groups.refresh()

    def redo(self):
        self._apply(self.after)

    def undo(self):
        self._apply(self.before)


class GroupFrame(QtWidgets.QGraphicsObject):
    """Only the border and caption catch clicks; members remain editable."""
    TYPE = 'group_frame'
    is_editable = False

    def __init__(self, manager, group_id):
        super().__init__()
        self.manager = manager
        self.group_id = group_id
        self.members = []
        self._rect = QtCore.QRectF()
        self._header = QtCore.QRectF()
        self._note = ''
        self._zoom = 1
        self.setAcceptedMouseButtons(Qt.MouseButton.LeftButton)
        self.setToolTip(
            _('Drag the title or frame to move the whole group; '
              'double-click the title to edit the group note'))

    def boundingRect(self):
        pad = 8 / self._zoom
        return self._rect.united(self._header).adjusted(-pad, -pad, pad, pad)

    def shape(self):
        outline = QtGui.QPainterPath()
        outline.addRect(self._rect)
        stroker = QtGui.QPainterPathStroker()
        stroker.setWidth(12 / self._zoom)
        path = stroker.createStroke(outline)
        path.addRect(self._header)
        return path

    def sync(self, members):
        self.members = members
        visible = [i for i in members if i.isVisible()]
        self.setVisible(bool(visible))
        if not visible:
            return
        scene = self.manager.scene
        zoom = max(scene.views()[0].get_scale() if scene.views() else 1, 0.001)
        pad = 18 / zoom
        rect = scene.itemsBoundingRect(items=visible).adjusted(-pad, -pad, pad, pad)
        note = getattr(members[0], '_group_note', '') or _('Group · double-click to add a note')
        font = QtGui.QFont('Microsoft YaHei UI')
        font.setPixelSize(18)
        text_rect = QtGui.QFontMetricsF(font).boundingRect(
            QtCore.QRectF(0, 0, max(120, rect.width() * zoom - 20), 10000),
            int(Qt.TextFlag.TextWordWrap), note)
        height = (max(26, text_rect.height()) + 16) / zoom
        header = QtCore.QRectF(rect.left(), rect.top() - height, rect.width(), height)
        if (rect != self._rect or header != self._header or
                note != self._note or zoom != self._zoom):
            self.prepareGeometryChange()
            self._rect, self._header = rect, header
            self._note, self._zoom = note, zoom
            self.update()
        z = min(i.zValue() for i in visible) - 0.001
        if self.zValue() != z:
            self.setZValue(z)

    def paint(self, painter, option, widget=None):
        painter.setRenderHint(QtGui.QPainter.RenderHint.Antialiasing)
        pen = QtGui.QPen(QtGui.QColor('#7ba6cd'), 1.5)
        pen.setCosmetic(True)
        painter.setPen(pen)
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.drawRoundedRect(self._rect, 8 / self._zoom, 8 / self._zoom)
        painter.fillRect(self._header, QtGui.QColor('#294156'))
        painter.save()
        painter.translate(self._header.topLeft())
        painter.scale(1 / self._zoom, 1 / self._zoom)
        font = QtGui.QFont('Microsoft YaHei UI')
        font.setPixelSize(18)
        painter.setFont(font)
        painter.setPen(QtGui.QColor('#edf4fc'))
        painter.drawText(QtCore.QRectF(10, 6,
            self._header.width() * self._zoom - 20,
            self._header.height() * self._zoom - 12),
            Qt.TextFlag.TextWordWrap | Qt.AlignmentFlag.AlignVCenter, self._note)
        painter.restore()

    def begin_drag(self, point):
        self.manager.scene.deselect_all_items()
        for item in self.members:
            if item.isVisible():
                item.setSelected(True)
        self._start = QtCore.QPointF(point)
        self._positions = [(i, QtCore.QPointF(i.pos())) for i in self.members]

    def drag_to(self, point):
        delta = point - self._start
        for item, pos in self._positions:
            item.setPos(pos + delta)
        self.manager.refresh()

    def end_drag(self, point):
        self.drag_to(point)
        delta = point - self._start
        if not delta.isNull():
            self.manager.scene.undo_stack.push(commands.MoveItemsBy(
                [i for i, pos in self._positions], delta, ignore_first_redo=True))

    def edit_note(self):
        self.manager.edit_note(self.group_id)

    def sample_color_at(self, pos):
        return None


class GroupManager(QtCore.QObject):
    def __init__(self, scene):
        super().__init__(scene)
        self.scene = scene
        self.frames = {}
        self.timer = QtCore.QTimer(self)
        self.timer.setSingleShot(True)
        self.timer.timeout.connect(self.refresh)
        scene.changed.connect(self.schedule)

    def reset(self):
        self.timer.stop()
        self.frames.clear()  # QGraphicsScene.clear owns destruction.

    def schedule(self, *_):
        if not self.timer.isActive():
            self.timer.start(0)

    def refresh(self):
        groups = {}
        for item in self.scene.items_for_save():
            group_id = getattr(item, '_group_id', None)
            if group_id:
                groups.setdefault(group_id, []).append(item)
        for group_id in list(self.frames):
            if group_id not in groups:
                frame = self.frames.pop(group_id)
                self.scene.removeItem(frame)
                frame.deleteLater()
        for group_id, members in groups.items():
            if group_id not in self.frames:
                frame = GroupFrame(self, group_id)
                self.frames[group_id] = frame
                self.scene.addItem(frame)
            self.frames[group_id].sync(members)

    def create(self):
        items = [i for i in self.scene.selectedItems(user_only=True)
                 if i.TYPE in ('pixmap', 'video')]
        if len(items) >= 2:
            self.scene.undo_stack.push(ChangeGroup(self.scene, items, str(uuid.uuid4())))

    def selected_ids(self):
        return {i._group_id for i in self.scene.selectedItems(user_only=True)
                if getattr(i, '_group_id', None)}

    def ungroup(self):
        ids = self.selected_ids()
        items = [i for i in self.scene.items_for_save()
                 if getattr(i, '_group_id', None) in ids]
        if items:
            self.scene.undo_stack.push(ChangeGroup(self.scene, items, None, label=_('Ungroup')))

    def edit_note(self, group_id=None):
        if group_id is None:
            ids = self.selected_ids()
            if len(ids) != 1:
                return
            group_id = next(iter(ids))
        items = [i for i in self.scene.items_for_save()
                 if getattr(i, '_group_id', None) == group_id]
        if not items:
            return
        old = getattr(items[0], '_group_note', '')
        text, accepted = QtWidgets.QInputDialog.getMultiLineText(
            self.scene.views()[0], _('Group Note'), _('Note shown above the group frame:'), old)
        if accepted and text != old:
            self.scene.undo_stack.push(ChangeGroup(
                self.scene, items, group_id, text, _('Edit Group Note')))
