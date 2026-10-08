"""Media library sidebar panel.

The left panel is the resource tree; the filter widgets above it narrow
what the board shows.  The old "筛选与旧分类" list tab that used to sit
under them was removed on the user's request - categories and the tag
checkboxes no longer have a list to live in, and their management entry
points moved to the tree's context menu and the inspector.
"""

import logging
import re
import json

from PyQt6 import QtCore, QtGui, QtWidgets, sip

from prism import commands, tags
from prism.i18n import _
from prism.widgets import components, tag_panel
from prism.widgets.resource_tree_model import (
    NODE_ID_ROLE, NODE_TYPE_ROLE, SECTION_ROLE, ResourceTreeModel)
from prism.widgets.resource_tree_view import ResourceTreeView
from prism.workspace_service import ResourceTreeService, legacy_page_nodes
from prism.project_search import plain_text as plain_text_of, text_of_tree


logger = logging.getLogger(__name__)

#: 明显的标签查询语法。**判定故意保守**：只有一眼看出来是运算符的才当
#: 标签查询，别的都当文字搜。
#:
#: 猜错的代价不对称 —— 把普通词当成标签查询会让它搜不到东西（比如搜
#: 「武侠-爱情」这种带连字符的词），而当文字搜最多是结果多一些、用户
#: 一眼能看出来再改。
_TAG_SYNTAX = re.compile(r'\||\*|\s-\S')

#: 标注类图元的 TYPE —— 画笔线（`path`）和文字（`text`）。
#:
#: 它们**不是素材**：没有分类 / 标签 / 评分 / 文件名，按素材的筛选
#: 规则算永远是"不匹配"。用户 2026-10-01 报的「划线就消失」就是
#: 这个原因 —— 画完一笔 300ms 后 `scene.changed → _schedule_recount
#: → update_counts → _apply_filter` 把线当成不匹配的素材
#: `setVisible(False)` 藏了起来（数据没丢，用户的工程里躺着 26 条
#: 被藏起来的线；离线测试因为没挂筛选面板所以复现不了）。
#:
#: 用 TYPE 字符串而不是 import `prism.items`：这个模块对图元的判断
#: 一向走鸭子类型（`getattr(item, '_categories', [])` 等），保持同一
#: 风格，也避免模块间相互 import。
ANNOTATION_TYPES = ('path', 'text')


