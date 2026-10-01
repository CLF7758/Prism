"""回收站：看看删了什么，恢复它，或者永久删掉（§7.6）。

规范里的两条：**永久删除仅在回收站执行并再次确认**；**仅有 UI 删除而数据
不可恢复视为不合格** —— 所以这个面板只做三件事：列出来、恢复、永久删除，
恢复走服务层的 `restore_deleted`（父文件夹还在回收站里时会拒绝，提示也照实说）。

面板本身不动页面表；永久删除之后要把对应的页面正文一起清掉，由外面的
`purged` 信号交给主窗口处理。
"""
import logging

from PyQt6 import QtCore, QtWidgets

from prism import ui_tokens

logger = logging.getLogger(__name__)

#: 分区名（和资源树里显示的一致）
SECTION_TITLES = {'canvas': '画布', 'mindmap': '脑图', 'document': '文档'}
NODE_TITLES = {'folder': '文件夹', 'page': '页面'}


class TrashPanel(QtWidgets.QDialog):
    """项目内回收站。"""

    #: 永久删除完成；参数是**页面 id** 列表（调用方要据此清掉正文）。
    purged = QtCore.pyqtSignal(list)

    def __init__(self, view, parent=None):
        super().__init__(parent)
        self.view = view
        self.service = view.category_panel.resource_service
        self.setWindowTitle('回收站')
        self.resize(520, 420)
        self._setup_ui()
        self._refresh()

    def _setup_ui(self):
        layout = QtWidgets.QVBoxLayout(self)
        layout.setContentsMargins(12, 12, 12, 12)
        layout.setSpacing(8)

        self.hint = QtWidgets.QLabel(
            '回收站里的东西还在工程里，恢复就能回来；永久删除之后就真的没了。')
        self.hint.setWordWrap(True)
        self.hint.setProperty('uiRole', 'secondary')
        layout.addWidget(self.hint)

        self.list_widget = QtWidgets.QListWidget()
        self.list_widget.setSelectionMode(
            QtWidgets.QAbstractItemView.SelectionMode.ExtendedSelection)
        self.list_widget.itemSelectionChanged.connect(self._update_buttons)
        self.list_widget.setMinimumHeight(
            ui_tokens.COMPONENTS['textarea-min-height'])
        layout.addWidget(self.list_widget, 1)

        row = QtWidgets.QHBoxLayout()
        self.restore_button = QtWidgets.QPushButton('恢复')
        self.restore_button.clicked.connect(self.restore_selected)
        self.purge_button = QtWidgets.QPushButton('永久删除…')
        self.purge_button.clicked.connect(self.purge_selected)
        row.addWidget(self.restore_button)
        row.addWidget(self.purge_button)
        row.addStretch()
        close = QtWidgets.QPushButton('关闭')
        close.clicked.connect(self.accept)
        row.addWidget(close)
        layout.addLayout(row)

        self._update_buttons()

    # ── 列表 ──────────────────────────────────────────────────────

    def _refresh(self):
        selected = {item.data(QtCore.Qt.ItemDataRole.UserRole)
                    for item in self.list_widget.selectedItems()}
        self.list_widget.blockSignals(True)
        self.list_widget.clear()
        for node in self.service.trashed_nodes():
            when = (node.get('deletedAt') or '')[:19].replace('T', ' ')
            label = (f'{node["title"]}  ·  '
                     f'{SECTION_TITLES.get(node["section"], node["section"])}'
                     f'/{NODE_TITLES.get(node["nodeType"], node["nodeType"])}'
                     f'  ·  {when}')
            item = QtWidgets.QListWidgetItem(label)
            item.setData(QtCore.Qt.ItemDataRole.UserRole, node['id'])
            item.setToolTip(f'{node["title"]}\n删除于 {when}')
            self.list_widget.addItem(item)
            if node['id'] in selected:
                item.setSelected(True)
        self.list_widget.blockSignals(False)
        self._update_buttons()

    def _selected_ids(self):
        return [item.data(QtCore.Qt.ItemDataRole.UserRole)
                for item in self.list_widget.selectedItems()]

    def _update_buttons(self):
        empty = not self.list_widget.count()
        has_selection = bool(self.list_widget.selectedItems())
        self.restore_button.setEnabled(has_selection)
        self.purge_button.setEnabled(has_selection)
        if empty:
            self.hint.setText('回收站是空的。')
        else:
            self.hint.setText(
                f'回收站里有 {self.list_widget.count()} 项。'
                '恢复就能回来；永久删除之后就真的没了。')

    # ── 动作 ──────────────────────────────────────────────────────

    def restore_selected(self):
        ids = self._selected_ids()
        if not ids:
            return []
        try:
            restored = self.service.restore_deleted(ids)
        except ValueError as exc:
            QtWidgets.QMessageBox.information(self, '恢复', str(exc))
            return []
        self.view.mark_content_dirty()
        self._refresh()
        return restored

    def _pages_under(self, ids):
        """这些节点（含后代）里所有页面的 pageId。"""
        page_ids = []
        for node_id in ids:
            node = self.service._by_id(node_id)
            if node is None:
                continue
            for candidate in [node] + [
                    self.service._by_id(i)
                    for i in self.service._descendants(node_id)]:
                if (candidate and candidate.get('nodeType') == 'page'
                        and candidate.get('pageId')
                        and candidate['pageId'] not in page_ids):
                    page_ids.append(candidate['pageId'])
        return page_ids

    def purge_selected(self):
        ids = self._selected_ids()
        if not ids:
            return []
        titles = [item.text().split('  ·  ')[0]
                  for item in self.list_widget.selectedItems()]
        # 页面 id 要在删之前记下来 —— 删完节点就查不到了。
        page_ids = self._pages_under(ids)
        reply = QtWidgets.QMessageBox.question(
            self, '永久删除',
            '永久删除这些项目？\n\n' + '\n'.join(f'· {t}' for t in titles) +
            '\n\n删掉之后就找不回来了。',
            QtWidgets.QMessageBox.StandardButton.Yes
            | QtWidgets.QMessageBox.StandardButton.Cancel,
            QtWidgets.QMessageBox.StandardButton.Cancel)
        if reply != QtWidgets.QMessageBox.StandardButton.Yes:
            return []
        try:
            removed = self.service.purge(ids)
        except ValueError as exc:
            QtWidgets.QMessageBox.information(self, '永久删除', str(exc))
            return []
        self.view.mark_content_dirty()
        self._refresh()
        self.purged.emit(page_ids)
        return removed
