# This file is part of Prism.
#
# Prism is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# Prism is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with Prism.  If not, see <https://www.gnu.org/licenses/>.

from PyQt6 import QtCore, QtGui, QtWidgets
from prism.i18n import _
from prism.item_lifecycle import logical_item


class InsertItems(QtGui.QUndoCommand):

    def __init__(self, scene, items, position=None, ignore_first_redo=False):
        super().__init__(_('Insert items'))
        self.scene = scene
        self.items = [logical_item(item) for item in items]
        self.position = position
        self.ignore_first_redo = ignore_first_redo

    def redo(self):
        if self.ignore_first_redo:
            self.ignore_first_redo = False
            return

        self.scene.deselect_all_items()
        if self.position:
            self.old_positions = []
            rect = self.scene.itemsBoundingRect(items=self.items)
            for item in self.items:
                self.old_positions.append(item.pos())
                item.setPos(item.pos() + self.position - rect.center())
        for item in self.items:
            self.scene.addItem(item)
            item.setSelected(True)
            item.bring_to_front()

    def undo(self):
        self.scene.deselect_all_items()
        for item in self.items:
            self.scene.removeItem(item)
        if self.position:
            for item, pos in zip(self.items, self.old_positions):
                item.setPos(pos)


class DeleteItems(QtGui.QUndoCommand):
    def __init__(self, scene, items):
        super().__init__(_('Delete items'))
        self.scene = scene
        self.items = [logical_item(item) for item in items]

    def redo(self):
        for item in self.items:
            self.scene.removeItem(item)

    def undo(self):
        self.scene.deselect_all_items()
        for item in self.items:
            item.setSelected(True)
            self.scene.addItem(item)


def _trash_timestamp():
    """回收站时间戳，本地时区、精确到秒，和页面节点的 deletedAt 格式一致。"""
    return QtCore.QDateTime.currentDateTime().toString(QtCore.Qt.DateFormat.ISODate)


_SELECTABLE = QtWidgets.QGraphicsItem.GraphicsItemFlag.ItemIsSelectable


def _mark_item_trashed(item, stamp):
    """把一张素材标成"在回收站里"：盖时间戳、隐藏、取消可选中。

    数据一个字节都不动 —— 素材还留在场景里，`items_for_save()` 照样保存
    它，sqlar 里的原始字节也不会被清。所谓"删除"只是这一个状态位。
    """
    item._deleted_at = stamp
    if item.isSelected():
        item.setSelected(False)
    item.setFlag(_SELECTABLE, False)
    item.setVisible(False)


def _mark_item_restored(item):
    """从回收站恢复：清时间戳、恢复可选中、重新显示。

    位置、缩放、旋转、翻转、标签、评分、字节全都没动过，所以恢复出来的
    和删之前是同一张（方案 §7.3"恢复后素材属性不变"）。
    """
    item._deleted_at = None
    item.setFlag(_SELECTABLE, True)
    item.setVisible(True)


class TrashItems(QtGui.QUndoCommand):
    """软删除素材：丢进回收站，可撤销、可恢复、保存后重开仍在回收站。

    注释、绘制线这类"标注"不走这里（它们没有原始字节要保，用 `DeleteItems`
    直接删）。回收站的"永久删除"也不走这里 —— 那才是真正移除。
    """

    def __init__(self, scene, items):
        super().__init__(_('Move items to trash'))
        self.scene = scene
        self.items = [logical_item(item) for item in items]
        self._stamp = None

    def redo(self):
        if self._stamp is None:
            self._stamp = _trash_timestamp()
        self.scene.deselect_all_items()
        for item in self.items:
            _mark_item_trashed(item, self._stamp)
        self._notify()

    def undo(self):
        self.scene.deselect_all_items()
        for item in self.items:
            _mark_item_restored(item)
        self._notify()

    def _notify(self):
        if hasattr(self.scene, 'items_trashed'):
            self.scene.items_trashed.emit()
        if hasattr(self.scene, 'metadata_changed'):
            self.scene.metadata_changed.emit()


