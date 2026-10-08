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

from functools import partial
import logging
import math
from queue import Queue
import uuid
import weakref

from PyQt6 import QtCore, QtWidgets, QtGui
from PyQt6.QtCore import Qt

import rpack

from prism import commands
from prism.config import PrismSettings
from prism.items import item_registry, PrismErrorItem, PrismPixmapItem, sort_by_filename
from prism.selection import MultiSelectItem, RubberbandItem
from prism.tags import name_index, tag_system


logger = logging.getLogger(__name__)


class PrismGraphicsScene(QtWidgets.QGraphicsScene):
    cursor_changed = QtCore.pyqtSignal(QtGui.QCursor)
    cursor_cleared = QtCore.pyqtSignal()
    metadata_changed = QtCore.pyqtSignal()
    filter_changed = QtCore.pyqtSignal()

    MOVE_MODE = 1
    RUBBERBAND_MODE = 2

    def __init__(self, undo_stack):
        super().__init__()
        self.active_mode = None
        self.undo_stack = undo_stack
        self.max_z = 0
        self.min_z = 0
        self.Z_STEP = 0.001
        self.selectionChanged.connect(self.on_selection_change)
        self.changed.connect(self.on_change)
        self.items_to_add = Queue()
        # Removed items can remain in the undo stack. Keep weak references so
        # saving a deletion can preserve their deferred original bytes.
        self._prism_archive_items = weakref.WeakSet()
        self._import_in_progress = False
        self._interaction_in_progress = False
        self.edit_item = None
        self.crop_item = None
        self.settings = PrismSettings()
        # Document note, its embedded images and the mind map tree. These
        # live outside the item tables; see SQLiteIO.read_board_extras.
        self.note_html = ''
        self.attachments = {}
        self.mindmap_tree = None
        self.workspace_pages = []
        self.workspace_nodes = []
        self.project_id = uuid.uuid4().hex
        self.clear()
        self._clear_ongoing = False
        self._active_filter_categories = []
        self._active_filter_tags = []
        self._active_tag_match = self.TAG_MATCH_ALL
        from prism.groups import GroupManager
        self.groups = GroupManager(self)
        self._group_drag_frame = None

    def clear(self):
        if hasattr(self, 'groups'):
            self.groups.reset()
        self._group_drag_frame = None
        self.category_names = []
        self.tag_names = []
        self.edit_item = None
        self.crop_item = None
        self._clear_ongoing = True
        self._invalidate_view_bounds()
        super().clear()
        self.internal_clipboard = []
        self.rubberband_item = RubberbandItem()
        self.multi_select_item = MultiSelectItem()
        self._clear_ongoing = False

    def _invalidate_view_bounds(self):
        for view in self.views():
            view._cached_scene_bounds = None

    def addItem(self, item):
        logger.debug(f'Adding item {item}')
        self._invalidate_view_bounds()
        super().addItem(item)
        if isinstance(item, PrismPixmapItem):
            item._sync_grayscale_effect()

    def removeItem(self, item):
        self._invalidate_view_bounds()
        logger.debug(f'Removing item {item}')
        if self.edit_item is item:
            item.exit_edit_mode(commit=False)
        if self.crop_item is item:
            self.cancel_crop_mode()
        super().removeItem(item)

    def cancel_active_modes(self):
        """Cancels ongoing crop modes, rubberband modes etc, if there are
        any.
        """
        self.cancel_crop_mode()
        self.end_rubberband_mode()

    def end_rubberband_mode(self):
        if self.rubberband_item.scene():
            logger.debug('Ending rubberband selection')
            self.removeItem(self.rubberband_item)
        self.active_mode = None

    def cancel_crop_mode(self):
        """Cancels an ongoing crop mode, if there is any."""
        if self.crop_item:
            self.crop_item.exit_crop_mode(confirm=False)

    def copy_selection_to_internal_clipboard(self):
        self.internal_clipboard = []
        for item in self.selectedItems(user_only=True):
            self.internal_clipboard.append(item)

    def paste_from_internal_clipboard(self, position, canvas_id=None):
        copies = []
        for item in self.internal_clipboard:
            copy = item.create_copy()
            if canvas_id is not None:
                copy.canvas_id = canvas_id
            copies.append(copy)
        self.undo_stack.push(commands.InsertItems(self, copies, position))

    def raise_to_top(self):
        self.cancel_active_modes()
        items = self.selectedItems(user_only=True)
        z_values = map(lambda i: i.zValue(), items)
        delta = self.max_z + self.Z_STEP - min(z_values)
        logger.debug(f'Raise to top, delta: {delta}')
        for item in items:
            item.setZValue(item.zValue() + delta)

    def lower_to_bottom(self):
        self.cancel_active_modes()
        items = self.selectedItems(user_only=True)
        z_values = map(lambda i: i.zValue(), items)
        delta = self.min_z - self.Z_STEP - max(z_values)
        logger.debug(f'Lower to bottom, delta: {delta}')

        for item in items:
            item.setZValue(item.zValue() + delta)

    def normalize_width_or_height(self, mode):
        """Scale the selected images to have the same width or height, as
        specified by ``mode``.

        :param mode: "width" or "height".
        """

        self.cancel_active_modes()
        values = []
        items = self.selectedItems(user_only=True)
        for item in items:
            rect = self.itemsBoundingRect(items=[item])
            values.append(getattr(rect, mode)())
        if len(values) < 2:
            return
        avg = sum(values) / len(values)
        logger.debug(f'Calculated average {mode} {avg}')

        scale_factors = []
        for item in items:
            rect = self.itemsBoundingRect(items=[item])
            scale_factors.append(avg / getattr(rect, mode)())
        self.undo_stack.push(
            commands.NormalizeItems(items, scale_factors))

    def normalize_height(self):
        """Scale selected images to the same height."""
        return self.normalize_width_or_height('height')

    def normalize_width(self):
        """Scale selected images to the same width."""
        return self.normalize_width_or_height('width')

    def normalize_size(self):
        """Scale selected images to the same size.

        Size meaning the area = widh * height.
        """

        self.cancel_active_modes()
        sizes = []
        items = self.selectedItems(user_only=True)
        for item in items:
            rect = self.itemsBoundingRect(items=[item])
            sizes.append(rect.width() * rect.height())

        if len(sizes) < 2:
            return

        avg = sum(sizes) / len(sizes)
        logger.debug(f'Calculated average size {avg}')

        scale_factors = []
        for item in items:
            rect = self.itemsBoundingRect(items=[item])
            scale_factors.append(math.sqrt(avg / rect.width() / rect.height()))
        self.undo_stack.push(
            commands.NormalizeItems(items, scale_factors))

    def arrange_default(self, items=None):
        default = self.settings.valueOrDefault('Items/arrange_default')
        MAPPING = {
            'optimal': self.arrange_optimal,
            'horizontal': self.arrange,
            'vertical': partial(self.arrange, vertical=True),
            'square': self.arrange_square,
        }

        if items is None:
            MAPPING[default]()
        else:
            MAPPING[default](items=items)

    def arrange(self, vertical=False, items=None):
        """Arrange items in a line (horizontally or vertically)."""

        self.cancel_active_modes()

        if items is None:
            items = sort_by_filename(self.selectedItems(user_only=True))
        else:
            items = sort_by_filename(items)
        if len(items) < 2:
            return

        gap = self.settings.valueOrDefault('Items/arrange_gap')
        center = self.itemsBoundingRect(items=items).center()
        positions = []
        rects = []
        for item in items:
            rects.append({
                'rect': self.itemsBoundingRect(items=[item]),
                'item': item})

        if vertical:
            rects.sort(key=lambda r: r['rect'].topLeft().y())
            sum_height = sum(map(lambda r: r['rect'].height(), rects))
            y = round(center.y() - sum_height/2)
            for rect in rects:
                positions.append(
                    QtCore.QPointF(
                        round(center.x() - rect['rect'].width()/2), y))
                y += rect['rect'].height() + gap

        else:
            rects.sort(key=lambda r: r['rect'].topLeft().x())
            sum_width = sum(map(lambda r: r['rect'].width(), rects))
            x = round(center.x() - sum_width/2)
            for rect in rects:
                positions.append(
                    QtCore.QPointF(
                        x, round(center.y() - rect['rect'].height()/2)))
                x += rect['rect'].width() + gap

        self.undo_stack.push(
            commands.ArrangeItems(self,
                                  [r['item'] for r in rects],
                                  positions))

    def arrange_optimal(self, items=None):
        self.cancel_active_modes()

        if items is None:
            items = self.selectedItems(user_only=True)
        if len(items) < 2:
            return

        gap = self.settings.valueOrDefault('Items/arrange_gap')

        sizes = []
        for item in items:
            rect = self.itemsBoundingRect(items=[item])
            sizes.append((round(rect.width() + gap),
                          round(rect.height() + gap)))

        # The minimal area the items need if they could be packed optimally;
        # we use this as a starting shape for the packing algorithm
        min_area = sum(map(lambda s: s[0] * s[1], sizes))
        width = math.ceil(math.sqrt(min_area))

        positions = None
        while not positions:
            try:
                positions = rpack.pack(
                    sizes, max_width=width, max_height=width)
            except rpack.PackingImpossibleError:
                width = math.ceil(width * 1.2)

        # We want the items to center around the selection's center,
        # not (0, 0)
        center = self.itemsBoundingRect(items=items).center()
        bounds = rpack.bbox_size(sizes, positions)
        diff = center - QtCore.QPointF(bounds[0]/2, bounds[1]/2)
        positions = [QtCore.QPointF(*pos) + diff for pos in positions]

        self.undo_stack.push(commands.ArrangeItems(self, items, positions))

    def arrange_square(self, items=None):
        self.cancel_active_modes()
        max_width = 0
        max_height = 0
        gap = self.settings.valueOrDefault('Items/arrange_gap')

        if items is None:
            items = sort_by_filename(self.selectedItems(user_only=True))
        else:
            items = sort_by_filename(items)

        if len(items) < 2:
            return

        for item in items:
            rect = self.itemsBoundingRect(items=[item])
            max_width = max(max_width, rect.width() + gap)
            max_height = max(max_height, rect.height() + gap)

        # We want the items to center around the selection's center,
        # not (0, 0)
        num_rows = math.ceil(math.sqrt(len(items)))
        center = self.itemsBoundingRect(items=items).center()
        diff = center - num_rows/2 * QtCore.QPointF(max_width, max_height)

        iter_items = iter(items)
        positions = []
        for j in range(num_rows):
            for i in range(num_rows):
                try:
                    item = next(iter_items)
                    rect = self.itemsBoundingRect(items=[item])
                    point = QtCore.QPointF(
                        i * max_width + (max_width - rect.width())/2,
                        j * max_height + (max_height - rect.height())/2)
                    positions.append(point + diff)
                except StopIteration:
                    break

        self.undo_stack.push(commands.ArrangeItems(self, items, positions))

    def flip_items(self, vertical=False):
        """Flip selected items."""
        self.cancel_active_modes()
        self.undo_stack.push(
            commands.FlipItems(self.selectedItems(user_only=True),
                               self.get_selection_center(),
                               vertical=vertical))

    def crop_items(self):
        """Crop selected item."""

        if self.crop_item:
            return
        if self.has_single_image_selection():
            item = self.selectedItems(user_only=True)[0]
            if item.is_image:
                item.enter_crop_mode()

    def sample_color_at(self, position):
        item_at_pos = self.itemAt(position, self.views()[0].transform())
        if item_at_pos:
            return item_at_pos.sample_color_at(position)

    def select_all_items(self):
        self.cancel_active_modes()
        path = QtGui.QPainterPath()
        path.addRect(self.itemsBoundingRect())
        # This is faster than looping through all items and calling setSelected
        self.setSelectionArea(path)

    def deselect_all_items(self):
        self.cancel_active_modes()
        self.clearSelection()

    def has_selection(self):
        """Checks whether there are currently items selected."""

        return bool(self.selectedItems(user_only=True))

    def has_single_selection(self):
        """Checks whether there's currently exactly one item selected."""

        return len(self.selectedItems(user_only=True)) == 1

    def has_multi_selection(self):
        """Checks whether there are currently more than one items selected."""

        return len(self.selectedItems(user_only=True)) > 1

    def has_single_image_selection(self):
        """Checks whether the current selection is a single image."""

        if self.has_single_selection():
            return self.selectedItems(user_only=True)[0].is_image
        return False

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.RightButton:
            # Right-click invokes the context menu on the
            # GraphicsView. We don't need it here.
            return

        if event.button() == Qt.MouseButton.LeftButton:
            self.event_start = event.scenePos()
            item_at_pos = self.itemAt(
                event.scenePos(), self.views()[0].transform())
            if getattr(item_at_pos, 'TYPE', '') == 'group_frame':
                self._group_drag_frame = item_at_pos
                self.active_mode = None
                item_at_pos.begin_drag(event.scenePos())
                event.accept()
                return

            if self.edit_item:
                if item_at_pos != self.edit_item:
                    self.edit_item.exit_edit_mode()
                else:
                    super().mousePressEvent(event)
                    return
            if self.crop_item:
                if item_at_pos != self.crop_item:
                    self.cancel_crop_mode()
                else:
                    super().mousePressEvent(event)
                    return
            if item_at_pos:
                self.active_mode = self.MOVE_MODE
            elif self.items():
                self.active_mode = self.RUBBERBAND_MODE

        super().mousePressEvent(event)

    def mouseDoubleClickEvent(self, event):
        self.cancel_active_modes()
        item = self.itemAt(event.scenePos(), self.views()[0].transform())
        if getattr(item, 'TYPE', '') == 'group_frame':
            self._group_drag_frame = None
            item.edit_note()
            event.accept()
            return
        if item:
            if not item.isSelected():
                item.setSelected(True)
            if getattr(item, 'TYPE', None) == 'video':
                item.toggle_play()
            elif getattr(item, 'TYPE', None) == 'glb':
                item.open_preview()
            elif item.is_editable:
                item.enter_edit_mode()
                self.mousePressEvent(event)
            else:
                self.views()[0].fit_rect(
                    self.itemsBoundingRect(items=[item]),
                    toggle_item=item)
            return
        super().mouseDoubleClickEvent(event)

    def mouseMoveEvent(self, event):
        if self._group_drag_frame is not None:
            self._group_drag_frame.drag_to(event.scenePos())
            event.accept()
            return
        if self.active_mode == self.RUBBERBAND_MODE:
            if not self.rubberband_item.scene():
                logger.debug('Activating rubberband selection')
                self.addItem(self.rubberband_item)
                self.rubberband_item.bring_to_front()
            self.rubberband_item.fit(self.event_start, event.scenePos())
            self.setSelectionArea(self.rubberband_item.shape())
            self.views()[0].reset_previous_transform()
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event):
        if self._group_drag_frame is not None:
            frame = self._group_drag_frame
            self._group_drag_frame = None
            frame.end_drag(event.scenePos())
            event.accept()
            return
        if self.active_mode == self.RUBBERBAND_MODE:
            self.end_rubberband_mode()
        if (self.active_mode == self.MOVE_MODE
                and self.has_selection()
                and self.multi_select_item.active_mode is None
                and self.selectedItems()[0].active_mode is None):
            delta = event.scenePos() - self.event_start
            if not delta.isNull():
                self.undo_stack.push(
                    commands.MoveItemsBy(self.selectedItems(),
                                         delta,
                                         ignore_first_redo=True))
        self.active_mode = None
        super().mouseReleaseEvent(event)

    def selectedItems(self, user_only=False):
        """If ``user_only`` is set to ``True``, only return items added
        by the user (i.e. no multi select outlines and other UI items).

        User items are items that have a ``save_id`` attribute.
        """

        items = super().selectedItems()
        if user_only:
            return list(filter(lambda i: hasattr(i, 'save_id'), items))
        return items

    def items_by_type(self, itype):
        """Returns all items of the given type."""

        return filter(lambda i: getattr(i, 'TYPE', None) == itype,
                      self.items())

    def items_for_save(self):

        """Returns the items that are to be saved.

        Items to be saved are items that have a save_id attribute.
        """

        return filter(lambda i: hasattr(i, 'save_id'),
                      self.items(order=Qt.SortOrder.AscendingOrder))

    def clear_save_ids(self):
        for item in self.items_for_save():
            item.save_id = None

    def on_view_scale_change(self):
        for item in self.selectedItems():
            item.on_view_scale_change()

    def itemsBoundingRect(self, selection_only=False, items=None):
        """Returns the bounding rect of the scene's items; either all of them
        or only selected ones, or the items givin in ``items``.

        Re-implemented to not include the items's selection handles.
        """

        def filter_user_items(ilist):
            return list(filter(lambda i: hasattr(i, 'save_id'), ilist))

        if selection_only:
            base = filter_user_items(self.selectedItems())
        elif items is not None:
            base = items
        else:
            base = filter_user_items(self.items())

        if not base:
            return QtCore.QRectF(0, 0, 0, 0)

        # 边走边比，不收集两个列表再各跑一遍 min/max。剖析显示这个方法
        # 在打开工程期间要处理约八万个角点，建完列表再遍历一次是白花的。
        min_x = min_y = None
        max_x = max_y = 0.0
        for item in base:
            for corner in item.corners_scene_coords:
                px, py = corner.x(), corner.y()
                if min_x is None:
                    min_x = max_x = px
                    min_y = max_y = py
                    continue
                if px < min_x:
                    min_x = px
                elif px > max_x:
                    max_x = px
                if py < min_y:
                    min_y = py
                elif py > max_y:
                    max_y = py

        if min_x is None:
            return QtCore.QRectF(0, 0, 0, 0)

        return QtCore.QRectF(
            QtCore.QPointF(min_x, min_y),
            QtCore.QPointF(max_x, max_y))

    def get_selection_center(self):
        rect = self.itemsBoundingRect(selection_only=True)
        return (rect.topLeft() + rect.bottomRight()) / 2

    def on_selection_change(self):
        if self._clear_ongoing:
            # Ignore events while clearing the scene since the
            # multiselect item will get cleared, too
            return
        if self.has_multi_selection():
            self.multi_select_item.fit_selection_area(
                self.itemsBoundingRect(selection_only=True))
        if self.has_multi_selection() and not self.multi_select_item.scene():
            self.addItem(self.multi_select_item)
            self.multi_select_item.bring_to_front()
        if not self.has_multi_selection() and self.multi_select_item.scene():
            self.removeItem(self.multi_select_item)

    def on_change(self, region):
        if self._clear_ongoing:
            # Ignore events while clearing the scene since the
            # multiselect item will get cleared, too
            return
        if (self.multi_select_item.scene()
                and self.multi_select_item.active_mode is None):
            self.multi_select_item.fit_selection_area(
                self.itemsBoundingRect(selection_only=True))

    # ── Filtering ─────────────────────────────────────────────────────

    _FILTER_OPACITY = 0.3

    #: What several tags mean together, for :meth:`filter_items`.
    TAG_MATCH_ALL = 'all'
    TAG_MATCH_ANY = 'any'

    def filter_items(self, categories=None, tags=None, tag_match='all'):
        """Dim items not matching the given categories/tags.

        ``tag_match`` decides what several tags mean together: ``'all'``
        keeps an item only when it carries every one of them, ``'any'``
        keeps it as soon as one of them matches.  Names are matched the
        same way the tag sidebar matches them - a parent tag also finds
        its children, and a synonym finds the name it folds into - so the
        board and the sidebar never disagree about who has a tag.
        """
        self._active_filter_categories = categories or []
        self._active_filter_tags = tags or []
        self._active_tag_match = (
            self.TAG_MATCH_ANY
            if str(tag_match).lower() == self.TAG_MATCH_ANY
            else self.TAG_MATCH_ALL)
        any_of = self._active_tag_match == self.TAG_MATCH_ANY
        system = tag_system(self)
        user_items = [i for i in self.items() if hasattr(i, 'save_id')]
        for item in user_items:
            match = True
            if self._active_filter_categories:
                item_cats = getattr(item, '_categories', [])
                if not any(c in item_cats
                           for c in self._active_filter_categories):
                    match = False
            if match and self._active_filter_tags:
                index = name_index(getattr(item, '_tags', []), system)
                wanted = [str(name).strip().casefold()
                          for name in self._active_filter_tags]
                hits = sum(1 for name in wanted if name in index)
                match = hits > 0 if any_of else hits == len(wanted)
            item.setOpacity(1.0 if match else self._FILTER_OPACITY)
        self.filter_changed.emit()

    def clear_filter(self):
        """Remove all active filters and restore full opacity."""
        self._active_filter_categories = []
        self._active_filter_tags = []
        self._active_tag_match = self.TAG_MATCH_ALL
        for item in self.items():
            if hasattr(item, 'save_id'):
                item.setOpacity(getattr(item, '_user_opacity', 1.0))
        self.filter_changed.emit()

    def is_filtered(self):
        return bool(self._active_filter_categories
                    or self._active_filter_tags)

    def get_all_tags(self):
        tag_counts = {}
        for item in self.items():
            if hasattr(item, 'save_id'):
                for tag in getattr(item, '_tags', []):
                    tag_counts[tag] = tag_counts.get(tag, 0) + 1
        return tag_counts

    def get_all_categories(self):
        cat_counts = {}
        for item in self.items():
            if hasattr(item, 'save_id'):
                for cat in getattr(item, '_categories', []):
                    cat_counts[cat] = cat_counts.get(cat, 0) + 1
        return cat_counts

    def add_item_later(self, itemdata, selected=False):
        """Keep an item for adding later via ``add_queued_items``

        :param dict itemdata: Defines the item's data
        :param bool selected: Whether the item is initialised as selected
        """

        self.items_to_add.put((itemdata, selected))

    def add_queued_items(self):
        """Adds items added via ``add_item_later``

        All scene mutations are batched: signals are blocked and
        viewport updates disabled during the loop so that the expensive
        cascades (recalc_scene_rect, selection handlers) fire at most
        once at the end instead of per-item.
        """
        if self.items_to_add.empty():
            return

        collected = []
        while not self.items_to_add.empty():
            collected.append(self.items_to_add.get())
        if not collected:
            return

        # Suppress per-item signal cascades during batch insertion.
        was_blocked = self.blockSignals(True)
        vp = None
        updates_enabled = True
        try:
            views = self.views()
            vp = views[0].viewport() if views else None
        except Exception:
            pass
        if vp is not None:
            updates_enabled = vp.updatesEnabled()
            vp.setUpdatesEnabled(False)
        try:
            created = []
            for data, selected in collected:
                typ = data.pop('type')
                cls = item_registry.get(typ)
                if not cls:
                    logger.warning(
                        f'Encountered item of unknown type: {typ}')
                    cls = PrismErrorItem
                    data['data'] = {'text': f'Item of unknown type: {typ}'}
                item = cls.create_from_data(**data)
                item.update_from_data(**data)
                self.addItem(item)
                created.append((item, selected))

            # Single z-value pass: update max_z/min_z from existing
            # items, then bring selected items to front in order.
            for item, _selected in created:
                zv = item.zValue()
                if zv > self.max_z:
                    self.max_z = zv
                if zv < self.min_z:
                    self.min_z = zv
            for item, selected in created:
                if selected:
                    self.max_z += self.Z_STEP
                    item.setZValue(self.max_z)
                    item.setSelected(True)
        finally:
            self.blockSignals(was_blocked)
            if vp is not None:
                vp.setUpdatesEnabled(updates_enabled)
            if not was_blocked:
                self.selectionChanged.emit()
                self.changed.emit([QtCore.QRectF()])
