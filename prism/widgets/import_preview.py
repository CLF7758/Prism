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

"""落地之前先看一眼：外面这堆东西会变成什么。

导出不需要这一步（产物是我们自己写的），导入需要：来源是用户的目录，
里面可能混着无关文件、可能重名、可能是空目录。先摆出「将要建什么」
和「哪些会被跳过」，比事后给一份错误报告强。
"""
import os

from PyQt6 import QtCore, QtWidgets

from prism.i18n import _

#: 跳过清单最多展示这么多条，其余的只报个数。
MAX_LISTED = 50


class ImportPreviewDialog(QtWidgets.QDialog):
    """把 :class:`prism.fileio.resource_import.ImportPlan` 摆开给人看。"""

    def __init__(self, plan, target_title, parent=None):
        super().__init__(parent)
        self.plan = plan
        self.setWindowTitle(
            _('Import into “{title}”').format(title=target_title))
        self.resize(560, 480)

        layout = QtWidgets.QVBoxLayout(self)
        layout.setContentsMargins(14, 12, 14, 12)
        layout.setSpacing(8)

        summary = QtWidgets.QLabel(plan.summary())
        summary.setWordWrap(True)
        layout.addWidget(summary)

        self.tree = QtWidgets.QTreeWidget()
        self.tree.setHeaderLabels([_('Name'), _('Type')])
        self.tree.setRootIsDecorated(True)
        self.tree.setUniformRowHeights(True)
        self.tree.setSelectionMode(
            QtWidgets.QAbstractItemView.SelectionMode.NoSelection)
        self._fill_tree()
        layout.addWidget(self.tree, stretch=1)

        if plan.skipped:
            layout.addWidget(self._skipped_box())
        for note in plan.notes:
            note_label = QtWidgets.QLabel(note)
            note_label.setWordWrap(True)
            layout.addWidget(note_label)

        buttons = QtWidgets.QDialogButtonBox(
            QtWidgets.QDialogButtonBox.StandardButton.Ok
            | QtWidgets.QDialogButtonBox.StandardButton.Cancel)
        buttons.button(QtWidgets.QDialogButtonBox.StandardButton.Ok).setText(
            _('Import'))
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    # ── 内容 ─────────────────────────────────────────────────

    def _fill_tree(self):
        items = {}
        for node in self.plan.nodes:
            parent_item = (items.get(node.parent_path)
                           if node.parent_path is not None else None)
            item = QtWidgets.QTreeWidgetItem([node.title, self._type_label(node)])
            if parent_item is None:
                self.tree.addTopLevelItem(item)
            else:
                parent_item.addChild(item)
            items[node.relative_path] = item
        self.tree.expandAll()
        for column in (0, 1):
            self.tree.resizeColumnToContents(column)

    def _type_label(self, node):
        if node.node_type == 'folder':
            return _('Folder')
        if node.page_kind == 'canvas':
            return _('Canvas ({count} asset(s))').format(
                count=len(node.assets))
        if node.page_kind == 'mindmap':
            return _('Mind map')
        return _('Document')

    def _skipped_box(self):
        """跳过的文件：给得出名字的就列出来。"""
        group = QtWidgets.QGroupBox(
            _('Skipped ({count})').format(count=len(self.plan.skipped)))
        box = QtWidgets.QVBoxLayout(group)
        listbox = QtWidgets.QListWidget()
        listbox.setSelectionMode(
            QtWidgets.QAbstractItemView.SelectionMode.NoSelection)
        listbox.setMaximumHeight(120)
        for path, reason in self.plan.skipped[:MAX_LISTED]:
            listbox.addItem(f'{path} — {reason}')
        if len(self.plan.skipped) > MAX_LISTED:
            listbox.addItem(_('… and {count} more').format(
                count=len(self.plan.skipped) - MAX_LISTED))
        box.addWidget(listbox)
        return group
