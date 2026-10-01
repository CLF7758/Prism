"""「移动到…」对话框（§7.5）。

规范那一条：**移动对话框显示类型、搜索、目标树、完整目标路径、取消/移动按钮；
提交前再次校验。**

这个对话框只负责**选目标**：它把可以放的地方列出来（文件夹和分区根），
把不能放的地方挡掉（自己、自己的后代、别的分区），返回 `(section, parent_id)`。
真正移动由调用方交给 `ResourceTreeService.moveBatch` —— 校验在服务层那一条路上，
所以不会出现"界面上看着能移、点下去失败"。
"""
import logging

from PyQt6 import QtCore, QtWidgets
from PyQt6.QtCore import Qt

from prism import ui_tokens
from prism.workspace_service import TREE_SECTIONS
from prism.widgets.resource_tree_model import SECTION_TITLES

logger = logging.getLogger(__name__)


class MoveToDialog(QtWidgets.QDialog):
    """给一批节点选一个目标文件夹。"""

    def __init__(self, view, node_ids, parent=None):
        super().__init__(parent)
        self.view = view
        self.service = view.category_panel.resource_service
        self.node_ids = [node_id for node_id in node_ids if node_id]
        self.sections = {node['section'] for node_id in self.node_ids
                         if (node := self.service._by_id(node_id)) is not None}
        self.setWindowTitle('移动到…')
        self.resize(480, 520)
        self._setup_ui()
        self._populate()
        self._update_preview()

    def _setup_ui(self):
        layout = QtWidgets.QVBoxLayout(self)
        layout.setContentsMargins(12, 12, 12, 12)
        layout.setSpacing(8)

        self.summary = QtWidgets.QLabel(self._summary_text())
        self.summary.setWordWrap(True)
        layout.addWidget(self.summary)

        self.search = QtWidgets.QLineEdit()
        self.search.setPlaceholderText('搜索文件夹')
        self.search.setClearButtonEnabled(True)
        self.search.textChanged.connect(self._populate)
        self.search.setMinimumHeight(ui_tokens.COMPONENTS['input-height'])
        layout.addWidget(self.search)

        self.tree = QtWidgets.QTreeWidget()
        self.tree.setHeaderHidden(True)
        self.tree.setSelectionMode(
            QtWidgets.QAbstractItemView.SelectionMode.SingleSelection)
        self.tree.currentItemChanged.connect(
            lambda *_: self._update_preview())
        self.tree.itemDoubleClicked.connect(lambda *_: self.accept())
        layout.addWidget(self.tree, 1)

        self.path_label = QtWidgets.QLabel('')
        self.path_label.setWordWrap(True)
        self.path_label.setProperty('uiRole', 'secondary')
        layout.addWidget(self.path_label)

        buttons = QtWidgets.QDialogButtonBox()
        self.move_button = buttons.addButton('移动',
                                             QtWidgets.QDialogButtonBox.
                                             ButtonRole.AcceptRole)
        buttons.addButton('取消', QtWidgets.QDialogButtonBox.
                          ButtonRole.RejectRole)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    # ── 内容 ──────────────────────────────────────────────────────

    def _summary_text(self):
        count = len(self.node_ids)
        kinds = [self.service._by_id(node_id) for node_id in self.node_ids]
        folders = sum(1 for n in kinds if n and n['nodeType'] == 'folder')
        pages = sum(1 for n in kinds if n and n['nodeType'] == 'page')
        parts = []
        if folders:
            parts.append(f'{folders} 个文件夹')
        if pages:
            parts.append(f'{pages} 个页面')
        return f'把选中的 {count} 项（{"、".join(parts) or "—"}）移到：'

    def _forbidden(self):
        """不能放的地方：自己，以及自己的后代。"""
        blocked = set()
        for node_id in self.node_ids:
            blocked.add(node_id)
            blocked.update(self.service._descendants(node_id))
        return blocked

    def _populate(self, *_args):
        query = self.search.text().strip().casefold()
        blocked = self._forbidden()
        self.tree.clear()
        for section in TREE_SECTIONS:
            if section not in self.sections:
                continue
            item = QtWidgets.QTreeWidgetItem([SECTION_TITLES[section]])
            item.setData(0, Qt.ItemDataRole.UserRole, (section, None))
            item.setToolTip(0, SECTION_TITLES[section])
            self._add_children(item, section, None, blocked, query)
            if query and item.childCount() == 0:
                continue          # 搜索时把空的分区收起来
            self.tree.addTopLevelItem(item)
            item.setExpanded(bool(query) or True)
        self._select_first()

    def _add_children(self, parent_item, section, parent_id, blocked, query):
        for node in self.service.listChildren(section, parent_id):
            if node['nodeType'] != 'folder' or node['id'] in blocked:
                continue
            item = QtWidgets.QTreeWidgetItem([node['title']])
            item.setData(0, Qt.ItemDataRole.UserRole, (section, node['id']))
            item.setToolTip(0, self._path_of(section, node['id']))
            self._add_children(item, section, node['id'], blocked, query)
            matched = (not query or query in node['title'].casefold()
                       or item.childCount())
            if matched:
                parent_item.addChild(item)
                item.setExpanded(True)

    def _path_of(self, section, parent_id):
        """完整目标路径：文档 / 第一章 / 外景。"""
        parts = [SECTION_TITLES.get(section, section)]
        chain = []
        current = parent_id
        while current:
            node = self.service._by_id(current)
            if node is None:
                break
            chain.append(node['title'])
            current = node.get('parentId')
        return ' / '.join(parts + chain[::-1])

    def _select_first(self):
        for row in range(self.tree.topLevelItemCount()):
            item = self.tree.topLevelItem(row)
            if item.childCount():
                self.tree.setCurrentItem(item.child(0))
                return
            self.tree.setCurrentItem(item)
            return

    def _update_preview(self):
        item = self.tree.currentItem()
        if item is None:
            self.path_label.setText('还没选目标')
            if self.move_button is not None:
                self.move_button.setEnabled(False)
            return
        section, parent_id = item.data(0, Qt.ItemDataRole.UserRole)
        self.path_label.setText(f'完整路径：{self._path_of(section, parent_id)}')
        if self.move_button is not None:
            self.move_button.setEnabled(True)

    # ── 结果 ──────────────────────────────────────────────────────

    def target(self):
        """选中的目标；(section, parent_id)，没选返回 None。"""
        item = self.tree.currentItem()
        if item is None:
            return None
        return item.data(0, Qt.ItemDataRole.UserRole)
