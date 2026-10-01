"""Resource tree interaction surface for the shared workspace hierarchy."""

from PyQt6 import QtCore, QtGui, QtWidgets
from PyQt6.QtCore import Qt

from prism.widgets.components.icons import icon
from prism.widgets.resource_tree_model import (
    NODE_ID_ROLE, NODE_TYPE_ROLE, SECTION_ROLE, TAG_NAME_ROLE, UI_SECTIONS)
from prism import ui_tokens
from prism.i18n import _


class _NameEditor(QtWidgets.QLineEdit):
    """就地命名的输入框（§7.4）。

    在普通 QLineEdit 之上只加两件事：

    - 打开时全选 —— 默认名不用先删一遍，直接输入就替换掉；
    - **中文输入法组合期间按 Enter 只提交候选，不算确认**：预编辑文字
      （``preeditString``）还在的时候，那个回车是输入法的，不能把名字定死。
    """

    def __init__(self, parent=None):
        super().__init__(parent)
        self._preedit = ''

    @property
    def composing(self):
        return bool(self._preedit)

    def inputMethodEvent(self, event):
        self._preedit = event.preeditString()
        super().inputMethodEvent(event)

    def keyPressEvent(self, event):
        if (event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter)
                and self.composing):
            event.accept()
            return
        super().keyPressEvent(event)


class _BlankAreaDelegate(QtWidgets.QStyledItemDelegate):
    #: 分区行右侧那个「+」的边长与右边距（§7.2）
    ADD_BUTTON = 20
    ADD_MARGIN = 6

    def __init__(self, parent=None):
        super().__init__(parent)
        self._refused = False
        # 注意：不能定义名为 `closeEditor` 的方法 —— 在这个类上它是**信号**
        # 而不是虚函数，同名方法会把信号遮蔽掉（super().closeEditor 会报
        # 「native Qt signal is not callable」），Qt 那边的编辑器关闭流程也
        # 就接不上了。所以这里改成连自己的信号。
        self.closeEditor.connect(self._on_editor_closed)

    def add_button_rect(self, rect, index):
        """「+」在这行里的矩形；不是分区行就返回 None。"""
        if not index.isValid() or index.data(NODE_TYPE_ROLE) != 'section':
            return None
        side = self.ADD_BUTTON
        return QtCore.QRect(rect.right() - side - self.ADD_MARGIN,
                            rect.top() + (rect.height() - side) // 2,
                            side, side)

    def paint(self, painter, option, index):
        super().paint(painter, option, index)
        if option.state & QtWidgets.QStyle.StateFlag.State_Selected:
            painter.save()
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QtGui.QColor(ui_tokens.COLOURS_DARK['accent']))
            painter.drawRoundedRect(QtCore.QRectF(option.rect.left(), option.rect.top() + 5, 2, option.rect.height() - 10), 1, 1)
            painter.restore()
        kind = index.data(NODE_TYPE_ROLE)
        if kind == 'section':
            rect = self.add_button_rect(option.rect, index)
            if rect is not None:
                icon('plus').paint(painter, rect)
            return
        if (kind != 'blank' or
                not option.state & QtWidgets.QStyle.StateFlag.State_MouseOver):
            return
        hint = ('右键新建标签' if index.data(SECTION_ROLE) == 'tag'
                else '右键新建页面或文件夹')
        painter.save()
        painter.setPen(QtGui.QColor(ui_tokens.COLOURS_DARK['text-secondary']))
        font = painter.font()
        font.setPixelSize(12)
        painter.setFont(font)
        painter.drawText(option.rect.adjusted(8, 0, -4, 0),
                         Qt.AlignmentFlag.AlignVCenter, hint)
        painter.restore()

    # ── 就地命名（C5 / §7.4）──────────────────────────────────────

    def createEditor(self, parent, option, index):
        editor = _NameEditor(parent)
        editor._prism_index = QtCore.QPersistentModelIndex(index)
        # 全选要等视图把文字填进编辑器之后再排队做一次。
        QtCore.QTimer.singleShot(0, editor.selectAll)
        return editor

    def setModelData(self, editor, model, index):
        self._refused = False
        if isinstance(editor, _NameEditor):
            if not model.setData(index, editor.text(),
                                 Qt.ItemDataRole.EditRole):
                # 重名 / 非法字符：模型已经把草稿行放回去、也保留了输入，
                # 这里只记一下，关掉编辑器之后再重开。
                self._refused = True
            return
        super().setModelData(editor, model, index)

    def _on_editor_closed(self, editor, hint):
        """编辑器关掉了：Esc 就收掉草稿行，被拒就重开让用户接着改。

        这个槽连的是 `closeEditor` **信号**（见上面的注释）。它比视图自己的
        closeEditor 槽先跑，所以这时编辑器还活着，能读到里面的文字。
        """
        refused = getattr(self, '_refused', False)
        reverted = (hint == QtWidgets.QAbstractItemDelegate.EndEditHint.
                    RevertModelCache)
        index = getattr(editor, '_prism_index', None)
        if not isinstance(editor, _NameEditor):
            return
        view = self.parent()
        model = view.model() if isinstance(view, QtWidgets.QAbstractItemView) \
            else None
        if reverted:
            # Esc：临时节点直接消失。服务层从头到尾没被写过，
            # 所以也不会留下撤销记录（§7.4）。
            if model is not None:
                model.clear_draft()
        elif refused and index is not None and index.isValid():
            QtCore.QTimer.singleShot(
                0, lambda idx=QtCore.QModelIndex(index): view.edit(idx))


