"""Main-window workspace and resource workflows; construction stays in __main__."""
import copy
import logging
import os
from datetime import datetime, timezone
from prism.performance_trace import operation, record as trace_record

from PyQt6 import QtCore, QtGui, QtWidgets
from PyQt6.QtCore import Qt
from prism import page_selection
from prism.i18n import _
from prism.widgets.components.icons import icon

logger = logging.getLogger(__name__)

class WindowWorkspaceMixin:
    def request_navigation(self, callback):
        """Commit the visible editor once, then execute only the latest intent.

        The editor is temporarily disabled so input cannot race the renderer's
        snapshot. Other navigation controls remain usable. Saving fails closed
        while this barrier is outstanding. Preview cancellation never cancels it.
        """
        if getattr(self.view, '_upgrade_preview_readonly', False):
            session = getattr(self.view, '_upgrade_preview_session', None)
            if session is not None and session.phase in ('opening_preview', 'opening_converted'):
                return
            # Browsing old-source pages must not read an editor back or adopt
            # attachments. It is a read-only navigation, not a commit barrier.
            previous = getattr(self, '_navigation_committed_page', None)
            self._navigation_committed_page = (self.tabs.currentWidget(), self.view.scene.project_id)
            try:
                callback()
            finally:
                self._navigation_committed_page = previous
            return
        if getattr(self, '_page_store_in_progress', False):
            if getattr(self, '_navigation_commit_pending', False):
                self._navigation_intent = callback
            else:
                trace_record('workspace_navigation_save_barrier_blocked')
            return
        self._navigation_intent = callback
        current = self.tabs.currentWidget()
        project_id = self.view.scene.project_id
        self._page_store_in_progress = True
        self._navigation_commit_pending = True
        enabled = current.isEnabled()
        current.setEnabled(False)
        from prism.item_lifecycle import is_deleted
        completed = False

        def finished(ok):
            nonlocal completed
            if completed:
                return
            completed = True
            alive = not is_deleted(self) and not is_deleted(current)
            if alive:
                current.setEnabled(enabled)
            self._page_store_in_progress = False
            self._navigation_commit_pending = False
            intent = self._navigation_intent
            self._navigation_intent = None
            if not alive or self.view.scene.project_id != project_id:
                return
            if not ok:
                self.statusBar().showMessage(
                    _('Editor content could not be read. Please retry.'), 6000)
                trace_record('workspace_navigation_commit_failed')
                return
            previous = getattr(self, '_navigation_committed_page', None)
            self._navigation_committed_page = (current, project_id)
            try:
                if intent is not None:
                    intent()
            finally:
                self._navigation_committed_page = previous

        try:
            async_store = getattr(current, 'store_to_scene_async', None)
            if async_store is not None:
                async_store(finished)
            else:
                store = getattr(current, 'store_to_scene', None)
                finished(store() is not False if store is not None else True)
        except Exception:
            logger.exception('Navigation editor commit failed')
            if getattr(self, '_page_store_in_progress', False):
                finished(False)

    def _navigation_is_committed(self):
        token = getattr(self, '_navigation_committed_page', None)
        return (token is not None and token[0] is self.tabs.currentWidget()
                and token[1] == self.view.scene.project_id)

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
        after_id = None
        if node_type == 'folder':
            from prism.widgets.resource_tree_model import SECTION_ROLE, NODE_ID_ROLE, NODE_TYPE_ROLE
            current = panel.resource_tree.currentIndex()
            if current.data(SECTION_ROLE) == section:
                if current.data(NODE_TYPE_ROLE) == 'folder' and parent_id is None:
                    parent_id = current.data(NODE_ID_ROLE)
                elif current.data(NODE_TYPE_ROLE) == 'page':
                    node = panel.resource_service._by_id(current.data(NODE_ID_ROLE))
                    if node is not None and parent_id in (None, node.get('parentId')):
                        after_id = node['id']
                        parent_id = node.get('parentId')
        title = panel.resource_service.unique_suggestion(
            section, parent_id, node_type, base)
        panel.resource_tree.begin_create(section, node_type, parent_id, title, after_id=after_id)

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
            if self._store_current_page() is False:
                return
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

    def _store_current_page(self, current=None):
        if getattr(self.view, '_upgrade_preview_readonly', False):
            return True  # Never materialize editor edits while browsing a source.
        """把当前页签里的编辑写回 scene（切页/删除前都该做这一步）。"""
        current = current if current is not None else self.tabs.currentWidget()
        with operation('workspace.store_current_page',
                       panel=type(current).__name__ if current is not None else None):
            committed = getattr(self, '_navigation_committed_page', None)
            if (committed is not None and committed[0] is current
                    and committed[1] == self.view.scene.project_id):
                trace_record('workspace_commit_reused')
                return True
            # Legacy editor read-back runs a nested event loop. Reject reentrant
            # navigation/save rather than starting another read or using stale data.
            if getattr(self, '_page_store_in_progress', False):
                trace_record('workspace_commit_reentrant_blocked')
                return False
            self._page_store_in_progress = True
            try:
                if hasattr(current, 'store_to_scene'):
                    with operation('page.store_to_scene',
                                   panel=type(current).__name__):
                        stored = current.store_to_scene()
                    if stored is False:
                        self.statusBar().showMessage(
                            _('Editor content could not be read. Please retry.'), 6000)
                        return False
                return True
            finally:
                self._page_store_in_progress = False

    def _set_widget_after_page_store(self, widget, stored_page):
        """Reuse only this navigation's commit during its synchronous tab signal.

        This token never survives into another user action or project. Direct
        tab clicks still commit normally in _on_tab_changed.
        """
        previous = getattr(self, '_navigation_committed_page', None)
        self._navigation_committed_page = (stored_page, self.view.scene.project_id)
        try:
            self.tabs.setCurrentWidget(widget)
        finally:
            self._navigation_committed_page = previous

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
        # 没有合法画布时就停在空状态，不要凭空造一页出来。
        fallback = self._first_canvas_id()
        if fallback is not None:
            self.open_workspace_page('canvas', fallback)

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
            if self._store_current_page() is False:
                return
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
        """Show the recycle bin as a scrollable library page, not a modal canvas."""
        if not self._navigation_is_committed():
            return self.request_navigation(self._open_trash_panel)
        previous = self.tabs.currentWidget()
        if self._store_current_page(previous) is False:
            return
        from prism.widgets.trash_panel import TrashPanel
        panel = getattr(self, '_trash_library_page', None)
        project = self.view.scene.project_id
        if panel is None or panel._project_id != project:
            if panel is not None:
                self.tabs.removeTab(self.tabs.indexOf(panel))
                panel.deleteLater()
            panel = TrashPanel(self.view, self, embedded=True)
            panel._project_id = project
            panel.purged.connect(self._on_trash_purged)
            panel.assets_purged.connect(self._on_assets_purged)
            self._trash_library_page = panel
            self.tabs.addTab(panel, _('Recycle bin'))
        else:
            panel._refresh_assets()
            panel._refresh_nodes()
            panel._update_hint()
        self._set_widget_after_page_store(panel, previous)

    def _on_assets_purged(self, items):
        """永久删除素材：把图元移出场景，保存时数据库记录和 sqlar 原始字节
        随之删除。这一步不可逆，所以顺手清空撤销栈 —— 老命令不能把它复活。

        部分加载的工程在这里必须停手：`items` 行是共享原始字节的，部分保存
        既没有依据删它，也不敢假装它不在。图元移出场景之后每次保存都会被
        拒绝，而用户看不出原因 —— 那就一步都别动，并把原因说清楚。
        """
        if not items:
            return
        scene = self.view.scene
        if getattr(scene, '_partial_project_source', None):
            from prism.widgets.trash_panel import ASSET_PURGE_REFUSED
            QtWidgets.QMessageBox.warning(self, '永久删除', ASSET_PURGE_REFUSED)
            return
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
            self._clear_purged_mindmap()
        self._sync_page_panels()
        self.view.category_panel.rebuild_workspace_tree()
        if self.current_page_id in page_ids:
            fallback = self._first_canvas_id()
            if fallback is not None:
                self.open_workspace_page('canvas', fallback)
        self.view.mark_content_dirty()

    def _clear_purged_mindmap(self):
        """清空已删脑图页的正文。

        回收站里永久删除这一页，就是那条明确的「清空脑图」指令：清空之前先向
        保存会话取得授权（`authorize_mindmap_clear`），否则这次保存会被拒绝，
        之后每一次保存也都会被拒绝。板子没载入过就不需要授权 —— 那种情况下
        清空根本不产生任何写入。真拿不到授权时保留正文并照实说，而不是把
        工程变成再也存不了的样子。
        """
        scene = self.view.scene
        session = getattr(scene, '_partial_project_session', None)
        if session is not None and session.mindmap_clear_needed():
            from prism.fileio.partial_save import PartialSaveConflict
            try:
                session.authorize_mindmap_clear()
            except PartialSaveConflict as exc:
                QtWidgets.QMessageBox.warning(self, '永久删除', str(exc))
                return
        scene.mindmap_tree = None
        self.mindmap_panel.load_from_scene()

    def _export_resource_item(self, section, node_id):
        """左侧树右键「导出…」：导出这一项以及它下面的所有层级。

        形态（文件夹 / .zip）和落地位置都在对话框里问，写盘走
        `export_workflow.export_resource_subtree`。``node_id`` 为 None
        表示整个分区。
        """
        from prism.actions.export_workflow import export_resource_subtree

        export_resource_subtree(self.view, section, node_id, typed=True)

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
            return self._append_workspace_page(page)
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
        self.view.category_panel.resource_records_changed.emit({'kind': 'page'})

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
            # The resource tree already contains every saved page. A large
            # library does not need a thousand hidden editor widget trees
            # and tab layouts just to display its first canvas.
            if len(pages) <= 64:
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

    def _reload_workspace_pages(self, landing=True):
        """Rebuild the workspace pages and install the landing state.

        ``landing=False`` is the project-switch path: the panels of the closed
        project have to go, but the project that replaces them is still
        loading.  Installing the landing state here as well emptied the
        sidebar and swapped the central widget a second time for a single
        open -- that is the flash the user sees when a project opens.  The
        landing state belongs to the finalization of the open, and only there.
        """
        if not hasattr(self, '_page_panels'):
            return
        if hasattr(self, 'search_controller'):
            self.search_controller.reset_project()
        if landing:
            self.tabs.setCurrentIndex(0)
        # 增量同步：还存在的页面保留自己的面板，只补新页面、只删已经不在的。
        # 以前这里是「全部删掉重建」，见 _on_workspace_changed 的说明。
        with operation('page.reload.sync_panels'):
            self._sync_page_panels()
        if not landing:
            # The resource tree and the central widget belong to the project
            # that is still loading.  Rebuilding them here installed an empty
            # resource service, which emptied the sidebar tree and swapped the
            # central widget -- and the open then did it all over again.
            canvas_id = self._first_canvas_id()
            self.current_page_kind = 'canvas'
            self.current_page_id = canvas_id
            self.view.current_canvas_id = canvas_id
            if hasattr(self, 'asset_browser'):
                self.asset_browser.history = []
                self.asset_browser.history_index = -1
            # The panels that were dropped are whole Chromium views.  Their
            # QWebEnginePage is released asynchronously, and a page that is
            # still alive when the profile is released ends the process with
            # "Release of profile requested but WebEnginePage still not
            # deleted" and 0xC0000005.  Draining the deferred deletes releases
            # them while the application is alive.
            QtCore.QCoreApplication.sendPostedEvents(
                None, QtCore.QEvent.Type.DeferredDelete)
            return
        # The resource tree has to be current BEFORE the landing canvas is
        # chosen: the set of soft-deleted canvases comes from the tree's
        # nodes, and a tree that still belongs to the previous project would
        # report a deleted canvas as legal.  Rebuilding it here also installs
        # the resource service for the new project, which is what
        # `_first_canvas_id` reads the deleted pages from.
        with operation('page.reload.rebuild_tree'):
            self.view.category_panel.rebuild_workspace_tree()
        # 落在**第一张合法画布**上（工程顺序）。以前这里优先用
        # `_paged_open_target`，而那个目标是后台按「上次打开」或「素材最多」
        # 选的 —— 于是后台按页加载的是一张画布，界面落的却是另一张，两边
        # 永远对不上。现在两边用同一套规则（prism/page_selection.py）。
        canvas_id = self._first_canvas_id()
        self.current_page_kind = 'canvas'
        self.current_page_id = canvas_id
        self.view.current_canvas_id = canvas_id
        if canvas_id is not None:
            with operation('page.reload.switch_context'):
                self.view._overview_renderer.tiles.switch_context(canvas_id)
        # The filter runs after the canvas id is known: it filters by the
        # canvas that is actually on screen.
        with operation('page.reload.apply_filter'):
            self.view.category_panel._apply_filter()

        if hasattr(self, 'asset_browser'):
            self.asset_browser.history = []
            self.asset_browser.history_index = -1
        # 打开工程显示**这张画布**，不是全部素材。旧代码在这里强制
        # asset_browser.open('all')，用户看到的是整个项目的素材列表，而不是
        # 自己工程的第一张画布。素材浏览器的范围改为跟随落点画布，两边口径
        # 一致；「全部素材」仍然是左栏里一个明确的入口，而不是打开时的默认。
        with operation('page.reload.set_widget'):
            self._set_widget_after_page_store(self.canvas_page, self.tabs.currentWidget())
        if canvas_id is not None:
            with operation('page.reload.sync_scope'):
                self._sync_canvas_scope(canvas_id)
            with operation('page.reload.select_tree'):
                self._select_resource_page(canvas_id)
            with operation('page.reload.reset_camera'):
                from prism import camera_policy
                camera_policy.request_reset(self.view,camera_policy.visible_asset_bounds)

    def _first_canvas_id(self):
        """The canvas to show after loading a project.

        One rule, shared with the background scope preparation that runs off
        the GUI thread (``prism/page_selection.py``): the first **legal**
        canvas in project order.

        * A canvas the project really stores counts even when it holds no
          assets -- an empty first canvas is still the first canvas.
        * A canvas whose resource-tree node is soft-deleted does not count.
        * The legacy implicit ``default-canvas`` is only used when the project
          still carries content for it, and it never outranks a stored canvas.

        ``None`` means the project has no legal canvas at all; the caller then
        shows the normal empty state instead of inventing a page.
        """
        library = getattr(self.view, 'category_panel', None)
        if library is None:
            return page_selection.DEFAULT_CANVAS_ID
        deleted = page_selection.deleted_page_ids(
            getattr(getattr(library, 'resource_service', None), 'nodes', []) or [])
        stored = page_selection.stored_canvas_ids(
            getattr(getattr(self.view, 'scene', None), 'workspace_pages', []) or [])
        checker = getattr(library, '_implicit_default_has_content', None)
        implicit = bool(checker()) if callable(checker) else True
        return page_selection.first_legal_canvas(stored, deleted, implicit)

    def _sync_canvas_scope(self, canvas_id):
        """Reset the asset browser's scope so it cannot show a stale one.

        The browser is NOT opened here any more: opening a project lands on
        the canvas, and forcing a browser view on top of it was the second
        half of the "I opened my project and see all assets" complaint.

        The scope is reset rather than narrowed -- ``all`` is the whole
        project (§6), so pinning it to the landed canvas would re-introduce
        the current-page restriction.  The explicit left-bar entry is what
        opens the browser.
        """
        browser = getattr(self, 'asset_browser', None)
        if browser is None:
            return
        browser.reset_scope()

    def _select_resource_page(self, page_id):
        """Select the resource-tree row of the canvas we landed on."""
        panel = getattr(self.view, 'category_panel', None)
        tree = getattr(panel, 'resource_tree', None)
        model = tree.model() if tree is not None else None
        if model is None:
            return
        nodes = getattr(getattr(panel, 'resource_service', None), 'nodes', []) or []
        for node in nodes:
            if node.get('pageId') != page_id or node.get('deletedAt'):
                continue
            index = model.index_for_id(node.get('id'))
            if not index.isValid():
                continue
            parent = index.parent()
            while parent.isValid():
                tree.expand(parent)
                parent = parent.parent()
            tree.setCurrentIndex(index)
            tree.scrollTo(index)
            return

    # ── 库面板预热 ───────────────────────────────────────────────
    #
    # 材质库和模型库的面板都是"第一次进入才构造"的。构造发生在点击处理
    # 函数内部时，窗口在这段时间里不重绘：真实工程实测第一次切换要
    # 1723ms（模型库）/ 2118ms（材质库），主线程被占死，Windows 把窗口
    # 标成"未响应"——用户看到的就是"程序闪退了"。同一会话里索引就绪后
    # 再切同样的页面只要 13–86ms。
    #
    # 构造必须在控件内部切片，不能只在两个完整构造函数之间让出一拍。
    # 两库顺序组装，每片之间恢复输入/绘制；首次进入复用同一个组装任务。
    #
    # 预热本身也不能霸占窗口：构造现在会限速（片与片之间给事件循环留出
    # 空隙）、会在用户操作时让路，所以它占的是"零碎时间"而不是"一整秒"。
    #
    # 但"碎片时间"的前提在真实工程上不成立。会话 135164 实测：打开工程后
    # 4.8 秒内连着 10 次 GUI 硬阻塞（71–194ms），**每一次**都落在两库预热
    # 的 `_construct` 里（material_details / material_slots）。194ms 那次
    # 已经越过 tests/test_library_prewarm_never_blocks.py 钉的上限（150ms），
    # 窗口被占住不放，用户看到的就是"整个界面闪了一下"。
    #
    # ⇒ 预热**不能被推迟**（同一份测试把"打开完就预热"钉成了契约：推迟
    # 只会让用户更可能在面板建好之前点进来），要修的是**切片本身**：
    # 单步不能长到把窗口占死。见 prism/widgets/widget_construction.py。

    #: Set this environment variable to ``0`` to switch the prewarm off.
    #: The panels are then built when the user opens them, which runs the
    #: same sliced construction, only started by the click.
    PREWARM_ENABLE_ENV = 'PRISM_LIBRARY_PREWARM'

    @classmethod
    def _prewarm_enabled(cls):
        value = os.environ.get(cls.PREWARM_ENABLE_ENV, '1').strip()
        return value not in ('0', 'false', 'off', 'no')

    def schedule_library_prewarm(self):
        """Build the library panels while the app is idle after an open."""
        if getattr(self, '_library_prewarm_scheduled', False):
            return
        if not self._prewarm_enabled():
            return
        self._library_prewarm_scheduled = True
        # 预热**不能被推迟**：推迟只会让用户更可能在面板建好之前点进来
        # （tests/test_library_prewarm_never_blocks.py 把这条钉成了契约）。
        # 用户正在操作时让路，由切片机制自己负责（WidgetConstruction）。
        parent = self if isinstance(self, QtCore.QObject) else None
        self._prewarm_timer = QtCore.QTimer(parent)
        self._prewarm_timer.setSingleShot(True)
        self._prewarm_timer.timeout.connect(self._prewarm_model_library)
        self._prewarm_timer.start(0)

    def _prewarm_model_library(self):
        self._library_prewarm_scheduled = False
        try:
            # 只建面板，**不**调 `composition.ensure()`：那一步会打开模型的
            # 工程上下文并建模型表，是一次真实的写入 —— 源的指纹随之改变，
            # 目录缓存失配，正在进行的画布分页读取直接失败
            # （"Catalog is incomplete or source has changed"）。模型服务
            # 留给用户真正打开模型库时再接入。
            job = self.model_library.prewarm()
            if job is not None and not job.done:
                job.finished.connect(lambda *_: self._prewarm_material_library())
                job.failed.connect(lambda *_: self._prewarm_material_library())
                return
        except Exception:
            logger.exception('模型库面板预热失败')
        # 一个面板建不起来，不能把另一个也带下水。
        QtCore.QTimer.singleShot(0, self._prewarm_material_library)

    def _prewarm_material_library(self):
        try:
            self.material_library.prewarm()
        except Exception:
            logger.exception('材质库面板预热失败')

    def _library_switch_status(self, text):
        """Say what the window is doing while a library page is prepared."""
        try:
            if hasattr(self, 'statusBar'):
                self.statusBar().showMessage(text, 4000)
        except RuntimeError:
            pass

    def open_model_library(self, folder_id=None, *, from_search=False):
        """Navigate to the model library through the existing commit gate."""
        if not self._navigation_is_committed():
            return self.request_navigation(lambda: self.open_model_library(folder_id, from_search=from_search))
        previous = self.tabs.currentWidget()
        if self._store_current_page(previous) is False:
            return
        controller = getattr(self, 'search_controller', None)
        if controller is not None and controller.active and not from_search:
            controller.exit()
        with operation('page.model.open'):
            composition = getattr(self, 'model_composition', None)
            if composition is not None:
                composition.ensure()
            if not self.model_library.initialized:
                # 面板还没建好（预热没赶上）：这一下会花掉构造的时间，
                # 先告诉用户在做什么，别让窗口看着像死了。
                self._library_switch_status(_('正在准备模型库…'))
            # The panel is built on first use. Building it here - instead of
            # letting the first attribute access do it - keeps the cost in
            # one measured place, and lets the readiness wait below have a
            # page to wait for.
            self.model_library.ensure_created()
            # Bind the real session identity before the page reads anything.
            # The project id alone cannot tell two openings of the same
            # project apart, so the open epoch travels with it.
            self.model_library.bind_scope(
                getattr(self.view.scene, 'project_id', ''),
                getattr(self.view, '_trace_open_epoch', 0))
            self.model_library.set_folder(folder_id)
            self.model_library.load_from_scene()
            self.current_page_kind = 'model'
            self.current_page_id = None
            # Same contract as the material library: the panel is only shown
            # once it has something to show.
            self.model_library.wait_for_content()
            self._set_widget_after_page_store(self.model_library, previous)
            from prism.widgets.resource_tree_model import UI_SECTIONS
            tree = self.view.category_panel.resource_tree
            index = (tree.model().index_for_id(folder_id) if folder_id else
                     tree.model().index(UI_SECTIONS.index('model'), 0))
            tree.setCurrentIndex(index)
            tree.scrollTo(index)
            self.model_library.setFocus()

    def _open_material_library_entry(self, folder_id=None):
        if (folder_id is None
                and self.tabs.currentWidget() is not self.material_library):
            self.material_library._sync_project()
            folder_id = self.material_library.folder_id
        self.open_material_library(folder_id)

    def open_material_library(self, folder_id=None, *, from_search=False):
        if self.material_library.waiting_for_content:
            # 正在等面板就绪：外层那一次会完成切换，这里不重复切一次
            # （重复切会把还没内容的空板显示出来）。
            return
        if not self._navigation_is_committed():
            return self.request_navigation(lambda: self.open_material_library(
                folder_id, from_search=from_search))
        previous = self.tabs.currentWidget()
        if self._store_current_page(previous) is False:
            return
        controller = getattr(self, 'search_controller', None)
        if controller is not None and controller.active and not from_search:
            controller.exit()
        with operation('page.material.open', folder_id=folder_id,
                       from_search=from_search):
            # The panel is built on first use. Build it here, in one measured
            # place, so the readiness wait below has a page to wait for.
            self.material_library.ensure_created()
            with operation('page.material.open_folder'):
                self.material_library.open_folder_cooperatively(folder_id)
            self.current_page_kind = 'material'
            self.current_page_id = None
            with operation('page.material.set_widget'):
                # 面板有内容之前不切：切过去是一块空板，卡片随后才出现 ——
                # 用户看到的那一下"闪"就是它。
                if not self.material_library.is_content_ready():
                    self._library_switch_status(_('正在加载材质库…'))
                self.material_library.wait_for_content()
                self._set_widget_after_page_store(self.material_library, previous)
            with operation('page.material.select_tree'):
                tree = self.view.category_panel.resource_tree
                model = tree.model()
                if self.material_library.folder_id is None:
                    from prism.widgets.resource_tree_model import UI_SECTIONS
                    index = model.index(UI_SECTIONS.index('material'), 0)
                else:
                    index = model.index_for_id(self.material_library.folder_id)
                tree.setCurrentIndex(index)
                tree.scrollTo(index)
            with operation('page.material.navigation_update'):
                self.view.category_panel._update_navigation()
            self.material_library.setFocus()

    def _open_resource_section(self, section):
        """A category row opens that category's first concrete content.

        * 画布 / 脑图 / 文档 → the section's first page. When the section's first
          entry is a folder, that folder's first page is what the user means.
        * 材质库 / 模型库 → the section's first folder. A section without any
          folder has nothing narrower to show, so it keeps the section itself.
          The same row, clicked while that library is already open, goes back to
          the section itself - that is the only way back to "everything".
        * 标签 → a tag has no folder to descend into, so the row does nothing.
        """
        model = self.view.category_panel.resource_tree.model()
        if section in ('canvas', 'mindmap', 'document'):
            node = model.first_page_under(section)
            if node is None:
                return
            self._open_resource_page(node['section'], node['pageId'])
            return
        if section == 'material':
            if self.tabs.currentWidget() is self.material_library:
                self.open_material_library(None)
                return
            node = model.first_folder_under(section)
            self.open_material_library(node['id'] if node is not None else None)
            return
        if section == 'model':
            if self.tabs.currentWidget() is self.model_library:
                self.open_model_library(None)
                return
            node = model.first_folder_under(section)
            self.open_model_library(node['id'] if node is not None else None)

    def _open_resource_page(self, kind, page_id):
        controller = getattr(self, 'search_controller', None)
        if controller is not None and controller.active:
            if kind=='canvas':
                controller.exit(restore_origin=False)
                self.open_workspace_page(kind,page_id)
                return
            title = next((p['title'] for p in self.view.category_panel._pages_with_implicit_defaults()
                          if p['id'] == page_id), _('Current page'))
            controller.set_scope('page', (kind, page_id), title)
            return
        self.open_workspace_page(kind, page_id)

    def _record_recent_use(self, section, resource_id, name=''):
        """记一笔「最近使用」。

        账本在用户目录里，按工程分组 —— 工程是用户自己的文件，最近使用
        是应用自己的状态。
        """
        from prism import project_resources
        if not resource_id:
            return
        if not name:
            name = next((page.get('title') or '' for page in
                         (getattr(self.view.scene, 'workspace_pages', None) or [])
                         if page.get('id') == resource_id), '')
        self.view.category_panel.record_resource_usage(section, resource_id, name)

    def open_workspace_page(self, kind, page_id):
        if not self._navigation_is_committed():
            return self.request_navigation(lambda: self.open_workspace_page(kind, page_id))
        previous = self.tabs.currentWidget()
        if self._store_current_page(previous) is False:
            return
        trace_record('workspace_page', page_kind=kind, page_id=page_id)
        with operation('page.open', kind=kind, page_id=page_id):
            if getattr(self, 'current_page_id', None) != page_id:
                with operation('page.open.switch_context'):
                    self.view._overview_renderer.tiles.switch_context(page_id)
            if hasattr(self, 'asset_browser'):
                self.asset_browser.list.clear_annotations()
            if hasattr(self, 'asset_browser'):
                self.asset_browser.saved_rule = None
            self.current_page_kind = kind
            self.current_page_id = page_id
            self._record_recent_use(kind, page_id)
            if kind == 'canvas':
                self.view.current_canvas_id = page_id
                with operation('page.open.request_canvas_assets'):
                    self.view.request_canvas_assets(page_id)
                with operation('page.open.set_widget', panel='canvas'):
                    self._set_widget_after_page_store(self.canvas_page, previous)
                with operation('page.open.canvas_ui'):
                    self.view.category_panel._apply_filter()
                    self.filter_popover.color_host.sync_canvas()
                    self.color_filter.recount()
                    self.canvas_toolbar.refresh()
                    self._refresh_filter_bar()
                    self.view.on_action_fit_scene()
            else:
                loader = getattr(self.view, '_paged_canvas_loader', None)
                if loader is not None:
                    loader.cancel()
                if page_id == 'default-document':
                    panel = self.document_panel
                elif page_id == 'default-mindmap':
                    panel = self.mindmap_panel
                else:
                    panel = self._page_panels.get(page_id)
                    if panel is None:
                        page = next((p for p in self.view.scene.workspace_pages
                                     if p['id'] == page_id and p.get('kind') == kind), None)
                        if page is not None:
                            with operation('page.open.append_panel', kind=kind):
                                panel = self._append_workspace_page(page)
                if panel is not None:
                    with operation('page.open.set_widget',
                                   panel=type(panel).__name__):
                        self._set_widget_after_page_store(panel, previous)

            with operation('page.open.navigation_update'):
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