class RestoreItems(QtGui.QUndoCommand):
    """从回收站恢复素材（回收站面板里的"恢复"按钮走这条，可撤销）。"""

    def __init__(self, scene, items):
        super().__init__(_('Restore items from trash'))
        self.scene = scene
        self.items = [logical_item(item) for item in items]
        self._stamps = {}

    def redo(self):
        self.scene.deselect_all_items()
        for item in self.items:
            self._stamps[id(item)] = getattr(item, '_deleted_at', None)
            _mark_item_restored(item)
        self._notify()

    def undo(self):
        self.scene.deselect_all_items()
        for item in self.items:
            _mark_item_trashed(item, self._stamps.get(id(item))
                              or _trash_timestamp())
        self._notify()

    def _notify(self):
        if hasattr(self.scene, 'items_trashed'):
            self.scene.items_trashed.emit()
        if hasattr(self.scene, 'metadata_changed'):
            self.scene.metadata_changed.emit()


class DeleteSelection(QtGui.QUndoCommand):
    """一次"删除"里素材和标注分开处理，但合成一步撤销。

    素材（图片/视频/模型）走软删除进回收站，可恢复；标注（文字、绘制线、
    错误占位）没有原始字节要保，直接删。用户按一次 Delete，撤销一次就
    两类全回来 —— 不用记"哪部分是回收站、哪部分是真删"。
    """

    def __init__(self, scene, assets, others):
        super().__init__(_('Delete items'))
        self.scene = scene
        self._trash = TrashItems(scene, assets) if assets else None
        self._delete = DeleteItems(scene, others) if others else None

    def redo(self):
        if self._trash is not None:
            self._trash.redo()
        if self._delete is not None:
            self._delete.redo()

    def undo(self):
        if self._delete is not None:
            self._delete.undo()
        if self._trash is not None:
            self._trash.undo()


class MoveItemsBy(QtGui.QUndoCommand):

    def __init__(self, items, delta, ignore_first_redo=False):
        super().__init__(_('Move items'))
        self.items = [logical_item(item) for item in items]
        self.delta = delta
        self.ignore_first_redo = ignore_first_redo

    def redo(self):
        if self.ignore_first_redo:
            self.ignore_first_redo = False
            return
        for item in self.items:
            item.moveBy(self.delta.x(), self.delta.y())

    def undo(self):
        for item in self.items:
            item.moveBy(-self.delta.x(), -self.delta.y())


class ScaleItemsBy(QtGui.QUndoCommand):
    """Scale items by a given factor around the given anchor."""

    def __init__(self, items, factor, anchor, ignore_first_redo=False):
        super().__init__(_('Scale items'))
        self.ignore_first_redo = ignore_first_redo
        self.items = [logical_item(item) for item in items]
        self.factor = factor
        self.anchor = anchor

    def redo(self):
        if self.ignore_first_redo:
            self.ignore_first_redo = False
            return
        for item in self.items:
            item.setScale(item.scale() * self.factor,
                          item.mapFromScene(self.anchor))

    def undo(self):
        for item in self.items:
            item.setScale(item.scale() / self.factor,
                          item.mapFromScene(self.anchor))


class RotateItemsBy(QtGui.QUndoCommand):
    """Rotate items by a given delta around the given anchor."""

    def __init__(self, items, delta, anchor, ignore_first_redo=False):
        super().__init__(_('Rotate items'))
        self.ignore_first_redo = ignore_first_redo
        self.items = [logical_item(item) for item in items]
        self.delta = delta
        self.anchor = anchor

    def redo(self):
        if self.ignore_first_redo:
            self.ignore_first_redo = False
            return
        for item in self.items:
            item.setRotation(
                item.rotation() + self.delta * item.flip(),
                item.mapFromScene(self.anchor))

    def undo(self):
        for item in self.items:
            item.setRotation(item.rotation() - self.delta * item.flip(),
                             item.mapFromScene(self.anchor))


class NormalizeItems(QtGui.QUndoCommand):

    def __init__(self, items, scale_factors):
        super().__init__(_('Normalize items'))
        self.items = [logical_item(item) for item in items]
        self.scale_factors = scale_factors

    def redo(self):
        self.old_scale_factors = []
        for item, factor in zip(self.items, self.scale_factors):
            self.old_scale_factors.append(item.scale())
            item.setScale(item.scale() * factor, item.center)

    def undo(self):
        for item, factor in zip(self.items, self.old_scale_factors):
            item.setScale(factor, item.center)


