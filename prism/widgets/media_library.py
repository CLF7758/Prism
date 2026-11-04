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
import time

from PyQt6 import QtCore, QtGui, QtWidgets, sip

from prism import commands, page_selection, tags
from prism.count_contract import (
    CountPublication, LIBRARY_TOTAL, LIBRARY_UNTAGGED, LIBRARY_UNCLASSIFIED,
    LIBRARY_TRASH, LIBRARY_VISIBLE, SELECTION_COUNT, RESULT_COUNT,
    BASIS_CATALOG, BASIS_RESIDENT, BASIS_RESULT_ROWS,
    BASIS_TAG_ASSET, BASIS_TAG_MATERIAL, BASIS_TAG_PAGE,
    next_request_token)
from prism.i18n import _
from prism.project_catalog import (
    ProjectAssetCatalog, AssetResult, CatalogNotReady,
    UNASSIGNED_CANVAS, UNASSIGNED_LABEL)
from prism.widgets import components, tag_panel
from prism.widgets.resource_tree_model import (
    NODE_ID_ROLE, NODE_TYPE_ROLE, SECTION_ROLE, ResourceTreeModel)
from prism.widgets.resource_tree_view import ResourceTreeView
from prism.workspace_service import ResourceTreeService, legacy_page_nodes
from prism.project_search import plain_text as plain_text_of, text_of_tree
from prism.item_lifecycle import is_deleted


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

#: 「移动到画布…」命令的快捷键。Ctrl+Shift+M 已经被 toggle_side_panel
#: 占用（actions.py 里写着同样的话）—— 两个动作抢一个键只有一个能触发，
#: 另一个等于失灵，所以这里另选一个没人用的。
MOVE_TO_CANVAS_SHORTCUT = 'Ctrl+Shift+C'

#: 关键词搜索防抖**按规模分档**。
#:
#: 以前是固定 180 ms：工程小的时候白等，工程大的时候又挡不住连击。
#: 规模的口径是「这次关键词检索要扫多少条素材」——项目里的素材总数
#: （左栏「全部」那个数字，`ProjectAssetCatalog.count_snapshot()` 的
#: `total`）。分档的**间隔**沿用 Eagle 的四档数值
#: （50 / 200 / 250 / 300 ms），**阈值**按 Prism 的素材规模自己定：
#:
#:   档    素材数             间隔 ms   依据
#:   S     ≤ 500               50      一次检索是毫秒级；只要挡住连击
#:   M     501 – 5 000        200      几千条要扫一遍，等一下更划算
#:   L     5 001 – 50 000     250      几万条已经是几十毫秒量级
#:   XL    > 50 000           300      十万级：一次全项目检索就是百毫秒级
#:
#: 规模**未知**（按页打开、目录还没建好，计数是 None）时用最大的一档：
#: 证明不了它小，就按大的算。
SEARCH_DEBOUNCE_TIERS = (
    (500, 50),
    (5000, 200),
    (50000, 250),
)
SEARCH_DEBOUNCE_MAX = 300


def search_debounce_interval(scale):
    """The keyword-search debounce (ms) for a project of ``scale`` assets.

    ``scale`` is the number of assets the search has to look at.  ``None``
    means "not known yet" and gets the longest interval.
    """
    if scale is None:
        return SEARCH_DEBOUNCE_MAX
    try:
        scale = int(scale)
    except (TypeError, ValueError):
        return SEARCH_DEBOUNCE_MAX
    for limit, interval in SEARCH_DEBOUNCE_TIERS:
        if scale <= limit:
            return interval
    return SEARCH_DEBOUNCE_MAX


