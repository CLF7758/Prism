"""回收站：看看删了什么，恢复它，或者永久删掉（§7.6）。

规范里的两条：**永久删除仅在回收站执行并再次确认**；**仅有 UI 删除而数据
不可恢复视为不合格** —— 所以这个面板只做三件事：列出来、恢复、永久删除。

面板分两个页签：

- **素材**：画布上按 Delete（或查重里删）丢进来的图片 / 视频 / 模型。它们是
  **软删除**：数据一个字节都没动，只是被隐藏 + 打了 `deletedAt` 时间戳。恢复
  走可撤销的 `RestoreItems` 命令；永久删除才真正把图元移出场景（保存时数据库
  记录和 sqlar 原始字节随之删除），并清空撤销栈（不可逆）。
- **页面/文件夹**：资源树里删掉的画布 / 文档 / 脑图节点。恢复走服务层的
  `restore_deleted`（父节点还在回收站里时会拒绝，提示照实说）；永久删除之后
  要把对应的页面正文一起清掉，由外面的 `purged` 信号交给主窗口处理。
"""
import logging

from PyQt6 import QtCore, QtGui, QtWidgets

from prism import commands, ui_tokens

logger = logging.getLogger(__name__)

#: 分区名（和资源树里显示的一致）
SECTION_TITLES = {'canvas': '画布', 'mindmap': '脑图', 'document': '文档'}
NODE_TITLES = {'folder': '文件夹', 'page': '页面'}


def _thumb_icon(item, side=56):
    """给回收站列表里的一行做个小缩略图。取不到就返回 None（不画图标）。

    缩略图只是锦上添花，绝不能因为它把回收站面板搞崩 —— 所以整段包在
    try 里，任何解码 / 绘制异常都退化成"没有图标"。
    """
    try:
        image = None
        if hasattr(item, 'analysis_image'):
            image = item.analysis_image(side)
        if image is None and hasattr(item, 'pixmap'):
            pix = item.pixmap()
            if pix is not None and not pix.isNull():
                image = pix.toImage()
        if image is None or image.isNull():
            return None
        scaled = image.scaled(
            side, side,
            QtCore.Qt.AspectRatioMode.KeepAspectRatio,
            QtCore.Qt.TransformationMode.SmoothTransformation)
        return QtGui.QIcon(QtGui.QPixmap.fromImage(scaled))
    except Exception:  # pragma: no cover - 缩略图失败不影响功能
        logger.debug('回收站缩略图生成失败', exc_info=True)
        return None