class FlipItems(QtGui.QUndoCommand):

    def __init__(self, items, anchor, vertical):
        super().__init__(_('Flip items'))
        self.items = [logical_item(item) for item in items]
        self.anchor = anchor
        self.vertical = vertical

    def redo(self):
        for item in self.items:
            item.do_flip(self.vertical, item.mapFromScene(self.anchor))

    def undo(self):
        self.redo()


class ResetScale(QtGui.QUndoCommand):

    def __init__(self, items):
        super().__init__(_('Reset Scale'))
        self.items = [logical_item(item) for item in items]

    def redo(self):
        self.old_scale_factors = []
        for item in self.items:
            self.old_scale_factors.append(item.scale())
            item.setScale(1, anchor=item.center)

    def undo(self):
        for item, scale_factor in zip(self.items, self.old_scale_factors):
            item.setScale(scale_factor, anchor=item.center)


class ResetRotation(QtGui.QUndoCommand):

    def __init__(self, items):
        super().__init__(_('Reset Rotation'))
        self.items = [logical_item(item) for item in items]

    def redo(self):
        self.old_rotations = []
        for item in self.items:
            self.old_rotations.append(item.rotation())
            item.setRotation(0, anchor=item.center)

    def undo(self):
        for item, rotation in zip(self.items, self.old_rotations):
            item.setRotation(rotation, anchor=item.center)


class ResetFlip(QtGui.QUndoCommand):

    def __init__(self, items):
        super().__init__(_('Reset Flip'))
        self.items = [logical_item(item) for item in items]

    def redo(self):
        self.old_flips = []
        for item in self.items:
            self.old_flips.append(item.flip())
            if item.flip() == -1:
                item.do_flip(anchor=item.center)

    def undo(self):
        for item, flip in zip(self.items, self.old_flips):
            if flip == -1:
                item.do_flip(anchor=item.center)


class ResetCrop(QtGui.QUndoCommand):

    def __init__(self, items):
        super().__init__(_('Reset Crop'))
        self.items = [logical_item(item) for item in items if item.is_image]

    def redo(self):
        self.old_crops = []
        for item in self.items:
            self.old_crops.append(item.crop)
            item.reset_crop()

    def undo(self):
        for item, crop in zip(self.items, self.old_crops):
            item.crop = crop


class ResetTransforms(QtGui.QUndoCommand):

    def __init__(self, items):
        super().__init__(_('Reset All Transformations'))
        self.items = [logical_item(item) for item in items]

    def redo(self):
        self.old_values = []
        for item in self.items:
            values = {
                'scale': item.scale(),
                'rotation': item.rotation(),
                'flip': item.flip(),
            }
            if item.is_image:
                values['crop'] = item.crop
                item.reset_crop()
            self.old_values.append(values)

            item.setScale(1, anchor=item.center)
            item.setRotation(0, anchor=item.center)
            if item.flip() == -1:
                item.do_flip(anchor=item.center)

    def undo(self):
        for item, old in zip(self.items, self.old_values):
            item.setScale(old['scale'], anchor=item.center)
            item.setRotation(old['rotation'], anchor=item.center)
            if old['flip'] == -1:
                item.do_flip(anchor=item.center)
            if item.is_image:
                item.crop = old['crop']


class ArrangeItems(QtGui.QUndoCommand):

    def __init__(self, scene, items, positions):
        super().__init__(_('Arrange items'))
        self.scene = scene
        self.items = [logical_item(item) for item in items]
        self.positions = positions

    def redo(self):
        self.old_positions = []
        for item, pos in zip(self.items, self.positions):
            self.old_positions.append(item.pos())
            orig_topleft = item.mapToScene(QtCore.QPointF(0, 0))
            rect_topleft = self.scene.itemsBoundingRect(
                items=[item]).topLeft()
            item.setPos(pos + orig_topleft - rect_topleft)

    def undo(self):
        for item, pos in zip(self.items, self.old_positions):
            item.setPos(pos)


