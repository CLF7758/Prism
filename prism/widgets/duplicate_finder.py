"""Non-destructive duplicate-photo results dialog."""

import logging
import os

from PyQt6 import QtCore, QtGui, QtWidgets
from PyQt6.QtCore import Qt

from prism import commands
from prism.duplicate_scan import ScanScope, canvas_label
from prism.i18n import _
from prism.widgets import components


logger = logging.getLogger(__name__)


def _has_a_size(item):
    """Whether a canvas item actually has pixels to show.

    ``boundingRect()`` is not the thing to ask: once an item is selected its
    selection handles widen the rectangle, so a photo whose size never made
    it onto the canvas would still look like it had one.  The unselected
    rectangle is the honest answer.
    """
    rectangle = getattr(item, 'bounding_rect_unselected', None)
    if not callable(rectangle):
        return True
    rect = rectangle()
    return rect is not None and not rect.isEmpty()


class DuplicateFinderDialog(QtWidgets.QDialog):
    """Show duplicate groups and act on them without touching the filter.

    Locating picks out exactly the photo the user clicked, and photos can be
    deleted straight from here - both go through the undo stack, so nothing
    happens that Ctrl+Z cannot take back.
    """

    def __init__(self, view, groups, parent=None, scope=None):
        super().__init__(parent or view.window())
        self.view = view
        self.groups = groups
        self.scope = scope
        self.setWindowTitle(_('Duplicate Photos'))
        self.resize(840, 520)
        self.setModal(False)

        layout = QtWidgets.QVBoxLayout(self)
        self.summary = QtWidgets.QLabel()
        self.summary.setWordWrap(True)
        layout.addWidget(self.summary)

        splitter = QtWidgets.QSplitter(Qt.Orientation.Horizontal)
        self.group_list = QtWidgets.QListWidget()
        self.image_list = QtWidgets.QListWidget()
        self.image_list.setIconSize(QtCore.QSize(128, 96))
        self.image_list.setSelectionMode(
            QtWidgets.QAbstractItemView.SelectionMode.ExtendedSelection)
        splitter.addWidget(self.group_list)
        splitter.addWidget(self.image_list)
        splitter.setSizes([220, 560])
        layout.addWidget(splitter, 1)

        self._fill_groups()

        buttons = QtWidgets.QDialogButtonBox(
            QtWidgets.QDialogButtonBox.StandardButton.Close)
        buttons.rejected.connect(self.close)
        self.locate_button = buttons.addButton(
            _('Locate on Canvas'),
            QtWidgets.QDialogButtonBox.ButtonRole.ActionRole)
        self.locate_button.clicked.connect(self._locate_selected_images)
        self.delete_button = buttons.addButton(
            _('Delete Selected'),
            QtWidgets.QDialogButtonBox.ButtonRole.DestructiveRole)
        self.delete_button.clicked.connect(self._delete_selected_images)
        layout.addWidget(buttons)

        # Feedback line: the buttons used to act silently, and because this
        # window floats over the canvas it was impossible to tell whether
        # anything had happened.
        self.feedback = components.RoleLabel(role='secondary')
        self.feedback.setWordWrap(True)
        layout.addWidget(self.feedback)

        self.group_list.currentItemChanged.connect(self._show_group)
        self.image_list.itemSelectionChanged.connect(self._update_buttons)
        self.image_list.itemDoubleClicked.connect(
            lambda _item: self._locate_selected_images())
        self._update_summary()
        if self.group_list.count():
            self.group_list.setCurrentRow(0)

    # ── Filling ───────────────────────────────────────────────

    def _fill_groups(self):
        self.group_list.blockSignals(True)
        self.group_list.clear()
        for index, group in enumerate(self.groups, 1):
            item = QtWidgets.QListWidgetItem(
                _('Group {number} ({count})').format(
                    number=index, count=len(group)))
            item.setData(Qt.ItemDataRole.UserRole, index - 1)
            self.group_list.addItem(item)
        self.group_list.blockSignals(False)

    def _update_summary(self):
        total = sum(len(group) for group in self.groups)
        text = _('{groups} groups · {images} images. Selecting a group does '
                 'not hide other canvas items.').format(
                     groups=len(self.groups), images=total)
        if self.scope:
            # 走 label() 拿到给人看的名字。以前直接填 self.scope，那是
            # ScanScope 的原始值，用户看到的是 'Scanned the "all-canvases"
            # category only.' —— 机器码加一句停用的"分类"措辞。
            text += '\n' + _('Scanned: {scope}').format(
                scope=ScanScope.label(self.scope))
        else:
            text += '\n' + _('Scanned every canvas in the project.')
        self.summary.setText(text)

    @staticmethod
    def _canvas_item(source):
        """把查重结果里的一条解成画布图元，解不出来返回 None。

        组里存的是 ``ImageSource`` —— 查重服务用来描述"一张待比对的图"
        的数据对象，**不是画布图元**。它有个 ``item`` 属性指回图元。
        以前这里直接把数据对象当图元用，后果是三处一起坏：

          * 缩略图拿不到（数据对象没有 ``pixmap``）—— 你看到的"重复图片
            没有缩略图"
          * 点"定位图片"崩溃（数据对象没有 ``setVisible``）
          * 标题只能显示 '?'（数据对象没有 ``filename``）

        传进来的也可能是图元本身（有些地方直接塞图元），所以先看有没有
        ``item`` 属性。``item`` 为 None 表示这张图已经没有对应的画布图元
        —— 比如它属于另一个页面，或者已经被删了。
        """
        inner = getattr(source, 'item', None)
        if inner is not None:
            return inner
        # 不是 ImageSource（没有 item 属性），那它自己就是图元
        if not hasattr(source, 'item'):
            return source
        return None

    def _show_group(self, current, _previous=None):
        self.image_list.clear()
        if current is None:
            self._update_buttons()
            return
        group = self.groups[current.data(Qt.ItemDataRole.UserRole)]
        for source in group:
            scene_item = self._canvas_item(source) or source
            filename = getattr(scene_item, 'filename', '') or ''
            title = getattr(scene_item, '_title', '') or os.path.basename(
                filename)
            title = title or _('Untitled image')
            # 页面名 + 分类。任务书 §6：同一图片在不同页面出现时要能
            # 看出它是从哪来的，光有分类名分不出两页上的同一张图。
            where = canvas_label(scene_item, self._pages())
            row = QtWidgets.QListWidgetItem(
                title + '\n' + (where or _('Uncategorised')))
            pixmap = getattr(scene_item, 'pixmap', lambda: None)()
            if pixmap is not None and not pixmap.isNull():
                row.setIcon(QtGui.QIcon(pixmap))
            row.setToolTip(filename or title)
            row.setData(Qt.ItemDataRole.UserRole, scene_item)
            self.image_list.addItem(row)
        if self.image_list.count():
            self.image_list.setCurrentRow(0)
        self._update_buttons()

    # ── Actions ───────────────────────────────────────────────

    def _pages(self):
        """当前的页面目录。取不到就给空的 —— 老工程或测试里可能没有。"""
        scene = getattr(self.view, 'scene', None)
        return getattr(scene, 'workspace_pages', None) or ()

    def _selected_items(self):
        """选中的行对应的**画布图元**。

        解不出图元的行会被丢掉 —— 那是"这张图已经不在任何画布上了"
        （被删了，或者属于另一个工程）。定位和删除都用这个，所以两者
        都不会再拿到非图元对象。
        """
        items = []
        for row in self.image_list.selectedItems():
            item = self._canvas_item(row.data(Qt.ItemDataRole.UserRole))
            if item is not None:
                items.append(item)
        return items

    def _update_buttons(self):
        if not hasattr(self, 'delete_button'):
            return
        count = len(self.image_list.selectedItems())
        self.delete_button.setEnabled(bool(count))
        self.delete_button.setText(
            _('Delete Selected ({count})').format(count=count) if count
            else _('Delete Selected'))

    def _report(self, text):
        """Put a line of feedback under the lists.

        Locating used to change the canvas with no word of acknowledgement,
        and since this window floats over the canvas the user could not tell
        whether anything had happened at all.
        """
        if hasattr(self, 'feedback'):
            self.feedback.setText(text)

    def _locate_selected_images(self):
        """Centre the canvas on the photo the user picked.

        Only the picked photo (or photos) is selected, so the canvas shows
        exactly where it sits instead of the bounding box of a whole group.
        """
        picked = self._selected_items()
        if not self.image_list.selectedItems():
            self._report(_('Select a photo in the list first.'))
            return
        if not picked:
            # Rows are ticked but none of them still has a canvas item.
            self._report(_(
                'This photo is no longer on a canvas, so there is nothing '
                'to locate. It may have been deleted or belong to another '
                'project.'))
            return
        items = picked

        # The window floats over the canvas, so move it out of the way;
        # otherwise the thing being located is hidden behind it.
        centred = self._move_out_of_the_way(items)

        names = [getattr(item, '_title', '') or getattr(item, 'filename', '')
                 or '?' for item in items]
        where = '、'.join(getattr(items[0], '_categories', []) or []) \
            or _('Uncategorised')
        if not centred:
            # Saying "located" when the view did not move is the worst of
            # both worlds: the user believes it worked and goes looking for
            # a photo that is not on screen anywhere.
            self._report(_(
                'Selected {names}, but the view could not be centred on it. '
                'The photo has no size on the canvas yet - its original may '
                'still be loading.').format(names='、'.join(names[:3])))
            return
        self._report(
            _('Located {names} (category: {where}).').format(
                names='、'.join(names[:3]), where=where))

    def _move_out_of_the_way(self, items):
        """Nudge this window aside and centre the view on ``items``.

        Returns whether the view was actually moved.  A photo whose
        ``boundingRect()`` is empty - its size never made it onto the canvas
        - gives an empty rectangle, and ``fit_rect`` then does nothing at
        all; the caller has to be able to tell that apart from success.
        """
        screen = self.screen()
        if screen is not None:
            available = screen.availableGeometry()
            geometry = self.frameGeometry()
            # Only move when the window actually covers the middle of the screen.
            centre_x = available.center().x()
            if geometry.left() <= centre_x <= geometry.right():
                x = available.right() - geometry.width() - 12
                y = available.top() + 12
                self.move(max(available.left() + 12, x), y)
        # Keep the user's filters intact.  The selected rows are made visible
        # below so locating a cross-page duplicate never makes the canvas
        # appear empty or silently switches its category.
        library = self.view.category_panel
        library.update_counts()
        tabs = getattr(self.view.parent, 'tabs', None)
        if tabs is not None:
            tabs.setCurrentWidget(self.view)
        # Selecting and showing them is done with the scene's signals off.
        # Both make QGraphicsScene emit ``changed``, and the sidebar answers
        # that with a recount 300 ms later - which runs the filter, which
        # recomputes visibility from the current category and hides the
        # photo we just revealed.  To the user that is "it flashes and then
        # disappears".  The selection itself is still real (the handles are
        # drawn by the items), so the sidebar is told once, by hand, after.
        self.view.scene.blockSignals(True)
        try:
            self.view.scene.deselect_all_items()
            rect = QtCore.QRectF()
            for item in items:
                item.setVisible(True)
                # ...and the filter must not take it away again either: a
                # recount scheduled before this click can still land after
                # it.  The pin is dropped as soon as the user touches the
                # filter (see MediaLibraryPanel._apply_filter).
                item._keep_visible = True
                item.setSelected(True)
                rect = rect.united(item.sceneBoundingRect())
        finally:
            self.view.scene.blockSignals(False)
        self.view.scene.selectionChanged.emit()
        # One line that answers "it did not go where I told it to" from a
        # user's log: which canvas the photos claim, where the rectangle
        # ended up, and whether they have a size and are visible at all.
        logger.info(
            'Locating %d photo(s): canvas=%s rect=%s size=%s visible=%s',
            len(items),
            [getattr(item, '_canvas_id', None) for item in items],
            rect,
            ['%gx%g' % (item.boundingRect().width(),
                        item.boundingRect().height()) for item in items],
            [item.isVisible() for item in items])
        if not any(_has_a_size(item) for item in items):
            # A photo whose size never made it onto the canvas still has a
            # rectangle once it is selected - the selection handles add one -
            # so centring on that would zoom into a few pixels of empty
            # space.  Saying "located" there is the worst of both worlds:
            # the user believes it worked and hunts for a photo that is not
            # on screen anywhere.
            return False
        self.view.fit_rect(rect)
        self.view.setFocus(Qt.FocusReason.OtherFocusReason)
        return True

    def _delete_selected_images(self):
        items = self._selected_items()
        if not items:
            self._report(_('Select a photo in the list first.'))
            return
        count = len(items)
        answer = QtWidgets.QMessageBox.question(
            self, _('Delete Photos'),
            _('Delete the {count} selected photo(s) from the canvas?')
            .format(count=count))
        if answer != QtWidgets.QMessageBox.StandardButton.Yes:
            return

        self.view.scene.deselect_all_items()
        self.view.undo_stack.push(commands.DeleteItems(self.view.scene, items))
        removed = set(id(item) for item in items)

        # Drop the deleted photos from the results and forget empty groups.
        remaining = []
        for group in self.groups:
            kept = [item for item in group if id(item) not in removed]
            if len(kept) > 1:
                remaining.append(kept)
        self.groups = remaining
        self._fill_groups()
        self._update_summary()
        if self.group_list.count():
            self.group_list.setCurrentRow(0)
        else:
            self.image_list.clear()
            self._update_buttons()
        # Say what happened, and how to take it back - deletions go through
        # the undo stack, so Ctrl+Z restores them.
        self._report(
            _('Deleted {count} photo(s). Press Ctrl+Z to undo.').format(
                count=count))