class MediaLibraryPanel(QtWidgets.QWidget):
    counts_changed = QtCore.pyqtSignal(dict)
    #: The immediate, **partial** publication: the counters that can be
    #: verified from the loaded scene right now, published under the current
    #: revision while the whole project counters are still being recomputed.
    #: Only the status bar listens to it - the sidebar numbers and the colour
    #: bar wait for the debounced recount.
    counts_previewed = QtCore.pyqtSignal(dict)
    categories_changed = QtCore.pyqtSignal()
    workspace_requested = QtCore.pyqtSignal(str, str)
    #: 左栏一级分类行被点了；参数是分区 key。分类行没有内容，窗口去解析它的第一个内容。
    section_requested = QtCore.pyqtSignal(str)
    material_library_requested = QtCore.pyqtSignal(object)
    model_library_requested = QtCore.pyqtSignal()
    material_records_changed = QtCore.pyqtSignal(object)
    resource_create_requested = QtCore.pyqtSignal(str, str, object)
    resource_delete_requested = QtCore.pyqtSignal(str)
    #: 右键「导出…」：参数是 (分区, 节点 id)；节点 id 为 None 表示整个分区。
    resource_export_requested = QtCore.pyqtSignal(str, object)
    #: 右键「导入…」：参数同上；内容落在这一项下面。
    resource_import_requested = QtCore.pyqtSignal(str, object)
    resource_nodes_changed = QtCore.pyqtSignal(object)
    resource_usage_changed = QtCore.pyqtSignal()
    resource_records_changed = QtCore.pyqtSignal(object)

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
        # 关键词检索要扫的素材数（规模口径）。None = 还不知道（按页打开、
        # 目录没建好，计数是 None）。防抖分档用它 —— 见
        # `search_debounce_interval`。
        self._search_scale = None
        # 结果模式（方案 §6.3）：None=浏览资源目录；'all'=全项目素材；
        # 'untagged'=全项目未标签；'search'=搜索框驱动的综合项目搜索。
        # 三种结果模式都复用 search_results 树控件，但渲染内容和返回行为不同。
        self._result_mode = None
        # 全项目素材查询服务（方案 §5.2）。在 _install_resource_service 里
        # 创建，因为需要 resource_service 来解析画布路径。
        self.catalog = None
        # 隐式默认画布（全项目口径的每画布计数）。None = **还不知道**，
        # 不是 0：目录没就绪时无法证明默认画布是空的，而「未知」当成「空」
        # 会让旧工程的素材凭空消失。刷新点与计数刷新同一个，见
        # `_update_navigation` 与 `_install_resource_service`。
        self._implicit_default_canvases = None
        self._setup_ui()
        self._recount_timer = QtCore.QTimer(self)
        self._recount_timer.setObjectName('library.recount')
        self._recount_timer.setSingleShot(True)
        # 计数防抖：**不要**改成 0 ms。0 ms 只是把防抖拿掉，不是优化：
        # 每一个事件循环回合都会跑一次完整重算（目录聚合 + 标签分布 + 标签树
        # + 场景过滤 + 颜色计数），连续 30 次编辑就会变成 30 次全量重算 ——
        # 那就是刷新风暴。300 ms 是**上限节流**：一轮重算结束后最多 300 ms
        # 才允许下一次，突发编辑会被合并成一次，最后一次也不会丢。
        # 真正的优化在别处：一次计算同时供侧栏、底部、颜色条和工具栏消费
        # （见 `update_counts` 里的 CountPublication），而不是把防抖调小。
        self._recount_timer.setInterval(300)
        self._recount_timer.timeout.connect(self.update_counts)
        #: 本次会话的**编辑修订**：每来一次可能改变计数的通知就 +1。
        #: 它和目录侧的修订一起构成 CountSnapshot 的两个修订号。
        self._count_revision = 0
        #: 最近一次发布的计数快照（CountPublication）。
        self._last_publication = None
        #: 预览发布也**按帧合并**：一批通知（比如导入 2000 张素材）只产生
        #: 一次预览发布。逐条发布会让预览自己变成 O(n^2) 的忙等。
        self._preview_timer = QtCore.QTimer(self)
        self._preview_timer.setObjectName('library.count.preview')
        self._preview_timer.setSingleShot(True)
        self._preview_timer.setInterval(0)
        self._preview_timer.timeout.connect(self.publish_local_counts)
        self.scene.assets_changed.connect(self._schedule_recount)
        self.scene.items_trashed.connect(self._schedule_recount)
        self.material_records_changed.connect(self._resource_records_updated)
        self.resource_records_changed.connect(self._resource_records_updated)
        self.resource_nodes_changed.connect(self._resource_records_updated)
        tag_panel.tag_signals().changed.connect(self.refresh_tags)
        self.update_counts()

    def _scene_alive(self):
        """Whether the scene's C++ object still exists.

        Qt emits scene signals while the scene is torn down and again while
        the interpreter shuts down. Without this check those late callbacks
        touch a deleted C++ object and log an unhandled RuntimeError.
        """
        return self.scene is not None and not is_deleted(self.scene)

    def _schedule_recount(self, *, bump_revision=True, preview=True):
        """Ask for one recount, merged with whatever is already pending.

        Every notification that can move a number bumps the session's edit
        revision first, so the publication that comes out of the recount can
        say *which* revision it answers.  The counters that can be verified
        from the loaded scene are published **right now** under that
        revision; the whole project counters wait for the directory and are
        merged into one debounced recount.  The timer only runs when it is
        idle: a burst of edits becomes one recount, and the last edit is
        never dropped - it either starts the timer or is covered by the one
        that is already pending.

        ``bump_revision=False`` is for a trigger that does **not** change
        anything (a published result set): the recount it asks for answers
        the revision that is already current.
        """
        if not self._scene_alive():
            return
        if getattr(self.view, '_project_opening', False):
            return
        if bump_revision:
            self._count_revision += 1
        if preview:
            # 预览也按帧合并：一批通知只发一次预览。
            self._preview_timer.start()
        if not self._recount_timer.isActive():
            self._recount_timer.start()

    def schedule_recount_after_open(self):
        """Publish the loaded project's counters after the first frame.

        The open must not wait for the whole library directory: the counts
        are published as soon as the project is usable, and the merge keeps
        one recount for a burst.  The panel side of this is in place; the
        call site (``prism/view.py``'s open finalisation) belongs to AI-02
        and is handed over as a patch in ``BACKLOG.md``.
        """
        if not self._scene_alive():
            return
        QtCore.QTimer.singleShot(0, self._schedule_recount)

    def _open_epoch(self):
        """The generation of the open project.

        Reopening the same project is a new generation: a publication of
        the previous one must not be applied to it.
        """
        return getattr(self.view, '_trace_open_epoch', None)

    def _selection_count(self):
        """How many items the user has selected (scene scope)."""
        try:
            return len(self.scene.selectedItems(user_only=True))
        except (RuntimeError, AttributeError):
            return None

    def _resident_visible_count(self):
        """How many loaded assets are visible right now.

        The **same rule** the catalog uses for ``visible`` (see
        ``ProjectAssetCatalog._catalog_count_snapshot``): an active asset
        whose canvas is not deleted and that is currently visible.  The
        catalog's own number is checked against this one in the probe, so a
        drift between the two would be caught instead of shown.
        """
        catalog = self.catalog
        if catalog is None:
            return None
        deleted_pages = set()
        service = getattr(self, 'resource_service', None)
        if service is not None:
            for node in service.trashed_nodes():
                page_id = node.get('pageId')
                if page_id:
                    deleted_pages.add(page_id)
        count = 0
        for item in catalog.asset_items():
            if not catalog.is_active(item):
                continue
            if getattr(item, '_canvas_id', None) in deleted_pages:
                continue
            if item.isVisible():
                count += 1
        return count

    def publish_local_counts(self):
        """Publish what can be verified locally, right now.

        The whole project counters need the directory, so they keep their
        last confirmed value and are marked **pending** - the UI has to say
        that they do not answer the current revision yet.  The counters
        that only need the loaded scene (visible, selected) are recomputed
        here and now, under the same revision, so the bottom bar answers
        the user's action without waiting for the directory.
        """
        previous = getattr(self, '_last_publication', None)
        if previous is not None and previous.is_stale(
                project=self.scene.project_id, open_epoch=self._open_epoch()):
            previous = None
        counts = dict(previous.counts) if previous is not None else {}
        bases = dict(previous.bases) if previous is not None else {}
        pending = set()
        for kind in (LIBRARY_TOTAL, LIBRARY_UNTAGGED,
                     LIBRARY_UNCLASSIFIED, LIBRARY_TRASH):
            if kind in counts:
                pending.add(kind)
        visible = getattr(self, '_filter_visible', None)
        if visible is None:
            # The filter has not run since the last change: the last
            # confirmed number is the best we have, and it does not answer
            # this revision yet.
            visible = counts.get(LIBRARY_VISIBLE)
            pending.add(LIBRARY_VISIBLE)
        counts[LIBRARY_VISIBLE] = visible
        bases[LIBRARY_VISIBLE] = BASIS_RESIDENT
        counts[SELECTION_COUNT] = self._selection_count()
        bases[SELECTION_COUNT] = BASIS_RESIDENT
        publication = CountPublication(
            request_token=next_request_token(),
            project=self.scene.project_id,
            query=self._count_query_signature(),
            source_revision=(previous.source_revision
                             if previous is not None else None),
            overlay_revision=self._count_revision,
            open_epoch=self._open_epoch(),
            complete=(previous.complete if previous is not None else False),
            counts=counts,
            tag_counts=previous.tag_counts if previous is not None else {},
            bases=bases,
            pending=frozenset(pending),
            tag_counts_by_basis=(previous.tag_counts_by_basis
                                 if previous is not None else {}),
        )
        self._last_publication = publication
        self.counts_previewed.emit({'snapshot': publication})
        return publication

    def _setup_ui(self):
        self.setObjectName('prismSidebar')
        self.setAttribute(QtCore.Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setMinimumWidth(200)
        self.setMaximumWidth(16777215)

        layout = QtWidgets.QVBoxLayout(self)
        layout.setContentsMargins(12, 14, 12, 12)
        layout.setSpacing(8)

        # 方案需求 5：整行"Prism 图标 + Prism 文字 + 加号按钮"品牌区删除。
        # 新建页面/文件夹的入口在资源树右键菜单里（`create_requested`），
        # 品牌行那个加号是重复入口，一并去掉。
        # （这里原来 import 了 icon_button/tinted_icon 供品牌行使用，
        #   品牌行删除后这两个已无人调用；`icon` 在下面搜索框处再导入。）

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
        self.search_kind = 'all'
        self.search_type = self.make_search_type_combo()
        search_row = QtWidgets.QHBoxLayout()
        search_row.setSpacing(4)
        search_row.addWidget(self.search_type)
        search_row.addWidget(self.search, 1)
        layout.addLayout(search_row)
        self._navigation = QtWidgets.QButtonGroup(self)
        self._navigation.setExclusive(True)
        self._nav_counts = {}
        self._nav_buttons = {}
        navigation = QtWidgets.QWidget()
        navigation_layout = QtWidgets.QVBoxLayout(navigation)
        navigation_layout.setContentsMargins(0, 0, 0, 0)
        navigation_layout.setSpacing(2)
        for key, name, glyph in (
                ('all', _('All assets'), 'library'),
                ('unclassified', '未分类', 'unclassified'),
                ('untagged', '未标签', 'tag-outline'),
                ('recent', '最近使用', 'clock'),
                ('trash', '回收站', 'trash')):
            row = QtWidgets.QWidget()
            row_layout = QtWidgets.QHBoxLayout(row)
            row_layout.setContentsMargins(0, 0, 6, 0)
            row_layout.setSpacing(4)
            button = QtWidgets.QToolButton()
            button.setObjectName('libraryNav')
            button.setText(name)
            button.setIcon(icon(glyph))
            button.setIconSize(QtCore.QSize(17, 17))
            button.setToolButtonStyle(QtCore.Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
            button.setSizePolicy(QtWidgets.QSizePolicy.Policy.Expanding,
                                 QtWidgets.QSizePolicy.Policy.Fixed)
            button.setFixedHeight(29)
            button.setToolTip('当前画布的素材' if key == 'all' else name)
            if key != 'trash':
                button.setCheckable(True)
                self._navigation.addButton(button)
            button.clicked.connect(lambda checked=False, target=key: self._navigate(target))
            row_layout.addWidget(button, 1)
            count = components.RoleLabel(role='secondary')
            row_layout.addWidget(count)
            self._nav_counts[key] = count
            self._nav_buttons[key] = button
            if key == 'all':
                self.all_button = button
                button.setChecked(True)
            elif key == 'untagged':
                self.untagged_button = button
            navigation_layout.addWidget(row)
        layout.addWidget(navigation)
        layout.addWidget(components.RoleLabel('资源库', role='treeSection'))
        clear_button = components.Button('清除筛选')
        self.clear_button = clear_button
        clear_button.clicked.connect(self._clear_search_or_filters)

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

        # 左栏现在只有这一棵树。「筛选与旧分类」那个旧列表页签按用户
        # 要求删掉了 —— 分类行、标签勾选行和「未打标签」行都随它一起
        # 消失，标签勾选筛选暂时没有界面入口（筛选能力还在代码里）。
        self.resource_tree = ResourceTreeView(self, settings=self.view.settings)
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
        self.resource_tree.open_material_requested.connect(
            self.material_library_requested.emit)
        self.resource_tree.open_section_requested.connect(
            self.section_requested.emit)
        self.resource_tree.open_model_requested.connect(
            self.model_library_requested.emit)
        self.resource_tree.open_folder_requested.connect(self._browse_folder)
        self.resource_tree.new_tag_requested.connect(self.add_tag)
        self.resource_tree.manage_tags_requested.connect(self.manage_tags)
        layout.addWidget(self.resource_tree, stretch=1)
        # 「移动到画布…」命令入口（拖拽之外的另一条路）。要在树建好之后
        # 装：右键菜单的入口是挂在这棵树上的。
        self._setup_move_to_canvas()
        # 「返回资源目录」按钮（方案 §6.1）：全项目结果模式下必须有一个
        # 明确的返回入口，不能进入结果后连返回都找不到。默认隐藏，只在
        # _result_mode 非 None 时显示。
        self.back_to_tree_button = components.Button('← 返回资源目录')
        self.back_to_tree_button.setObjectName('sidebarBackToTree')
        self.back_to_tree_button.clicked.connect(self._exit_result_mode)
        self.back_to_tree_button.hide()
        layout.addWidget(self.back_to_tree_button)
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
        self._search_timer.timeout.connect(self._on_search_timer)
        self.resource_tree.tag_requested.connect(self._browse_tag)
        self.resource_tree.rename_tag_requested.connect(self.rename_tag)
        self.resource_tree.delete_tag_requested.connect(self.delete_tag)

        self.hint = QtWidgets.QLabel()
        self.hint.setWordWrap(True)
        layout.addWidget(self.hint)

        self.filter_controls = QtWidgets.QWidget()
        filter_layout = QtWidgets.QVBoxLayout(self.filter_controls)
        filter_layout.addLayout(rating_row)
        filter_layout.addWidget(clear_button)
        self.filter_controls.hide()
        layout.addWidget(self.filter_controls)
        self.filter_button = components.LineEdit()
        self.filter_button.setPlaceholderText('筛选目录')
        self.filter_button.addAction(icon('filter'), QtWidgets.QLineEdit.ActionPosition.LeadingPosition)
        self.filter_button.textChanged.connect(self._filter_directories)
        self.filter_button.hide()
        self.counts_changed.connect(self._update_navigation)

        self.scene.metadata_changed.connect(self._on_metadata_changed)

    def _browse_folder(self, index):
        controller = getattr(self.view.window(), 'search_controller', None)
        if (index.data(NODE_TYPE_ROLE) != 'folder'
                or index.data(SECTION_ROLE) not in ('canvas', 'document', 'mindmap')):
            return
        if controller is not None and controller.active:
            controller.exit()
        window = self.view.window()
        if not hasattr(window, 'asset_browser'):
            return
        node_id = index.data(NODE_ID_ROLE)
        nodes = [self.resource_service._by_id(i) for i in self.resource_service._descendants(node_id)]
        pages = {n['pageId'] for n in nodes if n and n.get('pageId') and not n.get('deletedAt')}
        window.asset_browser.saved_rule = None
        window.asset_browser.search.clear()
        window.asset_browser.open('all' if index.data(SECTION_ROLE) == 'canvas' else 'pages',
                                  pages, str(index.data()))

    def _browse_tag(self, name):
        window = self.view.window()
        from prism.navigation import defer_navigation
        if defer_navigation(window, lambda: self._browse_tag(name), owner=self):
            return
        controller = getattr(window, 'search_controller', None)
        if controller is not None:
            visual = getattr(controller, 'visual_search', None)
            if controller.active or (visual is not None and visual.active):
                controller.exit(restore_origin=False)
        browser = getattr(window, 'asset_browser', None)
        if browser is None:
            self.set_project_query('tag:' + json.dumps(name, ensure_ascii=False), programmatic=True)
            return
        self.set_search_kind('all', refresh=False)
        self._active_tags = {name}
        self._active_filter = None
        self._min_rating = 0
        with QtCore.QSignalBlocker(browser.search):
            browser.search.clear()
        browser.saved_rule = None
        browser.open('all', title=name)

    def set_project_query(self, text, *, programmatic=False):
        controller = getattr(self.view.window(), 'search_controller', None)
        if controller is not None:
            controller.input.setText(text)
            if programmatic:
                controller.submit(programmatic=True)
            else:
                controller.submit()
        else:
            self.search.setText(text)

    def _filter_directories(self, text):
        needle = text.strip().casefold()
        model = self.resource_tree.model()
        def walk(parent):
            found = False
            for row in range(model.rowCount(parent)):
                index = model.index(row, 0, parent)
                children = walk(index)
                match = not needle or needle in str(index.data() or '').casefold() or children
                self.resource_tree.setRowHidden(row, parent, not match)
                if needle and children:
                    self.resource_tree.expand(index)
                found |= match
            return found
        walk(QtCore.QModelIndex())

    def _navigate(self, target):
        controller = getattr(self.view.window(), 'search_controller', None)
        if controller is not None and target != 'trash':
            self.set_search_kind('asset')
        if controller is not None and controller.active and target != 'trash':
            controller.set_scope('navigation', target, str(self._nav_buttons[target].text()))
            return
        if hasattr(self.view.window(), 'asset_browser'):
            # 五个口径是**同一张混排列表**的五种筛选：图片素材、画布、脑图、
            # 文档、材质、模型平级躺着，口径只决定「哪些在列表里」。
            browser = self.view.window().asset_browser
            browser.saved_rule = None
            browser.search.clear()
            self._active_tags.clear()
            self._min_rating = 0
            self._active_filter = None
            if controller is not None:
                controller.scope = ('project', None)
                controller.scope_combo.setCurrentIndex(0)
                controller.min_rating, controller.color, controller.active_tags = 0, None, set()
            browser.open(target)
        else:
            self.clear_filter()
            self._apply_filter()
            self._update_navigation()

    def resource_scope_options(self, query='', kind=None):
        """The same resource predicates for navigation totals and grid rows."""
        text, expression = self._split_query(query)
        return dict(text=text, tag_expression=expression,
                    active_tags=tuple(self._active_tags),
                    min_rating=int(self._min_rating or 0),
                    kind=kind if kind not in ('all', 'asset') else None)

    def _resource_scope_counts(self, image_counts=None):
        """左栏那五个口径的计数：全项目资源，混排。

        和点进去看到的那张列表是同一套口径 —— 数字和内容必须对得上。
        """
        from prism import project_resources
        try:
            browser = getattr(self.view.window(), 'asset_browser', None)
            scoped = (browser is not None and self.view.window().tabs.currentWidget() is browser
                      and browser.mode in project_resources.SCOPES)
            query = browser.search.text() if scoped else ''
            options = (self.resource_scope_options(query, self.search_kind) if scoped else
                       dict(text='',tag_expression='',active_tags=(),min_rating=0,kind=None))
            recent = project_resources.recent_entries(getattr(self.scene, 'project_id', ''))
            self._watch_recent_usage_refresh()
            scope = self.catalog._catalog_scope()
            pages = self._pages_with_implicit_defaults()
            domain_shape = (tuple((page.get('id'), page.get('kind'), page.get('title'),
                                  tuple(page.get('tags') or ())) for page in pages),
                id(self.scene.material_records), len(self.scene.material_records),
                id(self.scene.model_records), len(self.scene.model_records))
            key = (self.catalog, self.scene, self.scene.project_id,
                   getattr(self.scene, '_asset_revision', None),
                   self.resource_service.revision,
                   self.material_service.revision, self._count_revision,
                   getattr(browser, '_catalog_metadata_epoch', self.material_service.revision),
                   repr(scope.source_state()) if scope is not None else None,
                   repr(options), repr(recent),
                   browser._catalog_overlay_revision('recent') if browser is not None else None,
                   domain_shape)
            cached = getattr(self, '_resource_counts_cache', None)
            if cached is not None and cached[0] == key:
                return dict(cached[1])
            # Recent-use stamps and view filters do not change the resource
            # metadata. Reuse its search projection across those recounts.
            domain_key = self._resource_domain_key(pages, shape=domain_shape)
            domain_cached = getattr(self, '_resource_domain_cache', None)
            if domain_cached is None or domain_cached[0] != domain_key:
                domain_size = (len(pages) + len(self.scene.material_records)
                               + len(self.scene.model_records) + len(self.resource_service.nodes))
                if domain_size > 128:
                    self._schedule_resource_domains(pages, domain_key)
                    return {name: None for name in project_resources.SCOPES}
                domain_cached = (domain_key, project_resources.resource_domain_snapshot(
                    self.scene, self.resource_service, pages))
                self._resource_domain_cache = domain_cached
            counts = project_resources.scope_counts(
                self.scene, self.resource_service,
                recent=recent,
                catalog=self.catalog,
                image_counts=image_counts,
                page_records=pages, domain_snapshot=domain_cached[1],
                **options)
            self._resource_counts_cache = (key, dict(counts))
            return counts
        except CatalogNotReady:
            # A rebuilding directory has no authoritative whole-project totals.
            # Its ready notification refreshes these labels; do not query the
            # resident page or report a transient readiness state as an error.
            return {key: None for key in project_resources.SCOPES}
        except Exception:                       # noqa: BLE001 - logging only
            logger.exception('Whole-project resource totals are unavailable')
            return {'all': None, 'unclassified': None, 'untagged': None,
                    'recent': None, 'trash': None}

    def _watch_recent_usage_refresh(self):
        from prism.recent_usage import ledger
        future = ledger().refresh_future
        if future is None or future is getattr(self, '_usage_refresh_future', None):
            return
        self._usage_refresh_future = future
        if not hasattr(self, '_usage_refresh_signals'):
            from prism.widgets.material_task import TaskSignals
            from prism.qt_thread_safety import gui_signal_carrier
            self._usage_refresh_signals = gui_signal_carrier(TaskSignals(), QtCore.QCoreApplication.instance())
            self._usage_refresh_signals.result.connect(self._usage_refreshed)
        signals = self._usage_refresh_signals
        def refreshed(result):
            try:
                signals.result.emit(result.result())
            except Exception:
                logger.exception('Recent-use refresh failed')
        future.add_done_callback(refreshed)

    def _usage_refreshed(self, changed):
        if changed:
            self._usage_revision = getattr(self, '_usage_revision', 0) + 1
            self._resource_counts_cache = None
            self.resource_usage_changed.emit()
            self._schedule_recount(bump_revision=False, preview=False)

    def _resource_domain_key(self, pages, *, shape=None):
        browser = getattr(self.view.parent, 'asset_browser', None)
        if shape is None:
            shape = (tuple((page.get('id'), page.get('kind'), page.get('title'),
                            tuple(page.get('tags') or ())) for page in pages),
                     id(self.scene.material_records), len(self.scene.material_records),
                     id(self.scene.model_records), len(self.scene.model_records))
        return (self.scene, self.scene.project_id, self.resource_service.revision,
                self.material_service.revision, self._count_revision,
                getattr(browser, '_catalog_metadata_epoch', None), shape)

    def _cached_resource_domains(self, pages):
        cached = getattr(self, '_resource_domain_cache', None)
        if cached is not None and cached[0] == self._resource_domain_key(pages):
            return cached[1]
        return None

    def _schedule_resource_domains(self, pages, key):
        """Build a large first projection without making recount synchronous."""
        if getattr(self, '_resource_domain_failure', None) == key:
            return
        current = getattr(self, '_resource_domain_pending', None)
        if current is not None:
            if current['key'] == key:
                return
            current['timer'].stop()
            current['timer'].deleteLater()
            current['steps'].close()
        from prism import project_resources
        steps = project_resources.resource_domain_snapshot_steps(self.scene, self.resource_service, pages)
        timer = QtCore.QTimer(self)
        timer.setSingleShot(True)
        pending = dict(key=key, steps=steps, timer=timer)
        self._resource_domain_pending = pending

        def finish():
            timer.stop()
            timer.deleteLater()
            steps.close()
            if getattr(self, '_resource_domain_pending', None) is pending:
                self._resource_domain_pending = None

        def advance():
            if getattr(self, '_resource_domain_pending', None) is not pending:
                return
            if key != self._resource_domain_key(self._pages_with_implicit_defaults()):
                finish()
                self._recount_timer.start()
                return
            deadline = time.perf_counter() + .004
            try:
                for _ in range(64):
                    next(steps)
                    if time.perf_counter() >= deadline:
                        break
            except StopIteration as complete:
                self._resource_domain_cache = (key, complete.value)
                self._resource_counts_cache = None
                finish()
                self._recount_timer.start()
                return
            except Exception:
                self._resource_domain_failure = key
                finish()
                logger.exception('Resource metadata projection failed')
                return
            timer.start(0)

        timer.timeout.connect(advance)
        timer.start(0)

    def _update_navigation(self, counts=None):
        """刷新左栏导航数字和资源树计数（方案 §5.4）。

        全部/未标签/回收站都走 `ProjectAssetCatalog` 的全项目口径，不再
        按当前画布过滤。资源树父文件夹的递归汇总由
        `ResourceTreeModel.set_asset_counts()` 基于 `canvas_counts()` 做。

        计数只读元数据，不解码图像。300 ms 防抖由 `_recount_timer` 保证，
        平移/缩放/拖动中的每一次 `scene.changed` 不会触发完整重扫。
        """
        if self.catalog is None:
            # _install_resource_service 还没跑（极早期初始化），退回到
            # 空数字，避免 AttributeError。
            for key in ('all', 'untagged', 'trash'):
                if key in self._nav_counts:
                    self._nav_counts[key].setText('0')
            # 目录还不存在 → 每画布计数未知，不是 0。
            self._implicit_default_canvases = None
            return
        snapshot = self.count_snapshot_for_display()
        # 顺带缓存每画布计数：隐式默认画布是否还有内容由它回答。
        # `canvases` 为 None 表示目录不可用且是按页打开 —— 那是**未知**，
        # 必须和「目录说它是 0」分开。
        self._implicit_default_canvases = snapshot.get('canvases')
        total = snapshot['total']
        if isinstance(total, int) and total >= 0:
            # 规模口径：项目里的素材总数。这个数这一次已经算出来了，
            # 防抖分档直接用 —— 不必为分档再扫一遍。
            self._search_scale = total
        def count_text(value):
            return _('Preparing…') if value is None else f'{value:,}'
        # 左栏这五个数字是**全项目资源**口径：图片素材、画布、脑图、文档、
        # 材质、模型混在一起数。数字和点进去看到的那张列表必须是同一套
        # 口径 —— 否则用户点下去发现数量对不上，就不再信这个数字了。
        resource_counts = self._resource_scope_counts(snapshot)
        self._nav_counts['all'].setText(count_text(resource_counts['all']))
        self._nav_counts['unclassified'].setText(count_text(resource_counts['unclassified']))
        self._nav_counts['untagged'].setText(count_text(resource_counts['untagged']))
        if 'recent' in self._nav_counts:
            self._nav_counts['recent'].setText(count_text(resource_counts['recent']))
        # 需求 1：左栏「标签管理」入口已删除，`_nav_counts` 不再有 'tags'。
        if 'trash' in self._nav_counts:
            self._nav_counts['trash'].setText(count_text(resource_counts['trash']))
        browser = getattr(self.view.window(), 'asset_browser', None)
        is_browser = browser is not None and self.view.window().tabs.currentWidget() is browser
        mode = browser.mode if is_browser and browser.scope_pages is None and not browser.saved_rule else None
        self._navigation.setExclusive(False)
        for key, button in self._nav_buttons.items():
            if button.isCheckable():
                button.setChecked(key == mode)
        self._navigation.setExclusive(True)
        if mode is not None:
            self.resource_tree.clearSelection()
            self.resource_tree.setCurrentIndex(QtCore.QModelIndex())
        active = (self._min_rating or self._untagged_only
                  or self._active_tags or self._active_filter)
        self.filter_button.setToolTip('按名称筛选左侧目录；素材筛选在顶部漏斗中')
        # 资源树计数：全项目口径，含 UNASSIGNED_CANVAS 桶。
        # set_asset_counts 已经实现文件夹层级的递归 roll-up。
        page_counts = snapshot['canvases']
        self.resource_tree.model().set_asset_counts(page_counts)

    def _install_resource_service(self):
        previous = getattr(self, 'resource_service', None)
        materials = getattr(self, 'material_service', None)
        if (previous is not None and materials is not None
                and materials.scene is self.scene
                and previous.project_id == self.scene.project_id
                and getattr(self.scene, 'workspace_nodes', None) is previous.nodes
                and getattr(self.scene, 'material_records', None) is materials.records):
            return
        library = getattr(self.view.window(), 'material_library', None)
        if library is not None:
            library.store_to_scene()
        project_id = self.scene.project_id
        nodes = getattr(self.scene, 'workspace_nodes', []) or []
        previous = getattr(self, 'resource_service', None)
        if (previous is not None and previous.project_id != project_id
                and nodes is previous.nodes):
            # Legacy loads can change project id without supplying nodes.
            # The live service still belongs to the previous/new scene.
            nodes = []
        if not nodes:
            nodes = legacy_page_nodes(
                getattr(self.scene, 'workspace_pages', []) or [], project_id)
        self.resource_service = ResourceTreeService(project_id, nodes, self, model_section_enabled=True)
        self.scene.workspace_nodes = self.resource_service.nodes
        from prism.material_service import MaterialLibraryService
        if (materials is not None and materials.scene is self.scene
                and materials.project_id == project_id
                and materials.records is self.scene.material_records):
            # The validated, service-owned records did not change. Replacing
            # the folder model must not copy/validate all materials again.
            self.material_service = materials
            materials.folders = self.resource_service
        else:
            self.material_service = MaterialLibraryService(
                self.scene, self.resource_service, self)
            self.material_service.changed.connect(self._on_material_records_changed)
        self.resource_service.changed.connect(self._on_resource_nodes_changed)
        # The tree keeps its open categories across the new model: a fresh
        # model would collapse every one of them, and the user sees that as
        # the sidebar blinking shut in the middle of an open.
        self.resource_tree.replace_model(ResourceTreeModel(
            self.resource_service, self.resource_tree))
        self.resource_tree.sync_sections()
        # 全项目素材查询服务（方案 §5.2）。左栏数字、资源树计数、结果列表
        # 和状态栏都走这一层，避免各处写不同的过滤条件导致口径漂移。
        # scope_provider 让按页打开（view._open_scope 非空且 ready）时，计数
        # 走只读目录的全项目口径，而不是当前已加载一页的内存口径。
        self.catalog = ProjectAssetCatalog(
            self.scene, self.resource_service,
            scope_provider=lambda: self.view.current_catalog_scope())
        # 新工程 → 旧结论作废。目录此刻多半还没就绪，所以状态是「未知」，
        # 不是「空」。
        self._implicit_default_canvases = None

    def _count_publication_key(self):
        return (self.catalog, self.scene, self.scene.project_id,
                getattr(self.scene, '_asset_revision', None),
                getattr(self.resource_service, 'revision', None),
                getattr(self.material_service, 'revision', None))

    def count_snapshot_for_display(self):
        """Share one whole-project count only within a synchronous notification.

        This is not a long-lived cache: source validation and dirty overlays
        are recomputed on the next notification/direct request. Reentrant
        edits with a new revision cannot consume the previous publication.
        """
        publication = getattr(self, '_count_publication', None)
        if publication is not None and publication[0] == self._count_publication_key():
            return publication[1]
        return self.catalog.count_snapshot()

    def _on_material_records_changed(self, change):
        self.view.mark_content_dirty()
        self.material_records_changed.emit(change)
        if change.get('tagsChanged') or change.get('operation') in ('create', 'delete', 'restore', 'purge', 'trash', 'tags'):
            # Preview pixels must not trigger a global tag-tree rebuild.
            if not hasattr(self, '_material_tag_timer'):
                self._material_tag_timer = QtCore.QTimer(self)
                self._material_tag_timer.setSingleShot(True)
                self._material_tag_timer.setInterval(100)
                self._material_tag_timer.timeout.connect(self.refresh_tags)
            self._material_tag_timer.start()

    def _resource_records_updated(self, change):
        # Counts belong to the sidebar, even when the mixed browser is hidden.
        # Cover pixels do not change resource membership or tag counts.
        if isinstance(change, dict) and change.get('operation') == 'preview':
            return
        self._resource_counts_cache = None
        self._schedule_recount(preview=False)

    def _on_resource_nodes_changed(self, _change):
        self.scene.workspace_nodes = self.resource_service.nodes
        self.resource_nodes_changed.emit(_change)
        self.view.mark_content_dirty()
        self.search_content_changed()

    def record_resource_usage(self, section, resource_id, name=''):
        from prism import project_resources
        if project_resources.record_use(self.scene.project_id, section, resource_id, name) is not None:
            self._usage_revision = getattr(self, '_usage_revision', 0) + 1
            self._resource_counts_cache = None
            self.resource_usage_changed.emit()
            self._schedule_recount(bump_revision=False, preview=False)

    def search_content_changed(self):
        controller = getattr(self.view.window(), 'search_controller', None)
        if controller is not None:
            controller.content_changed()
        browser = getattr(self.view.window(), 'asset_browser', None)
        if browser is not None and browser.search.text().strip():
            browser.schedule()
        if self.search.text().strip():
            self._start_search_timer()

    def _clear_search_or_filters(self):
        # 结果模式下，这个按钮的文案是"退出结果 / 返回资源目录"，
        # 点击应退出结果模式（方案 §6.1）。
        if self._result_mode is not None:
            self._exit_result_mode()
            return
        if self.search.text().strip():
            self.search.clear()
        else:
            self.clear_filter()

    def _search_debounce_interval(self):
        """这次检索该等多久（按规模分档，见 `search_debounce_interval`）。"""
        return search_debounce_interval(self._search_scale)

    def _start_search_timer(self):
        """按当前规模起关键词搜索的防抖 timer。"""
        timer = getattr(self, '_search_timer', None)
        if timer is None:
            return
        timer.setInterval(self._search_debounce_interval())
        timer.start()

    def _on_project_search(self, text):
        active = bool(text.strip())
        # 方案 §6.1：在全项目结果模式（all/untagged）下，搜索框用来过滤
        # 当前结果列表，不切换到综合项目搜索。只有从浏览模式进入搜索时
        # 才切换到 'search' 模式。
        if self._result_mode in ('all', 'untagged'):
            # 结果模式内部过滤：用 timer 防抖，避免每次按键都重建结果列表
            self._start_search_timer()
            return
        if active and self._result_mode is None:
            # 从浏览模式进入搜索：记录原位置，切换到 search 模式
            self._enter_result_mode('search')
            return
        if active and self._result_mode == 'search':
            # 已经在 search 模式，刷新结果
            self._start_search_timer()
            return
        if not active and self._result_mode == 'search':
            # 清空搜索框：退出 search 模式，回到资源目录
            self._exit_result_mode()
            return
        # 其余情况（active 但 _result_mode 已经是 all/untagged 已在上面
        # 处理；not active 且 _result_mode 是 None）不做任何事。

    def _on_search_timer(self):
        """搜索防抖 timer 的分发：按当前结果模式选择刷新方式。"""
        if not self._scene_alive():
            return
        if self._result_mode in ('all', 'untagged'):
            self._refresh_result_mode()
        elif self._result_mode == 'search':
            self._refresh_project_search()

    def make_search_type_combo(self):
        from prism.widgets.search_canvas import TYPE_LABELS
        combo = QtWidgets.QComboBox()
        for key, label in [('all', 'All content'), *TYPE_LABELS.items()]:
            combo.addItem(_(label), key)
        combo.setToolTip(_('Search content type'))
        combo.setMaximumWidth(100)
        combo.currentIndexChanged.connect(lambda _index: self.set_search_kind(combo.currentData()))
        return combo

    def set_search_kind(self, kind, *, refresh=True):
        self.search_kind = kind
        browser = getattr(self.view.window(), 'asset_browser', None)
        for combo in [self.search_type, getattr(browser, 'search_type', None)]:
            if combo is not None:
                blocker = QtCore.QSignalBlocker(combo)
                combo.setCurrentIndex(combo.findData(kind))
                del blocker
        if not refresh:
            return
        if self.search.text().strip():
            self._refresh_project_search()
        controller = getattr(self.view.window(), 'search_controller', None)
        if controller is not None:
            controller.type_changed(kind)
        if browser is not None and (controller is None or self.view.window().tabs.currentWidget() is browser):
            browser.refresh()

    def project_search_results(self, text):
        from prism.project_search import search_project
        window = self.view.window()
        panels = list(getattr(window, '_page_panels', {}).values())
        panels += [p for p in (getattr(window, 'document_panel', None),
                               getattr(window, 'mindmap_panel', None)) if p is not None]
        query, expression = self._split_query(text)
        results = search_project(self.scene, self.resource_service, query, panels, expression,
                                 self._pages_with_implicit_defaults(),
                                 allow_empty=self.search_kind != 'all')
        kind = self.search_kind
        return [r for r in results if kind == 'all' or
                ('asset' if r['kind'] == 'asset' else r.get('page_kind', r['kind'])) == kind]

    def _refresh_project_search(self):
        if (not self.search.text().strip() and self.search_kind == 'all') or not self._scene_alive():
            return
        results = self.project_search_results(self.search.text())
        self.search_results.clear()
        titles = {'asset': '素材', 'canvas': '画布', 'mindmap': '脑图',
            'mindmap_node': '脑图节点', 'document': '文档', 'document_text': '文档正文',
            'folder': '文件夹', 'tag': '标签', 'material': '材质球', 'model': '模型'}
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
        # 全项目结果模式（方案 §6）：AssetResult 实例
        if isinstance(result, AssetResult):
            self._locate_asset_result(result)
            return
        self.activate_project_result(result)

    def _reveal_tree_node(self, node_id):
        """树里定位到某个节点：展开父链、选中、滚动到可见。"""
        index = self.resource_tree.model().index_for_id(node_id)
        if not index.isValid():
            return
        parent = index
        while parent.isValid():
            self.resource_tree.expand(parent)
            parent = parent.parent()
        self.resource_tree.setCurrentIndex(index)
        self.resource_tree.scrollTo(index)

    def _reveal_page_node(self, page_id):
        """树里定位到某个页面。

        页面在树里是有节点的（``nodeType == 'page'``），所以能定位到它
        所在的那一行 —— 用户点了卡片，左栏就得指给他看在哪。
        """
        if not page_id:
            return
        node_id = next((node['id'] for node in self.resource_service.nodes
                        if node.get('nodeType') == 'page'
                        and node.get('pageId') == page_id
                        and not node.get('deletedAt')), None)
        if node_id is None:
            return
        self._reveal_tree_node(node_id)

    def activate_project_result(self, result):
        # Both search surfaces navigate through the same targets.
        window = self.view.window()
        kind = result['kind']
        if kind == 'model':
            window.model_library.reveal_model(result.get('model_id'))
            return
        if kind == 'material':
            window.material_library.reveal_material(result.get('material_id'))
            return
        if kind == 'tag':
            self.search.setText('tag:' + json.dumps(result['tag'], ensure_ascii=False))
            return
        if kind == 'folder':
            self._search_origin = None
            self.search.clear()
            self._reveal_tree_node(result['node_id'])
            return
        from prism.navigation import defer_navigation
        if defer_navigation(window, lambda: self.activate_project_result(result), owner=self):
            return
        page_id = result.get('page_id')
        if page_id and any(n.get('pageId') == page_id and n.get('deletedAt')
                           for n in self.resource_service.nodes):
            return
        if kind == 'asset':
            item = result.get('item')
            if item is None or is_deleted(item) or item.scene() is not self.scene or getattr(item, '_deleted_at', None):
                return
        if hasattr(window, 'open_workspace_page'):
            window.open_workspace_page(result.get('page_kind', 'canvas'), page_id or window._first_canvas_id())
            # 左栏也跟着跳：树里定位到这一页，用户才看得见自己到了哪儿。
            # 材质和模型不在树里 —— 它们由各自的库面板定位，树里只选分区。
            self._reveal_page_node(page_id)
        if kind == 'asset':
            item = result['item']
            if is_deleted(item):
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

    def _locate_asset_result(self, result):
        """定位全项目结果里的一张素材（方案 §6.1"选中结果能打开原画布并
        定位/选中该图片"）。

        - 打开素材所属画布（无归属时回落到 default-canvas 或第一个画布）
        - pin 住素材使其不被画布隔离隐藏
        - 选中并居中
        - 不重排、不移动、不重新导入（方案 §6.3"定位图片只改变打开的
          页面和选择/视角"）
        """
        from prism.navigation import defer_navigation
        if defer_navigation(self.view.window(), lambda: self._locate_asset_result(result), owner=self):
            return
        item = result.item
        if item is None or is_deleted(item):
            self._refresh_result_mode()
            return
        window = self.view.window()
        if getattr(item, '_catalog_proxy', False):
            # 全项目结果里还没加载的行：显示代理不允许选中/编辑，先按主键要
            # 回真正的场景对象；拿不到就只打开它所属的画布并说明情况。
            loader = getattr(self.view, 'ensure_asset_loaded', None)
            loaded = loader(str(item.save_id)) if callable(loader) else None
            if loaded is None or is_deleted(loaded):
                if hasattr(window, 'open_workspace_page'):
                    window.open_workspace_page('canvas', result.canvas_id)
                self.hint.setText(
                    _('Asset is not loaded; its source canvas was opened instead'))
                return
            item = loaded
        page_kind, page_id = (self.catalog.locate(item)
                              if self.catalog is not None
                              else ('canvas', result.canvas_id))
        if page_id is None:
            # 原画布已永久删除且没有可用回落（方案 §7.4）。提示用户，
            # 不悄悄恢复到不存在的 ID。
            QtWidgets.QMessageBox.information(
                self, '无法定位',
                '该素材的原画布已不存在，且项目里没有可用的回落画布。\n'
                '请先在资源树里创建一个画布，或从回收站恢复原画布。')
            return
        if hasattr(window, 'open_workspace_page'):
            window.open_workspace_page(page_kind, page_id)
        item._keep_visible = True
        if not hasattr(self, '_search_pinned_items'):
            self._search_pinned_items = []
        self._search_pinned_items.append(item)
        item.setVisible(True)
        self.scene.clearSelection()
        item.setSelected(True)
        self.view.centerOn(item)

    # ── 全项目结果模式（方案 §6） ─────────────────────────────

    def _enter_result_mode(self, mode):
        """进入全项目结果模式。

        mode: 'all' | 'untagged' | 'search'

        - 隐藏资源树，显示 search_results 和「返回资源目录」按钮
        - 记录进入前的页面/视角，退出时恢复（方案 §6.3"返回目录时恢复
          原有展开状态和滚动位置"）
        - 立即渲染结果
        """
        if mode not in ('all', 'untagged', 'search'):
            return
        window = self.view.window()
        # 记录原位置（只在第一次进入结果模式时记，避免在结果模式内部
        # 切换 all/untagged 时把"原位置"覆盖成结果模式自己的状态）。
        if self._result_mode is None and not getattr(self, '_search_origin', None):
            self._search_origin = (
                getattr(window, 'current_page_kind', 'canvas'),
                getattr(window, 'current_page_id', None),
                self.view.transform(),
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
        # 保存资源树展开状态和滚动位置，退出时恢复
        if self._result_mode is None:
            self._tree_expanded_state = self._capture_tree_state()
        self._result_mode = mode
        # 方案 §6.3：全部/未标签决定全项目查询范围，不再过滤当前画布。
        # 清除 _untagged_only，让画布保持正常显示（未标签过滤由结果列表
        # 承担）。search 模式不动这个标志。
        if mode in ('all', 'untagged'):
            self._untagged_only = False
        self.resource_tree.setVisible(False)
        self.search_results.setVisible(True)
        self.back_to_tree_button.setVisible(True)
        self.rating_label.setVisible(False)
        self.rating_filter.setVisible(False)
        # 结果模式下清除筛选按钮的文案跟随模式
        self.clear_button.setText('退出结果 / 返回资源目录')
        self._refresh_result_mode()
        self._update_navigation()

    def _exit_result_mode(self):
        """退出结果模式，回到资源目录浏览（方案 §6.1"返回资源目录"）。"""
        from prism.navigation import defer_navigation
        if self._result_mode is None:
            return
        if defer_navigation(self.view.window(), self._exit_result_mode, owner=self):
            return
        self._result_mode = None
        self.search_results.clear()
        self.search_results.setVisible(False)
        self.back_to_tree_button.setVisible(False)
        self.resource_tree.setVisible(True)
        self.rating_label.setVisible(True)
        self.rating_filter.setVisible(True)
        self.clear_button.setText(_('Show All / Clear Filters'))
        # 清除搜索框（不触发 _on_project_search 的递归）
        self.search.blockSignals(True)
        self.search.clear()
        self.search.blockSignals(False)
        # 取消 pin 住的素材
        for item in getattr(self, '_search_pinned_items', []):
            if not is_deleted(item):
                item._keep_visible = False
        self._search_pinned_items = []
        # 恢复原页面/视角
        window = self.view.window()
        origin = getattr(self, '_search_origin', None)
        self._search_origin = None
        if origin and origin[1] and hasattr(window, 'open_workspace_page'):
            window.open_workspace_page(origin[0], origin[1])
            if origin[0] == 'canvas':
                self.view.setTransform(origin[2])
                self.view.centerOn(origin[3])
            else:
                panel = getattr(self, '_search_origin_panel', None)
                if panel is not None and not is_deleted(panel):
                    panel.restore_search_view(
                        getattr(self, '_search_origin_view', None))
        # 恢复资源树展开状态和滚动位置
        self._restore_tree_state(getattr(self, '_tree_expanded_state', None))
        self._tree_expanded_state = None
        # 结果模式下可能 pin 了素材，退出后恢复画布隔离
        self._untagged_only = False
        self._apply_filter()
        self.update_counts()

    def _refresh_result_mode(self):
        """按当前 _result_mode 重新渲染结果列表。"""
        if self._result_mode is None:
            return
        if self._result_mode == 'search' or self.search_kind != 'all':
            self._refresh_project_search()
            return
        # 'all' / 'untagged'：走 catalog 查询
        if self.catalog is None:
            return
        text = self.search.text().strip()
        query, expression = self._split_query(text)
        try:
            results = self.catalog.query(
                text=query,
                tag_expression=expression,
                untagged_only=(self._result_mode == 'untagged'),
                min_rating=self._min_rating,
            )
        except CatalogNotReady:
            # 缺陷 B：目录不可用时把"已加载的一页"（或空）当全项目渲染，等于
            # 告诉用户"这个工程没有素材"。这里必须说清楚是目录没准备好。
            logger.warning('Whole-project asset results unavailable; '
                           'the project directory is not ready')
            self._render_directory_unavailable()
            return
        self._render_asset_results(results)

    def _render_directory_unavailable(self):
        """目录不可用时的明确提示（对齐素材浏览器 `_refresh_catalog_assets`）。"""
        self.search_results.clear()
        row = QtWidgets.QTreeWidgetItem(
            [_('Project directory unavailable; retry after it is ready')])
        row.setFlags(QtCore.Qt.ItemFlag.ItemIsEnabled)
        self.search_results.addTopLevelItem(row)
        self.hint.setText(_('Whole-project directory unavailable'))

    def _render_asset_results(self, results):
        """把 AssetResult 列表渲染到 search_results 树控件。

        按画布路径分组（方案 §6.1"结果至少显示素材名称、所属画布及资源
        路径"）。1562 行 QTreeWidgetItem 在 Qt 里可接受（万级才需要虚拟化），
        不加载缩略图，纯文本行，构建开销 < 100 ms。
        """
        self.search_results.clear()
        if not results:
            # 空状态（方案 §6.2"若结果为空显示明确提示，而不是一片空白"）
            empty = QtWidgets.QTreeWidgetItem([
                '项目中的素材均已添加标签'
                if self._result_mode == 'untagged'
                else '没有匹配的素材'])
            empty.setFlags(QtCore.Qt.ItemFlag.ItemIsEnabled)
            self.search_results.addTopLevelItem(empty)
            self.hint.setText('0 个结果')
            return
        # 按 path 分组
        groups = {}
        order = []
        for result in results:
            key = result.path or UNASSIGNED_LABEL
            if key not in groups:
                groups[key] = []
                order.append(key)
            groups[key].append(result)
        total_shown = 0
        for key in order:
            group_results = groups[key]
            group = QtWidgets.QTreeWidgetItem(
                [f'{key}  ({len(group_results)})'])
            group.setFlags(QtCore.Qt.ItemFlag.ItemIsEnabled)
            self.search_results.addTopLevelItem(group)
            for result in group_results:
                title = result.title or '未命名'
                suffix = ''
                if result.rating:
                    suffix += '  ' + '★' * result.rating
                if result.tags:
                    suffix += '  [' + ', '.join(result.tags[:3]) + (
                        '…' if len(result.tags) > 3 else '') + ']'
                row = QtWidgets.QTreeWidgetItem(group, [title + suffix])
                row.setData(0, QtCore.Qt.ItemDataRole.UserRole, result)
                row.setToolTip(0, self._asset_tooltip(result))
                total_shown += 1
        self.search_results.expandAll()
        self.hint.setText(f'全项目：{len(results)} 个素材')

    @staticmethod
    def _asset_tooltip(result):
        lines = [result.title]
        if result.path:
            lines.append('位置：' + result.path)
        if result.filename:
            lines.append('文件：' + result.filename)
        if result.tags:
            lines.append('标签：' + ', '.join(result.tags))
        if result.rating:
            lines.append('评分：' + '★' * result.rating)
        if result.notes:
            notes = result.notes.strip()
            if len(notes) > 120:
                notes = notes[:120] + '…'
            lines.append('备注：' + notes)
        return '\n'.join(lines)

    def _capture_tree_state(self):
        """保存资源树展开状态和滚动位置。"""
        tree = self.resource_tree
        model = tree.model()
        if model is None:
            return None
        expanded = []

        def walk(parent_index):
            for row in range(model.rowCount(parent_index)):
                index = model.index(row, 0, parent_index)
                if tree.isExpanded(index):
                    node_id = index.data(NODE_ID_ROLE)
                    if node_id:
                        expanded.append(node_id)
                    walk(index)
        walk(QtCore.QModelIndex())
        return {
            'expanded': expanded,
            'scroll': tree.verticalScrollBar().value(),
            'identity': (self.scene.project_id, getattr(self.view, '_trace_open_epoch', None)),
        }

    def _restore_tree_state(self, state):
        """恢复资源树展开状态和滚动位置。"""
        if not state:
            return
        if (state.get('identity') != (self.scene.project_id, getattr(self.view, '_trace_open_epoch', None))
                or getattr(self.view, '_project_opening', False)):
            return
        tree = self.resource_tree
        model = tree.model()
        if model is None:
            return
        tree.collapseAll()
        for node_id in state.get('expanded', []):
            index = model.index_for_id(node_id)
            if index.isValid():
                tree.expand(index)
        tree.verticalScrollBar().setValue(state.get('scroll', 0))

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
        session = getattr(self.scene, '_partial_project_session', None)
        if session is not None:
            from prism.fileio.partial_save import PartialSaveConflict
            try:
                session.authorize_canvas_move(items, page_id, self.scene)
            except PartialSaveConflict as error:
                parent = getattr(self.view, 'parent', None)
                if parent is not None and hasattr(parent, 'statusBar'):
                    parent.statusBar().showMessage(str(error), 6000)
                return False
        self.scene.undo_stack.push(commands.ChangeMetadata(
            items, 'canvas_id', [page_id for _ in items]))
        self.update_counts()
        self._apply_filter()
        return True

    # ── 跨画布移动：拖拽之外的命令入口 ───────────────────────

    def _setup_move_to_canvas(self):
        """注册「移动到画布…」：一个 QAction + 快捷键，外加左栏右键入口。

        拖拽是「把素材拖到左栏的画布行上」；这个命令是同一件事的键盘 /
        右键版本，走的还是 `move_items_to_canvas`，不另写一套移动逻辑。

        动作挂在主窗口上（`addAction`），所以左栏收起来的时候快捷键照样
        能用 —— 选中的素材在画布上，命令也归这条线管。
        """
        action = QtGui.QAction(_('Move to canvas…'), self)
        action.setObjectName('prismMoveToCanvas')
        action.setShortcut(QtGui.QKeySequence(MOVE_TO_CANVAS_SHORTCUT))
        action.setShortcutContext(QtCore.Qt.ShortcutContext.WindowShortcut)
        action.setToolTip(_('Move the selected assets to another canvas'))
        action.setStatusTip(_('Move the selected assets to another canvas'))
        action.triggered.connect(lambda: self.move_selected_to_canvas())
        self.move_to_canvas_action = action
        window = self.view.window()
        if window is not None:
            window.addAction(action)
        self._install_tree_move_entry()

    def _install_tree_move_entry(self):
        """左栏资源树的右键菜单里加一条「把选中的素材移到这里」。

        资源树自己建右键菜单（`ResourceTreeView.menu_for_index`），没留
        扩展口子。这里在**本面板自己创建的这棵树**上包一层：菜单还是树
        建的，只在画布页行上补一条入口，别的文件一个字都不用动。
        """
        tree = self.resource_tree
        build_menu = tree.menu_for_index

        def menu_for_index(index):
            menu = build_menu(index)
            try:
                self._add_tree_move_action(menu, index)
            except Exception:  # pragma: no cover - 入口失败不该弄坏右键菜单
                logger.debug('画布移动右键入口添加失败', exc_info=True)
            return menu

        tree.menu_for_index = menu_for_index

    def _add_tree_move_action(self, menu, index):
        """在画布页那一行下面补一条移动入口。别的行不补。"""
        if index is None or not index.isValid():
            return
        if (index.data(SECTION_ROLE) != 'canvas'
                or index.data(NODE_TYPE_ROLE) != 'page'):
            return
        node = self.resource_service._by_id(index.data(NODE_ID_ROLE))
        page_id = node.get('pageId') if node else None
        if not page_id:
            return
        menu.addSeparator()
        action = menu.addAction(_('Move the selected assets here'))
        action.triggered.connect(lambda: self.move_selected_to_canvas(page_id))

    def move_selected_to_canvas(self, page_id=None):
        """把当前选中的素材移到另一个画布 —— 拖拽之外的命令入口。

        返回 True 表示真的移动了。返回 False 的每一种情况（没选中、目标
        不合法、授权冲突）都会给用户一句明确的话，而且**不会**往撤销栈里
        压东西、**不会**改归属。成功时才报成功。
        """
        items = self._selected_canvas_items()
        if not items:
            self._report_move_feedback(
                _('Select the assets on the canvas first.'))
            return False
        if page_id is None:
            page_id = self._choose_canvas_destination()
            if page_id is None:
                return False  # 用户取消，不是错误
        if not self._canvas_destination_allowed(page_id):
            return False  # 原因已经说给用户了
        movable = [item for item in items
                   if self._item_canvas(item) != page_id]
        if not movable:
            self._report_move_feedback(
                _('These assets are already on that canvas.'))
            return False
        if not self.move_items_to_canvas(movable, page_id):
            # 冲突原因 `move_items_to_canvas` 已经报给用户了，这里不再重复。
            return False
        self._report_move_feedback(
            _('Moved {n} asset(s) to {title}.').format(
                n=len(movable), title=self._canvas_title(page_id)))
        return True

    def _selected_canvas_items(self):
        """当前画布上选中的素材（只有能改归属的那些）。"""
        try:
            selected = self.scene.selectedItems(user_only=True)
        except TypeError:  # pragma: no cover - 老签名没有 user_only
            selected = self.scene.selectedItems()
        return [item for item in selected if hasattr(item, 'save_id')]

    def _known_canvas_ids(self):
        """工程里活着的画布页 id（含隐式的默认画布）。"""
        return {page['id'] for page in self._pages_with_implicit_defaults()
                if page.get('kind') == 'canvas'}

    def _trashed_page_ids(self):
        """回收站里的页面 id —— 节点自己被删，或它的某个祖先被删。"""
        service = getattr(self, 'resource_service', None)
        if service is None:
            return set()
        nodes = {node.get('id'): node for node in service.nodes}
        trashed = set()
        for node in service.nodes:
            if node.get('nodeType') != 'page' or not node.get('pageId'):
                continue
            current, seen = node, set()
            while current is not None and current.get('id') not in seen:
                seen.add(current.get('id'))
                if current.get('deletedAt'):
                    trashed.add(node['pageId'])
                    break
                current = nodes.get(current.get('parentId'))
        return trashed

    def _canvas_destination_allowed(self, page_id):
        """目标画布能不能去：必须是工程里还活着的画布页。"""
        if not isinstance(page_id, str) or not page_id:
            self._report_move_feedback(
                _('Choose an existing canvas as the move destination.'))
            return False
        if page_id in self._trashed_page_ids():
            self._report_move_feedback(
                _('That canvas is in the recycle bin; restore it first.'))
            return False
        if page_id in self._known_canvas_ids():
            return True
        pages = {page['id']: page for page in self._pages_with_implicit_defaults()}
        if page_id in pages:
            # 这个页面在工程里，但它不是画布（文档 / 脑图）。
            self._report_move_feedback(_('That page is not a canvas.'))
            return False
        # 按页打开时，场景里只装着当前工程的一部分页面：工程文件里还有
        # 这个工程自己知道、场景没装的画布页。只有保存会话能判断那些页，
        # 所以交给它 —— 它拒绝的时候会把原因报给用户。
        if getattr(self.scene, '_partial_project_session', None) is not None:
            return True
        self._report_move_feedback(
            _('That canvas does not exist in this project.'))
        return False

    def _canvas_destinations(self):
        """可以去的画布：工程里还活着的画布页，按标题排。"""
        trashed = self._trashed_page_ids()
        result = []
        for page in self._pages_with_implicit_defaults():
            if page.get('kind') != 'canvas':
                continue
            page_id = page.get('id')
            if not page_id or page_id in trashed:
                continue
            result.append((page.get('title') or page_id, page_id))
        return result

    def _choose_canvas_destination(self):
        """让用户挑一个目标画布。返回 page id，取消返回 None。"""
        destinations = self._canvas_destinations()
        if not destinations:
            self._report_move_feedback(
                _('This project has no other canvas to move assets to.'))
            return None
        labels, mapping = [], {}
        for title, page_id in destinations:
            label, suffix = title, 0
            while label in mapping:
                suffix += 1
                label = f'{title} ({suffix})'
            mapping[label] = page_id
            labels.append(label)
        choice, ok = QtWidgets.QInputDialog.getItem(
            self, _('Move to canvas'), _('Destination canvas:'),
            labels, 0, False)
        if not ok:
            return None
        return mapping.get(choice)

    def _canvas_title(self, page_id):
        for page in self._pages_with_implicit_defaults():
            if page.get('id') == page_id:
                return page.get('title') or page_id
        return page_id

    def _report_move_feedback(self, text):
        """把一句话说给用户听：状态栏 + 右下角提示。"""
        parent = getattr(self.view, 'parent', None)
        if parent is not None and hasattr(parent, 'statusBar'):
            parent.statusBar().showMessage(text, 6000)
        try:
            from prism.widgets import PrismNotification
            PrismNotification(self.view, text)
        except Exception:  # pragma: no cover - 提示失败不该弄坏这次移动
            logger.debug('移动提示显示失败', exc_info=True)

    def _implicit_default_has_content(self):
        """Does the project still carry content for the legacy default canvas?

        The whole-project summary answers this when it is available.  Three
        states have to stay apart:

        * the summary is unknown (``None``) -- the catalog is not ready, so we
          do **not** know.  Unknown must never be read as empty: dropping the
          page here would make a legacy project's assets unreachable.
        * the summary is ready and reports a non-zero count -- it holds
          content, the page stays.
        * the summary is ready and reports zero -- it is empty, the page goes
          away.

        The loaded scene is consulted as well, because the summary counts live
        assets and a trashed asset still belongs to a canvas.
        """
        canvases = self._implicit_default_canvases
        if canvases is None:
            return True
        if canvases.get(page_selection.DEFAULT_CANVAS_ID, 0):
            return True
        iter_assets = getattr(self.scene, 'iter_assets', None)
        if iter_assets is not None:
            try:
                for item in iter_assets():
                    if getattr(item, '_canvas_id', None) == page_selection.DEFAULT_CANVAS_ID:
                        return True
            except Exception:  # pragma: no cover - a dead scene must not crash us
                return True
        return False

    def _pages_with_implicit_defaults(self):
        """Return the page list, recreating a legacy page only if it holds data.

        The canvas/mind map/document entries used to be hard-coded, which left
        an empty tree looking permanently "full" and could not be removed.  A
        default page now appears only while the project still carries content
        that belongs to it, so a new document starts empty while an existing
        .prism keeps its canvas, mind map and document reachable.

        The implicit default canvas is a LEGACY landing spot, not a page of its
        own.  It used to be inserted at index 0 whenever the project had no
        explicit ``default-canvas`` page, which made every project open on
        that empty page instead of on its own first canvas.  It is now
        appended -- never in front of the stored canvases -- and only while
        the project still carries content for it.  The stored canvases keep
        the project order, empty ones included; see ``prism/page_selection.py``
        for the single rule the landing page and the background scope share.
        """
        pages = list(getattr(self.scene, 'workspace_pages', []))
        page_ids = {page.get('id') for page in pages}
        if 'default-canvas' not in page_ids and self._implicit_default_has_content():
            # Appended, never inserted at the front: an implicit page must not
            # outrank the canvases the project actually stores.
            pages.append({'id': 'default-canvas', 'kind': 'canvas',
                          'title': '默认画布', 'tags': [],
                          'permanent': True})
        if 'default-mindmap' not in page_ids and getattr(self.scene, 'mindmap_tree', None):
            pages.append({'id': 'default-mindmap', 'kind': 'mindmap',
                          'title': '默认脑图', 'tags': [], 'permanent': True})
        if 'default-document' not in page_ids and getattr(self.scene, 'note_html', ''):
            pages.append({'id': 'default-document', 'kind': 'document',
                          'title': '默认文档', 'tags': [], 'permanent': True})
        return pages

    def _tag_nodes(self):
        """The project's tags as one tree: catalogue, items and page tags.

        Parents that only exist because a tag below them does show up too,
        so a nested tag always has a row to sit under, and synonyms are
        folded into the row of the name they belong to.
        """
        return tag_panel.project_tag_nodes(self.scene, self.catalog)

    def rebuild_workspace_tree(self):
        if (self.resource_service.project_id != self.scene.project_id
                or getattr(self.scene, 'workspace_nodes', None) is not self.resource_service.nodes
                or getattr(self.scene, 'material_records', None) is not self.material_service.records):
            self._install_resource_service()
        self.resource_service.ensurePages(
            getattr(self.scene, 'workspace_pages', []) or [])
        self.update_counts()

    # ── Count updates ─────────────────────────────────────────

    def _count_query_signature(self):
        """What the counters on screen are about: the active query.

        It is part of the published contract, so a consumer can tell
        "37 assets" for one filter from "37 assets" for another.
        """
        try:
            return json.dumps({
                'filter': list(self._active_filter or ()),
                'tags': sorted(self._active_tags),
                'min_rating': self._min_rating,
                'untagged_only': self._untagged_only,
                'search': (self.search.text() or '').strip(),
                'result_mode': self._result_mode,
            }, sort_keys=True, ensure_ascii=False)
        except Exception:                                   # noqa: BLE001
            logger.exception('Could not build the count query signature')
            return None

    def _source_revision_token(self):
        """The source side of the counters: the directory, not the edits.

        The session's own edits live in the overlay revision; this is the
        part that comes from the project directory (resource tree revision,
        material revision and, for a page-wise open, the archive's stat
        fingerprint).  It is read **here**, on the recount path, never on the
        interaction path - zooming must not touch the disk.
        """
        parts = []
        for name in ('resource_service', 'material_service'):
            service = getattr(self, name, None)
            parts.append(str(getattr(service, 'revision', None)))
        scope = getattr(self.view, '_open_scope', None)
        if scope is not None:
            try:
                parts.append(repr(scope.source_state()))
            except Exception:                               # noqa: BLE001
                logger.exception('Could not fingerprint the open project directory')
        return '|'.join(parts)

    def count_identity(self):
        """Current count identity, without directory I/O on display paths."""
        source = getattr(self, '_count_source_identity', None)
        project, epoch = self.scene.project_id, self._open_epoch()
        return dict(project=project, open_epoch=epoch,
                    query=self._count_query_signature(),
                    source_revision=(source[2] if source is not None
                                     and source[:2] == (project, epoch) else None),
                    overlay_revision=self._count_revision)

    def _build_publication(self, snapshot, nodes):
        """One publication out of one recount.

        The directory aggregate and the tag distribution are computed here,
        once, and every consumer reads the same numbers under the same
        revision.  A value that is not known stays ``None`` - unknown is
        never turned into ``0``.  Every counter carries the **basis** it
        was counted over, and the tag counts are split by what carries the
        tag (asset / material / page).
        """
        counts = {}
        bases = {}
        complete = True
        source = snapshot if isinstance(snapshot, dict) else {}
        for kind, key in ((LIBRARY_TOTAL, 'total'),
                          (LIBRARY_UNTAGGED, 'untagged'),
                          (LIBRARY_UNCLASSIFIED, 'unclassified'),
                          (LIBRARY_TRASH, 'trash'),
                          (LIBRARY_VISIBLE, 'visible')):
            value = source.get(key)
            counts[kind] = value if isinstance(value, int) else None
            bases[kind] = BASIS_RESIDENT if kind == LIBRARY_VISIBLE \
                else BASIS_CATALOG
            if value is None:
                complete = False
        counts[SELECTION_COUNT] = self._selection_count()
        bases[SELECTION_COUNT] = BASIS_RESIDENT
        tag_counts = {}
        for node in nodes or ():
            count = getattr(node, 'count', None)
            tag_counts[node.name] = count if isinstance(count, int) else None
        typed = tag_panel.project_tag_typed_counts(self.scene, self.catalog)
        typed_by_name = {}
        for node in nodes or ():
            key = node.name.casefold()
            typed_by_name[node.name] = {
                BASIS_TAG_ASSET: typed.get('asset', {}).get(key),
                BASIS_TAG_MATERIAL: typed.get('material', {}).get(key),
                BASIS_TAG_PAGE: typed.get('page', {}).get(key),
            }
        source_revision = self._source_revision_token()
        self._count_source_identity = (
            self.scene.project_id, self._open_epoch(), source_revision)
        return CountPublication(
            request_token=next_request_token(),
            project=self.scene.project_id,
            query=self._count_query_signature(),
            source_revision=source_revision,
            overlay_revision=self._count_revision,
            open_epoch=self._open_epoch(),
            complete=complete,
            counts=counts,
            bases=bases,
            tag_counts=tag_counts,
            tag_counts_by_basis={
                basis: {name: values.get(basis) for name, values in
                        typed_by_name.items()}
                for basis in (BASIS_TAG_ASSET, BASIS_TAG_MATERIAL, BASIS_TAG_PAGE)
            },
        )

    def count_publication(self):
        """The last published counts, or ``None`` when nothing was published.

        Consumers that must not touch the directory (the zoom path, the
        canvas) read this instead of asking the catalog again.
        """
        return getattr(self, '_last_publication', None)

    def update_counts(self):
        """Recompute every counter once and publish them together.

        One recount answers the sidebar, the status bar, the colour bar and
        the toolbar: the directory aggregate, the tag distribution and the
        scene filter are computed here and travel out as one
        :class:`CountPublication`.  No consumer asks the directory a second
        time, and all of them see the same revision.
        """
        if not self._scene_alive():
            return
        if getattr(self.view, '_project_opening', False):
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
        # Navigation and status subscribe to this signal. Compute the complete
        # directory+dirty-overlay snapshot once, not independently in each slot.
        key = self._count_publication_key()
        snapshot = self.catalog.count_snapshot()
        publication = self._build_publication(snapshot, nodes)
        previous = getattr(self, '_count_publication', None)
        try:
            self._count_publication = (key, snapshot)
            self._last_publication = publication
            self.counts_changed.emit(self._publication_payload(
                counts, snapshot, publication))
        finally:
            self._count_publication = previous
        self.search_content_changed()
        # 方案 §6.2：结果模式下，素材增删/标签编辑后结果列表要同步刷新。
        # 用 timer 防抖，避免连续 metadata_changed 触发多次重建。
        if self._result_mode is not None:
            self._start_search_timer()
        if changed:
            self.categories_changed.emit()

    def _publication_payload(self, counts, snapshot, publication):
        """What ``counts_changed`` carries.

        The legacy keys stay exactly as they were (``categories`` and
        ``tags`` are the **resident** helper of `_calc_counts`, not the
        whole project numbers).  The authoritative whole project numbers
        travel in ``snapshot`` (the CountPublication) and ``library``.
        """
        payload = dict(counts)
        payload['snapshot'] = publication
        payload['library'] = snapshot
        payload['tag_counts'] = dict(publication.tag_counts)
        return payload

    def _calc_counts(self):
        """Count items per category.

        **Resident scope**: it walks the items that are loaded in this
        scene, so it answers "how many of the loaded items carry this
        category", *not* "how many assets in the project carry it".  The
        whole project numbers come from the catalog and are published in the
        CountPublication; this helper stays for the per-category UI and
        for the callers that ask for it directly.
        """
        if not self._scene_alive():
            return {'categories': {}, 'tags': {}}
        result = {'categories': dict.fromkeys(self.scene.category_names, 0), 'tags': {}}
        for item in self.scene.iter_assets():
            for tag in getattr(item, '_tags', []):
                result['tags'][tag] = result['tags'].get(tag, 0) + 1
            for cat in getattr(item, '_categories', []):
                if cat:
                    result['categories'][cat] = result['categories'].get(cat, 0) + 1
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
        browser = getattr(self.view.window(), 'asset_browser', None)
        if browser is not None and browser.isVisible():
            browser.schedule()
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
        for item in self.catalog.asset_items():
            # 软删除的素材（在回收站里）永远不在画布上露面，也不参与任何
            # 画布隔离/分类/标签判定。万一它此刻是可见的（例如刚从回收站
            # 状态被别的逻辑改动），这里把它按回隐藏。
            if getattr(item, '_deleted_at', None) is not None:
                if item.isVisible():
                    changes.append((item, False))
                continue
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

        # The filter already walked every asset: remember how many it shows
        # so the immediate publication does not have to walk them again.
        self._filter_visible = visible
        if self._result_mode is None:
            self.hint.setText(
                _('{count} items shown').format(count=visible)
                if visible else _('No matching items'))
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
        # 只需要"哪些分类存在"，不需要 z 序：走 iter_assets() 绕开
        # items_for_save() 的 10 万条全排序（F4-S1），并用 set 判重
        # 替代 list 线性扫描（F4-S2）。
        names = list(self.scene.category_names)
        seen = set(names)
        for item in self.scene.iter_assets():
            for name in getattr(item, '_categories', []):
                if name and name not in seen:
                    names.append(name)
                    seen.add(name)
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
        """A metadata edit changed what matches the active filter.

        Two different jobs, two different costs:

        * **the filter** is answered from the items that are loaded, so it
          is applied right away - the user must not keep looking at a photo
          that no longer matches the filter they chose;
        * **the counts** need the directory, so they are recomputed on the
          recount debounce (see `_schedule_recount`).

        The previous round moved both behind the debounce.  That made the
        filtered picture stay on screen until the timer fired, and it broke
        the contract that a metadata edit re-applies the filter in the same
        turn.
        """
        if not self._scene_alive():
            return
        self._apply_filter(keep_pinned=True)
        self._schedule_recount()


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