class CropItem(QtGui.QUndoCommand):
    def __init__(self, item, crop):
        super().__init__(_('Crop item'))
        self.item = logical_item(item)
        self.crop = crop

    def redo(self):
        self.old_crop = self.item.crop
        self.item.crop = self.crop

    def undo(self):
        self.item.crop = self.old_crop


class ChangeText(QtGui.QUndoCommand):

    def __init__(self, item, new_text, old_text):
        super().__init__(_('Change text'))
        self.item = logical_item(item)
        self.new_text = new_text
        self.old_text = old_text

    def redo(self):
        self.item.setPlainText(self.new_text)

    def undo(self):
        self.item.setPlainText(self.old_text)


class ChangeTextStyle(QtGui.QUndoCommand):
    """改文字的样式（颜色 / 字体 / 字号 / 粗体 / 斜体）。

    和 `ChangeText` 分开，因为这两件事在用户那边是两件事：改字是改内容，
    改样式是改外观。分开之后撤销栈里的名字也说得清（"改文字" vs
    "改文字样式"）。
    """

    def __init__(self, item, new_style, old_style=None):
        super().__init__(_('Change text style'))
        self.item = logical_item(item)
        self.new_style = dict(new_style)
        self.old_style = dict(old_style if old_style is not None
                              else item.style())

    def redo(self):
        self.item.set_style(**self.new_style)

    def undo(self):
        self.item.set_style(**self.old_style)


class ChangeOpacity(QtGui.QUndoCommand):
    """Change opacity on images."""

    def __init__(self, items, opacity, ignore_first_redo=False):
        super().__init__(_('Change Opacity'))
        self.ignore_first_redo = ignore_first_redo
        self.items = [logical_item(item) for item in items if item.is_image]
        self.opacity = opacity
        self.old_opacities = [
            getattr(item, '_filter_original_opacity', item.opacity())
            for item in self.items]

    def redo(self):
        if self.ignore_first_redo:
            self.ignore_first_redo = False
            return

        for item in self.items:
            if hasattr(item, '_filter_original_opacity'):
                item._filter_original_opacity = self.opacity
            item.setOpacity(self.opacity)

    def undo(self):
        for item, opacity in zip(self.items, self.old_opacities):
            if hasattr(item, '_filter_original_opacity'):
                item._filter_original_opacity = opacity
            item.setOpacity(opacity)


class ToggleGrayscale(QtGui.QUndoCommand):
    """Toggle grayscale mode on images."""

    def __init__(self, items, grayscale):
        super().__init__(_('Toggle Grayscale'))
        self.items = [logical_item(item) for item in items if item.is_image]
        self.grayscale = grayscale
        self.old_grayscales = [item.grayscale for item in items]

    def redo(self):
        for item in self.items:
            item.grayscale = self.grayscale

    def undo(self):
        for item, grayscale in zip(self.items, self.old_grayscales):
            item.grayscale = grayscale


class ChangeMetadata(QtGui.QUndoCommand):
    """Change a persisted metadata field on one or more items."""

    def __init__(self, items, field, new_values):
        super().__init__(f'Change {field}')
        self.items = [logical_item(item) for item in items]
        self.field = field
        self.new_values = list(new_values)
        self.old_values = [getattr(item, field) for item in items]

    def redo(self):
        for item, value in zip(self.items, self.new_values):
            attr = f'_{self.field}'
            if self.field in ('notes', 'title'):
                object.__setattr__(item, attr, value)
            elif self.field in ('categories', 'tags'):
                object.__setattr__(item, attr, list(value) if value else [])
            else:
                object.__setattr__(item, attr, value)
        if self.items and self.items[0].scene():
            scene = self.items[0].scene()
            if hasattr(scene, 'metadata_changed'):
                scene.metadata_changed.emit()

    def undo(self):
        for item, value in zip(self.items, self.old_values):
            if self.field in ('notes', 'title'):
                object.__setattr__(item, f'_{self.field}', value)
            elif self.field in ('categories', 'tags'):
                object.__setattr__(item, f'_{self.field}',
                                   list(value) if value else [])
            else:
                object.__setattr__(item, f'_{self.field}', value)
        if self.items and self.items[0].scene():
            scene = self.items[0].scene()
            if hasattr(scene, 'metadata_changed'):
                scene.metadata_changed.emit()