class TrashPanel(QtWidgets.QDialog):
    """项目内回收站。"""

    #: 页面/文件夹永久删除完成；参数是**页面 id** 列表（调用方据此清正文）。
    purged = QtCore.pyqtSignal(list)
    #: 素材永久删除完成；参数是被移出场景的**图元对象**列表（调用方据此
    #: 清 sqlar 原始字节并清空撤销栈）。
    assets_purged = QtCore.pyqtSignal(list)

    def __init__(self, view, parent=None):
        super().__init__(parent)
        self.view = view
        self.service = view.category_panel.resource_service
        self.catalog = getattr(view.category_panel, 'catalog', None)
        if self.catalog is None:
            from prism.project_catalog import ProjectAssetCatalog
            self.catalog = ProjectAssetCatalog(view.scene, self.service)
        self.setWindowTitle('回收站')
        self.resize(600, 460)
        self._setup_ui()
        self._refresh_assets()
        self._refresh_nodes()

    # ── 界面 ──────────────────────────────────────────────────

    def _setup_ui(self):
        layout = QtWidgets.QVBoxLayout(self)
        layout.setContentsMargins(12, 12, 12, 12)
        layout.setSpacing(8)

        self.tabs = QtWidgets.QTabWidget()
        self.tabs.addTab(self._build_asset_tab(), '素材')
        self.tabs.addTab(self._build_node_tab(), '页面 / 文件夹')
        layout.addWidget(self.tabs, 1)

        row = QtWidgets.QHBoxLayout()
        self.hint = QtWidgets.QLabel('')
        self.hint.setWordWrap(True)
        self.hint.setProperty('uiRole', 'secondary')
        row.addWidget(self.hint, 1)
        close = QtWidgets.QPushButton('关闭')
        close.clicked.connect(self.accept)
        row.addWidget(close)
        layout.addLayout(row)

        self.tabs.currentChanged.connect(self._update_hint)
        self._update_hint()

    def _build_asset_tab(self):
        page = QtWidgets.QWidget()
        vbox = QtWidgets.QVBoxLayout(page)
        vbox.setContentsMargins(0, 8, 0, 0)
        vbox.setSpacing(8)

        self.asset_list = QtWidgets.QListWidget()
        self.asset_list.setSelectionMode(
            QtWidgets.QAbstractItemView.SelectionMode.ExtendedSelection)
        self.asset_list.setIconSize(QtCore.QSize(56, 56))
        self.asset_list.itemSelectionChanged.connect(self._update_asset_buttons)
        self.asset_list.setMinimumHeight(
            ui_tokens.COMPONENTS['textarea-min-height'])
        vbox.addWidget(self.asset_list, 1)

        row = QtWidgets.QHBoxLayout()
        self.asset_restore_button = QtWidgets.QPushButton('恢复')
        self.asset_restore_button.clicked.connect(self.restore_selected_assets)
        self.asset_purge_button = QtWidgets.QPushButton('永久删除…')
        self.asset_purge_button.clicked.connect(self.purge_selected_assets)
        row.addWidget(self.asset_restore_button)
        row.addWidget(self.asset_purge_button)
        row.addStretch()
        vbox.addLayout(row)

        self._update_asset_buttons()
        return page

    def _build_node_tab(self):
        page = QtWidgets.QWidget()
        vbox = QtWidgets.QVBoxLayout(page)
        vbox.setContentsMargins(0, 8, 0, 0)
        vbox.setSpacing(8)

        self.list_widget = QtWidgets.QListWidget()
        self.list_widget.setSelectionMode(
            QtWidgets.QAbstractItemView.SelectionMode.ExtendedSelection)
        self.list_widget.itemSelectionChanged.connect(self._update_node_buttons)
        self.list_widget.setMinimumHeight(
            ui_tokens.COMPONENTS['textarea-min-height'])
        vbox.addWidget(self.list_widget, 1)

        row = QtWidgets.QHBoxLayout()
        self.restore_button = QtWidgets.QPushButton('恢复')
        self.restore_button.clicked.connect(self.restore_selected)
        self.purge_button = QtWidgets.QPushButton('永久删除…')
        self.purge_button.clicked.connect(self.purge_selected)
        row.addWidget(self.restore_button)
        row.addWidget(self.purge_button)
        row.addStretch()
        vbox.addLayout(row)

        self._update_node_buttons()
        return page

    def _update_hint(self):
        if self.tabs.currentIndex() == 0:
            count = self.asset_list.count()
            self.hint.setText(
                '回收站是空的。' if not count else
                f'回收站里有 {count} 个素材。恢复就能回到画布；'
                '永久删除之后就真的没了。')
        else:
            count = self.list_widget.count()
            self.hint.setText(
                '回收站是空的。' if not count else
                f'回收站里有 {count} 项。恢复就能回来；'
                '永久删除之后就真的没了。')

    # ── 素材页签 ──────────────────────────────────────────────

    def _asset_rows(self):
        """把软删除的素材整理成 (item, 显示文本, tooltip) 列表。"""
        rows = []
        for item in self.catalog.deleted_items():
            cid = self.catalog._effective_canvas_id(item)
            where = self.catalog.canvas_path(cid)
            name = (getattr(item, 'filename', '') or
                    getattr(item, '_title', '') or
                    f'素材 #{getattr(item, "save_id", "?")}')
            name = str(name).split('/')[-1].split('\\')[-1]
            when = (getattr(item, '_deleted_at', '') or '')[:19].replace('T', ' ')
            label = f'{name}  ·  {where}' + (f'  ·  {when}' if when else '')
            tooltip = f'{name}\n所在画布：{where}' + (
                f'\n删除于 {when}' if when else '')
            rows.append((item, label, tooltip))
        return rows

    def _refresh_assets(self):
        selected = {id(item) for item in self._selected_assets()}
        self.asset_list.blockSignals(True)
        self.asset_list.clear()
        for item, label, tooltip in self._asset_rows():
            row = QtWidgets.QListWidgetItem(label)
            icon = _thumb_icon(item)
            if icon is not None:
                row.setIcon(icon)
            row.setData(QtCore.Qt.ItemDataRole.UserRole, id(item))
            row.setToolTip(tooltip)
            self.asset_list.addItem(row)
            row.setData(QtCore.Qt.ItemDataRole.UserRole + 1, item)
            if id(item) in selected:
                row.setSelected(True)
        self.asset_list.blockSignals(False)
        self._update_asset_buttons()

    def _selected_assets(self):
        items = []
        for index in range(self.asset_list.count()):
            row = self.asset_list.item(index)
            if row.isSelected():
                item = row.data(QtCore.Qt.ItemDataRole.UserRole + 1)
                if item is not None:
                    items.append(item)
        return items

    def _update_asset_buttons(self):
        has_selection = bool(self.asset_list.selectedItems())
        self.asset_restore_button.setEnabled(has_selection)
        self.asset_purge_button.setEnabled(has_selection)

    def restore_selected_assets(self):
        """恢复选中的素材：走可撤销的 `RestoreItems` 命令。"""
        items = self._selected_assets()
        if not items:
            return []
        self.view.undo_stack.push(commands.RestoreItems(self.view.scene, items))
        self.view.mark_content_dirty()
        self._refresh_assets()
        self._update_hint()
        return items

    def purge_selected_assets(self):
        """永久删除选中的素材：再次确认后交给主窗口真正移除。"""
        items = self._selected_assets()
        if not items:
            return []
        names = []
        for item in items:
            name = (getattr(item, 'filename', '') or
                    getattr(item, '_title', '') or
                    f'素材 #{getattr(item, "save_id", "?")}')
            names.append(str(name).split('/')[-1].split('\\')[-1])
        reply = QtWidgets.QMessageBox.question(
            self, '永久删除',
            f'永久删除这 {len(items)} 个素材？\n\n'
            + '\n'.join(f'· {n}' for n in names[:20])
            + ('\n…' if len(names) > 20 else '')
            + '\n\n删掉之后原始文件也一起没了，找不回来。',
            QtWidgets.QMessageBox.StandardButton.Yes
            | QtWidgets.QMessageBox.StandardButton.Cancel,
            QtWidgets.QMessageBox.StandardButton.Cancel)
        if reply != QtWidgets.QMessageBox.StandardButton.Yes:
            return []
        self.assets_purged.emit(items)
        self._refresh_assets()
        self._update_hint()
        return items

    # ── 页面 / 文件夹页签 ─────────────────────────────────────

    def _refresh_nodes(self):
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
        self._update_node_buttons()

    def _selected_ids(self):
        return [item.data(QtCore.Qt.ItemDataRole.UserRole)
                for item in self.list_widget.selectedItems()]

    def _update_node_buttons(self):
        has_selection = bool(self.list_widget.selectedItems())
        self.restore_button.setEnabled(has_selection)
        self.purge_button.setEnabled(has_selection)

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
        self._refresh_nodes()
        self._update_hint()
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
        self._refresh_nodes()
        self._update_hint()
        self.purged.emit(page_ids)
        return removed
