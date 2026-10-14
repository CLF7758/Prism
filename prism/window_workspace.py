"""Main-window workspace and resource workflows; construction stays in __main__."""
import copy
import logging
from datetime import datetime, timezone
from prism.performance_trace import measured, record as trace_record

from PyQt6 import QtCore, QtGui, QtWidgets
from PyQt6.QtCore import Qt
from prism.i18n import _
from prism.widgets.components.icons import icon

logger = logging.getLogger(__name__)

class WindowWorkspaceMixin:
    def _build_workspace_rail(self):
        rail = QtWidgets.QFrame()
        rail.setObjectName('workspaceRail')
        rail.setFixedWidth(104)
        rail.setStyleSheet(
            '#workspaceRail { background: #202024; border-right: 1px solid #34343a; }'
            '#workspaceRail QToolButton { min-height: 38px; padding: 4px 7px; '
            'text-align: left; border: 0; border-radius: 6px; }'
            '#workspaceRail QToolButton:checked { background: #3d5f8a; color: white; }'
            '#workspaceRail QToolButton:hover:!checked { background: #303036; }')
        self._rail_layout = QtWidgets.QVBoxLayout(rail)
        self._rail_layout.setContentsMargins(6, 8, 6, 8)
        self._rail_layout.setSpacing(4)
        self._workspace_buttons = QtWidgets.QButtonGroup(self)
        self._workspace_buttons.setExclusive(True)
        self._dynamic_buttons = []
        self._dynamic_panels = []

        self._add_workspace_button(_('Canvas'), 0, checked=True)
        self._add_workspace_button(_('Document'), 1)
        self._add_workspace_button(_('Mind Map'), 2)
        self._dynamic_start = self._rail_layout.count()
        self._rail_layout.addStretch(1)
        add_button = QtWidgets.QToolButton()
        add_button.setText(_('Add'))
        add_button.setIcon(icon('plus'))
        add_button.setIconSize(QtCore.QSize(16, 16))
        add_button.setToolButtonStyle(
            Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        add_button.setToolTip(_('Add a document or mind-map page'))
        add_button.clicked.connect(self._show_add_workspace_menu)
        self._rail_layout.addWidget(add_button)
        return rail

    def _add_workspace_button(self, title, tab_index, checked=False,
                              page_id=None):
        button = QtWidgets.QToolButton()
        button.setText(title)
        button.setCheckable(True)
        button.setChecked(checked)
        button.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextOnly)
        button.clicked.connect(
            lambda _checked=False, index=tab_index: self.tabs.setCurrentIndex(index))
        if page_id:
            button.setProperty('page_id', page_id)
            button.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
            button.customContextMenuRequested.connect(
                lambda _pos, pid=page_id: self._show_workspace_page_menu(pid))
        self._workspace_buttons.addButton(button)
        # Dynamic buttons belong above the stretch and + button.
        if hasattr(self, '_dynamic_start'):
            self._rail_layout.insertWidget(self._rail_layout.count() - 2, button)
            self._dynamic_buttons.append(button)
        else:
            self._rail_layout.addWidget(button)
        return button

    def _show_add_workspace_menu(self):
        menu = QtWidgets.QMenu(self)
        menu.addAction(_('New Document Page'),
                       lambda: self._create_workspace_page('document'))
        menu.addAction(_('New Mind Map Page'),
                       lambda: self._create_workspace_page('mindmap'))
        menu.exec(QtGui.QCursor.pos())

    def _create_workspace_page(self, kind, parent=None):
        defaults = {'canvas': _('Canvas'), 'document': _('Document'),
                    'mindmap': _('Mind Map')}
        default = defaults.get(kind, _('Page'))
        title, ok = QtWidgets.QInputDialog.getText(
            self, _('New Page'), _('Page name:'), text=default)
        if not ok or not title.strip():
            return
        page = self.workspace_service.add(kind, title.strip(), parent)
        self.open_workspace_page(kind, page['id'])
        self.view.mark_content_dirty()
        return self._page_panels.get(page['id'], self.view)

    def _create_resource_item(self, section, node_type, parent_id):
        """就地新建：先放一条临时行，名字确认了才真写进工程（C5 / §7.4）。

        以前这里是 `QInputDialog`：弹窗、输名字、确定。现在改成临时节点 +
        就地改名 —— Esc 时那一行直接消失，工程里不留任何痕迹。
        """
        defaults = {'canvas': '未命名画布', 'mindmap': '未命名脑图',
                    'document': '未命名文档'}
        base = '未命名文件夹' if node_type == 'folder' else defaults[section]
        panel = self.view.category_panel
        title = panel.resource_service.unique_suggestion(
            section, parent_id, node_type, base)
        panel.resource_tree.begin_create(section, node_type, parent_id, title)

    def _on_resource_created(self, node):
        """就地新建确认之后：补上旧的页面表、打开它、把工程标脏。"""
        if node.get('nodeType') != 'page':
            self.view.mark_content_dirty()
            return
        self.workspace_service.add(node['section'], node['title'],
                                   page_id=node['pageId'])
        self.open_workspace_page(node['section'], node['pageId'])
        self.view.mark_content_dirty()

    def _on_resource_tree_change(self, change):
        # An inline page rename must also update the legacy page payload;
        # the node ID itself stays stable and never depends on the title.
        if not (isinstance(change.get('old'), str) and
                isinstance(change.get('new'), str)):
            return
        for node_id in change['ids']:
            node = self.view.category_panel.resource_service._by_id(node_id)
            if node and node['nodeType'] == 'page':
                self.workspace_service.rename(node['pageId'], node['title'])

    def _delete_resource_item(self, node_id):
        """右键「删除」/ Delete：移到回收站，并给 8 秒反悔时间（§7.6）。

        - **空文件夹、单个页面**：不打断，直接删，然后弹一条带「撤销」的提示；
        - **非空文件夹**：先确认，确认框写清「含 X 个文件夹、Y 个页面」。

        回收站是软删除：节点、页面正文、附件都还在工程里，恢复就能回来 ——
        规范那句「仅有 UI 删除而数据不可恢复视为不合格」说的就是这个。
        """
        service = self.view.category_panel.resource_service
        node = service._by_id(node_id)
        if node is None or not service.can_trash(node_id):
            return
        descendants = service._descendants(node_id)
        pages = sum(service._by_id(i)['nodeType'] == 'page'
                    for i in descendants)
        folders = sum(service._by_id(i)['nodeType'] == 'folder'
                      for i in descendants)
        if descendants:
            reply = QtWidgets.QMessageBox.question(
                self, '移到回收站',
                f'把“{node["title"]}”移到回收站？其中包含 {folders} 个文件夹、'
                f'{pages} 个页面。内容会保留在工程中，可以再恢复。',
                QtWidgets.QMessageBox.StandardButton.Yes
                | QtWidgets.QMessageBox.StandardButton.Cancel,
                QtWidgets.QMessageBox.StandardButton.Yes)
            if reply != QtWidgets.QMessageBox.StandardButton.Yes:
                return
        pages_removed = self._pages_under_node(node_id)
        # 删掉当前页之前，先把面板里的编辑写回 scene —— 回收站里要留着它。
        if self.current_page_id in pages_removed:
            self._store_current_page()
        # 后继必须在删之前算：删完它就不在兄弟列表里了。
        successor = (self._fallback_page_after_delete(node_id)
                     if self.current_page_id in pages_removed else None)
        service.trash([node_id], datetime.now(timezone.utc).isoformat())
        self.view.mark_content_dirty()
        self._open_successor_after_trash(successor, pages_removed)
        text = f'已把“{node["title"]}”移到回收站'
        if pages or folders:
            text += f'（含 {folders} 个文件夹、{pages} 个页面）'
        self._show_trash_toast(text, [node_id])

    def _store_current_page(self):
        """把当前页签里的编辑写回 scene（切页/删除前都该做这一步）。"""
        current = self.tabs.currentWidget()
        if hasattr(current, 'store_to_scene'):
            current.store_to_scene()

    def _pages_under_node(self, node_id):
        """这个节点（含后代）里所有页面的 id。"""
        service = self.view.category_panel.resource_service
        found = []
        node = service._by_id(node_id)
        if node is None:
            return found
        for candidate in [node] + [service._by_id(i)
                                   for i in service._descendants(node_id)]:
            if (candidate and candidate.get('nodeType') == 'page'
                    and candidate.get('pageId')
                    and candidate['pageId'] not in found):
                found.append(candidate['pageId'])
        return found

    def _first_page_of(self, node):
        """这个节点里第一个页面：自己就是页面就用自己，否则找后代。"""
        service = self.view.category_panel.resource_service
        if node.get('nodeType') == 'page' and node.get('pageId'):
            return node['pageId']
        for child in service.listChildren(node['section'], node['id']):
            found = self._first_page_of(child)
            if found:
                return found
        return None

    def _fallback_page_after_delete(self, node_id):
        """§7.6：删掉当前页面之后打开哪一个 —— **后继 → 前驱 → 父级**。"""
        service = self.view.category_panel.resource_service
        node = service._by_id(node_id)
        if node is None:
            return None
        siblings = service.listChildren(node['section'], node.get('parentId'))
        ids = [s['id'] for s in siblings]
        if node_id not in ids:
            return None
        at = ids.index(node_id)
        for candidate in list(siblings[at + 1:]) + list(siblings[:at][::-1]):
            page_id = self._first_page_of(candidate)
            if page_id:
                return candidate['section'], page_id
        parent_id = node.get('parentId')
        if parent_id:
            for candidate in service.listChildren(node['section'], parent_id):
                page_id = self._first_page_of(candidate)
                if page_id:
                    return candidate['section'], page_id
        return None

    def _open_successor_after_trash(self, successor, pages_removed):
        """被删的里面正好有当前打开的页面时，按优先级换一个打开。"""
        if self.current_page_id not in pages_removed:
            return
        # 先把被删页面的面板摘下来：它还在页签上的话，切页时会把
        # 「已经删掉的那一页」的内容再写回 scene。
        for page_id in pages_removed:
            panel = self._page_panels.pop(page_id, None)
            if panel is None:
                continue
            index = self.tabs.indexOf(panel)
            if index >= 0:
                self.tabs.removeTab(index)
            panel.deleteLater()
        if successor is not None:
            self.open_workspace_page(*successor)
            return
        # 一个页面都不剩了：回画布，别停在一个已经不存在的页上。
        self.open_workspace_page('canvas', self._first_canvas_id())

    def _show_trash_toast(self, text, undo_ids):
        """「已移到回收站」+「撤销」，8 秒内点了就能回来（§7.6 / §505）。"""
        from prism.widgets.toast import UndoToast

        if self._trash_toast is not None and self._trash_toast.is_alive():
            self._trash_toast.dismiss()
        ids = list(undo_ids)
        self._trash_toast = UndoToast(
            self, text, lambda: self._undo_trash(ids))
        self._trash_toast.show_at_bottom()

    def _undo_trash(self, node_ids):
        """撤销刚才那次删除：从回收站恢复回来（单个或一批）。"""
        if isinstance(node_ids, str):
            node_ids = [node_ids]
        service = self.view.category_panel.resource_service
        try:
            service.restore_deleted(list(node_ids))
        except ValueError as exc:
            QtWidgets.QMessageBox.information(self, '恢复', str(exc))
            return
        self.view.mark_content_dirty()

    def _top_ancestors(self, node_ids):
        """只留最上层祖先：父子都选中时别重复处理（§7.5）。"""
        service = self.view.category_panel.resource_service
        selected = set(node_ids)
        tops = []
        for node_id in node_ids:
            node = service._by_id(node_id)
            if node is None:
                continue
            ancestor = node.get('parentId')
            while ancestor is not None and ancestor not in selected:
                parent = service._by_id(ancestor)
                ancestor = parent.get('parentId') if parent else None
            if ancestor is None:
                tops.append(node_id)
        return tops

    def _count_under(self, node_ids):
        """这批节点**里面**有多少文件夹和页面（不含它们自己，与单个删除一致）。"""
        service = self.view.category_panel.resource_service
        folders = pages = 0
        for node_id in node_ids:
            for child_id in service._descendants(node_id):
                child = service._by_id(child_id)
                if child and child['nodeType'] == 'page':
                    pages += 1
                else:
                    folders += 1
        return folders, pages

    def _delete_resource_items(self, node_ids):
        """批量「删除」：一次确认、一次 trash、一条能撤销的提示。"""
        service = self.view.category_panel.resource_service
        roots = self._top_ancestors([i for i in node_ids if i and service.can_trash(i)])
        if not roots:
            return
        folders, pages = self._count_under(roots)
        has_children = any(service._descendants(node_id)
                           for node_id in roots)
        if has_children:
            reply = QtWidgets.QMessageBox.question(
                self, '移到回收站',
                f'把选中的 {len(roots)} 项移到回收站？其中包含 '
                f'{folders} 个文件夹、{pages} 个页面。'
                '内容会保留在工程中，可以再恢复。',
                QtWidgets.QMessageBox.StandardButton.Yes
                | QtWidgets.QMessageBox.StandardButton.Cancel,
                QtWidgets.QMessageBox.StandardButton.Yes)
            if reply != QtWidgets.QMessageBox.StandardButton.Yes:
                return
        current_removed = self.current_page_id in [
            page_id for node_id in roots
            for page_id in self._pages_under_node(node_id)]
        successor = None
        if current_removed:
            # 删的是当前页：先把面板里的编辑存回，再找一个接班页。
            self._store_current_page()
            for node_id in roots:
                successor = self._fallback_page_after_delete(node_id)
                if successor:
                    break
        stamped = datetime.now(timezone.utc).isoformat()
        service.trash(roots, stamped)
        self.view.mark_content_dirty()
        if current_removed:
            self._open_successor_after_trash(
                successor,
                [page_id for node_id in roots
                 for page_id in self._pages_under_node(node_id)])
        text = (f'已把选中的 {len(roots)} 项移到回收站'
                f'（含 {folders} 个文件夹、{pages} 个页面）')
        self._show_trash_toast(text, roots)

    def _move_resource_items(self, node_ids):
        """批量「移动到…」：先选目标，再交给服务层 —— 它会再校验一次。"""
        from prism.widgets.move_to_dialog import MoveToDialog

        node_ids = [i for i in node_ids if i]
        if not node_ids:
            return
        dialog = MoveToDialog(self.view, node_ids, self)
        if dialog.exec() != QtWidgets.QDialog.DialogCode.Accepted:
            return
        target = dialog.target()
        if target is None:
            return
        section, parent_id = target
        service = self.view.category_panel.resource_service
        try:
            moved = service.moveBatch(node_ids, parent_id, section)
        except ValueError as exc:
            QtWidgets.QMessageBox.warning(self, '移动到', str(exc))
            return
        if not moved:
            return
        self.view.category_panel.rebuild_workspace_tree()
        tree = self.view.category_panel.resource_tree
        if parent_id:
            tree.expand(tree.model().index_for_id(parent_id))
        index = tree.model().index_for_id(moved[0])
        tree.setCurrentIndex(index)
        tree.scrollTo(index)
        self.view.mark_content_dirty()

    def _open_trash_panel(self):
        """打开回收站：恢复或者永久删除（§7.6）。"""
        from prism.widgets.trash_panel import TrashPanel

        panel = TrashPanel(self.view, self)
        panel.purged.connect(self._on_trash_purged)
        panel.assets_purged.connect(self._on_assets_purged)
        panel.exec()
        # 面板里改过回收站，树跟着刷新一次。
        self.view.category_panel.rebuild_workspace_tree()

    def _on_assets_purged(self, items):
        """永久删除素材：把图元移出场景，保存时数据库记录和 sqlar 原始字节
        随之删除。这一步不可逆，所以顺手清空撤销栈 —— 老命令不能把它复活。
        """
        if not items:
            return
        scene = self.view.scene
        for item in list(items):
            # 先把归档引用丢掉，否则 `delete_items` 的 retain_bytes 会把它
            # 的字节又留一份到临时文件里 —— 那就不叫"永久删除"了。
            archive = getattr(scene, '_prism_archive_items', None)
            if archive is not None:
                archive.discard(item)
            scene.removeItem(item)
        self.view.undo_stack.clear()
        self.view.mark_content_dirty()

    def _on_trash_purged(self, page_ids):
        """永久删除之后，把对应的页面正文也清掉 —— 这次是真的没了。"""
        if not page_ids:
            self.view.mark_content_dirty()
            return
        scene = self.view.scene
        scene.workspace_pages = [page for page in
                                 (scene.workspace_pages or [])
                                 if page['id'] not in page_ids]
        for item in list(scene.items_for_save()):
            if getattr(item, 'canvas_id', None) in page_ids:
                scene.removeItem(item)
        # Purging is irreversible: old commands must not resurrect its items.
        self.view.undo_stack.clear()
        if 'default-document' in page_ids:
            scene.note_html = ''
            self.document_panel.load_from_scene()
        if 'default-mindmap' in page_ids:
            scene.mindmap_tree = None
            self.mindmap_panel.load_from_scene()
        self._sync_page_panels()
        self.view.category_panel.rebuild_workspace_tree()
        if self.current_page_id in page_ids:
            self.open_workspace_page('canvas', self._first_canvas_id())
        self.view.mark_content_dirty()

    def _export_resource_item(self, section, node_id):
        """左侧树右键「导出…」：导出这一项以及它下面的所有层级。

        形态（文件夹 / .zip）和落地位置都在对话框里问，写盘走
        `export_workflow.export_resource_subtree`。``node_id`` 为 None
        表示整个分区。
        """
        from prism.actions.export_workflow import export_resource_subtree

        export_resource_subtree(self.view, section, node_id)

    def _import_resource_item(self, section, node_id):
        """左侧树右键「导入…」：把外面的目录 / 压缩包 / 文件导进这一项。

        规则和「拖一个文件夹进画布」是同一套（一层目录一个容器、层级跟
        着目录走、中间层不建空容器、重名加序号）。``node_id`` 为 None
        表示整个分区。
        """
        from prism.actions.import_workflow import import_resource_subtree

        import_resource_subtree(self.view, section, node_id)

    def create_workspace_page_quietly(self, kind, title=None):
        """Create a page without asking for a name.

        Imports use this: asking someone to open an empty page before they
        are allowed to import into it is a step with no purpose.
        """
        page = self.workspace_service.add(kind, title or _('Imported'))
        self.open_workspace_page(kind, page['id'])
        self.view.mark_content_dirty()
        return self._page_panels.get(page['id'], self.view)

    def find_workspace_panel(self, kind):
        """Return an existing page panel of this kind, or None."""
        for page in getattr(self.view.scene, 'workspace_pages', []) or []:
            if page.get('kind') != kind:
                continue
            panel = self._page_panels.get(page.get('id'))
            if panel is not None:
                return panel
        return None

    def _materialise_implicit_page(self, page_id):
        title = self.IMPLICIT_TITLES.get(page_id)
        if title is None:
            return None
        kind = page_id.split('-', 1)[1]
        page = {'id': page_id, 'kind': kind, 'title': title,
                'content': None, 'tags': [], 'permanent': True}
        return self.workspace_service.restore_page(page)

    def _on_workspace_changed(self, _change):
        """Reflect a service mutation in the UI without rebuilding everything.

        This used to call `_reload_workspace_pages()` on every change: editing
        one page title threw away and re-created every document/mind map panel
        - each of them a whole Chromium view.  Besides being slow, the panels
        that were only `deleteLater`d are never collected before the
        application dies, so Qt exits with

            Release of profile requested but WebEnginePage still not deleted

        and the process ends in 0xC0000005.  Only the pages that actually
        changed are touched now.
        """
        self._sync_page_panels()

    def _sync_page_panels(self):
        """Make the tabs match the scene's page list, minimally.

        Pages that are still there keep their panel (and the browser state
        inside it); only pages that appeared or disappeared are added or
        removed, and a rename just rewrites the tab text.
        """
        pages = {page['id']: page
                 for page in getattr(self.view.scene, 'workspace_pages', [])
                 or []}
        for page_id, panel in list(self._page_panels.items()):
            if page_id in pages:
                continue
            index = self.tabs.indexOf(panel)
            if index >= 0:
                self.tabs.removeTab(index)
            panel.deleteLater()
            del self._page_panels[page_id]
        for page_id, page in pages.items():
            if page.get('kind') == 'canvas' or page_id in self._page_panels:
                continue
            self._append_workspace_page(page)
        for page_id, panel in self._page_panels.items():
            title = pages[page_id].get('title') or _('Untitled')
            index = self.tabs.indexOf(panel)
            if index >= 0 and self.tabs.tabText(index) != title:
                self.tabs.setTabText(index, title)

    def _append_workspace_page(self, page):
        from prism.widgets.document_panel import DocumentPanel
        from prism.widgets.mindmap_panel import MindMapPanel
        if page.get('kind') == 'canvas':
            return self.view
        if page.get('kind') == 'mindmap':
            panel = MindMapPanel(self.view, self, page_id=page['id'])
        else:
            panel = DocumentPanel(self.view, self, page_id=page['id'])
        self.tabs.addTab(panel, page.get('title') or _('Untitled'))
        self._page_panels[page['id']] = panel
        return panel

    def _reload_workspace_pages(self):
        if not hasattr(self, '_page_panels'):
            return
        if hasattr(self, 'search_controller'):
            self.search_controller.reset_project()
        self.tabs.setCurrentIndex(0)
        # 增量同步：还存在的页面保留自己的面板，只补新页面、只删已经不在的。
        # 以前这里是「全部删掉重建」，见 _on_workspace_changed 的说明。
        self._sync_page_panels()
        # Land on the first canvas that exists rather than a placeholder, so
        # opening a project shows its content straight away.
        canvas_id = self._first_canvas_id()
        self.current_page_kind = 'canvas'
        self.current_page_id = canvas_id
        self.view.current_canvas_id = canvas_id
        # 打开工程显示**全部**素材。旧列表在的时候这里会自动选中第一个
        # 分类、只显示那一类；分类筛选的界面入口已经随列表删掉了，默认
        # 再藏素材就没人知道为什么看不见了。
        self.view.category_panel.rebuild_workspace_tree()
        self.view.category_panel._apply_filter()

        if hasattr(self, 'asset_browser'):
            self.asset_browser.history = []
            self.asset_browser.history_index = -1
            self.asset_browser.open('all', record=False)

    def _first_canvas_id(self):
        """The canvas to show after loading a project."""
        library = getattr(self.view, 'category_panel', None)
        if library is not None:
            deleted = {node.get('pageId') for node in library.resource_service.nodes
                       if node.get('deletedAt') and node.get('pageId')}
            for page in library._pages_with_implicit_defaults():
                if page.get('kind') == 'canvas' and page['id'] not in deleted:
                    return page['id']
            if 'default-canvas' in deleted:
                return None
        return 'default-canvas'

    def _open_resource_page(self, kind, page_id):
        controller = getattr(self, 'search_controller', None)
        if controller is not None and controller.active:
            title = next((p['title'] for p in self.view.category_panel._pages_with_implicit_defaults()
                          if p['id'] == page_id), _('Current page'))
            controller.set_scope('page', (kind, page_id), title)
            return
        self.open_workspace_page(kind, page_id)

    @measured('workspace.open_page', 0)
    def open_workspace_page(self, kind, page_id):
        trace_record('workspace_page', page_kind=kind, page_id=page_id)
        if getattr(self, 'current_page_id', None) != page_id:
            self.view._overview_renderer.tiles.reset()
        if hasattr(self, 'asset_browser'):
            self.asset_browser.list.clear_annotations()
        if hasattr(self, 'asset_browser'):
            self.asset_browser.saved_rule = None
        current = self.tabs.currentWidget()
        if hasattr(current, 'store_to_scene'):
            current.store_to_scene()
        self.current_page_kind = kind
        self.current_page_id = page_id
        if kind == 'canvas':
            self.view.current_canvas_id = page_id
            self.tabs.setCurrentWidget(self.canvas_page)
            self.view.category_panel._apply_filter()
            self.filter_popover.color_host.sync_canvas()
            self.color_filter.recount()
            self.canvas_toolbar.refresh()
            self._refresh_filter_bar()
            self.view.on_action_fit_scene()
        else:
            if page_id == 'default-document':
                panel = self.document_panel
            elif page_id == 'default-mindmap':
                panel = self.mindmap_panel
            else:
                panel = self._page_panels.get(page_id)
            if panel is not None:
                self.tabs.setCurrentWidget(panel)

        panel = self.view.category_panel
        tree = panel.resource_tree
        model = tree.model()
        target = next((node['id'] for node in panel.resource_service.nodes
                       if node.get('pageId') == page_id and not node.get('deletedAt')), None)
        if target is None and page_id == 'default-canvas':
            target = 'implicit:default-canvas'
        if target:
            index = model.index_for_id(target)
            if index.isValid():
                tree.setCurrentIndex(index)
                tree.scrollTo(index)
        panel._update_navigation()

    def _workspace_page_action(self, action, page_id):
        if action == 'restore':
            index = next((i for i in range(len(self._deleted_pages) - 1, -1, -1)
                          if self._deleted_pages[i].get('kind') == page_id), None)
            if index is None:
                QtWidgets.QMessageBox.information(
                    self, _('Restore Pages'),
                    _('This group has no pages to restore.'))
                return
            page = self.workspace_service.restore_page(
                self._deleted_pages.pop(index))
            self.view.mark_content_dirty()
            return
        page = next((p for p in self.view.scene.workspace_pages
                     if p.get('id') == page_id), None)
        if page is None:
            if action != 'rename':
                return
            page = self._materialise_implicit_page(page_id)
        if page is None:
            return
        if action == 'rename':
            self._rename_workspace_page(page)
        elif action == 'newchild':
            self._create_workspace_page(page.get('kind'), parent=page['id'])
        elif action == 'delete':
            self._delete_workspace_page(page)
        elif action == 'duplicate':
            self.workspace_service.duplicate(
                page['id'], page['title'] + _(' Copy'))
            self.view.mark_content_dirty()
        elif action in ('up', 'down'):
            if self.workspace_service.move(
                    page['id'], -1 if action == 'up' else 1):
                self.view.mark_content_dirty()
        elif action == 'tag':
            names = list(getattr(self.view.scene, 'tag_names', []))
            # Which object is being tagged is the whole point of this
            # dialog: it edits the page, and no asset gains or loses a tag
            # because of it.
            text, ok = QtWidgets.QInputDialog.getText(
                self, _('Set Page Tags'),
                _('Tags of the page “{title}” - assets keep their own:')
                .format(title=page.get('title', '')),
                text='，'.join(page.get('tags', [])))
            if ok:
                tags = [part.strip() for part in
                        text.replace(',', '，').split('，') if part.strip()]
                self.workspace_service.set_tags(page['id'], tags)
                for tag in page['tags']:
                    if tag not in names:
                        names.append(tag)
                self.view.scene.tag_names = names
                self.view.mark_content_dirty()

    def _show_workspace_page_menu(self, page_id):
        page = next((page for page in self.view.scene.workspace_pages
                     if page.get('id') == page_id), None)
        if page is None:
            return
        menu = QtWidgets.QMenu(self)
        menu.addAction(_('Rename'), lambda: self._rename_workspace_page(page))
        menu.addAction(_('Delete Page'), lambda: self._delete_workspace_page(page))
        menu.exec(QtGui.QCursor.pos())

    def _rename_workspace_page(self, page):
        title, ok = QtWidgets.QInputDialog.getText(
            self, _('Rename Page'), _('Page name:'), text=page['title'])
        if not ok or not title.strip():
            return
        if self.workspace_service.rename(page['id'], title):
            self.view.mark_content_dirty()

    def _delete_workspace_page(self, page):
        if page.get('id') == 'default-canvas' or page.get('permanent'):
            return
        answer = QtWidgets.QMessageBox.question(
            self, _('Delete Page'),
            _('Delete “{title}”? This can be undone only by closing without saving.')
            .format(title=page['title']))
        if answer != QtWidgets.QMessageBox.StandardButton.Yes:
            return
        removed = self.workspace_service.remove(page['id'])
        if removed:
            self._deleted_pages.append(copy.deepcopy(page))
            self.view.mark_content_dirty()