class MediaLibraryPanel(QtWidgets.QWidget):
    counts_changed = QtCore.pyqtSignal(dict)
    categories_changed = QtCore.pyqtSignal()
    workspace_requested = QtCore.pyqtSignal(str, str)
    resource_create_requested = QtCore.pyqtSignal(str, str, object)
    resource_delete_requested = QtCore.pyqtSignal(str)
    #: 右键「导出…」：参数是 (分区, 节点 id)；节点 id 为 None 表示整个分区。
    resource_export_requested = QtCore.pyqtSignal(str, object)
    #: 右键「导入…」：参数同上；内容落在这一项下面。
    resource_import_requested = QtCore.pyqtSignal(str, object)
    resource_nodes_changed = QtCore.pyqtSignal(object)

    def __init__(self, view, parent=None):
        super().__init__(parent)
        self.view = view
        self.scene = view.scene
        self._active_filter = None  # None = show all; ('category', name) = filter
        self._active_tags = set()
        self._tag_node_names = set()
        self._min_rating = 0
        # 「只看没有标签的素材」（§8.3 筛选弹层的「未标记」字段）。
        # 界面上那条勾选行随红框删掉了，状态与筛选条件留着 —— 现在是弹层在用。
        self._untagged_only = False
        self._setup_ui()
        self._recount_timer = QtCore.QTimer(self)
        self._recount_timer.setSingleShot(True)
        self._recount_timer.setInterval(300)
        self._recount_timer.timeout.connect(self.update_counts)
        self.scene.changed.connect(self._schedule_recount)
        tag_panel.tag_signals().changed.connect(self.refresh_tags)
        self.update_counts()

    def _scene_alive(self):
        """Whether the scene's C++ object still exists.

        Qt emits scene signals while the scene is torn down and again while
        the interpreter shuts down. Without this check those late callbacks
        touch a deleted C++ object and log an unhandled RuntimeError.
        """
        return self.scene is not None and not sip.isdeleted(self.scene)

    def _schedule_recount(self):
        if not self._scene_alive():
            return
        if not self._recount_timer.isActive():
            self._recount_timer.start()

    def _setup_ui(self):
        self.setObjectName('prismSidebar')
        self.setMinimumWidth(200)
        self.setMaximumWidth(16777215)

        layout = QtWidgets.QVBoxLayout(self)
        layout.setContentsMargins(12, 14, 12, 12)
        layout.setSpacing(8)

        self.search = components.LineEdit()
        self.search.setPlaceholderText(_('Search photos, documents, mind maps'))
        self.search.setClearButtonEnabled(True)
        self.search.setToolTip(_(
            'Names, paths, notes, tags — and the text inside documents and '
            'mind maps.\n'
            'Several words all have to match (space means "and").\n\n'
            'When you need the tag operators:\n'
            '  scenery:*      wildcard\n'
            '  night | wall    either one\n'
            '  wall -sketch    rule one out\n'
            '  -*             the assets that carry no tag at all\n'
            '  tag:sunset     force a tag query\n'
            '  "soft light"    quotes force plain text'))
        self.search.textChanged.connect(self._on_project_search)
        from prism.widgets.components.icons import icon
        self.search.addAction(icon('search'), QtWidgets.QLineEdit.ActionPosition.LeadingPosition)
        layout.addWidget(self.search)
        clear_button = components.Button(_('Show All / Clear Filters'))
        self.clear_button = clear_button
        clear_button.clicked.connect(self._clear_search_or_filters)
        layout.addWidget(clear_button)

        # （这里原来还有第二个输入框「标签查询」。用户问过"标签查询功能
        # 是不是和搜索功能重叠了"—— 界面上两个盒子确实让人分不清。现在
        # 合并成一个：`_split_query()` 按输入内容自动分派，语法照旧能写。）
        #
        # 再往上原来还有一行「标签筛选：同时包含 / 任意包含」下拉 + 管理
        # 标签的齿轮，以及一行说明「标签筛选只作用于本画布的素材」——
        # 用户带红框截图要求删掉（2026-10-01）。标签管理器仍在：资源树
        # 的「标签」分区右键、素材右键菜单里都有「管理标签」。

        rating_row = QtWidgets.QHBoxLayout()
        self.rating_label = QtWidgets.QLabel(_('Minimum rating'))
        rating_row.addWidget(self.rating_label)
        self.rating_filter = QtWidgets.QComboBox()
        self.rating_filter.addItems(
            [_('All'), '★+', '★★+', '★★★+', '★★★★+', '★★★★★'])
        self.rating_filter.currentIndexChanged.connect(self._on_rating_filter)
        rating_row.addWidget(self.rating_filter, 1)
        layout.addLayout(rating_row)

        # 左栏现在只有这一棵树。「筛选与旧分类」那个旧列表页签按用户
        # 要求删掉了 —— 分类行、标签勾选行和「未打标签」行都随它一起
        # 消失，标签勾选筛选暂时没有界面入口（筛选能力还在代码里）。
        self.resource_tree = ResourceTreeView(self)
        self._install_resource_service()
        self.resource_tree.create_requested.connect(
            self.resource_create_requested.emit)
        self.resource_tree.open_page_requested.connect(
            self.workspace_requested.emit)
        self.resource_tree.delete_requested.connect(
            self.resource_delete_requested.emit)
        self.resource_tree.export_requested.connect(
            self.resource_export_requested.emit)
        self.resource_tree.import_requested.connect(
            self.resource_import_requested.emit)
        self.resource_tree.new_tag_requested.connect(self.add_tag)
        self.resource_tree.manage_tags_requested.connect(self.manage_tags)
        layout.addWidget(self.resource_tree, stretch=1)
        self.search_results = QtWidgets.QTreeWidget()
        self.search_results.setObjectName('projectSearchResults')
        self.search_results.setStyleSheet(
            'QTreeWidget#projectSearchResults { background: #222327; border: none; }'
            'QTreeWidget#projectSearchResults::item { padding: 6px 4px; border-radius: 5px; }'
            'QTreeWidget#projectSearchResults::item:hover { background: #30333A; }'
            'QTreeWidget#projectSearchResults::item:selected { background: #263E5A; }')
        self.search_results.setHeaderHidden(True)
        self.search_results.setWordWrap(True)
        self.search_results.setIndentation(12)
        self.search_results.itemActivated.connect(self._activate_search_result)
        self.search_results.itemClicked.connect(self._activate_search_result)
        self.search_results.hide()
        layout.addWidget(self.search_results, stretch=1)
        self._search_timer = QtCore.QTimer(self)
        self._search_timer.setSingleShot(True)
        self._search_timer.setInterval(180)
        self._search_timer.timeout.connect(self._refresh_project_search)
        self.resource_tree.tag_requested.connect(
            lambda name: self.search.setText('tag:' + json.dumps(name, ensure_ascii=False)))
        self.resource_tree.rename_tag_requested.connect(self.rename_tag)
        self.resource_tree.delete_tag_requested.connect(self.delete_tag)

        self.hint = QtWidgets.QLabel()
        self.hint.setWordWrap(True)
        layout.addWidget(self.hint)

        self.scene.metadata_changed.connect(self._on_metadata_changed)

    def _install_resource_service(self):
        project_id = self.scene.project_id
        nodes = getattr(self.scene, 'workspace_nodes', []) or []
        if not nodes:
            nodes = legacy_page_nodes(
                self._pages_with_implicit_defaults(), project_id)
        self.resource_service = ResourceTreeService(project_id, nodes, self)
        self.scene.workspace_nodes = self.resource_service.nodes
        self.resource_service.changed.connect(self._on_resource_nodes_changed)
        self.resource_tree.setModel(ResourceTreeModel(
            self.resource_service, self.resource_tree))
        self.resource_tree.expandAll()

    def _on_resource_nodes_changed(self, _change):
        self.scene.workspace_nodes = self.resource_service.nodes
        self.resource_nodes_changed.emit(_change)
        self.view.mark_content_dirty()
        self.search_content_changed()

    def search_content_changed(self):
        if self.search.text().strip():
            self._search_timer.start()

    def _clear_search_or_filters(self):
        if self.search.text().strip():
            self.search.clear()
        else:
            self.clear_filter()

    def _on_project_search(self, text):
        active = bool(text.strip())
        window = self.view.window()
        if active and not getattr(self, '_search_origin', None):
            self._search_origin = (getattr(window, 'current_page_kind', 'canvas'),
                getattr(window, 'current_page_id', None), self.view.transform(),
                self.view.mapToScene(self.view.viewport().rect().center()))
            self._search_origin_panel = None
            self._search_origin_view = None
            if hasattr(window, 'tabs'):
                panel = window.tabs.currentWidget()
                if hasattr(panel, 'capture_search_view'):
                    self._search_origin_panel = panel
                    origin = self._search_origin
                    def captured(state):
                        if getattr(self, '_search_origin', None) is origin:
                            self._search_origin_view = state
                    panel.capture_search_view(captured)
        self.resource_tree.setVisible(not active)
        self.search_results.setVisible(active)
        self.rating_label.setVisible(not active)
        self.rating_filter.setVisible(not active)
        self.clear_button.setText('退出搜索 / 返回原位置' if active else _('Show All / Clear Filters'))
        if active:
            self._search_timer.start()
        else:
            self._search_timer.stop()
            self.search_results.clear()
            for item in getattr(self, '_search_pinned_items', []):
                if not sip.isdeleted(item):
                    item._keep_visible = False
            self._search_pinned_items = []
            origin = getattr(self, '_search_origin', None)
            self._search_origin = None
            if origin and origin[1] and hasattr(window, 'open_workspace_page'):
                window.open_workspace_page(origin[0], origin[1])
                if origin[0] == 'canvas':
                    self.view.setTransform(origin[2])
                    self.view.centerOn(origin[3])
                else:
                    panel = getattr(self, '_search_origin_panel', None)
                    if panel is not None and not sip.isdeleted(panel):
                        panel.restore_search_view(getattr(self, '_search_origin_view', None))
            self.update_counts()

    def _refresh_project_search(self):
        from prism.project_search import search_project
        if not self.search.text().strip() or not self._scene_alive():
            return
        window = self.view.window()
        panels = list(getattr(window, '_page_panels', {}).values())
        panels += [p for p in (getattr(window, 'document_panel', None),
                               getattr(window, 'mindmap_panel', None)) if p is not None]
        query, expression = self._split_query(self.search.text())
        results = search_project(self.scene, self.resource_service, query, panels, expression,
                                 self._pages_with_implicit_defaults())
        self.search_results.clear()
        titles = {'asset': '素材', 'canvas': '画布', 'mindmap': '脑图',
            'mindmap_node': '脑图节点', 'document': '文档', 'document_text': '文档正文',
            'folder': '文件夹', 'tag': '标签'}
        groups = {}
        for result in results[:200]:
            kind = result['kind']
            if kind not in groups:
                group = QtWidgets.QTreeWidgetItem([titles[kind]])
                group.setFlags(QtCore.Qt.ItemFlag.ItemIsEnabled)
                self.search_results.addTopLevelItem(group)
                groups[kind] = group
            row = QtWidgets.QTreeWidgetItem(groups[kind], [result['title'] + '\n' +
                result.get('path', '') + ('\n' + result['snippet'] if result['snippet'] else '')])
            row.setData(0, QtCore.Qt.ItemDataRole.UserRole, result)
            row.setToolTip(0, row.text(0))
        self.search_results.expandAll()
        self.hint.setText(f'全项目：{len(results)} 个结果' + ('（显示前 200 项）' if len(results) > 200 else ''))

    def _activate_search_result(self, row, *_args):
        result = row.data(0, QtCore.Qt.ItemDataRole.UserRole)
        if not result:
            return
        window = self.view.window()
        kind = result['kind']
        if kind == 'tag':
            self.search.setText('tag:' + json.dumps(result['tag'], ensure_ascii=False))
            return
        if kind == 'folder':
            self._search_origin = None
            self.search.clear()
            index = self.resource_tree.model().index_for_id(result['node_id'])
            parent = index
            while parent.isValid():
                self.resource_tree.expand(parent)
                parent = parent.parent()
            self.resource_tree.setCurrentIndex(index)
            self.resource_tree.scrollTo(index)
            return
        page_id = result.get('page_id')
        if hasattr(window, 'open_workspace_page'):
            window.open_workspace_page(result.get('page_kind', 'canvas'), page_id or window._first_canvas_id())
        if kind == 'asset':
            item = result['item']
            if sip.isdeleted(item):
                self._refresh_project_search()
                return
            item._keep_visible = True
            if not hasattr(self, '_search_pinned_items'):
                self._search_pinned_items = []
            self._search_pinned_items.append(item)
            item.setVisible(True)
            self.scene.clearSelection()
            item.setSelected(True)
            self.view.centerOn(item)
        elif kind in ('document_text', 'mindmap_node'):
            panel = getattr(window, '_page_panels', {}).get(page_id)
            if panel is None:
                panel = getattr(window, 'document_panel' if kind == 'document_text' else 'mindmap_panel', None)
            if panel is not None:
                panel.locate_search_result(result.get('match_text', '') if kind == 'document_text' else
                    {'path': result['node_path'], 'uid': result.get('node_uid')})

    # ── Navigation helpers ────────────────────────────────────

    def _item_canvas(self, item):
        """Canvas a graphics item belongs to, or None for non-media items."""
        return getattr(item, '_canvas_id', None)

    def _canvas_item_count(self, canvas_id):
        return sum(1 for item in self.scene.items()
                   if self._item_canvas(item) == canvas_id)

    def canvas_at(self, global_pos):
        """这个屏幕点上是不是一个画布页节点？是就返回它的 pageId。

        画布上拖素材到左栏的判定用它：命中走 `QTreeView.indexAt`，
        只有落在**画布分区的一个页面行**上才算 —— 文件夹、标签、
        文档/脑图分区都不接受素材。别根据"最后选中的那一行"猜。
        """
        tree = getattr(self, 'resource_tree', None)
        if tree is None:
            return None
        spot = tree.viewport().mapFromGlobal(global_pos)
        if not tree.viewport().rect().contains(spot):
            return None
        index = tree.indexAt(spot)
        if not index.isValid():
            return None
        if (index.data(SECTION_ROLE) != 'canvas'
                or index.data(NODE_TYPE_ROLE) != 'page'):
            return None
        node = self.resource_service._by_id(index.data(NODE_ID_ROLE))
        return node.get('pageId') if node else None

    def move_items_to_canvas(self, items, page_id):
        """把素材归到另一个画布页（从画布拖到左栏的画布行）。

        只改归属：`_canvas_id` 走撤销栈（`ChangeMetadata`），Ctrl+Z 能把
        它们送回原来的画布。移过去之后它们会在当前画布上消失、到目标
        画布才出现 —— 那是 `_apply_filter` 里画布归属这条规则在管。
        """
        items = [item for item in items
                 if hasattr(item, 'save_id')
                 and self._item_canvas(item) != page_id]
        if not items:
            return False
        self.scene.undo_stack.push(commands.ChangeMetadata(
            items, 'canvas_id', [page_id for _ in items]))
        self.update_counts()
        self._apply_filter()
        return True

    def _pages_with_implicit_defaults(self):
        """Return the page list, recreating a legacy page only if it holds data.

        The canvas/mind map/document entries used to be hard-coded, which left
        an empty tree looking permanently "full" and could not be removed.  A
        default page now appears only while the project still carries content
        that belongs to it, so a new document starts empty while an existing
        .prism keeps its canvas, mind map and document reachable.
        """
        pages = list(getattr(self.scene, 'workspace_pages', []))
        kinds = {page.get('kind') for page in pages}
        if 'canvas' not in kinds and self._canvas_item_count('default-canvas'):
            pages.insert(0, {'id': 'default-canvas', 'kind': 'canvas',
                             'title': '默认画布', 'tags': [],
                             'permanent': True})
        if 'mindmap' not in kinds and getattr(self.scene, 'mindmap_tree', None):
            pages.append({'id': 'default-mindmap', 'kind': 'mindmap',
                          'title': '默认脑图', 'tags': [], 'permanent': True})
        if 'document' not in kinds and getattr(self.scene, 'note_html', ''):
            pages.append({'id': 'default-document', 'kind': 'document',
                          'title': '默认文档', 'tags': [], 'permanent': True})
        return pages

    def _tag_nodes(self):
        """The project's tags as one tree: catalogue, items and page tags.

        Parents that only exist because a tag below them does show up too,
        so a nested tag always has a row to sit under, and synonyms are
        folded into the row of the name they belong to.
        """
        return tag_panel.project_tag_nodes(self.scene)

    def rebuild_workspace_tree(self):
        if self.resource_service.project_id != self.scene.project_id:
            self._install_resource_service()
        else:
            known = {n.get('pageId') for n in self.resource_service.nodes}
            for page in self._pages_with_implicit_defaults():
                if page['id'] not in known:
                    self.resource_service.createPage(
                        page['kind'], page['title'], page['id'])
        self.update_counts()

    # ── Count updates ─────────────────────────────────────────

    def update_counts(self):
        if not self._scene_alive():
            return
        nodes = self._tag_nodes()
        self.resource_tree.model().set_tags(nodes)
        names = {node.name for node in nodes}
        cat_names = set(self._load_categories())
        # 分类和标签都可能变了（改名、新用、加别名），右栏的分类下拉与
        # 标签面板要跟着刷新。旧列表还在的时候这里比的是行对象，列表
        # 删掉之后直接比集合。
        changed = (getattr(self, '_tag_node_names', set()) != names
                   or getattr(self, '_cat_names', set()) != cat_names)
        self._tag_node_names = names
        self._cat_names = cat_names
        counts = self._calc_counts()
        # A recount is not the user changing the filter, so a photo that was
        # pinned visible (「在画布中定位」) may stay visible through it.
        self._apply_filter(keep_pinned=True)
        self.counts_changed.emit(counts)
        self.search_content_changed()
        if changed:
            self.categories_changed.emit()

    def _calc_counts(self):
        """Count items per category."""
        if not self._scene_alive():
            return {'categories': {}, 'tags': {}}
        result = {'categories': {}, 'tags': self.scene.get_all_tags()}
        for c in self._load_categories():
            result['categories'][c] = 0
        for item in self.scene.items():
            if not hasattr(item, 'save_id'):
                continue
            for cat in getattr(item, '_categories', []):
                if cat in result['categories']:
                    result['categories'][cat] += 1
        return result

    # ── Filtering (batched for performance) ───────────────────

    def _on_rating_filter(self, index):
        self._min_rating = int(index)
        self._apply_filter()

    def clear_filter(self):
        """Clear every filter and let the whole board show again.

        分类筛选的界面入口随「筛选与旧分类」一起删掉了，所以这里不再
        "落到第一个分类" —— 清空就是真的清空，没有藏着的筛选了。
        """
        self._active_filter = None
        self._active_tags.clear()
        self._min_rating = 0
        self._untagged_only = False
        self.search.clear()
        self.rating_filter.setCurrentIndex(0)
        self.update_counts()

    def _split_query(self, text):
        """把搜索框里的内容分成"文字搜索"和"标签查询"两半。

        用户问过"标签查询功能是不是和搜索功能重叠了"—— 界面上两个盒子
        确实让人分不清。合成一个，但**能力一点没少**：需要标签那套语法时
        照旧能写。

        分派规则（`_TAG_SYNTAX` 那个判定故意保守）：

            tag:古风       -> 标签查询（前缀强制）
            "古风 的意境"   -> 文字（引号强制）
            场景:*         -> 标签查询（有通配符）
            雨夜 | 红墙      -> 标签查询（有或号）
            红墙 -草图      -> 标签查询（有排除）
            古风           -> 文字（普通词，也是最常见的用法）
            雨夜 红墙       -> 文字，但**多个词都要命中**（空格 = 与）

        最后那条让"空格=与"在两边行为一致：文字搜索也走多词 AND，所以
        用户敲空格的意思不会因为它在哪个盒子里而变。
        """
        text = (text or '').strip()
        if not text:
            return '', ''
        if text.lower().startswith('tag:'):
            return '', text[4:].strip()
        if len(text) >= 2 and text.startswith('"') and text.endswith('"'):
            return text[1:-1].strip(), ''
        if _TAG_SYNTAX.search(text):
            return '', text
        return text, ''

    def _apply_filter(self, keep_pinned=False):
        """Batched visibility update: compute first, apply with signals blocked.

        ``keep_pinned`` says whether an item the user explicitly asked for -
        see ``_keep_visible`` - may be hidden again.  A recount that runs on
        its own must not undo 「在画布中定位」; a filter the user just changed
        must.
        """
        if not self._scene_alive():
            return
        active = self._active_filter
        # The project search box does not change canvas visibility.
        query, tag_expression = '', ''
        query = query.casefold()
        active_tags = {str(name).casefold() for name in self._active_tags}
        tag_query = tags.parse_query(tag_expression)
        tag_system = tags.tag_system(self.scene)
        # 画布之间是隔开的：素材只在自己归属的画布上露面（拖到左栏另一个
        # 画布就是把归属改过去，见 `move_items_to_canvas`）。归属不明的
        # 素材 —— 没有 canvasId，或者指向的页面已经不在工程里 —— 不参与
        # 这条判定：宁可多显示，也不能让旧工程的素材凭空消失。
        current_canvas = getattr(self.view, 'current_canvas_id', None)
        known_canvases = {
            page['id'] for page in self._pages_with_implicit_defaults()
            if page.get('kind') == 'canvas'}

        # Phase 1: compute visibility (no scene mutations)
        changes = []
        visible = 0
        for item in self.scene.items_for_save():
            # Annotations ignore media filters, but still belong to one canvas.
            if getattr(item, 'TYPE', '') in ANNOTATION_TYPES:
                canvas = self._item_canvas(item) or 'default-canvas'
                show = canvas == current_canvas
                if item.isVisible() != show:
                    changes.append((item, show))
                continue
            show = True
            canvas = self._item_canvas(item)
            if canvas is not None and canvas in known_canvases:
                show = canvas == current_canvas
            # 搜索时**忽略分类筛选**。
            #
            # 用户的原话："我搜索一个标签，他应该展示的是我这个项目里所有
            # 分类里这个标签的图片"。以前选了某个分类之后，搜索就只在那个
            # 分类里找 —— 别处明明有也看不到。
            #
            # 清空搜索框之后 `query` 变空，分类筛选自然又生效（所以"搜完
            # 回到当前分类"是这个条件的副产品，不用另外写）。
            if active and active[0] == 'category' and not query:
                selected = active[1]
                cats = getattr(item, '_categories', [])
                # 选父分类要**包含所有子分类的素材**。用户要三级分类，
                # 点"古风"的时候"古风/山水"里的图当然也该出来 ——
                # 不然父分类那个数字（自己 + 子分类）和看到的东西对不上。
                show = show and any(
                    str(cat) == selected
                    or tags.is_descendant(str(cat), selected)
                    for cat in cats)
            # 标签勾选行和「同时包含 / 任意包含」下拉都随红框一起删掉了
            # （2026-10-01），但状态和这条分支留着：多选标签的语义固定是
            # **同时包含**（够用，也是原来的默认值），语法 `tag:…` 和 `-*`
            # 仍然在搜索框里可用。
            if active_tags or tag_query:
                # One index per item: the tag itself, every level above it,
                # the names it implies and every synonym of all of those.
                item_tags = getattr(item, '_tags', [])
                index = tags.name_index(item_tags, tag_system)
                if active_tags:
                    hits = sum(1 for name in active_tags if name in index)
                    show = show and hits == len(active_tags)
                if show and tag_query:
                    show = tag_query.matches(index)
            if self._untagged_only and getattr(item, '_tags', []):
                # 「只看没有标签的素材」：挂了任何标签的都不算。
                show = False
            if self._min_rating:
                show = show and getattr(item, '_rating', 0) >= self._min_rating
            if query:
                text = ' '.join(
                    str(getattr(item, f, '') or '')
                    for f in ('_title', 'filename', '_notes'))
                text += ' ' + ' '.join(getattr(item, '_tags', []))
                text = text.casefold()
                # 多个词**都要命中**（空格 = 与）。整串匹配的话"雨夜 红墙"
                # 永远搜不到东西 —— 而用户敲空格的意思就是"两个都要"，
                # 标签查询那边也是这么理解的。两边行为一致，用户不用记
                # "空格在哪个盒子里是什么意思"。
                words = query.split()
                show = show and all(word in text for word in words)
            # A photo the user asked for by name stays visible.  「查重里点
            # 在画布中定位」sets this: the filter often hides exactly the
            # photo being looked for, and without this the recount that runs
            # 300 ms later puts it back out of sight - the user sees it flash
            # and vanish.  The exemption ends as soon as the filter would
            # show it anyway, or as soon as the user touches the filter
            # again (that is what ``keep_pinned`` tells apart).
            pinned = getattr(item, '_keep_visible', False)
            if pinned and (show or not keep_pinned):
                item._keep_visible = False
                pinned = False
            if item.isVisible() != show and not (pinned and not show):
                changes.append((item, show))
            visible += bool(show or pinned)

        # Phase 2: batch apply with signals blocked
        if changes:
            vp = self.view.viewport() if hasattr(self.view, 'viewport') else None
            if vp:
                vp.setUpdatesEnabled(False)
            self.scene.blockSignals(True)
            try:
                for item, show in changes:
                    item.setVisible(show)
            finally:
                self.scene.blockSignals(False)
                if vp:
                    vp.setUpdatesEnabled(True)
                self.scene.changed.emit([QtCore.QRectF()])

        self.hint.setText(
            _('{count} items shown').format(count=visible)
            if visible else
            _('No matching items'))
        if self.search.text().strip():
            self.search_content_changed()

    # ── Category management ───────────────────────────────────

    def categories(self):
        """工程里已有的分类，公开接口。

        view.py 原来直接调 _load_categories() —— 跨对象调私有方法是界面层
        互相穿透的典型例子，也让「谁能问分类」变得说不清。
        """
        return self._load_categories()

    def _load_categories(self):
        names = list(self.scene.category_names)
        for item in self.scene.items_for_save():
            for name in getattr(item, '_categories', []):
                if name and name not in names:
                    names.append(name)
        return names

    def _save_categories(self, categories):
        self.scene.category_names = list(categories)

    def add_category(self, parent=None):
        """新建分类。名字里带 `/` 会顺带把祖先建出来。

        用户要三级分类 —— 输入 `古风/山水/秋雨` 就该一次把三层都建好，
        而不是让人一层层点。`parent` 是"新建子分类"那条右键项传进来的，
        它只用来预填名字。

        旧工程的分类不含 `/`，`split_path` 返回单元素，所以这条路径和
        以前完全一样。
        """
        title = '新建分类' if not parent else '新建子分类'
        default = f'{parent}/' if parent else ''
        name, ok = QtWidgets.QInputDialog.getText(
            self, title, '分类名称：', text=default)
        if not ok or not name.strip():
            return
        parts = tags.split_path(name)
        if not parts:
            return
        cats = self._load_categories()
        # 每一级祖上都建出来（`古风/山水` 要保证 `古风` 也在目录里）
        wanted = [tags.join_path(parts[:depth])
                  for depth in range(1, len(parts) + 1)]
        new = [item for item in wanted if item not in cats]
        if not new:
            QtWidgets.QMessageBox.warning(
                self, '提示', '该分类已存在')
            return
        self.scene.undo_stack.push(ChangeCategory(self, cats + new))

    # ── Tags ──────────────────────────────────────────────────

    def refresh_tags(self):
        """Refresh after the tags themselves changed.

        The active filters are deliberately left alone: organising tags
        must not throw away what the user is currently looking at.
        """
        if not self._scene_alive():
            return
        self.update_counts()
        self._apply_filter()

    def manage_tags(self):
        """Open the tag manager and stay in step while it is used."""
        dialog = tag_panel.TagPanel(self.view, self)
        dialog.exec()
        self.refresh_tags()

    def _carry_active_tag(self, old_name, new_name):
        """Keep the checkboxes honest when a tag is renamed or moved."""
        for name in list(self._active_tags):
            if name == old_name or tags.is_descendant(name, old_name):
                self._active_tags.discard(name)
                self._active_tags.add(
                    tag_panel.rename_prefix(name, old_name, new_name))

    def add_tag(self):
        # The tree's tag menu and the inspector create tags; assigning a
        # tag to assets stays on the selection, not on the tree.
        if tag_panel.ask_create_tag(self.view, self, parent_tag=None):
            self.refresh_tags()

    def rename_tag(self, old_name):
        """Rename one tag everywhere it is used.

        Tags live on the items themselves, so a rename rewrites every item
        that carries the old name - pushing it through the undo stack keeps
        that reversible.
        """
        new_name = tag_panel.ask_rename_tag(self.view, old_name, self)
        if not new_name:
            return
        self._carry_active_tag(old_name, new_name)
        self.refresh_tags()

    def set_tag_synonym(self, name):
        if tag_panel.ask_set_synonym(self.view, name, self):
            self.refresh_tags()

    def delete_tag(self, name):
        if not tag_panel.ask_delete_tag(self.view, name, self):
            return
        for active in list(self._active_tags):
            if active == name or tags.is_descendant(active, name):
                self._active_tags.discard(active)
        self.refresh_tags()

    def rename_category(self, old_name):
        new_name, ok = QtWidgets.QInputDialog.getText(
            self, '\u91cd\u547d\u540d\u5206\u7c7b',
            '\u65b0\u540d\u79f0\uff1a', text=old_name)
        if not ok or not new_name.strip() or new_name.strip() == old_name:
            return
        new_name = new_name.strip()
        cats = self._load_categories()
        if new_name in cats:
            QtWidgets.QMessageBox.warning(
                self, '\u63d0\u793a',
                '\u8be5\u5206\u7c7b\u5df2\u5b58\u5728')
            return

        old_parts = tags.split_path(old_name)

        def renamed(name):
            """改一层名字时，它下面的层级要跟着改。

            `古风` 改名成 `国风`，`古风/山水` 就该变成 `国风/山水` ——
            不然子分类会挂在半空中，而且点了也筛不出东西。和标签那边的
            `rename_prefix` 同一个道理。
            """
            if name == old_name:
                return new_name
            if tags.is_descendant(name, old_name):
                rest = tags.split_path(name)[len(old_parts):]
                return tags.join_path([new_name] + rest)
            return name

        self.scene.undo_stack.push(ChangeCategory(
            self,
            [renamed(one) for one in cats],
            old_name, new_name))

    def delete_category(self, name):
        # 删父分类**连带子分类一起删**。留着子分类的话它们会悬挂 ——
        # 目录里还有，但父级没了，缩进和计数都对不上。提示语说清带几个。
        children = [c for c in self._load_categories()
                    if c != name and tags.is_descendant(c, name)]
        extra = (f'\n（连同它下面的 {len(children)} 个子分类）'
                 if children else '')
        reply = QtWidgets.QMessageBox.question(
            self, '\u5220\u9664\u5206\u7c7b',
            f'\u786e\u5b9a\u5220\u9664\u5206\u7c7b\u300c{name}\u300d\u5417\uff1f{extra}\n'
            '\u5176\u4e2d\u7684\u7d20\u6750\u4f1a\u8f6c\u4e3a\u672a\u5206\u7c7b\uff0c'
            '\u7d20\u6750\u672c\u8eab\u4e0d\u4f1a\u5220\u9664\u3002')
        if reply != QtWidgets.QMessageBox.StandardButton.Yes:
            return
        self.scene.undo_stack.push(ChangeCategory(
            self,
            [c for c in self._load_categories()
             if c != name and c not in children],
            name))

    # ── Assign items to category (public API) ────────────────

    def assign_category(self, items, category_name):
        values = [category_name] if category_name else []
        items = [i for i in items if list(i.categories) != values]
        if items:
            self.scene.undo_stack.push(commands.ChangeMetadata(
                items, 'categories', [list(values) for i in items]))

    def build_category_menu(self, parent_menu):
        for cat in self._load_categories():
            act = parent_menu.addAction(cat)
            act.triggered.connect(
                lambda checked=False, c=cat: self._do_assign(c))
        parent_menu.addSeparator()
        parent_menu.addAction(
            '\u79fb\u81f3\u672a\u5206\u7c7b',
            lambda: self._do_assign(None))
        parent_menu.addAction(
            '\u65b0\u5efa\u5206\u7c7b\u2026', self.add_category)

    def build_tag_menu(self, parent_menu):
        """The tag submenu of an item's context menu.

        The names come from the tag tree, so a parent that only exists
        because its children do can be applied as well.  The first line
        says which object is being tagged - the selection, not the page.
        """
        selected = self.scene.selectedItems(user_only=True)
        header = parent_menu.addAction(
            _('Tagging {count} selected assets').format(count=len(selected))
            if selected else _('Select assets first, then tag them'))
        header.setEnabled(False)
        parent_menu.addSeparator()
        for node in self._tag_nodes():
            label = node.name
            if node.aliases:
                label += ' (' + '、'.join(node.aliases) + ')'
            action = parent_menu.addAction(label)
            action.setCheckable(True)
            action.setChecked(bool(selected) and all(
                node.name in getattr(item, '_tags', []) for item in selected))
            action.triggered.connect(
                lambda checked=False, name=node.name:
                self._set_selected_tag(name, checked))
        parent_menu.addSeparator()
        parent_menu.addAction(_('New tag…'), self.add_tag)
        parent_menu.addAction(_('Manage tags…'), self.manage_tags)

    def _set_selected_tag(self, tag, enabled):
        items = self.scene.selectedItems(user_only=True)
        values = []
        for item in items:
            own = set(getattr(item, '_tags', []))
            own.add(tag) if enabled else own.discard(tag)
            values.append(sorted(own))
        if items:
            self.scene.undo_stack.push(
                commands.ChangeMetadata(items, 'tags', values))

    def _do_assign(self, category_name):
        selected = self.scene.selectedItems(user_only=True)
        if not selected:
            return
        self.assign_category(selected, category_name)

    def force_rebuild(self):
        # Loading/clearing a project must not navigate back into the old one.
        self._search_origin = None
        self.search.clear()
        self.update_counts()
        self.categories_changed.emit()

    def _on_metadata_changed(self):
        self.update_counts()