class ResourceTreeView(QtWidgets.QTreeView):
    tag_requested = QtCore.pyqtSignal(str)
    rename_tag_requested = QtCore.pyqtSignal(str)
    delete_tag_requested = QtCore.pyqtSignal(str)
    create_requested = QtCore.pyqtSignal(str, str, object)
    #: 就地新建确认成功；参数是服务返回的节点字典。
    create_committed = QtCore.pyqtSignal(object)
    open_page_requested = QtCore.pyqtSignal(str, str)
    delete_requested = QtCore.pyqtSignal(str)
    #: 批量操作（§7.5）：参数是被选中的节点 id 列表。
    move_requested = QtCore.pyqtSignal(list)
    delete_many_requested = QtCore.pyqtSignal(list)
    #: 导出这一项（以及它下面的全部内容）；参数是 (分区, 节点 id)。
    #: 节点 id 为 None 表示「整个分区」—— 分区根行和它的空白创建行都发这个。
    export_requested = QtCore.pyqtSignal(str, object)
    #: 把外面的一个目录 / .zip / 一批文件导进来，内容落在这一项下面；
    #: 参数同样是 (分区, 节点 id)，None 表示整个分区。
    #: 页面行不发这个 —— 页面是叶子，装不下子节点。
    import_requested = QtCore.pyqtSignal(str, object)
    new_tag_requested = QtCore.pyqtSignal()
    manage_tags_requested = QtCore.pyqtSignal()
    #: 想看回收站（§7.6）。
    trash_requested = QtCore.pyqtSignal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName('resourceTree')
        self.setIconSize(QtCore.QSize(16, 16))
        self.setItemDelegate(_BlankAreaDelegate(self))
        self.setMouseTracking(True)
        self.setHeaderHidden(True)
        self.setIndentation(16)
        self.setUniformRowHeights(False)
        self.setSelectionMode(
            QtWidgets.QAbstractItemView.SelectionMode.ExtendedSelection)
        self.setEditTriggers(
            QtWidgets.QAbstractItemView.EditTrigger.NoEditTriggers)
        self.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.customContextMenuRequested.connect(self._show_context_menu)
        self.clicked.connect(self._on_clicked)
        # 拖拽即移动（§7.5）：树内部移动，不复制、不跨应用。
        self.setDragDropMode(
            QtWidgets.QAbstractItemView.DragDropMode.InternalMove)
        self.setDefaultDropAction(Qt.DropAction.MoveAction)
        self.setDragDropOverwriteMode(False)
        self.setDropIndicatorShown(True)
        self._drop_intent = None
        self._hover_index = None
        self._hover_timer = QtCore.QTimer(self)
        self._hover_timer.setSingleShot(True)
        self._hover_timer.setInterval(self.HOVER_EXPAND_MS)
        self._hover_timer.timeout.connect(self._expand_hovered)
        # 焦点 / 展开 / 滚动位置的变化都攒一下再写设置（§7.6）
        self._state_timer = QtCore.QTimer(self)
        self._state_timer.setSingleShot(True)
        self._state_timer.setInterval(self.STATE_SAVE_DELAY_MS)
        self._state_timer.timeout.connect(self.save_state)
        self.expanded.connect(self._schedule_state_save)
        self.collapsed.connect(self._schedule_state_save)
        self.verticalScrollBar().valueChanged.connect(self._schedule_state_save)
        self._trimming_selection = False

    def setModel(self, model):
        super().setModel(model)
        if model is None:
            return
        model.draft_committed.connect(self.create_committed)
        model.draft_refused.connect(self._show_refusal)
        # 换工程了：把上次记下的焦点/展开/滚动位置放回去（§7.6）
        self.restore_state()
        selection = self.selectionModel()
        if selection is not None:
            selection.currentChanged.connect(self._schedule_state_save)
            selection.selectionChanged.connect(self._enforce_single_section)

    def begin_create(self, section, node_type, parent_id=None,
                     base_title=''):
        """就地新建：临时行 → 展开父级 → 滚到它 → 全选改名（§7.4）。

        名字确认之前什么都没写进工程；按 Esc 这一行会直接消失。
        """
        model = self.model()
        index = model.begin_draft(section, node_type, parent_id, base_title)
        parent_index = model.index_for_id(parent_id) if parent_id else \
            model.index(UI_SECTIONS.index(section), 0)
        if parent_index.isValid():
            self.expand(parent_index)
        self.setCurrentIndex(index)
        self.scrollTo(index)
        self.edit(index)
        return index

    def _show_refusal(self, message):
        """命名被拒时给一句人话，不弹窗口（输入还留着，接着改就行）。"""
        QtWidgets.QToolTip.showText(QtGui.QCursor.pos(), message, self)

    # ── 拖拽（C6 / §7.5）──────────────────────────────────────────

    #: 目标行顶部/底部各 25% 是「同级插入」，中间 50% 是「移入文件夹」
    INSERT_BAND = 0.25
    #: 悬停多久自动展开折叠的文件夹
    HOVER_EXPAND_MS = 650

    def selected_ids(self):
        """当前选中的节点 id（拖动和批量操作用同一份）。"""
        return [index.data(NODE_ID_ROLE)
                for index in self.selectionModel().selectedIndexes()
                if index.data(NODE_TYPE_ROLE) in ('folder', 'page')]

    def _enforce_single_section(self, *_args):
        """§7.5：**选择只在同一分区内**，越界的部分当场取消掉。

        点另一个分区的行、或者 Shift 拖出一段跨分区的范围时都会走到这里；
        跨分区之后留下的是「焦点所在分区」的那些行。
        """
        if self._trimming_selection or self.selectionModel() is None:
            return
        current = self.currentIndex()
        if not current.isValid():
            return
        section = current.data(SECTION_ROLE)
        stray = [index for index in self.selectionModel().selectedIndexes()
                 if index.data(SECTION_ROLE) != section]
        if not stray:
            return
        self._trimming_selection = True
        try:
            for index in stray:
                self.selectionModel().select(
                    index,
                    QtCore.QItemSelectionModel.SelectionFlag.Deselect |
                    QtCore.QItemSelectionModel.SelectionFlag.Rows)
        finally:
            self._trimming_selection = False

    def drop_intent_for(self, index, pos, node_ids=None):
        """这一点是什么意思：``((mode, target_id), '')`` 或 ``(None, 原因)``。

        判定就是 §7.5 的那两句话：顶部/底部 25% 同级插入，文件夹中间 50%
        移入；页面中间不接受子节点，只能排在前/后。分区行和空白创建区
        表示「移到这个分区的末尾」。
        """
        model = self.model()
        ids = self.selected_ids() if node_ids is None else node_ids
        if model is None or index is None or not index.isValid():
            return None, '这里不能放'
        kind = index.data(NODE_TYPE_ROLE)
        section = index.data(SECTION_ROLE)
        if kind in ('section', 'blank'):
            reason = model.can_accept(ids, 'append', None, section)
            return (None, reason) if reason else (('append', None), '')
        if kind not in ('folder', 'page'):
            return None, '这里不能放'
        node_id = index.data(NODE_ID_ROLE)
        rect = self.visualRect(index)
        ratio = (pos.y() - rect.top()) / max(1, rect.height())
        if kind == 'folder' and self.INSERT_BAND <= ratio <= 1 - self.INSERT_BAND:
            mode = 'into'
        else:
            mode = 'before' if ratio < 0.5 else 'after'
        reason = model.can_accept(ids, mode, node_id, section)
        return (None, reason) if reason else ((mode, node_id), '')

    def dragMoveEvent(self, event):
        pos = event.position().toPoint()
        index = self.indexAt(pos)
        ids = self.model().dragged_ids(event.mimeData())
        intent, reason = self.drop_intent_for(index, pos, ids)
        self._drop_intent = intent
        self._drop_index = QtCore.QPersistentModelIndex(index)
        self.viewport().update()
        if intent is None:
            self._set_hover(None)
            event.ignore()
            if reason:
                QtWidgets.QToolTip.showText(self.viewport().mapToGlobal(pos),
                                             reason, self)
            return
        # 悬停 650ms 展开折叠的文件夹（只对「移入」有意义）
        self._set_hover(index if intent[0] == 'into' else None)
        # 交给 Qt 画那条落点指示线；语义已经由 _drop_intent 定下来了。
        event.setDropAction(Qt.DropAction.MoveAction)
        event.accept()

    def dragEnterEvent(self, event):
        ids = self.model().dragged_ids(event.mimeData())
        if ids and all(i in self.model()._nodes and
                       self.model()._nodes[i]['nodeType'] in ('folder', 'page') for i in ids):
            event.setDropAction(Qt.DropAction.MoveAction)
            event.accept()
        else:
            event.ignore()

    def startDrag(self, supported_actions):
        indexes = [self.model().index_for_id(i) for i in self.selected_ids()]
        if not indexes:
            return
        drag = QtGui.QDrag(self)
        drag.setMimeData(self.model().mimeData(indexes))
        drag.exec(Qt.DropAction.MoveAction)
        self._drop_intent = None
        self._set_hover(None)
        self.viewport().update()

    def paintEvent(self, event):
        super().paintEvent(event)
        index = getattr(self, '_drop_index', None)
        if self._drop_intent is None or index is None or not index.isValid():
            return
        rect = self.visualRect(QtCore.QModelIndex(index))
        painter = QtGui.QPainter(self.viewport())
        colour = QtGui.QColor(ui_tokens.COLOURS_DARK['accent'])
        painter.setPen(QtGui.QPen(colour, 2))
        mode = self._drop_intent[0]
        if mode in ('into', 'append'):
            fill = QtGui.QColor(colour)
            fill.setAlpha(40)
            painter.setBrush(fill)
            painter.drawRoundedRect(rect.adjusted(1, 1, -1, -1), 4, 4)
        else:
            y = rect.top() if mode == 'before' else rect.bottom()
            painter.drawLine(rect.left(), y, rect.right(), y)
        painter.end()

    def dropEvent(self, event):
        # Recompute at release: selection and cached hover can be stale.
        pos = event.position().toPoint()
        index = self.indexAt(pos)
        ids = self.model().dragged_ids(event.mimeData())
        intent, reason = self.drop_intent_for(index, pos, ids)
        self._drop_intent = None
        self.viewport().update()
        self._set_hover(None)
        if intent is None:
            event.ignore()
            return
        mode, target_id = intent
        section = index.data(SECTION_ROLE)
        if self.model().apply_drop(ids, mode, target_id, section):
            if mode == 'into':
                self.expand(self.model().index_for_id(target_id))
            if ids:
                moved_index = self.model().index_for_id(ids[0])
                self.setCurrentIndex(moved_index)
                self.scrollTo(moved_index)
            event.setDropAction(Qt.DropAction.MoveAction)
            event.accept()
        else:
            event.ignore()

    def dragLeaveEvent(self, event):
        self._drop_intent = None
        self._set_hover(None)
        self.viewport().update()
        super().dragLeaveEvent(event)

    def _set_hover(self, index):
        if (index is not None and index.isValid()
                and index.data(NODE_TYPE_ROLE) == 'folder'
                and not self.isExpanded(index)):
            if self._hover_index != index:
                self._hover_index = QtCore.QPersistentModelIndex(index)
                self._hover_timer.start()
        else:
            self._hover_timer.stop()
            self._hover_index = None

    def _expand_hovered(self):
        if self._hover_index is not None and self._hover_index.isValid():
            self.expand(QtCore.QModelIndex(self._hover_index))
        self._hover_index = None

    def _on_clicked(self, index):
        if index.data(NODE_TYPE_ROLE) == 'tag':
            self.tag_requested.emit(index.data(TAG_NAME_ROLE))
        if index.data(NODE_TYPE_ROLE) == 'page':
            node_id = index.data(NODE_ID_ROLE)
            node = self.model().service._by_id(node_id)
            self.open_page_requested.emit(node['section'], node['pageId'])

    def mousePressEvent(self, event):
        """分区行右边的「+」：点它弹「新建页面 / 新建文件夹」（§7.2）。"""
        if event.button() == Qt.MouseButton.LeftButton:
            index = self.indexAt(event.position().toPoint())
            if index.isValid():
                rect = self.itemDelegate().add_button_rect(
                    self.visualRect(index), index)
                if rect is not None and rect.contains(
                        event.position().toPoint()):
                    menu = self.menu_for_index(index)
                    menu.exec(event.globalPosition().toPoint())
                    event.accept()
                    return
        super().mousePressEvent(event)

    def menu_for_index(self, index):
        """Build the menu from hit-tested section, never last selection."""
        menu = QtWidgets.QMenu(self)
        # 选了好几个的时候，先把"批量"这件事说清楚（§7.5）。
        selected = self.selected_ids()
        if len(selected) > 1:
            header = menu.addAction(f'已选择 {len(selected)} 项')
            header.setEnabled(False)
            menu.addAction('移动到…',
                           lambda: self.move_requested.emit(list(selected)))
            menu.addAction('删除',
                           lambda: self.delete_many_requested.emit(
                               list(selected)))
            menu.addSeparator()
        kind = index.data(NODE_TYPE_ROLE) if index.isValid() else None
        section = index.data(SECTION_ROLE) if index.isValid() else None
        if kind == 'tag' or section == 'tag':
            if kind == 'tag':
                name = index.data(TAG_NAME_ROLE)
                menu.addAction('查看关联内容', lambda: self.tag_requested.emit(name))
                menu.addAction('重命名标签', lambda: self.rename_tag_requested.emit(name))
                menu.addAction('删除标签', lambda: self.delete_tag_requested.emit(name))
                menu.addSeparator()
            menu.addAction('新建标签', self.new_tag_requested.emit)
            menu.addAction('管理标签', self.manage_tags_requested.emit)
        elif kind in ('section', 'blank', 'folder'):
            parent_id = index.data(NODE_ID_ROLE) if kind == 'folder' else None
            page_title = {'canvas': '画布', 'mindmap': '脑图',
                          'document': '文档'}[section]
            menu.addAction('新建' + page_title,
                           lambda: self.create_requested.emit(
                               section, 'page', parent_id))
            menu.addAction('新建文件夹', lambda: self.create_requested.emit(
                section, 'folder', parent_id))
            menu.addSeparator()
            if kind == 'folder':
                self._add_import_action(menu, section, parent_id)
                self._add_export_action(menu, section, parent_id)
                menu.addAction('重命名 (F2)', lambda: self.edit(index))
                menu.addAction('移动到…', lambda: self.move_requested.emit(
                    [parent_id]))
                menu.addAction('删除', lambda: self.delete_requested.emit(
                    parent_id))
            else:
                # 分区根行 / 分区里的空白创建行：整块导入 / 导出。
                self._add_import_action(menu, section, None)
                self._add_export_action(menu, section, None)
        elif kind == 'page':
            node_id = index.data(NODE_ID_ROLE)
            menu.addAction('重命名 (F2)', lambda: self.edit(index))
            menu.addAction('移动到…', lambda: self.move_requested.emit(
                [node_id]))
            menu.addAction('删除', lambda: self.delete_requested.emit(node_id))
            menu.addSeparator()
            self._add_export_action(menu, section, node_id)
        else:
            # The final unowned viewport gap has no implicit section.
            for section, title in (('canvas', '画布'), ('mindmap', '脑图'),
                                   ('document', '文档')):
                menu.addAction('新建' + title, lambda _, s=section:
                               self.create_requested.emit(s, 'page', None))
                menu.addAction(title + '文件夹', lambda _, s=section:
                               self.create_requested.emit(s, 'folder', None))
            menu.addSeparator()
            menu.addAction('新建标签', self.new_tag_requested.emit)
            menu.addAction('回收站…', self.trash_requested.emit)
        return menu

    def _add_import_action(self, menu, section, node_id):
        """「导入…」——把外面的一棵目录树导进来，内容落在这一项下面。

        和「导出…」是一对：导出右键哪一项就带走哪一项以下的内容，导入
        则把外面那份放回这一项下面。`node_id` 为 None 表示整个分区。
        """
        menu.addAction(_('Import...'),
                       lambda: self.import_requested.emit(section, node_id))

    def _add_export_action(self, menu, section, node_id):
        """「导出…」——右键哪一项，就导出哪一项和它下面的全部内容。

        `node_id` 为 None 时导出的是整个分区（右键点在分区根行或它的
        空白创建行上）。
        """
        menu.addAction(_('Export...'),
                       lambda: self.export_requested.emit(section, node_id))

    def _show_context_menu(self, pos):
        menu = self.menu_for_index(self.indexAt(pos))
        menu.exec(self.viewport().mapToGlobal(pos))

    def keyPressEvent(self, event):
        index = self.currentIndex()
        kind = index.data(NODE_TYPE_ROLE) if index.isValid() else None
        is_node = kind in ('folder', 'page')
        if event.key() == Qt.Key.Key_F2 and is_node:
            self.edit(index)
            event.accept()
            return
        if event.key() == Qt.Key.Key_Delete and is_node:
            self.delete_requested.emit(index.data(NODE_ID_ROLE))
            event.accept()
            return
        if (event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter)
                and is_node):
            # §7.6：Enter 是「打开」，不是「重命名」（重命名归 F2）。
            self._open_current(index)
            event.accept()
            return
        if event.key() == Qt.Key.Key_Home and is_node:
            self._focus_section_edge(index, first=True)
            event.accept()
            return
        if event.key() == Qt.Key.Key_End and is_node:
            self._focus_section_edge(index, first=False)
            event.accept()
            return
        if (event.key() in (Qt.Key.Key_Menu, Qt.Key.Key_F10)
                and event.modifiers() & Qt.KeyboardModifier.ShiftModifier):
            self._show_menu_for_current()
            event.accept()
            return
        super().keyPressEvent(event)

    # ── 键盘导航（§7.6）───────────────────────────────────────────

    def _open_current(self, index):
        """Enter：页面就打开它；文件夹不自动展开（免得碰出重型编辑器）。"""
        if index.data(NODE_TYPE_ROLE) != 'page':
            return
        node = self.model().service._by_id(index.data(NODE_ID_ROLE))
        if node and node.get('pageId'):
            self.open_page_requested.emit(node['section'], node['pageId'])

    def _section_index_of(self, index):
        """这个 index 属于哪个分区（返回分区根行的 index）。"""
        model = self.model()
        kind = index.data(NODE_TYPE_ROLE) if index.isValid() else None
        if kind in ('section', 'blank'):
            section = index.data(SECTION_ROLE)
        else:
            node = model.service._by_id(index.data(NODE_ID_ROLE))
            section = (node or {}).get('section') or index.data(SECTION_ROLE)
        if section not in UI_SECTIONS:
            return QtCore.QModelIndex()
        return model.index(UI_SECTIONS.index(section), 0)

    def visible_rows_of_section(self, index):
        """同分区里可见的节点行（深度优先，展开着的才往下走）。"""
        model = self.model()
        section_index = self._section_index_of(index)
        if not section_index.isValid():
            return []
        rows = []

        def walk(parent):
            for row in range(model.rowCount(parent)):
                child = model.index(row, 0, parent)
                child_kind = child.data(NODE_TYPE_ROLE)
                if child_kind in ('folder', 'page'):
                    rows.append(child)
                    if child_kind == 'folder' and self.isExpanded(child):
                        walk(child)

        walk(section_index)
        return rows

    def _focus_section_edge(self, index, first):
        """Home / End：到同分区可见的第一项 / 最后一项。"""
        rows = self.visible_rows_of_section(index)
        if not rows:
            return
        target = rows[0] if first else rows[-1]
        self.setCurrentIndex(target)
        self.scrollTo(target)

    def _show_menu_for_current(self, index=None):
        """Shift+F10 / 菜单键：给当前行开右键菜单。"""
        if index is None:
            index = self.currentIndex()
        rect = self.visualRect(index)
        point = rect.center() if rect.isValid() \
            else self.viewport().rect().center()
        menu = self.menu_for_index(index)
        menu.exec(self.viewport().mapToGlobal(point))

    # ── 记住焦点 / 展开 / 滚动位置（§7.6）─────────────────────────

    #: 状态写到设置的哪一支，按工程 id 分开
    STATE_GROUP = 'Workspace/resource_tree'
    #: 展开状态最多记多少条（防止超大工程把设置文件撑爆）
    MAX_REMEMBERED_EXPANDED = 500
    #: 状态变化的防抖：拖滚动条的时候别每像素写一次设置
    STATE_SAVE_DELAY_MS = 500

    def _project_id(self):
        model = self.model()
        return getattr(getattr(model, 'service', None), 'project_id', '')

    def _state_key(self, name):
        return f'{self.STATE_GROUP}/{self._project_id()}/{name}'

    def _schedule_state_save(self, *_args):
        if self._state_timer.isActive():
            return
        self._state_timer.start()

    def save_state(self):
        """把 activeNodeId、expandedIds、滚动锚点记下来。"""
        from prism.config import PrismSettings

        model = self.model()
        if model is None or not self._project_id():
            return
        settings = PrismSettings()
        current = self.currentIndex()
        node_id = current.data(NODE_ID_ROLE) if current.isValid() else None
        settings.setValue(self._state_key('active'), node_id or '')
        expanded = self._expanded_node_ids()
        settings.setValue(self._state_key('expanded'),
                          ','.join(expanded[:self.MAX_REMEMBERED_EXPANDED]))
        anchor = self.indexAt(QtCore.QPoint(1, 1))
        anchor_id = anchor.data(NODE_ID_ROLE) if anchor.isValid() else ''
        offset = self.verticalScrollBar().value()
        settings.setValue(self._state_key('anchor'),
                          f'{anchor_id or ""}|{offset}')

    def _expanded_node_ids(self):
        """现在展开着的文件夹 id（含分区这一层的递归）。"""
        model = self.model()
        found = []

        def walk(parent):
            for row in range(model.rowCount(parent)):
                child = model.index(row, 0, parent)
                kind = child.data(NODE_TYPE_ROLE)
                if kind == 'folder':
                    if not self.isExpanded(child):
                        continue
                    node_id = child.data(NODE_ID_ROLE)
                    if node_id:
                        found.append(node_id)
                elif kind != 'section':
                    continue          # 空白行底下没有东西
                walk(child)

        walk(QtCore.QModelIndex())
        return found

    def restore_state(self):
        """把上次的焦点、展开、滚动位置放回去（无效 id 直接跳过）。"""
        from prism.config import PrismSettings

        model = self.model()
        if model is None or not self._project_id():
            return
        settings = PrismSettings()
        raw = settings.value(self._state_key('expanded'), '', type=str) or ''
        for node_id in [part for part in raw.split(',') if part]:
            index = model.index_for_id(node_id)
            if index.isValid():
                self.expand(index)
        anchor = settings.value(self._state_key('anchor'), '', type=str) or ''
        if '|' in anchor:
            anchor_id, _, offset = anchor.partition('|')
            index = model.index_for_id(anchor_id) if anchor_id else \
                QtCore.QModelIndex()
            if index.isValid():
                self.scrollTo(index, QtWidgets.QAbstractItemView.
                              ScrollHint.PositionAtTop)
            try:
                self.verticalScrollBar().setValue(int(offset or 0))
            except ValueError:
                pass
        active = settings.value(self._state_key('active'), '', type=str) or ''
        index = model.index_for_id(active) if active else \
            QtCore.QModelIndex()
        if index.isValid():
            self.setCurrentIndex(index)
            self.scrollTo(index)
        # 无效的 active 就保持默认（不猜用户想看哪个分区）。
