"""Image-query UI lives in the existing project search input and asset canvas."""
import logging
from pathlib import Path
from PyQt6 import QtCore, QtGui, QtWidgets
from prism.project_search import item_key
from prism.visual_search_service import VisualSearchService, source_snapshot
from prism.widgets.components.icons import icon


logger = logging.getLogger(__name__)


class CandidateSet:
    """The candidates one round embeds, plus what was trimmed away.

    ``total`` is how many candidates matched **before** the budget cut them,
    or ``None`` when the catalog itself was cut off and the real number is
    unknown.  An unknown count is reported as unknown; it never becomes ``0``.
    """

    __slots__ = ('items', 'total', 'truncated', 'scope_label')

    def __init__(self, items, total=None, truncated=False, scope_label=''):
        self.items = items
        self.total = total
        self.truncated = truncated
        self.scope_label = scope_label

    @property
    def count(self):
        return len(self.items)


class VisualSearchController(QtCore.QObject):
    #: Hard cap on the candidates one round embeds.  The worker keeps its own
    #: ceiling (``components/visual_search_worker.MAX_ITEMS``); this is the
    #: *policy* ceiling that stops a 100k-asset project from being embedded
    #: end to end.  It is an instance attribute so a test can lower it.
    candidate_limit = 2000

    def __init__(self, search):
        super().__init__(search)
        self.search = search
        self.window, self.view = search.window, search.view
        self.active = False
        self.source = None
        self.source_project = None
        self.request = None
        self.dirty = False
        #: Round token.  Bumped whenever the round on screen is cancelled or
        #: replaced, so a late result of a retired round can never be written
        #: back into the UI (the same contract the text search uses).
        self.generation = 0
        self.request_generation = None
        #: Candidate accounting for the round that is on screen.  ``None``
        #: means "unknown", never "zero".
        self.candidate_count = 0
        self.candidate_total = None
        self.candidate_truncated = False
        self.candidate_scope = ''
        self.item_map = {}
        self.service = VisualSearchService(self)
        self.service.progress.connect(self.progress)
        self.service.results.connect(self.finished)
        self.service.failed.connect(self.failed)
        self.service.preview.connect(self.preview)
        self.action = search.input.addAction(icon('image-search'), QtWidgets.QLineEdit.ActionPosition.TrailingPosition)
        self.action.setText('以图搜图')
        self.action.setToolTip('以图搜图：在本地素材库中查找相似图片')
        self.action.triggered.connect(self.menu)
        self.clear_action = search.input.addAction(icon('close'), QtWidgets.QLineEdit.ActionPosition.LeadingPosition)
        self.clear_action.setToolTip('退出以图搜图')
        self.clear_action.triggered.connect(self.exit)
        self.clear_action.setVisible(False)
        search.input.setAcceptDrops(True)
        search.input.installEventFilter(self)
        self.timer = QtCore.QTimer(self)
        self.timer.setSingleShot(True)
        self.timer.setInterval(200)
        self.timer.timeout.connect(self.submit)
        self.view.scene.assets_changed.connect(self.schedule)
        self.view.scene.metadata_changed.connect(self.schedule)

    def menu(self):
        menu = QtWidgets.QMenu(self.search.input)
        menu.addAction('选择图片…', self.choose)
        paste = menu.addAction('粘贴剪贴板图片', self.paste)
        paste.setEnabled(QtWidgets.QApplication.clipboard().mimeData().hasImage())
        if self.active:
            menu.addSeparator()
            menu.addAction('取消当前检索', self.cancel)
            menu.addAction('退出以图搜图', self.exit)
        menu.exec(self.search.input.mapToGlobal(self.search.input.rect().bottomRight()))

    def choose(self):
        path, _ = QtWidgets.QFileDialog.getOpenFileName(self.window, '选择查询图片', '', '图片 (*.png *.jpg *.jpeg *.webp *.bmp *.tif *.tiff)')
        if path:
            self.start(path)

    def paste(self):
        image = QtWidgets.QApplication.clipboard().image()
        if not image.isNull():
            self.start(image)

    def eventFilter(self, watched, event):
        if event.type() in (QtCore.QEvent.Type.DragEnter, QtCore.QEvent.Type.Drop):
            mime = event.mimeData()
            path = next((url.toLocalFile() for url in mime.urls() if url.isLocalFile()), None) if mime.hasUrls() else None
            accepted = mime.hasImage() or (path and Path(path).suffix.lower() in ('.png', '.jpg', '.jpeg', '.webp', '.bmp', '.tif', '.tiff'))
            if accepted:
                if event.type() == QtCore.QEvent.Type.Drop:
                    image = mime.imageData()
                    self.start(path or (image.toImage() if isinstance(image, QtGui.QPixmap) else image))
                event.acceptProposedAction()
                return True
        return False

    def start(self, source):
        if self.search.active:
            self.search.exit()
        if not self.active:
            self.previous_text = self.search.input.text()
            self.previous_placeholder = self.search.input.placeholderText()
            self.previous_tab = self.window.tabs.currentWidget()
            browser = self.window.asset_browser
            self.previous_browser = (browser.mode, browser.scope_pages, getattr(browser, 'title', '素材'))
        self.cancel()
        self.search.timer.stop()
        self.active = True
        self.source = source_snapshot(source) if getattr(source, 'TYPE', None) == 'pixmap' else source
        self.source_project = self.view.scene.project_id
        self.previous_page = self.view.current_canvas_id
        blocker = QtCore.QSignalBlocker(self.search.input)
        self.search.input.clear()
        del blocker
        self.search.input.setPlaceholderText('以图搜图：继续输入名称或标签筛选')
        self.clear_action.setVisible(True)
        self.search._sync_clear_action()
        self.search.scope_combo.show()
        self.search.filter_button.show()
        self.search.info.show()
        self.search.info.setText('准备本地以图搜图…')
        self.search.scope = ('project', None)
        blocker = QtCore.QSignalBlocker(self.search.scope_combo)
        self.search.scope_combo.setCurrentIndex(0)
        del blocker
        self.search.type_combo.setEnabled(False)
        self.schedule()

    def schedule(self, *_):
        if self.active:
            self.dirty = True
            self.timer.start()

    def candidates(self):
        """Candidates for one round, or ``None`` when the set is unknown.

        按页打开时 `catalog.query` 走磁盘目录 + live/parked 覆盖层，所以候选
        集是全项目口径而不是"已加载的一页"。目录不可用时返回 ``None``（= 还
        不知道），绝不返回空列表假装"没有可检索的素材"。

        先裁剪再 embedding：作用域（当前页 / 文件夹）下推给目录的
        `canvas_scope`，整轮再受 `candidate_limit` 预算约束。被裁掉的量在
        `CandidateSet` 里如实记账 —— UI 必须说出来，不许静默截断。
        """
        from prism.project_catalog import CatalogNotReady
        text, expression = self.search.library._split_query(self.search.input.text())
        canvas_ids = self._canvas_scope()
        budget = max(1, int(self.candidate_limit))
        try:
            results = self.search.library.catalog.query(
                text=text, tag_expression=expression,
                min_rating=max(self.search.library._min_rating, self.search.min_rating),
                canvas_scope=canvas_ids, limit=budget + 1)
        except CatalogNotReady:
            self._report_unavailable(
                '索引准备中：全项目目录不可用，候选集未知',
                '项目目录未就绪，无法检索全库素材；请稍后重试或先完成工程准备')
            return None
        except Exception as error:
            logger.exception('visual search candidate query failed')
            self._report_unavailable(
                f'索引不可用：{error}，候选集未知',
                f'项目索引不可用：{error}；请稍后重试')
            return None
        from prism import tags
        active_tags = self.search.library._active_tags | self.search.active_tags
        system = tags.tag_system(self.view.scene)
        items = []
        for row in results:
            item = row.item
            if getattr(item, 'TYPE', '') != 'pixmap':
                continue
            if active_tags and not active_tags.issubset(tags.name_index(row.tags, system)):
                continue
            if self.search.color and getattr(item, 'color_group', None) != self.search.color:
                continue
            if not self._in_candidate_scope(row):
                continue
            items.append(item)
        # `budget + 1` 的那一行是"还有更多"的证据。目录自己先到顶时真实
        # 总数未知，UI 必须说"至少 N 张"，不能把裁剪后的数写成总数。
        saturated = len(results) >= budget + 1
        trimmed = items[:budget]
        total = None if saturated else len(items)
        truncated = saturated or len(items) > budget
        return CandidateSet(trimmed, total=total, truncated=truncated,
                            scope_label=self._scope_label(canvas_ids))

    def _canvas_scope(self):
        """Canvas ids the candidate query may be narrowed to, or ``None``.

        目录把 `canvas_ids` 下推成 SQL 条件，所以按页/按文件夹检索时只读那
        几页的行，而不是全项目。**返回值永远是元组**：
        `project_catalog_index._predicate` 会做 `tuple(canvas_ids)`，传字符串
        会被拆成逐字符元组（`IN ('s','t','r',…)`）→ 查空而且不报错。
        """
        kind, value = self.search.scope
        if kind == 'project':
            return None
        if kind == 'page':
            page_kind, page_id = value if isinstance(value, (tuple, list)) else (None, value)
            if page_kind not in (None, 'canvas') or not page_id:
                return None
            return (str(page_id),)
        if kind == 'folder':
            return self._folder_canvas_scope(value)
        return None

    def _folder_canvas_scope(self, node_id):
        """Canvas ids under a folder node, or ``None`` when there are none."""
        try:
            service = self.search.library.resource_service
            pages = {page.get('id') for page in self.search.library._pages_with_implicit_defaults()
                     if page.get('kind') == 'canvas'}
            ids = []
            for index in service._descendants(node_id):
                node = service._by_id(index)
                if not node or node.get('deletedAt'):
                    continue
                page_id = node.get('pageId')
                if page_id and page_id in pages and str(page_id) not in ids:
                    ids.append(str(page_id))
        except Exception:
            logger.exception('folder scope lookup failed; searching without pushdown')
            return None
        return tuple(ids) or None

    def _scope_label(self, canvas_ids):
        """Short label for the scope the candidates came from."""
        kind, value = self.search.scope
        if kind == 'page':
            page_kind, page_id = value if isinstance(value, (tuple, list)) else (None, value)
            return f'{page_kind or "页面"} {page_id}'
        if kind == 'folder':
            return f'文件夹（{len(canvas_ids)} 个画布）' if canvas_ids else '文件夹'
        if kind in ('category', 'navigation'):
            return kind
        return '全项目'

    def _in_candidate_scope(self, row):
        """Scope filter on the catalog's own canvas id.

        `AssetResult.canvas_id` 是目录记的画布 id；未加载素材的显示代理
        （`CatalogAssetProxy`）带的是 `default-canvas`，所以作用域必须从行上
        读，不能从代理对象上读。
        """
        return self.search.in_scope(dict(kind='asset', item=row.item,
            page_id=row.canvas_id or 'default-canvas'))

    def _report_unavailable(self, title, detail):
        """Say the candidate set is unknown; never imply "nothing found"."""
        self.search.info.show()
        self.search.info.setText(title)
        self.search.info.setToolTip(detail)

    def _candidate_text(self):
        """The on-screen candidate count, including what was trimmed away."""
        if self.candidate_truncated:
            return f'已裁剪到 {self.candidate_count} 张候选'
        return f'候选 {self.candidate_count} 张'

    def submit(self):
        if not self.active:
            return
        if self.source_project != self.view.scene.project_id:
            self.exit()
            return
        if self.service.active:
            return  # A single latest rescan replaces repeated filter changes.
        self.dirty = False
        candidates = self.candidates()
        if candidates is None:
            # The candidate set is unknown, not empty: say so and wait.  The
            # round identity is dropped as well, so a late result of the
            # previous round cannot land in the UI now that the set is
            # unknown.
            self.request = None
            self.request_generation = None
            return
        self.candidate_count = candidates.count
        self.candidate_total = candidates.total
        self.candidate_truncated = candidates.truncated
        self.candidate_scope = candidates.scope_label
        self.item_map = {item_key(item): item for item in candidates.items}
        self.generation += 1
        self.request_generation = self.generation
        try:
            identifier = self.service.search(
                self.source, [(item_key(item), source_snapshot(item)) for item in candidates.items])
        except (OSError, ValueError, RuntimeError) as error:
            self.request = None
            self.request_generation = None
            self._show_failure(str(error))
            return
        self.request = identifier
        self.search.info.setText(f'{self._candidate_text()} · 检索中…')

    def progress(self, value):
        if not self.active:
            return
        if value.get('phase') == 'index':
            self.search.info.setText(
                f'{self._candidate_text()} · 建立索引 {value["done"]}/{value["total"]}')
        else:
            self.search.info.setText(
                f'{self._candidate_text()} · {value.get("message", "正在本地检索…")}')
        self.search.info.setToolTip(value.get('message', ''))

    def preview(self, image):
        if self.active:
            self.clear_action.setIcon(QtGui.QIcon(QtGui.QPixmap.fromImage(image)))
            self.clear_action.setToolTip('查询图片：点击移除图像条件')

    def finished(self, identifier, value):
        if not self.active or identifier != self.request or self.request_generation != self.generation:
            return
        if self.source_project != self.view.scene.project_id:
            return
        if self.dirty:
            self.timer.start(0)
            return
        rows = [(self.item_map[row['key']], row['score']) for row in value['results'] if row['key'] in self.item_map]
        browser = self.window.asset_browser
        browser.visual_results = rows
        browser.visual_project = self.source_project
        # The browser filters/reranks its whole-project model by this list, so a
        # new round has to invalidate the model it installed for the last one.
        browser._visual_epoch = getattr(browser, '_visual_epoch', 0) + 1
        browser.open('visual', title='以图搜图', record=False)
        self.search.info.setText(f'{self._candidate_text()} · {value["indexed"]}/{value["total"]} 可检索')
        self.search.info.setToolTip(self._result_tooltip(value))

    def _result_tooltip(self, value):
        """Say what was searched and what was trimmed away.

        被裁剪过就必须说出来：一次只检索了 N 张却让用户以为"全库无结果"，
        比慢更糟。总数未知时说"未知"，不写成 0。
        """
        text = f'已复用 {value["cached"]} 张索引；{value["failed"]} 张原图不可用或格式不支持'
        if self.candidate_truncated:
            if self.candidate_total is None:
                text += (f'\n候选集超过单次上限，已裁剪到 {self.candidate_count} 张参与检索；'
                         '真实总数未知（更多匹配素材未参与），结果不代表全库')
            else:
                text += (f'\n候选集共 {self.candidate_total} 张，已裁剪到 {self.candidate_count} 张参与检索；'
                         '结果不代表全库')
        else:
            text += f'\n候选范围：{self.candidate_scope or "全项目"}（全部候选已检索）'
        text += '\n综合比较内容、主色和明暗布局；相关度不是匹配概率，也不保证同一画风。'
        return text

    def failed(self, message):
        # A failure that arrives after the round was cancelled or replaced
        # belongs to a retired job: it must not raise a dialog over the round
        # that is on screen.
        if self.active and self.request is not None:
            self._show_failure(message)

    def _show_failure(self, message):
        self.search.info.setText('本地检索未完成')
        self.search.info.setToolTip(message)
        QtWidgets.QMessageBox.warning(self.window, '以图搜图', message)

    def cancel(self):
        self.timer.stop()
        self.service.stop()
        # The round token moves on: a result of the cancelled round must not
        # be written back into the UI.
        self.generation += 1
        self.request = None
        self.request_generation = None

    def exit(self, *, restore_origin=True):
        if not self.active:
            return
        self.cancel()
        self.active = False
        self.clear_action.setVisible(False)
        self.search.input.setPlaceholderText(self.previous_placeholder)
        blocker = QtCore.QSignalBlocker(self.search.input)
        self.search.input.setText(self.previous_text)
        del blocker
        self.search._sync_clear_action()
        self.source = None
        self.item_map = {}
        self.search.info.hide()
        self.search.scope_combo.hide()
        self.search.filter_button.hide()
        self.search.type_combo.setEnabled(True)
        self.window.asset_browser.visual_results = []
        if not restore_origin:
            return
        if self.previous_tab is self.window.asset_browser:
            mode, scope, title = self.previous_browser
            self.window.asset_browser.open(mode if mode != 'visual' else 'all', scope, title)
        else:
            from PyQt6 import sip
            if self.previous_tab is not None and not sip.isdeleted(self.previous_tab):
                self.window.tabs.setCurrentWidget(self.previous_tab)