class ChangeCategory(QtGui.QUndoCommand):
    """Keep catalogue, item membership and current filter together in undo."""
    def __init__(self, panel, names, old_name=None, new_name=None):
        super().__init__('\u4fee\u6539\u5206\u7c7b')
        self.panel = panel
        self.before_names = panel._load_categories()
        self.after_names = list(names)
        self.before_filter = panel._active_filter
        self.after_filter = self.before_filter
        if old_name and self.before_filter == ('category', old_name):
            self.after_filter = (
                ('category', new_name) if new_name else None)

        old_parts = tags.split_path(old_name) if old_name else []

        def touches(cat):
            """这个名字受影响吗 —— **带路径的也算**。

            改名 `古风` 时，素材上挂的 `古风/山水` 也要跟着动。只比精确
            相等的话，目录里改好了而素材上还指着旧名字，那个子分类就
            悬挂了 —— 点了筛不出东西，删了也删不干净。
            """
            if not old_name:
                return False
            cat = str(cat)
            return cat == old_name or tags.is_descendant(cat, old_name)

        def renamed(cat):
            if not touches(cat):
                return cat
            if not new_name:
                return None              # 删除：整条摘掉
            if cat == old_name:
                return new_name
            rest = tags.split_path(cat)[len(old_parts):]
            return tags.join_path([new_name] + rest)

        self.items = [i for i in panel.scene.items_for_save()
                      if any(touches(c) for c in i.categories)]
        self.before = [list(i.categories) for i in self.items]
        self.after = [
            [r for r in (renamed(c) for c in cats) if r]
            for cats in self.before]

    def _apply(self, names, values, active):
        self.panel.scene.category_names = list(names)
        for item, cats in zip(self.items, values):
            item._categories = list(cats)
        self.panel._active_filter = active
        self.panel.scene.metadata_changed.emit()
        self.panel.categories_changed.emit()

    def redo(self):
        self._apply(self.after_names, self.after, self.after_filter)

    def undo(self):
        self._apply(self.before_names, self.before, self.before_filter)
