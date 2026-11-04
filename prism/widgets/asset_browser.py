"""Virtualised project asset browser alongside the original canvas editor."""
import os
import time
import uuid
from collections import OrderedDict
from datetime import datetime, timezone
from prism.performance_trace import measured, record as trace_record

from PyQt6 import QtCore, QtGui, QtWidgets, sip
from prism.widgets.components.icons import icon, icon_button
from prism.i18n import _
from prism.item_lifecycle import is_deleted
from prism.qt_thread_safety import JobRegistry, gui_signal_carrier
from prism.presentation_evidence import (
    QueryEvidence, fingerprint, install_shutdown_hook, model_item_id)
from prism.catalog_browser import (
    CatalogResultCache, CatalogResultIds, CatalogLayoutToken, QueryKey, ResultPublication, estimate_result_bytes,
    publish_result,
)

#: 左栏那五个按钮，也是这张混排列表的五个口径。
_RESOURCE_SCOPES = ('all', 'unclassified', 'untagged', 'recent', 'trash')

#: 目录显示代理缓存的**条数**上限（LRU，按条数不按字节）。
#: 代理是 `CatalogAssetProxy`（`ImageRecord`），体积只随目录行的标量字段
#: 变化，逐个统计字节数本身又是一次 O(N) 遍历；用条数做上界最稳，也最容易
#: 在验收里说清楚。淘汰掉的代理随时可以按行重建（见 `_install_catalog_rows`），
#: 并且**始终**是只读显示代理，不会因为离开缓存就变成可编辑对象。
#:
#: **2048 这个数字的依据**（上一轮"依据不足"，这里补齐）：
#:
#: * *内存*：每条代理都深拷贝了一份目录行的字段（`asset_records.py:19` 的
#:   `copy.deepcopy(extra)`）。十万行全缓存 = 十万个代理，那才是内存问题；
#:   2048 ≈ 十万行的 **2%**，把代理内存压在项目规模的百分之几。
#: * *够用*：只有**可见**的卡片才会向委托请求显示，目录缩略图因此只被读
#:   可见的那些行。4K 视口 + 最小卡片（`card_size` 下限 96 px，网格步长
#:   96 × 77）≈ 40 列 × 28 行 ≈ **1.1 千张**，2048 在它之上还有余量。
#:   （这是按本文件里 `resize_cards()` 的网格公式**算**出来的，不是实测；
#:   可见卡片数的精确上界在 `asset_canvas.py` —— C-2 的文件 —— 那边若给出
#:   更准的数，这里应跟着改。）
#: * *淘汰不伤正确性*：被淘汰的代理仍被 `model.items` 强引用 → 画布上的
#:   卡片不会消失；下次走到同一行会重建一个（`_install_catalog_rows`）。
#:   代价只是"滚出再滚回来要多读一次目录缩略图"。
#:   ⇒ 宁可多缓存，也不要因为淘汰而反复重建。
CATALOG_PROXY_CACHE_LIMIT = 2048

#: How many directory row streams may stay open at the same time.
#:
#: A stream answers one filter set.  A filter change (all -> untagged) means
#: a different row set, so the old stream cannot answer the new query - but
#: a prefix checkpoint that was built on it still has to be resumable, so the
#: stream is retired, not closed.  Three is enough for the common switches
#: (all / untagged / unclassified) and keeps the retained page caches small.
CATALOG_STREAM_LIMIT = 3

_CATALOG_POOL = None
_STORAGE_POOL = None


def storage_thread_pool():
    """Slow size summaries cannot occupy directory/filter worker slots."""
    global _STORAGE_POOL
    if _STORAGE_POOL is None:
        _STORAGE_POOL = QtCore.QThreadPool(QtWidgets.QApplication.instance())
        _STORAGE_POOL.setMaxThreadCount(1)
        _STORAGE_POOL.setExpiryTimeout(30000)
    return _STORAGE_POOL


def catalog_thread_pool():
    """素材浏览自己的线程池。

    目录查询（`CatalogQuery`）与纯数据行过滤（`_RowWorker`）不与全局池里
    的其它任务抢线程：全局池同时跑瓦片、布局、解码等任务，目录查询被挤掉
    时安装进度会原地停住。两个线程位：目录查询一次只跑一个，行过滤不必
    等它。
    """
    global _CATALOG_POOL
    pool = _CATALOG_POOL
    if pool is None:
        pool = _CATALOG_POOL = QtCore.QThreadPool(QtWidgets.QApplication.instance())
        pool.setMaxThreadCount(2)
        pool.setExpiryTimeout(30000)
    return pool


#: 场景扫描单个 GUI 回合的时间预算（毫秒）。
#: 目录不可用时，素材浏览要在**场景内存**上回答一次全项目查询；十万级
#: 素材下这一趟是秒级的，同步做完会把 GUI 冻住一秒多。把它切成多个回合，
#: 每个回合最多花这么多时间，GUI 就不会被一次查询堵死。
SCENE_SCAN_SLICE_MS = 8
#: 场景扫描单个 GUI 回合最多处理的素材条数。时间预算之外再加一条兜底：
#: 单条素材特别慢时，一个回合也不会超出这个条数。
SCENE_SCAN_SLICE_ITEMS = 2000


class _AssetRow:
    """一条结果行的**纯数据**，外加它指向的那个场景对象。

    后台线程只读这里的标量字段。`item` 只是被**搬运**，从不解引用：场景
    对象可能在后台任务还在跑的时候被删除（它的 C++ 部分会随之释放），
    在后台线程读它的属性是未定义行为。
    """

    __slots__ = ('item', 'tags', 'sort_key', 'last_used', 'flags')

    #: `flags` 里的下标。四个布尔量分别对应"激活标签命中""未分类命中"
    #: "智能文件夹规则命中""画布范围命中"。它们在扫描时算好，交给后台
    #: 线程按同步路径的**同一顺序**过滤 —— 顺序不同，随机模式的洗牌
    #: 结果就会不同。
    F_ACTIVE = 0
    F_UNCLASSIFIED = 1
    F_RULE = 2
    F_SCOPE = 3

    def __init__(self, item, tags, sort_key, last_used, flags):
        # `__slots__` 的类默认没有 `__init__`：`object.__init__` 不接受参数，
        # 直接 `self.item = ...` 也会因为没有 `__dict__` 而失败 —— 所以这里
        # 必须显式写一个把五个字段填进去的构造器。
        self.item = item
        self.tags = tags
        self.sort_key = sort_key
        self.last_used = last_used
        self.flags = flags


class _RowSignals(QtCore.QObject):
    result = QtCore.pyqtSignal(object)


class _StorageWorker(QtCore.QRunnable):
    def __init__(self, items, token, callback):
        super().__init__()
        import threading
        self.items, self.token = items, token
        self.cancelled = threading.Event()
        # The carrier has no owner on purpose: it must outlive the browser
        # that queued the work.  The browser keeps the job alive until it
        # finished, and that is where the carrier is released -- on the GUI
        # thread, never on a worker.
        self.signals = gui_signal_carrier(_RowSignals(),
                                          QtCore.QCoreApplication.instance())
        self.signals.result.connect(callback)

    def run(self):
        from prism.source_store import storage_bytes
        try:
            value = storage_bytes(self.items, {}, cancel=self.cancelled.is_set)
        except Exception as error:
            trace_record('asset_storage_summary_failed', error=type(error).__name__)
            value = None
        try:
            self.signals.result.emit((self, value))
        except RuntimeError:
            pass  # Native owner may already have been deleted.
        finally:
            self.items = None


class _RowWorker(QtCore.QRunnable):
    """对一批纯数据行做过滤与排序（后台线程）。

    过滤与排序的**顺序**逐条对应同步路径，所以两条路径给出的是同一批
    行、同一个顺序。工作线程只碰字符串、数字、布尔和被搬运的对象引用。
    """

    def __init__(self, rows, spec, token, callback):
        super().__init__()
        self.rows = rows
        self.spec = spec
        self.token = token
        # The carrier has no owner on purpose; see ``_StorageWorker``.
        self.signals = gui_signal_carrier(_RowSignals(),
                                          QtCore.QCoreApplication.instance())
        self.signals.result.connect(callback)

    def run(self):
        try:
            result = self.token, self._process(self.rows), None
        except Exception as error:
            result = self.token, None, str(error)
        try:
            self.signals.result.emit(result)
        except RuntimeError:
            pass

    def _process(self, rows):
        spec = self.spec
        # 目录口径的排序：两条路径给出的原始顺序不同（目录路径已经排好，
        # 场景路径是场景枚举顺序），这里统一按同一把尺子排一次。
        rows.sort(key=lambda row: row.sort_key)
        rows = [row for row in rows if row.flags[_AssetRow.F_ACTIVE]]
        rows = [row for row in rows if row.flags[_AssetRow.F_UNCLASSIFIED]]
        mode = spec['mode']
        if mode == 'recent':
            rows = [row for row in rows if row.last_used]
            rows.sort(key=lambda row: row.last_used, reverse=True)
        elif mode == 'random':
            import random
            random.Random(spec['random_seed']).shuffle(rows)
        rows = [row for row in rows if row.flags[_AssetRow.F_RULE]]
        rows = [row for row in rows if row.flags[_AssetRow.F_SCOPE]]
        # 名称/评分排序留在落点那一步（`_install_items`）：它作用在**最终
        # 装进模型的那批素材**上，统一搜索命中也要按它排，和同步路径一致。
        return rows


def asset_name(item):
    if isinstance(item, dict):
        return item['title']
    return getattr(item, "_title", "") or os.path.basename(getattr(item, "filename", "") or "") or "未命名素材"


def _has_catalog_proxy(items):
    """Whether any of ``items`` is a directory proxy.

    The check used to be ``getattr(item, '_catalog_proxy', False)`` over
    every item. For a project that is not served from the directory that
    means 100 000 attribute probes that all *miss*, and every miss on an
    ``ImageRecord`` goes through ``__getattr__``, raises and is caught.
    The trace put that at 8.4 s of a single dispatch.

    An ``isinstance`` test answers the same question without raising
    anything, and it short-circuits on the first proxy -- but it depends on
    the *class symbol* ``CatalogAssetProxy``.  That symbol can be swapped
    out (a test that watches the constructor replaces it with a wrapper),
    and then the check either raises
    ``TypeError: isinstance() arg 2 must be a type`` or silently answers
    "no proxies" for rows that are proxies.

    The marker lives on the proxy's own class, so asking the item's own
    type answers the question without depending on any module-level name:

        getattr(type(item), '_catalog_proxy', False)

    A lookup on the type is a plain type lookup: it never goes through the
    instance ``__getattr__`` that made the original probe expensive.
    """
    return any(
        getattr(type(item), '_catalog_proxy', False)
        for item in items
    )


class _TagIndexCache:
    """One refresh's memo for the tag name index of a result row.

    `prism.tags.name_index(row_tags, system)` is the union of what every
    individual tag contributes (the tag itself, every level of its path and
    every implied parent).  So the per-tag contribution can be computed once
    per **distinct tag** and reused by every row carrying it, and the per-row
    index can be memoised by the row's tag tuple.  `prism/tags.py` itself is
    untouched: this only stops the same set from being rebuilt per row.
    """

    __slots__ = ('_system', '_parts', '_rows', '_masks', '_masks_active')

    #: Upper bound for the memo tables.  A project has a small tag vocabulary,
    #: so this is never reached in practice; it only keeps a pathological
    #: project (every row carries its own unique tag) from turning the memo
    #: into the memory problem it is meant to avoid.
    _LIMIT = 8192

    def __init__(self, system):
        self._system = system
        self._parts = {}
        self._rows = {}
        self._masks = {}
        self._masks_active = None

    @staticmethod
    def _key(row_tags):
        try:
            key = tuple(row_tags or ())
            hash(key)
            return key
        except TypeError:
            return None

    def names(self, row_tags):
        """The name index of one row - the same set as `tags.name_index`."""
        key = self._key(row_tags)
        if key is not None:
            names = self._rows.get(key)
            if names is not None:
                return names
        from prism import tags as tags_module
        names = set()
        for tag in (row_tags or ()):
            part = self._parts.get(tag)
            if part is None:
                part = frozenset(tags_module.name_index((tag,), self._system))
                if len(self._parts) < self._LIMIT:
                    self._parts[tag] = part
            names |= part
        names = frozenset(names)
        if key is not None and len(self._rows) < self._LIMIT:
            self._rows[key] = names
        return names

    def has_active(self, row_tags, active_tags):
        """Whether the row carries **every** active tag.

        Same answer as `active_tags.issubset(name_index(row_tags, system))`:
        the mask is the subset of the active tags the row's index contains.
        """
        if not active_tags:
            return True
        if self._masks_active != active_tags:
            # 掩码记忆化只对**同一组**激活标签成立：换了组就清空，
            # 否则会拿上一组的答案回答这一组（该刷新却没刷新）。
            self._masks_active = active_tags
            self._masks = {}
        key = self._key(row_tags)
        mask = self._masks.get(key) if key is not None else None
        if mask is None:
            names = self.names(row_tags)
            mask = frozenset(name for name in active_tags if name in names)
            if key is not None and len(self._masks) < self._LIMIT:
                self._masks[key] = mask
        return mask == active_tags


class AssetModel(QtCore.QAbstractListModel):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.items = []
        self.presentation_evidence = None
        self.layout_token = None
        self._replacing = False
        self._replace_pending = None

    def rowCount(self, parent=QtCore.QModelIndex()):
        return 0 if parent.isValid() else len(self.items)

    def data(self, index, role=QtCore.Qt.ItemDataRole.DisplayRole):
        if not index.isValid() or index.row() >= len(self.items):
            return None
        item = self.items[index.row()]
        if role == QtCore.Qt.ItemDataRole.UserRole:
            return item
        if role in (QtCore.Qt.ItemDataRole.DisplayRole, QtCore.Qt.ItemDataRole.ToolTipRole):
            return asset_name(item)

    def replace(self, items, changed=None, *, evidence=None, guard=None,
                publication_fields=None, layout_token=None):
        """Publish identity with rows; the last synchronous reset request wins.

        Nested resets are queued rather than interleaved. An old callback can
        never label a replacement installed by a reentrant callback as its own.
        Legacy callers clear the presentation identity when replacing rows.
        """
        request = items, changed, evidence, guard, publication_fields or {}, layout_token
        if self._replacing:
            pending = self._replace_pending
            # Install first: cancellation observers may queue a newer request.
            self._replace_pending = request
            if pending is not None:
                discarded = pending[2]
                if (discarded is not None and discarded is not evidence
                        and discarded is not self.presentation_evidence):
                    discarded.finish('catalog_query_cancelled', cancelled=True,
                                     reason='superseded_pending_reset')
            return False
        self._replacing = True
        try:
            while request is not None:
                values, changed, current, guard, extra, next_layout_token = request
                self._replace_pending = None
                valid = lambda: ((guard is None or guard()) and
                                 (current is None or current.active))
                if valid():
                    if changed is None:
                        changed = values != self.items
                    unchanged_metadata = (
                        next_layout_token is not None
                        and next_layout_token == self.layout_token
                        and len(values) == len(self.items)
                        and all(new is old for new, old in zip(values, self.items)))
                    previous = self.presentation_evidence
                    if changed:
                        self.beginResetModel()
                    try:
                        if valid() and self._replace_pending is None:
                            self.items = values
                            self.presentation_evidence = current
                            self.layout_token = next_layout_token
                            owner = self.parent()
                            if owner is not None:
                                owner._catalog_layout_token = next_layout_token
                            if previous is not None and previous is not current:
                                previous.finish('catalog_query_cancelled', cancelled=True,
                                                reason='model_replaced')
                    finally:
                        if changed:
                            self.endResetModel()
                    if (self.items is values and self.presentation_evidence is current
                            and valid() and self._replace_pending is None):
                        if current is not None:
                            # Model counts describe this publication, not query lifetime.
                            # Recreate the bounded counter; production retains no IDs.
                            with current._lock:
                                current._counts.pop('model', None)
                                if current.diagnostic:
                                    current.observe('model', (model_item_id(item) for item in values))
                                counts = current.snapshot('model')
                            current.final = True
                            current.record('asset_model_published', **extra,
                                model_rows=self.rowCount(), model_rows_unit='model_rows',
                                model_rows_cumulative=False, model_rows_deduplicated=False,
                                model_unique_ids=counts['unique_rows'],
                                model_unique_ids_unit='domain_qualified_ids',
                                model_unique_ids_cumulative=False,
                                model_unique_ids_deduplicated=True,
                                model_unique_ids_complete=counts['unique_rows_complete'],
                                id_budget=counts['id_budget'],
                                id_budget_exhausted=counts['id_budget_exhausted'],
                                complete=True, final=True)
                        if not changed and values and not unchanged_metadata:
                            self.dataChanged.emit(self.index(0), self.index(len(values)-1))
                    if (current is not None and current is not self.presentation_evidence
                            and self._replace_pending is not None
                            and current is not self._replace_pending[2]):
                        current.finish('catalog_query_cancelled', cancelled=True,
                                       reason='superseded_during_reset')
                request = self._replace_pending
        finally:
            self._replacing = False
        return self.items is items and self.presentation_evidence is evidence

    def append_preview(self, items, *, guard):
        """Append read-only loading rows without resetting scroll/selection."""
        if self._replacing or not items or not guard():
            return False
        current = self.items
        self._replacing = True
        try:
            start = len(current)
            self.beginInsertRows(QtCore.QModelIndex(), start, start + len(items) - 1)
            try:
                current.extend(items)
            finally:
                self.endInsertRows()
        finally:
            self._replacing = False
        pending, self._replace_pending = self._replace_pending, None
        if pending is not None:
            values, changed, evidence, check, fields, layout = pending
            self.replace(values, changed, evidence=evidence, guard=check,
                         publication_fields=fields, layout_token=layout)
        return self.items is current and guard()


class AssetDelegate(QtWidgets.QStyledItemDelegate):
    def sizeHint(self, option, index):
        return self.parent().gridSize()

    def paint(self, painter, option, index):
        item = index.data(QtCore.Qt.ItemDataRole.UserRole)
        if item is None or (not isinstance(item, dict) and is_deleted(item)):
            return
        painter.save()
        canvas = self.parent()
        if getattr(item, '_catalog_proxy', False):
            if item._catalog_display_failed:
                painter.setPen(QtGui.QColor('#d0d0d0'))
                painter.drawText(option.rect, QtCore.Qt.AlignmentFlag.AlignCenter, _('Preview unavailable; open source canvas'))
                painter.restore()
                return
            if not item._catalog_display_ready:
                item.request_catalog_display(canvas)
                painter.setPen(QtGui.QColor('#a0a0a0'))
                painter.drawText(option.rect, QtCore.Qt.AlignmentFlag.AlignCenter, _('Loading preview…'))
                painter.restore()
                return
        if isinstance(item, dict):
            # Use the same reference-card renderer as global search. These
            # cards never insert or mutate the source page in the scene.
            from prism.widgets.search_canvas import SearchCard
            if not hasattr(self, '_page_card'):
                self._page_card = SearchCard(item, canvas)
                self._page_card.setParent(self)
            card = self._page_card
            card.result = item
            card.setSelected(bool(option.state & QtWidgets.QStyle.StateFlag.State_Selected))
            painter.translate(option.rect.topLeft())
            painter.scale(option.rect.width()/card.WIDTH, option.rect.height()/card.HEIGHT)
            card.paint(painter, option)
            painter.restore()
            return
        if getattr(canvas, 'colour_group', None) and getattr(item, 'color_group', None) != canvas.colour_group:
            painter.setOpacity(.2)
        painter.setClipRect(option.rect)
        image_rect = option.rect
        from prism import lod
        lod.draw_image(painter, item, QtCore.QRectF(image_rect), canvas,
                       grayscale=getattr(canvas, 'display_grayscale', False))
        painter.restore()



class AssetBrowser(QtWidgets.QWidget):
    def __init__(self, window):
        super().__init__()
        # A catalog query must reach a terminal state even when the session
        # ends while it is still in flight: the hook gives every in-flight
        # query a shutdown reason, so the log can tell "the user closed the
        # application" from "the product forgot to finish".  It is installed
        # once, as early as the browser exists.
        install_shutdown_hook()
        self.window_host = window
        self.view = window.view
        self.catalog = self.view.category_panel.catalog
        self.mode = 'all'
        self.history = []
        self.history_index = -1
        self.sort_key = 'default'
        self.card_size = 156
        self.compact = False
        self.scope_pages = None
        self.saved_rule = None
        self._selecting = False
        self._catalog_generation = 0
        self._catalog_metadata_epoch = 0
        self._catalog_cancel = None
        self._catalog_pending = None
        self._catalog_query_job = None
        # 任务在结果送达前一直由 GUI 侧持有：线程池在工作线程里删除
        # runnable，那次删除不能成为某个 Qt 对象的最后一个引用。
        self._background_jobs = JobRegistry()
        # AI-06: the reuse layer.  Completed results and in-flight prefix
        # checkpoints are kept apart, and the completed ones are bounded by
        # **bytes** (see `catalog_browser.CatalogResultCache`), not by a row
        # count.  The previous row-count budget was raised from 131072 to
        # 262144 to make "all" + "untagged" fit; that hid the cost instead of
        # bounding it.
        self._catalog_results = CatalogResultCache()
        self._catalog_result_digest = None
        self._catalog_layout_token = None
        # Data identity is independent of viewport geometry. One compact
        # id->row table refers to the current result; it owns no extra proxies.
        self._catalog_data_index = None
        self._catalog_preview_active = False
        self._catalog_preview_owner = None
        # The business identity of the publications this browser produces.
        # A session id separates two runs of the same project; the query
        # generation separates two *requests*; the sequence orders the
        # publications inside one request.  The wall clock is deliberately not
        # used: it does not order anything, and a trace sequence belongs to
        # the log rather than to the product.
        self._catalog_session_id = uuid.uuid4().hex
        self._catalog_publication_generation = 0
        self._catalog_publication_seq = 0
        # The open directory row streams, keyed by (project, source revision,
        # pushed-down filters).  A filter change means a different row set,
        # so the streams cannot be one.  They are kept apart - and kept
        # *open* - because a prefix checkpoint belongs to the stream it was
        # built from and must stay resumable.
        self._catalog_streams = OrderedDict()
        # AI-06: pushing a filter into the directory read splits the physical
        # read per filter set, which conflicts with the contract that one
        # source revision is read once and replayed for every filter change.
        # It stays off until it is measured against that contract; see
        # `_catalog_pushdown_filters`.
        self._catalog_pushdown_enabled = False
        self._preserve_catalog_display = False
        # A destroyed receiver cannot receive the worker's terminal slot.
        # Retire its Python-side generation/pending state at destruction too.
        state = self.__dict__
        def retire_catalog_work():
            # Use detached Python state: even getattr on a deleted SIP object
            # can consult Qt when an optional attribute has never been set.
            state['_catalog_generation'] += 1
            cancel = state.get('_catalog_cancel')
            if cancel is not None:
                cancel.set()
            cached = state.get('_catalog_scalar_cache')
            if cached is not None and hasattr(cached[1], 'close'):
                cached[1].close()
            for stream in list(state.get('_catalog_streams', {}).values()):
                if hasattr(stream, 'close'):
                    stream.close()
            state['_catalog_streams'] = {}
            results = state.get('_catalog_results')
            if results is not None:
                results.clear('receiver_destroyed')
            state['_catalog_pending'] = None
            state['_catalog_query_job'] = None
            state['_catalog_data_index'] = None
            state['_catalog_preview_owner'] = None
            worker = state.get('_storage_worker')
            if worker is not None:
                worker.cancelled.set()
            state['_storage_pending'] = None
        self.destroyed.connect(retire_catalog_work)
        self._catalog_proxy_cache = OrderedDict()
        # 场景扫描（目录不可用时的全项目查询）的在途状态。`_refresh_generation`
        # 是**代数令牌**：每次刷新作废上一次的结果，旧结果不许覆盖新结果。
        self._refresh_generation = 0
        self._scan_token = None
        self._scan_state = None
        self._scan_spec = None
        self._row_worker = None
        self._layout_hint = None
        self.setObjectName('assetBrowser')
        outer = QtWidgets.QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)
        toolbar = QtWidgets.QFrame()
        self.toolbar = toolbar
        toolbar.setObjectName('assetBrowserToolbar')
        toolbar.installEventFilter(self)
        toolbar.setFixedHeight(40)
        row = QtWidgets.QHBoxLayout(toolbar)
        row.setContentsMargins(8, 0, 10, 0)
        row.setSpacing(5)
        self.back = icon_button('chevron-left', '后退', size=26)
        self.forward = icon_button('chevron-right', '前进', size=26)
        self.back.clicked.connect(lambda: self.travel(-1))
        self.forward.clicked.connect(lambda: self.travel(1))
        row.addWidget(self.back)
        row.addWidget(self.forward)
        self.breadcrumb = QtWidgets.QLabel(_('All assets'))
        self.breadcrumb.setMinimumWidth(0)
        self.breadcrumb.setSizePolicy(QtWidgets.QSizePolicy.Policy.Ignored,QtWidgets.QSizePolicy.Policy.Preferred)
        row.addWidget(self.breadcrumb)
        row.addStretch()
        zoom = QtWidgets.QSlider(QtCore.Qt.Orientation.Horizontal)
        self.zoom_slider = zoom
        zoom.setRange(96, 280)
        zoom.setValue(156)
        zoom.setFixedWidth(120)
        zoom.setToolTip('缩略图大小')
        zoom.valueChanged.connect(self.resize_cards)
        row.addStretch()
        self.sort_button = icon_button('layout', '排列与排序', size=28)
        self.sort_button.clicked.connect(self.sort_menu)
        row.addWidget(self.sort_button)
        from .thumbnail_zoom import thumbnail_zoom_bar
        self.zoom_bar=thumbnail_zoom_bar(self.zoom_slider)
        self.zoom_bar.setSizePolicy(QtWidgets.QSizePolicy.Policy.Minimum,QtWidgets.QSizePolicy.Policy.Fixed)
        row.addWidget(self.zoom_bar)
        filters = icon_button('filter', '筛选素材', size=28)
        filters.clicked.connect(self.filter_menu)
        self.filter_button=filters
        row.addWidget(filters)
        quick = icon_button('star', '批量评分 / 标签', size=28)
        quick.clicked.connect(self.quick_actions)
        row.addWidget(quick)
        self.search_type = self.view.category_panel.make_search_type_combo()
        row.addWidget(self.search_type)
        self.search = QtWidgets.QLineEdit()
        self.search.setPlaceholderText(_('Search names and content'))
        self.search.setClearButtonEnabled(True)
        self.search.addAction(icon('search'), QtWidgets.QLineEdit.ActionPosition.LeadingPosition)
        self.search.setMaximumWidth(260)
        self.search.textChanged.connect(self.schedule)
        row.addWidget(self.search)
        outer.addWidget(toolbar)
        from prism.widgets.asset_canvas import AssetCanvas
        from prism.widgets.asset_grid import AssetGrid, GridDelegate
        self.canvas_list = AssetCanvas(self)
        self.grid_list = AssetGrid(self)
        self.grid_list.card_size_changed.connect(self.zoom_slider.setValue)
        self.list = self.grid_list
        self.model = AssetModel(self)
        for presentation in (self.canvas_list, self.grid_list):
            presentation.setObjectName('assetGrid' if presentation is self.grid_list else 'assetCanvas')
            presentation.setContextMenuPolicy(QtCore.Qt.ContextMenuPolicy.CustomContextMenu)
            presentation.customContextMenuRequested.connect(self.context_menu)
            presentation.installEventFilter(self)
            presentation.setModel(self.model)
            renderer = AssetDelegate(presentation)
            presentation.setItemDelegate(GridDelegate(presentation, renderer)
                                         if presentation is self.grid_list else renderer)
            presentation.selectionModel().selectionChanged.connect(
                lambda *args, source=presentation: self.select_assets()
                if self.list is source else None)
            presentation.doubleClicked.connect(self.locate)
            outer.addWidget(presentation, 1)
        self.canvas_list.hide()
        self.resize_cards(156)
        self.empty = QtWidgets.QLabel(_('No matching results'))
        self.empty.setObjectName('assetEmpty')
        self.empty.installEventFilter(self)
        self.empty.setAlignment(QtCore.Qt.AlignmentFlag.AlignCenter)
        outer.addWidget(self.empty, 1)
        self.empty.hide()
        self.summary = QtWidgets.QLabel()
        self.summary.setObjectName('assetBrowserSummary')
        outer.addWidget(self.summary)
        self.summary.hide()

        self.timer = QtCore.QTimer(self)
        self.timer.setSingleShot(True)
        self.timer.setInterval(180)
        self.timer.timeout.connect(self._refresh_if_changed)
        self.view.scene.assets_changed.connect(self.schedule)
        self.view.scene.metadata_changed.connect(self.schedule)
        self.view.scene.metadata_changed.connect(self._invalidate_catalog_result)
        self.view.category_panel.resource_nodes_changed.connect(self._invalidate_catalog_result)
        self.view.category_panel.resource_nodes_changed.connect(self.schedule)
        self.view.category_panel.material_records_changed.connect(self._material_resource_changed)
        self.view.category_panel.resource_usage_changed.connect(self._usage_changed)
        self.view.category_panel.resource_records_changed.connect(self._records_changed)
        from prism.lod import cache
        cache().changed.connect(self._display_ready)

    @QtCore.pyqtSlot(object)
    def _display_ready(self, item):
        if self.isVisible() and self.list.needs_display(item):
            self.list.request_display_refresh(item)

    def schedule(self, *_args):
        if self.isVisible() and not self._selecting and not self.timer.isActive():
            self.timer.start()

    def _scene_refresh_key(self):
        panel, scene = self.view.category_panel, self.view.scene
        return (scene.project_id, getattr(scene, '_asset_revision', None),
                getattr(panel.resource_service, 'revision', None),
                getattr(panel.material_service, 'revision', None),
                getattr(self, '_catalog_metadata_epoch', None),
                getattr(panel, '_usage_revision', 0) if self.mode == 'recent' else None,
                self.mode, self.sort_key, tuple(self.scope_pages or ()),
                self.scope_pages is None, self.search.text(), panel.search_kind,
                panel._min_rating, frozenset(panel._active_tags),
                repr(panel._active_filter), repr(self.saved_rule),
                getattr(self, '_random_seed', None),
                id(getattr(self, 'visual_results', None)))

    def _refresh_if_changed(self):
        if (not getattr(self.view.scene, '_partial_project_source', None)
                and getattr(self, '_last_scene_refresh_key', None) == self._scene_refresh_key()):
            return
        self.refresh()

    def showEvent(self, event):
        super().showEvent(event)
        if not getattr(self, '_opening', False):
            self.refresh()

    def hideEvent(self, event):
        worker = getattr(self, '_storage_worker', None)
        if worker is not None:
            worker.cancelled.set()
        self._storage_pending = None
        self._cancel_catalog_query()
        self._cancel_scene_scan()
        self.list.clear_annotations()
        super().hideEvent(event)

    def resize_cards(self, value):
        self.card_size = value
        size = QtCore.QSize(value, int(value*(.40 if self.compact else .72))+8)
        self.grid_list.setGridSize(size)
        if self.list is self.canvas_list:
            self.canvas_list.setGridSize(size)

    @measured('assets.open', 0)
    def open(self, mode='all', scope_pages=None, title=None, record=True):
        if not self.window_host._navigation_is_committed():
            return self.window_host.request_navigation(
                lambda: self.open(mode, scope_pages, title, record))
        previous_page = self.window_host.tabs.currentWidget()
        if self.window_host._store_current_page(previous_page) is False:
            return
        trace_record('asset_scope', mode=mode, scope_pages=scope_pages, title=title)
        self.list.clear_annotations()
        previous_mode = self.mode
        # Folder browsing shares the resource grid; actual canvas pages keep
        # their own editor, and visual/smart boards retain their presentation.
        grid = (mode in ('all', 'unclassified', 'untagged', 'recent', 'trash', 'pages')
                and not self.saved_rule)
        previous_view = self.list
        self.list = self.grid_list if grid else self.canvas_list
        if previous_view is not self.list:
            previous_view.hide()
            previous_view.selectionModel().clearSelection()
            if previous_view is self.canvas_list:
                previous_view.suspend_for_grid()
            self.resize_cards(self.card_size)
            if self.list is self.canvas_list:
                self.list.rebuild()
            self.list.show()
        elif grid:
            self.grid_list.reset_position()
        self.mode = mode
        self.sort_button.setEnabled(mode != 'visual')
        if mode == 'random':
            import random
            self._random_seed = random.randrange(2**32)
        self.scope_pages = scope_pages
        self.title = title or {'all':_('All assets'), 'untagged':'未标签', 'unclassified':'未分类',
                               'recent':'最近使用', 'trash':'回收站', 'random':'随机模式'}.get(mode, '素材')
        if record or not self.history:
            self.history = self.history[:self.history_index+1] + [(mode, scope_pages, self.title, dict(self.saved_rule) if self.saved_rule else None, self.search.text())]
            self.history_index = len(self.history)-1
        self._opening = True
        try:
            self.window_host._set_widget_after_page_store(self, previous_page)
        finally:
            self._opening = False
        self.list.selectionModel().clearSelection()
        self.view.scene.clearSelection()
        self.window_host.filter_bar.hide()
        previous_items = self.model.items
        # 分片路径下结果不是立刻就到的，"行没变但排版策略变了"这件事改到
        # 落点那一步判断（`_install_items`），判据不变。
        self._layout_hint = (None if getattr(self.view.scene, '_partial_project_source', None)
                             else (previous_items, previous_mode))
        self.refresh()
        self.window_host.canvas_toolbar.refresh()
        self.view.category_panel._update_navigation()
        self.view.category_panel.resource_tree.show()
        self.view.category_panel.search_results.hide()

    def reset_scope(self, *, mode='all', scope_pages=None, title=None):
        """Set the scope WITHOUT navigating to the browser.

        Opening a project lands on the canvas.  The browser still has to end
        up with a scope that belongs to THIS project -- the scope of the
        previous project must not survive the open -- but switching the
        visible tab to the browser is exactly what made an open look like
        "all assets" instead of "my first canvas".

        The scope is *reset*, not narrowed: ``all`` means the whole project
        (§6), so pinning it to the canvas that was landed on would
        re-introduce the current-page restriction that §6 removed.  The
        explicit left-bar entry is what opens the browser, and it applies the
        project-wide scope.
        """
        self._preserve_catalog_display = False
        self.saved_rule = None
        self.search.clear()
        self.mode = mode
        self.scope_pages = scope_pages
        self.title = title or {'all': _('All assets'), 'untagged': '未标签',
                               'unclassified': '未分类', 'recent': '最近使用',
                               'random': '随机模式'}.get(mode, '素材')
        self.refresh()

    def travel(self, offset):
        target = self.history_index + offset
        if 0 <= target < len(self.history):
            self.history_index = target
            mode, scope, title, rule, query = self.history[target]
            self.saved_rule = rule
            self.search.setText(query)
            self.open(mode, scope, title, record=False)

    def refresh(self):
        if getattr(self.view, '_project_opening', False):
            self.timer.start()
            return
        # An explicit open/refresh already satisfies the pending refresh.
        # Leaving it queued rebuilds the view again just after navigation.
        self.timer.stop()
        self.catalog = self.view.category_panel.catalog
        if self.mode == 'pages':
            from prism import project_resources
            self._cancel_catalog_query()
            self._begin_refresh()
            self.list.setEnabled(True)
            panel = self.view.category_panel
            items = project_resources.folder_page_results(
                self.view.scene, self.scope_pages or (),
                page_records=panel._pages_with_implicit_defaults(),
                **panel.resource_scope_options(self.search.text(), panel.search_kind))
            return self._install_items(items, dict(unified=False, input_key=self._scene_refresh_key()))
        if getattr(self.view.scene, '_partial_project_source', None):
            # 按页打开时目录才是全项目口径；以图搜图结果（`visual_results`）
            # 和智能文件夹（`saved_rule`）同样在里面排序，不再另外走一遍只
            # 看已加载一页的 `catalog.query()`。
            # The directory installer owns its layout/checkpoints. This hint
            # belongs only to the legacy scan path and otherwise pins an old
            # entire proxy generation without ever being consumed.
            self._layout_hint = None
            return self._refresh_catalog_assets()
        self._cancel_catalog_query()
        self.list.setEnabled(True)
        text, expression = self.view.category_panel._split_query(self.search.text())
        kind = self.view.category_panel.search_kind
        if self.mode in ('unclassified', 'untagged') and kind == 'all':
            kind = 'asset'
        unified = self.mode != 'visual' and (bool(self.search.text().strip()) or kind not in ('all', 'asset'))
        # The smart-folder rule rides on the **same** query instead of a
        # second full-project pass.  Both of its scalar constraints are
        # conjunctions ("every word must hit" / "rating >= n"), so folding
        # them into the first query returns exactly the same rows.  Its tag
        # expression is evaluated per row below with the same `prism.tags`
        # primitives the catalog itself uses, so the result set is
        # unchanged - only the second traversal of the whole project is gone.
        rule = self.saved_rule or {}
        rule_text = str(rule.get('text', '') or '')
        rule_expression = str(rule.get('tag_expression', '') or '')
        rule_rating = int(rule.get('rating', 0) or 0)
        query_text = '' if unified else text
        if rule_text:
            query_text = f'{query_text} {rule_text}' if query_text else rule_text
        query_rating = max(int(self.view.category_panel._min_rating or 0), rule_rating)
        spec = {
            'input_key': self._scene_refresh_key(),
            'kind': kind,
            'unified': unified,
            'mode': self.mode,
            'expression': expression,
            'query_text': query_text,
            'query_rating': query_rating,
            'rule_expression': rule_expression,
            'sort_key': self.sort_key,
            'random_seed': getattr(self, '_random_seed', 0),
            'layout_hint': getattr(self, '_layout_hint', None),
        }
        self._layout_hint = None
        self._scan_spec = spec
        token = self._begin_refresh()
        if (self.mode in _RESOURCE_SCOPES
                and self.scope_pages is None and not self.saved_rule):
            # 左栏那五个口径：一张混排列表。图片素材、画布、脑图、文档、材质、
            # 模型平级躺在同一张列表上 —— 口径只决定「哪些在列表里」，不改变
            # 列表长什么样。搜索框里有词时走统一搜索（那也已经是混排的）；
            # 限定了画布或带标签规则时仍走素材管线 —— 那是另一种范围。
            return self._refresh_resource_scope(spec)
        # 目录可用 → `catalog.query()` 走目录（毫秒级）；目录不可用 → 它
        # 退化成场景内存扫描（秒级，GUI 冻结的根因：十万级素材下这一次
        # 同步查询就把 GUI 堵住一秒多）。所以目录不可用时改走
        # "分片扫描 + 后台纯数据过滤排序"，GUI 回合不再被一次查询堵死。
        # A 组的 T1 落地后，全量打开下目录也可用 → 自动回到同步路径，
        # 不会因为新写的扫描路径而二次退化。
        if self._catalog_scope_available():
            return self._refresh_synchronous(spec)
        return self._start_scene_scan(spec, token)

    def _refresh_resource_scope(self, spec):
        """左栏那五个口径的列表：一张混排墙。

        图片素材是素材对象（网格用真实缩略图），其余几类是结果字典（网格
        用 ``SearchCard`` 画卡片，封面是标题文字）。回收站口径收集的是软删
        的那批 —— 搜索只看活着的资源，软删的得单独收。
        """
        from prism import project_resources
        panel = self.view.category_panel
        scope = (self.catalog._catalog_scope()
                 if getattr(self.view.scene, '_partial_project_source', None) else None)
        query_key = self._catalog_query_key(scope) if scope is not None else None
        digest = query_key.digest() if query_key is not None else None
        entry = self._catalog_results.get(digest) if digest is not None else None
        if entry is not None and entry.get('items') is not None:
            items = entry['items']
            self._catalog_results.pin(digest)
            self._install_items(items, dict(spec, unified=False))
            trace_record('catalog_completed_cache_lookup', hit=True, mode=self.mode,
                         cache_stats=self._catalog_results.stats())
            return items
        items = project_resources.collect(
            self.view.scene, panel.resource_service, self.mode,
            recent=self._recent_usage_entries(), catalog=self.catalog,
            page_records=panel._pages_with_implicit_defaults(),
            **panel.resource_scope_options(self.search.text(), panel.search_kind))
        self._install_items(items, dict(spec, unified=False))
        if query_key is not None:
            self._remember_completed_result({}, items, query_key, digest)
        return items

    def _recent_usage_entries(self):
        """最近使用账本（用户级目录，按工程分组）。"""
        from prism import project_resources
        return project_resources.recent_entries(
            getattr(self.view.scene, 'project_id', ''))

    def _install_items(self, items, spec):
        """把一批素材装进模型并刷新 chrome。

        这是**两条路径共同的落点**：同步路径（目录可用）和分片扫描路径
        （目录不可用）给出的都是同一批素材，顺序也一致。
        """
        self.search_hits = []
        if spec['unified']:
            hits = [r for r in self.view.category_panel.project_search_results(self.search.text())
                    if r['kind'] not in ('folder', 'tag')]
            allowed = set(items)
            asset_scope = self.mode != 'all' or self.saved_rule or self.scope_pages is not None
            kind = spec['kind']
            self.search_hits = [r for r in hits if
                                (kind != 'asset' or r['kind'] == 'asset') and
                                (r.get('item') in allowed if r['kind'] == 'asset' else
                                 (not asset_scope or kind in ('document', 'mindmap', 'canvas')))]
            items = [r['item'] if r['kind'] == 'asset' else r for r in self.search_hits]
        if self.mode == 'visual':
            allowed = set(items)
            items = [item for item, score in getattr(self, 'visual_results', []) if item in allowed]
            if getattr(self, 'visual_project', None) != self.view.scene.project_id:
                items = []
        elif self.sort_key == 'name':
            items.sort(key=lambda i: asset_name(i).casefold())
        elif self.sort_key == 'rating':
            items.sort(key=lambda i: getattr(i, '_rating', 0), reverse=True)
        self._selecting = True
        # One comparison, reused by the model: the browser has to know
        # whether the row set changed (to clear the scene selection) and the
        # model has to know the same thing (to decide between a reset and a
        # data-changed).  Asking twice walked the whole list twice.
        changed = items != self.model.items
        if changed:
            self.view.scene.clearSelection()
        self.model.replace(items, changed=changed)
        self._selecting = False
        # 视觉模式换了排版策略、行没变时需要重新排布。同步路径下这件事
        # 发生在 `open()` 里紧跟 `refresh()`；分片路径下结果还没到，所以
        # 改到落点这里做 —— 判据是同一个（行没变）。
        hint = spec.get('layout_hint')
        if (hint is not None and not changed
                and (hint[1] == 'visual') != (self.mode == 'visual')
                and not getattr(self.view, '_project_opening', False)):
            self.list.arrange()
        self.back.setEnabled(self.history_index > 0)
        self.forward.setEnabled(self.history_index < len(self.history)-1)
        self.breadcrumb.setText(f'{getattr(self, "title", _("All assets"))} / 搜索结果 ({len(items)})' if self.search.text() else getattr(self, 'title', _('All assets')))
        self.summary.setText(f'{len(items):,} ' + _('assets · Scroll to browse · Double-click to open source') if getattr(self.list, 'is_asset_grid', False) else f'{len(items):,} 个素材 · 滚轮缩放 · 中键平移 · 双击定位到画布')
        self.empty.setVisible(not items)
        self.list.setVisible(bool(items))
        if not self.view.scene.selectedItems(user_only=True):
            self.show_scope_info(items)
        if hasattr(self.window_host, '_status_text'):
            self.window_host._update_status({})
        self._last_scene_refresh_key = spec.get('input_key')

    # ── 全项目查询的两条路径 ────────────────────────────────────
    #
    # 目录可用 → `catalog.query()` 走目录（毫秒级），同步做完。
    # 目录不可用 → 同一个 `query()` 会退化成**场景内存扫描**（秒级），
    #   十万级素材下这一次同步查询就把 GUI 堵住一秒多（GUI-BASELINE.md
    #   §2 根因①）。这条路径改成三步：
    #
    #   1. GUI 线程**分片**扫描场景：每个回合最多花 SCENE_SCAN_SLICE_MS
    #      毫秒，剩下的留给下一个回合（`QTimer.singleShot(0, ...)`，不用
    #      `processEvents` 忙等、不 `sleep`）。扫描只读场景对象的**标量
    #      字段**，产出不可变的纯数据行。
    #   2. 后台线程对纯数据行做过滤与排序 —— 不解引用任何场景对象。
    #   3. 回 GUI 线程**一次性**装进模型（`model.replace`），UI 不会看到
    #      半截状态。
    #
    # 全程带**代数令牌**：旧一轮的结果回来时令牌已经变了，直接丢弃。

    def _catalog_scope_available(self):
        """目录（catalog）此刻能不能用。

        `project_catalog.query()` 内部就是用 `_catalog_scope()` 决定走目录
        还是退化成场景内存扫描。这里问同一个问题，是为了**不重复**它内部
        的判定：目录可用时继续用 `query()`，不可用时改走分片扫描。
        """
        try:
            return self.catalog._catalog_scope() is not None
        except Exception:
            return False

    def _cancel_scene_scan(self):
        """作废在途的场景扫描与后台任务。

        令牌 = (代数, 工程 id)。任何在途的结果在交付时都要对一次令牌，
        对不上就丢弃 —— **旧结果不许覆盖新结果**。
        """
        self._refresh_generation += 1
        self._scan_token = (self._refresh_generation, self.view.scene.project_id)
        self._scan_state = None

    def _begin_refresh(self):
        """开始一次新的刷新：作废上一次的结果，返回本次的代数令牌。"""
        self._cancel_scene_scan()
        return self._scan_token

    def _refresh_synchronous(self, spec):
        """目录可用时的同步路径（`catalog.query()` 走目录，毫秒级）。"""
        results = self.catalog.query(text=spec['query_text'],
            tag_expression=spec['expression'],
            untagged_only=self.mode == 'untagged',
            min_rating=spec['query_rating'],
            canvas_scope=self.scope_pages)
        from prism import tags
        system = tags.tag_system(self.view.scene)
        active_tags = self.view.category_panel._active_tags
        tag_cache = _TagIndexCache(system)
        # No active tag and no rule expression means "every row matches":
        # the per-row tag index is not needed at all, and building it for
        # 100k rows was pure dead work.
        if active_tags or spec['rule_expression']:
            rows = [r for r in results if tag_cache.has_active(r.tags, active_tags)]
        else:
            rows = list(results)
        items = [r.item for r in rows]
        if self.mode == 'unclassified':
            known = self.catalog._known_canvas_ids()
            rows = [r for r in rows if self.catalog.is_unclassified(r.item, known)]
            items = [r.item for r in rows]
        if self.mode == 'recent':
            pairs = [(r.item, r) for r in rows if getattr(r.item, '_last_used', '')]
            pairs.sort(key=lambda pair: pair[0]._last_used, reverse=True)
            items = [item for item, _ in pairs]
            rows = [row for _, row in pairs]
        if self.mode == 'random':
            import random
            # Shuffling an index list gives the same permutation as shuffling
            # the items themselves (same sequence of draws), and it keeps the
            # rows aligned with the items for the rule filter below.
            order = list(range(len(items)))
            random.Random(getattr(self, '_random_seed', 0)).shuffle(order)
            items = [items[index] for index in order]
            rows = [rows[index] for index in order]
        if spec['rule_expression']:
            predicate = tags.parse_query(spec['rule_expression'])
            pairs = [(r.item, r) for r in rows
                     if predicate.matches(tag_cache.names(r.tags))]
            items = [item for item, _ in pairs]
        if self.scope_pages is not None:
            items = [i for i in items if (getattr(i, '_canvas_id', None) or 'default-canvas') in self.scope_pages]
        self._install_items(items, spec)

    def _scene_scan_context(self, spec):
        """一次场景扫描要用的**上下文**（全部在 GUI 线程上取好）。"""
        from prism import tags
        panel = self.view.category_panel
        return {
            'known': self.catalog._known_canvas_ids(),
            'deleted': self.catalog._deleted_page_ids(),
            'scope': set(self.scope_pages) if self.scope_pages is not None else None,
            'rating': int(spec['query_rating'] or 0),
            'untagged': self.mode == 'untagged',
            # 与 `project_catalog._Predicates.words` 逐字一致：空格分词、
            # casefold、丢掉空词。
            'words': tuple(word.casefold() for word in (spec['query_text'] or '').split() if word),
            'expression': tags.parse_query(spec['expression']) if spec['expression'] else None,
            'rule_expression': tags.parse_query(spec['rule_expression']) if spec['rule_expression'] else None,
            'active': set(panel._active_tags),
            'tag_cache': _TagIndexCache(tags.tag_system(self.view.scene)),
            'paths': {},
        }

    def _scene_source(self):
        """场景里的素材，一行一条。

        这里只取**标量**字段（标签列表），不在后台线程碰场景对象。
        """
        for item in self.catalog.asset_items():
            yield item, list(getattr(item, '_tags', []) or [])

    def _start_scene_scan(self, spec, token):
        """开始一次分片扫描。"""
        context = self._scene_scan_context(spec)
        self._scan_state = {'token': token, 'source': self._scene_source(),
                            'rows': [], 'context': context, 'spec': spec}
        # 第一回合**同步**跑（不走定时器）：它顺带回答了"这次全项目查询
        # 有多大" —— 扫描源在第一回合就被读空，说明工程不大，那就回到同步
        # 路径（`catalog.query()`）：语义与改动前**逐字相同**，结果当场可用。
        # 只有读不空（工程真的大）才继续分片。
        self._scene_scan_step(first=True)

    def _scene_scan_step(self, first=False):
        """一个 GUI 回合的扫描切片。

        时间预算到了就把剩下的**留在状态里**交给下一个回合（不丢工作），
        扫描完就把纯数据行交给后台线程。
        """
        import time
        state = self._scan_state
        if state is None or state['token'] != self._scan_token:
            return
        deadline = time.perf_counter() + SCENE_SCAN_SLICE_MS / 1000.0
        context = state['context']
        rows = state['rows']
        source = state['source']
        processed = 0
        exhausted = False
        while processed < SCENE_SCAN_SLICE_ITEMS:
            try:
                item, item_tags = next(source)
            except StopIteration:
                exhausted = True
                break
            row = self._build_row(item, item_tags, context)
            if row is not None:
                rows.append(row)
            processed += 1
            if time.perf_counter() >= deadline:
                break
        state['rows'] = rows
        if exhausted:
            self._scan_state = None
            if first:
                # 第一回合就把源读空了 → 工程不大 → 回到**同步**路径：
                # 语义与改动前逐字相同（`catalog.query()`），结果当场可用，
                # 不用等后台线程。分片只在工程真的大时才发生。
                self._refresh_synchronous(state['spec'])
                return
            self._start_worker(rows, state['spec'], state['token'])
            return
        QtCore.QTimer.singleShot(0, self._scene_scan_step)

    def _build_row(self, item, item_tags, context):
        """把一条场景素材变成纯数据行；不匹配就返回 None。

        判定逐条对应 `project_catalog._matches`（目录分支和内存分支共用的
        那一个判定函数），所以分片扫描给出的行集合与 `catalog.query()`
        **完全一样**。后面四个布尔量是"尾部过滤"用的：它们不决定这一行
        会不会出现，只决定它在同步路径的**同一位置**上留不留。
        """
        catalog = self.catalog
        if catalog.is_annotation(item):
            return None
        if catalog.is_deleted(item):
            return None
        cid = catalog._effective_canvas_id(item, context['known'])
        if cid in context['deleted']:
            return None
        if context['scope'] is not None and cid not in context['scope']:
            return None
        if context['untagged'] and catalog._has_valid_tags(item):
            return None
        if context['rating']:
            rating = int(getattr(item, '_rating', 0) or 0)
            if rating < context['rating']:
                return None
        names = None
        if context['expression'] is not None:
            names = context['tag_cache'].names(item_tags)
            if not context['expression'].matches(names):
                return None
        if context['words']:
            haystack = catalog._text_haystack(item, item_tags)
            if not all(word in haystack for word in context['words']):
                return None
        # ── 尾部过滤用的四个布尔量 ──
        active_ok = context['tag_cache'].has_active(item_tags, context['active'])
        unclassified_ok = True
        if self.mode == 'unclassified':
            unclassified_ok = catalog.is_unclassified(item, context['known'])
        rule_ok = True
        if context['rule_expression'] is not None:
            if names is None:
                names = context['tag_cache'].names(item_tags)
            rule_ok = context['rule_expression'].matches(names)
        scope_ok = True
        if context['scope'] is not None:
            scope_ok = ((getattr(item, '_canvas_id', None) or 'default-canvas')
                        in context['scope'])
        # ── 排序键：与目录口径逐字一致 ──
        paths = context['paths']
        path = paths.get(cid)
        if path is None:
            path = paths[cid] = catalog.canvas_path(cid)
        title = (getattr(item, '_title', '')
                 or getattr(item, 'filename', '') or '未命名')
        order_key = getattr(item, 'save_id', None)
        if order_key is None:
            order_key = getattr(item, 'original_save_id', None)
        order_key = '' if order_key is None else str(order_key)
        return _AssetRow(item, tuple(item_tags),
                         (path, str(title).casefold(), order_key),
                         getattr(item, '_last_used', ''),
                         (active_ok, unclassified_ok, rule_ok, scope_ok))

    def _start_worker(self, rows, spec, token):
        """把纯数据行交给后台线程过滤与排序。"""
        self._row_worker = _RowWorker(rows, spec, token, self._rows_filtered)
        self._background_jobs.start(catalog_thread_pool(), self._row_worker)

    @QtCore.pyqtSlot(object)
    def _rows_filtered(self, result):
        """后台线程的结果回到 GUI 线程：令牌对不上就丢弃。"""
        token, rows, error = result
        job, self._row_worker = self._row_worker, None
        self._background_jobs.release(job)
        if token != self._scan_token:
            return
        if error:
            self.summary.setText(_('Whole-project assets could not load: ') + error)
            self.summary.show()
            return
        items = [row.item for row in rows]
        self._install_items(items, self._scan_spec)

    def _invalidate_catalog_result(self, *_args):
        self._catalog_metadata_epoch += 1

    def _material_resource_changed(self, change):
        if isinstance(change, dict) and change.get('operation') == 'preview':
            self.grid_list.refresh_resource_covers('material', change.get('id'))
            return
        self._invalidate_catalog_result()
        self.schedule()

    def _records_changed(self, change):
        if isinstance(change, dict) and change.get('operation') == 'preview':
            self.grid_list.refresh_resource_covers(change.get('kind'), change.get('id'))
            return
        self._invalidate_catalog_result()
        QtCore.QTimer.singleShot(0, self.schedule)

    def _usage_changed(self):
        if self.mode == 'recent':
            QtCore.QTimer.singleShot(0, self.schedule)

    def _visual_results_key(self):
        """Ranked visual results are part of the query, not decoration.

        以图搜图每次完成都会换成一份新的 `visual_results`，所以模型里必须
        重新过滤一次；`_visual_epoch` 由 `VisualSearchController.finished()`
        递增，避免拿列表地址当身份。
        """
        return (getattr(self, '_visual_epoch', 0),
                len(getattr(self, 'visual_results', None) or ()))

    def invalidate_aggregate_result(self, reason='source_changed'):
        """Drop the cached whole-project rows and ask again.

        Called when the source version changed -- a save wrote a new file --
        and the catalog was rebuilt for it.  The rows in hand describe the
        file as it was BEFORE that change; keeping them would mean the list
        shows one version while the sidebar counts another.

        The generation counter goes up, so a reply that was already on its way
        for the OLD catalog is rejected when it lands instead of overwriting
        the new result.  The project id is part of that token too, so a reply
        from a different project cannot slip in either.

        While the catalog is being rebuilt the aggregation reports "unknown"
        and the list shows the preparing state -- it must not keep serving the
        previous page's rows as if they were the whole project.

        ``reason`` says *why* the result was dropped.  It is recorded, not
        guessed: a result that disappears without a stated reason is a
        result nobody can debug.
        """
        self._catalog_data_index = None
        self._cancel_catalog_query()
        cached = getattr(self, '_catalog_scalar_cache', None)
        if cached is not None and hasattr(cached[1], 'close'):
            cached[1].close()
        self._close_catalog_streams(reason)
        released = self._catalog_results.clear(reason)
        self._catalog_scalar_cache = None
        self._catalog_pending = None
        self._catalog_result_key = None
        self._catalog_result_digest = None
        self._catalog_result_items = None
        trace_record('catalog_result_invalidated', reason=reason,
                     released_bytes=released,
                     cache_stats=self._catalog_results.stats())
        self._selecting = True
        try:
            if not self._preserve_catalog_display:
                self.model.replace([])
        finally:
            self._selecting = False
        self.breadcrumb.setText(getattr(self, 'title', _('All assets')))
        # Model edits also invalidate this cache while another tab is open.
        # showEvent already refreshes on return; do not install 100k hidden rows.
        if self.isVisible() or getattr(self, '_opening', False):
            self.refresh()

    def suspend_aggregate_result(self):
        """Keep a labelled read-only old frame during a proven save update."""
        previous = getattr(self, '_save_display_snapshot', None)
        key = getattr(self, '_catalog_result_key', None)
        if (key is not None and self._catalog_result_items is self.model.items
                and self._save_projection_matches(key)
                and self._save_delta_scope_supported() and self.list._build_state is None):
            scope = self.catalog._catalog_scope()
            source_state = scope.source_state() if scope is not None else {}
            self._save_display_snapshot = dict(items=self.model.items, key=key,
                project=self.view.scene.project_id,
                source_state=source_state,
                asset_revision=getattr(self.view.scene, '_asset_revision', None),
                metadata_epoch=self._catalog_metadata_epoch,
                query=fingerprint(self._presentation_query_scope()))
        elif previous is None or previous['items'] is not self.model.items:
            self._save_display_snapshot = None
        self._preserve_catalog_display = True
        self.invalidate_aggregate_result('save_delta_pending')

    def _save_projection_matches(self, key):
        """Does the published projection still describe the current query?

        Every field is compared **by name**.  The previous implementation
        compared slices of a positional tuple (``key[2:11]``), so adding a
        field to the key silently changed what "the same projection" meant.
        """
        import json
        panel = self.view.category_panel
        pages = (tuple(sorted(set(self.scope_pages),
                              key=lambda value: (value is not None, str(value))))
                 if self.scope_pages is not None else ())
        return (key.metadata_epoch == self._catalog_metadata_epoch
                and key.mode == self.mode
                and key.scope_pages == pages
                and key.scope_all == (self.scope_pages is None)
                and key.search == self.search.text()
                and key.search_kind == panel.search_kind
                and key.min_rating == int(panel._min_rating or 0)
                and key.active_tags == tuple(sorted(panel._active_tags))
                and key.saved_rule == json.dumps(self.saved_rule, sort_keys=True)
                and key.sort_key == self.sort_key
                and key.random_seed == getattr(self, '_random_seed', None)
                and key.visual_revision == tuple(self._visual_results_key()))

    def _save_projection_filter_key(self):
        import json
        panel = self.view.category_panel
        pages = (tuple(sorted(set(self.scope_pages), key=lambda v: (v is not None, str(v))))
                 if self.scope_pages is not None else None)
        return (self.mode, pages, self.search.text(), panel.search_kind, self.sort_key,
                panel._min_rating, tuple(sorted(panel._active_tags)),
                json.dumps(self.saved_rule, sort_keys=True), getattr(self, '_random_seed', None))

    def _save_delta_scope_supported(self):
        # These projections already include current live edits. A confirmed
        # save changes their source revision, not their filter or ordering.
        # Unreconciled edits/membership still fail the row/epoch checks below.
        return (self.mode in ('all', 'untagged', 'unclassified')
                and self.view.category_panel.search_kind == 'asset'
                and self.sort_key in ('default', 'name', 'rating')
                and not getattr(self, 'visual_results', None))

    def _save_projection_declined(self, reason, guard_reason=None):
        trace_record('asset_model_delta_declined', reason=reason,
                     guard_reason=guard_reason,
                     retained_model_rows=len(self.model.items),
                     fallback='requires_requery', complete=False, final=False)
        return False

    def _save_projection_guard_reason(self, request, scope):
        """Which single guard stopped the reuse of the saved projection.

        These guards used to be one condition, and every one of them was
        reported as ``projection_guard_changed``: a decline could not be
        told apart from the log, so a reuse that never fired looked exactly
        like a reuse that was correctly refused.  The summary reason is
        unchanged -- it still counts the declines -- but the guard that
        actually refused is recorded next to it.
        """
        snapshot = getattr(self, '_save_display_snapshot', None)
        if snapshot is None:
            return 'no_display_snapshot'
        if not request.get('incremental_applied'):
            return 'no_incremental_application'
        if scope is None:
            return 'scope_unavailable'
        if snapshot['items'] is not self.model.items:
            return 'model_identity_changed'
        if snapshot['project'] != self.view.scene.project_id:
            return 'project_changed'
        if snapshot['query'] != fingerprint(self._presentation_query_scope()):
            return 'query_changed'
        if not self._save_delta_scope_supported():
            return 'scope_unsupported'
        if self.list._build_state is not None:
            return 'list_building'
        if snapshot['asset_revision'] != getattr(self.view.scene, '_asset_revision', None):
            return 'asset_revision_changed'
        if snapshot['metadata_epoch'] != self._catalog_metadata_epoch:
            return 'metadata_epoch_changed'
        return None

    def resume_saved_projection(self, request):
        """Reuse canonical live rows only; never relabel stale proxies as edits."""
        scope = self.catalog._catalog_scope()
        guard_reason = self._save_projection_guard_reason(request, scope)
        if guard_reason is not None:
            return self._save_projection_declined('projection_guard_changed',
                                                  guard_reason=guard_reason)
        snapshot = getattr(self, '_save_display_snapshot', None)
        state = scope.source_state()
        from prism.aggregate_save_delta import changed_ids_since
        # The receipt chain is compared against the source state the
        # projection was built on.  That state is kept next to the key, not
        # inside it: the key carries a *fingerprint* of the state, and the
        # fingerprint alone cannot answer "which source version is this".
        ids = changed_ids_since(snapshot['source_state'].get('source'),
                                state['source'],
                                request.get('catalog_deltas', ()))
        if ids is None:
            return self._save_projection_declined('receipt_chain_unproven')
        summaries = request.get('delta_summaries')
        if summaries is None:
            return self._save_projection_declined('missing_delta_summaries')
        rows = {row['id']: row for row in summaries}
        scene = self.view.scene
        session = getattr(scene, '_partial_project_session', None)
        if session is None or getattr(session, 'invalid', None) or len(session.loaded()) > 4096:
            return self._save_projection_declined('session_unavailable_or_unbounded')
        from prism.item_lifecycle import logical_item
        canonical = {}
        for count, value in enumerate(scene.iter_assets()):
            if count >= 4096:
                return self._save_projection_declined('live_overlay_unbounded')  # Also bound newly imported, not-yet-saved records.
            item = logical_item(value)
            identifier = getattr(item, 'save_id', None)
            if identifier in ids:
                canonical[identifier] = item
        changed_rows = []
        for identifier in ids:
            item = canonical.get(identifier) or session.retained_item(identifier)
            row = rows.get(identifier)
            if item is None or row is None or getattr(item, '_catalog_proxy', False):
                return self._save_projection_declined('canonical_or_summary_missing')
            position = self.list._item_rows.get(self.list._row_identity(item))
            deleted = self.catalog.is_deleted(item)
            if position is None and deleted and row['deleted_at'] is not None:
                continue  # The current overlay already excluded this saved deletion.
            if (position is None and row['kind'] == getattr(item, 'TYPE', None)
                    and row['canvas_id'] == getattr(item, '_canvas_id', None)
                    and not self._catalog_matches(item)):
                continue  # A saved edit outside this stable projection changes no displayed row.
            if (position is None or position >= len(self.model.items)
                    or self.model.items[position] is not item
                    or deleted or row['deleted_at'] is not None
                    or row['kind'] != getattr(item, 'TYPE', None)
                    or row['canvas_id'] != getattr(item, '_canvas_id', None)):
                return self._save_projection_declined('membership_or_grouping_unreconciled')  # Membership/grouping is not already reconciled.
            changed_rows.append(position)
        if scope.source_state() != state:
            return self._save_projection_declined('source_changed_during_validation')
        # Validation finished before the first mutation. No database write or
        # model reset occurs; post-capture edits were rejected above.
        self._save_display_snapshot = None
        # The source revision advances **now**, because the receipt chain has
        # been proven and applied to the index.  It does not advance earlier:
        # a key that claims a new source revision before the index carries it
        # would let a stale result pass as a current one.
        key = snapshot['key']
        advanced = QueryKey(
            project=key.project,
            source_revision=fingerprint(state),
            overlay_revision=snapshot['asset_revision'],
            metadata_epoch=snapshot['metadata_epoch'],
            scope_pages=key.scope_pages,
            scope_all=key.scope_all,
            mode=key.mode,
            search=key.search,
            search_kind=key.search_kind,
            min_rating=key.min_rating,
            active_tags=key.active_tags,
            saved_rule=key.saved_rule,
            sort_key=key.sort_key,
            random_seed=key.random_seed,
            visual_revision=key.visual_revision)
        self._catalog_result_key = advanced
        self._catalog_result_digest = advanced.digest()
        self._catalog_result_items = self.model.items
        # The rows are still the same rows: a confirmed delta changed a few
        # of them in place, it did not rebuild the result.  The finished
        # result is re-keyed to the advanced source revision so a later hot
        # switch can still reuse it, and the entries that describe the old
        # source revision are dropped with a stated reason.
        self._catalog_results.invalidate(
            'source_revision_advanced',
            predicate=lambda entry: (entry.get('query_key') is not None
                                     and entry['query_key'].source_revision
                                     != advanced.source_revision))
        self._catalog_results.pin(advanced.digest())
        self._catalog_results.put(advanced.digest(), dict(
            query_key=advanced,
            ids=tuple(str(getattr(item, 'save_id', None)
                          or getattr(item, 'original_save_id', None) or '')
                      for item in self.model.items),
            items=list(self.model.items),
            bytes=estimate_result_bytes(self.model.items),
            published_rows=len(self.model.items),
            completed_at=time.monotonic()))
        self._preserve_catalog_display = False
        evidence = QueryEvidence(self._presentation_query_scope(),
            source_revision=fingerprint((str(scope.project_path), advanced.project,
                                         state, advanced.overlay_revision,
                                         advanced.metadata_epoch)))
        self._catalog_evidence = self.model.presentation_evidence = evidence
        evidence.final = True
        evidence.record('asset_model_delta_published', changed_rows=len(changed_rows),
                        retained_model_rows=len(self.model.items),
                        model_reset=False, complete=True, final=True)
        for position in changed_rows:
            self.model.dataChanged.emit(self.model.index(position), self.model.index(position))
            if (self.model.items is not snapshot['items']
                    or self.view.scene.project_id != snapshot['project']
                    or snapshot['query'] != fingerprint(self._presentation_query_scope())):
                return True  # A synchronous observer navigated; do not overwrite it.
        self.list._invalidate_display_frame()
        self.list.setEnabled(True)
        self.summary.setText(getattr(self, 'title', _('All assets')) + f'：{len(self.model.items):,}')
        self.breadcrumb.setText(getattr(self, 'title', _('All assets')) + f' ({len(self.model.items):,})')
        self.empty.setVisible(not self.model.items)
        self.list.setVisible(bool(self.model.items))
        self.show_scope_info(self.model.items)
        return True

    def _remember_catalog_install(self, pending):
        """Keep an in-flight **prefix checkpoint** - not a finished result.

        A checkpoint holds the candidates that were filtered so far.  It is
        not a result: it must never be published as one, and it must never
        be counted as a finished query.  It is kept apart from the
        completed results in the cache for exactly that reason.
        """
        if pending is None:
            return
        pending['match_context'] = self._catalog_match_context
        digest = pending.get('requested_digest')
        if digest is None:
            return
        checkpoint = {key: value for key, value in pending.items()
                      if key not in ('grid_preview', 'grid_preview_view', 'grid_data_index',
                                     'install_queued')}
        self._catalog_results.put_pending(digest, checkpoint)

    @property
    def _catalog_checkpoints(self):
        """The in-flight prefix checkpoints, keyed by query digest.

        Compatibility view for the callers that used to read the checkpoint
        store directly.  The store itself now lives in the result cache, and
        the checkpoints are separated from the finished results there.
        """
        return self._catalog_results.pending_view()

    def _remember_completed_result(self, pending, items, query_key, digest):
        """Store a finished result: the order, the version and the objects.

        The entry always carries the **lightweight** published order (the
        asset ids) and the version information that proves the entry is
        still current.  The resident items - the expensive part - are kept
        only while the byte budget allows; the budget is what decides
        whether a hot switch can republish without rebuilding a proxy.
        """
        retained = list(items)
        entry = dict(query_key=query_key, ids=CatalogResultIds(retained), items=retained,
                     bytes=estimate_result_bytes(items),
                     layout_token=pending.get('layout_token'),
                     layout_snapshot=pending.get('layout_snapshot'),
                     published_rows=len(items),
                     completed_at=time.monotonic())
        # Pin first: the entry that is about to be published must never be
        # the one the budget evicts.
        self._catalog_results.pin(digest)
        self._catalog_results.put(digest, entry)
        return entry

    def _reuse_grid_proxy(self, asset_id, query_key):
        """Find a current display record independently of viewport residency."""
        index = getattr(self, '_catalog_data_index', None)
        if (index is None or query_key is None
                or query_key.source_revision == 'unknown'
                or index['project'] != str(query_key.project)
                or index['open_epoch'] != self._catalog_open_epoch()
                or index['source_revision'] != query_key.source_revision):
            return None
        position = index['rows'].get(asset_id)
        if position is None or position >= len(index['items']):
            return None
        from prism.catalog_browser import CatalogAssetProxy
        candidate = index['items'][position]
        return candidate if isinstance(candidate, CatalogAssetProxy) else None

    def _prepare_grid_data_index(self, pending, items, done):
        """One active id->row table, built in slices; no extra record copies.

        Integer keys/row numbers are separate from the grid's visible map.
        Existing records still have to pass matches_catalog_row before reuse.
        The table is replaced on final publication, not accumulated per filter.
        """
        key = pending.get('requested_key')
        if key is None or key.source_revision == 'unknown':
            done()
            return
        rows = {}
        offset = 0

        def advance():
            nonlocal offset
            if not self._catalog_install_valid(pending):
                return
            if (str(key.overlay_revision) != self._catalog_overlay_revision(key.mode)
                    or key.metadata_epoch != self._catalog_metadata_epoch):
                pending['evidence'].finish('catalog_query_cancelled', cancelled=True,
                                           reason='grid_identity_inputs_changed')
                self._clear_grid_preview(pending)
                if self._catalog_pending is pending:
                    self._catalog_pending = None
                if self.isVisible():
                    QtCore.QTimer.singleShot(0, self.refresh)
                return
            deadline = time.perf_counter()+.002
            stop = min(len(items), offset+512)
            while offset < stop:
                item = items[offset]
                identity = getattr(item, 'save_id', None)
                if identity is not None:
                    rows[identity] = offset
                offset += 1
                if time.perf_counter() >= deadline:
                    break
            if offset < len(items):
                QtCore.QTimer.singleShot(1, advance)
                return
            pending['grid_data_index'] = dict(items=items, rows=rows,
                project=str(key.project), open_epoch=self._catalog_open_epoch(),
                source_revision=key.source_revision)
            pending['evidence'].record('asset_grid_identity_ready',
                indexed_rows=len(rows), extra_proxy_records=0,
                index_scope='one_active_result_not_viewport')
            try:
                done()
            except Exception as error:
                self._catalog_install_failed(pending, error)

        advance()

    def _maybe_publish_grid_preview(self, pending):
        """Publish known domains first and append arriving rows while browsing."""
        if (not getattr(self.list, 'is_asset_grid', False)
                or not self._catalog_install_valid(pending)
                or self.sort_key != 'default' or self.mode == 'recent'
                or self.mode not in _RESOURCE_SCOPES or self.scope_pages is not None
                or self.saved_rule):
            return
        grid = self.list
        if 'grid_seed' not in pending:
            from prism import project_resources
            panel = self.view.category_panel
            pending['grid_seed'] = project_resources.collect(
                self.view.scene, panel.resource_service, self.mode,
                asset_items=[], catalog=self.catalog,
                recent=self._recent_usage_entries(),
                page_records=panel._pages_with_implicit_defaults(),
                **panel.resource_scope_options(self.search.text(), panel.search_kind))
        preview = pending.get('grid_preview')
        offset = pending.get('grid_preview_offset', 0)
        available = len(pending['items'])
        now = time.monotonic()
        if preview is not None:
            if self.model.items is not preview or now - pending.get('grid_preview_at', 0) < .08:
                return
            if available <= offset:
                return
            installed = self.model.append_preview(pending['items'][offset:available],
                guard=lambda: self._catalog_install_valid(pending))
            if not installed:
                return
        else:
            if not pending['grid_seed'] and available < 64:
                return
            preview = list(pending['grid_seed']) + pending['items'][:available]
            pending['grid_preview'] = preview
            pending['grid_preview_view'] = grid
            self._catalog_preview_active = True
            self._catalog_preview_owner = pending
            self._selecting = True
            try:
                self.view.scene.clearSelection()
                installed = self.model.replace(preview, evidence=None,
                    guard=lambda: self._catalog_install_valid(pending))
            finally:
                self._selecting = False
            if not installed or not self._catalog_install_valid(pending):
                return
            grid.setSelectionMode(QtWidgets.QAbstractItemView.SelectionMode.NoSelection)
            grid.setEnabled(True)
            grid.show()
            self.empty.hide()
        pending['grid_preview_offset'] = available
        pending['grid_preview_at'] = now
        self.breadcrumb.setText(getattr(self, 'title', _('All assets')))
        self.summary.setText(f'正在加载 · 已显示 {len(preview):,} 项，可继续滚动浏览')
        self.summary.show()
        pending['evidence'].record('asset_grid_prefix_presented',
            preview_rows=len(preview), result_rows=None, editable=False,
            complete=False, final=False, sort_complete=False,
            resource_kinds=sorted({item.get('kind') for item in pending['grid_seed']}))

    def _end_grid_preview(self, pending):
        if pending is None or pending.get('grid_preview') is None:
            return
        if getattr(self, '_catalog_preview_owner', None) is not pending:
            return  # A reentrant navigation may already own a newer preview.
        grid = pending.get('grid_preview_view')
        if grid is not None and not sip.isdeleted(grid):
            grid.setSelectionMode(QtWidgets.QAbstractItemView.SelectionMode.ExtendedSelection)
        self._catalog_preview_active = False
        self._catalog_preview_owner = None

    def _clear_grid_preview(self, pending):
        if pending is None or pending.get('grid_preview') is None:
            return
        self._end_grid_preview(pending)
        if self.model.items is pending['grid_preview']:
            self._selecting = True
            try:
                self.model.replace([])
            finally:
                self._selecting = False

    def _catalog_layout_token_steps(self, items, query_key):
        """Hash row order and geometry inputs without retaining another row list."""
        if query_key is None or query_key.source_revision == 'unknown':
            return None
        import hashlib
        import json
        digest = hashlib.sha256()
        for item in items:
            try:
                size = getattr(item, '_image_size', None)
                crop = getattr(item, 'crop', None)
                payload = (self.list._row_identity(item),
                           (size.width(), size.height()) if size is not None else None,
                           (crop.x(), crop.y(), crop.width(), crop.height())
                           if crop is not None else None)
                digest.update(json.dumps(payload, separators=(',', ':'),
                                         ensure_ascii=True).encode('utf-8'))
            except Exception as error:
                trace_record('catalog_layout_identity_unavailable',
                             error_type=type(error).__name__)
                return None  # Never require a cache optimization to publish rows.
            digest.update(b'\n')
            yield
        return CatalogLayoutToken(
            project=query_key.project, open_epoch=self._catalog_open_epoch(),
            query_digest=query_key.digest(), source_revision=query_key.source_revision,
            overlay_revision=str(query_key.overlay_revision),
            metadata_epoch=query_key.metadata_epoch,
            ordered_geometry_digest=digest.hexdigest(), row_count=len(items))

    def _catalog_layout_token_for(self, items, query_key):
        """Synchronous small-fixture accessor; production uses sliced capture."""
        steps = self._catalog_layout_token_steps(items, query_key)
        while True:
            try:
                next(steps)
            except StopIteration as finished:
                return finished.value

    def _equivalent_catalog_layout(self, token):
        state = getattr(self, '_catalog_previous_layout', None)
        if state is None or token is None:
            return None
        old = state.get('token')
        if (old is None or old.project != token.project
                or old.open_epoch != token.open_epoch
                or old.ordered_geometry_digest != token.ordered_geometry_digest
                or old.row_count != token.row_count
                or state.get('visual') != (self.mode == 'visual')
                or state.get('geometry_version') != self.list._geometry_version
                or state.get('content_geometry_revision') !=
                    self.list._layout_content_revisions.get(old, 0)):
            return None
        # New publication identity remains new. Only the geometry is certified
        # equivalent; source pixels, counts and query results are not reused.
        restored = dict(state, token=token,
                        content_geometry_revision=self.list._layout_content_revisions.get(token, 0))
        return restored

    def _prepare_catalog_layout_token(self, pending, items, query_key, done):
        """Validate ordered geometry in bounded turns, never one 100k hash loop."""
        if getattr(self.list, 'is_asset_grid', False):
            # Build data identity in bounded turns, not a geometry hash. This
            # index is independent of the small visible-row lookup in the grid.
            self._prepare_grid_data_index(pending, items, lambda: done(None))
            return
        steps = self._catalog_layout_token_steps(items, query_key)
        work_ms = 0.0
        checked = 0
        started = time.perf_counter()
        geometry_revision = self.list._geometry_version

        def deliver(value):
            if not self._catalog_install_valid(pending):
                return
            try:
                done(value)
            except Exception as error:
                self._catalog_install_failed(pending, error)

        def advance():
            nonlocal work_ms, checked
            if not self._catalog_install_valid(pending):
                return
            if self.list._geometry_version != geometry_revision:
                # Crop hydration/manual geometry may arrive between hash
                # slices. A mixed-time digest must never certify old layout.
                deliver(None)
                return
            if query_key is not None and (
                    str(query_key.overlay_revision) != self._catalog_overlay_revision(query_key.mode)
                    or query_key.metadata_epoch != self._catalog_metadata_epoch):
                pending['evidence'].finish('catalog_query_cancelled', cancelled=True,
                                           reason='layout_identity_inputs_changed')
                self._catalog_pending = None
                QtCore.QTimer.singleShot(0, self.refresh)
                return
            tick = time.perf_counter()
            try:
                for _ in range(256):
                    next(steps)
                    checked += 1
                    if time.perf_counter() - tick >= .004:
                        break
            except StopIteration as finished:
                work_ms += (time.perf_counter() - tick) * 1000
                if query_key is not None and query_key.source_revision != 'unknown':
                    try:
                        scope = self.catalog._catalog_scope()
                        source_current = (scope is not None and
                                          fingerprint(scope.source_state()) ==
                                          query_key.source_revision)
                    except Exception:
                        source_current = False
                    if not source_current:
                        pending['evidence'].finish('catalog_query_cancelled', cancelled=True,
                                                   reason='layout_identity_source_changed')
                        self._catalog_pending = None
                        QtCore.QTimer.singleShot(0, self.refresh)
                        return
                trace_record('catalog_layout_identity',
                             query_digest=query_key.digest() if query_key else None,
                             geometry_rows_checked=checked, work_ms=work_ms,
                             elapsed_ms=(time.perf_counter()-started)*1000,
                             asynchronous=len(items) > 256)
                deliver(finished.value)
                return
            work_ms += (time.perf_counter() - tick) * 1000
            QtCore.QTimer.singleShot(0, advance)

        pending['phase'] = 'layout_identity'
        if len(items) <= 256:
            advance()
        else:
            QtCore.QTimer.singleShot(0, advance)

    def _catalog_layout_token_current(self, token):
        """An unchanged row list alone is not proof that its metadata is current."""
        return (token is not None
                and token.project == str(self.view.scene.project_id)
                and token.open_epoch == self._catalog_open_epoch()
                and token.overlay_revision == self._catalog_overlay_revision(self.mode)
                and token.metadata_epoch == self._catalog_metadata_epoch)

    def _cancel_catalog_query(self):
        current_token = getattr(self, '_catalog_layout_token', None)
        snapshot = (self.list.snapshot_catalog_layout(self.model.items, token=current_token)
                    if current_token is not None else None)
        if snapshot is not None:
            self._catalog_previous_layout = snapshot
        digest = getattr(self, '_catalog_result_digest', None)
        if digest is not None and self._catalog_result_items is self.model.items:
            entry = self._catalog_results.get(digest)
            if entry is not None and entry.get('layout_token') == current_token:
                entry['layout_snapshot'] = snapshot
        self._remember_catalog_install(self._catalog_pending)
        self._clear_grid_preview(self._catalog_pending)
        self._catalog_generation += 1
        if self._catalog_cancel is not None:
            self._catalog_cancel.set()
        # The pool still owns an executing QRunnable. It no longer represents
        # a pending result for this browser generation, even if its cancelled
        # worker deliberately emits no result callback.
        self._background_jobs.release(self._catalog_query_job)
        self._catalog_query_job = None
        evidence = getattr(self, '_catalog_evidence', None)
        if evidence is not None:
            evidence.delivery('consumer_consumed', complete=False, final=True)
            evidence.finish('catalog_query_cancelled', cancelled=True, reason='cancel_or_switch')
        self._catalog_pending = None

    def _presentation_query_scope(self):
        panel = self.view.category_panel
        return dict(project=fingerprint(self.view.scene.project_id),
            mode=self.mode, scope_pages=self.scope_pages, search=self.search.text(),
            search_kind=panel.search_kind, sort_key=self.sort_key,
            min_rating=panel._min_rating, active_tags=sorted(panel._active_tags),
            saved_rule=self.saved_rule, random_seed=getattr(self, '_random_seed', None),
            visual_results=self._visual_results_key())

    def _catalog_overlay_revision(self, mode):
        revision = str(getattr(self.view.scene, '_asset_revision', None))
        if mode == 'recent':
            overlay, fresh = self.catalog._session_overlay()
            usage = tuple((str(getattr(item, 'save_id', id(item))),
                           getattr(item, '_last_used', ''))
                          for item in list(overlay.values()) + list(fresh))
            revision = fingerprint((revision, self._recent_usage_entries(), usage))
        return revision

    def _catalog_query_key(self, scope):
        """The stable identity of the current catalog query.

        Every field that can change *which rows belong to the result* or *in
        which order they are published* is here, and every field is a value -
        never an object address and never a file mtime.

        The previous implementation built a bare tuple inline and later sliced
        it by index (``key[2:11]``, ``key[11:13]``, ``key[12]``, ``key[13]``).
        That made every added field a silent wire change: a consumer that
        compared ``key[11:13]`` would keep comparing the wrong two elements.
        The named fields remove that class of mistake.
        """
        import json
        panel = self.view.category_panel
        pages = (tuple(sorted(set(self.scope_pages),
                              key=lambda value: (value is not None, str(value))))
                 if self.scope_pages is not None else ())
        try:
            source_revision = fingerprint(scope.source_state())
        except Exception:
            source_revision = 'unknown'
        # Opening a resource changes recent membership without an asset edit.
        overlay_revision = self._catalog_overlay_revision(self.mode)
        return QueryKey(
            project=str(self.view.scene.project_id),
            source_revision=source_revision,
            overlay_revision=overlay_revision,
            metadata_epoch=int(self._catalog_metadata_epoch),
            scope_pages=pages,
            scope_all=self.scope_pages is None,
            mode=self.mode,
            search=self.search.text(),
            search_kind=panel.search_kind,
            min_rating=int(panel._min_rating or 0),
            active_tags=tuple(sorted(panel._active_tags)),
            saved_rule=json.dumps(self.saved_rule, sort_keys=True),
            sort_key=self.sort_key,
            random_seed=getattr(self, '_random_seed', None),
            visual_revision=tuple(self._visual_results_key()))

    def _catalog_pushdown_filters(self):
        """Ask the existing directory for this scope's candidate set."""
        from prism.project_resources import directory_scope_filters
        panel = self.view.category_panel
        filters = directory_scope_filters(self.catalog, self.mode)
        if self.scope_pages is not None:
            pages = set(self.scope_pages)
            if 'canvas_ids' in filters:
                pages.intersection_update(filters['canvas_ids'])
            filters['canvas_ids'] = tuple(sorted(pages, key=lambda value: (value is not None, str(value))))
            filters['known_canvas_ids'] = tuple(sorted(self.catalog._known_canvas_ids(), key=str))
        if self.mode not in _RESOURCE_SCOPES and not getattr(self, '_catalog_pushdown_enabled', False):
            return filters
        rule = self.saved_rule or {}
        threshold = max(int(panel._min_rating or 0), int(rule.get('rating', 0) or 0))
        if threshold > 0:
            filters['min_rating'] = threshold
        text, expression = panel._split_query(self.search.text())
        from prism import tags
        expressions = [value for value in (expression, rule.get('tag_expression')) if value]
        if panel._active_tags or expressions:
            scope = self.catalog._catalog_scope()
            filters['tag_sets'] = tags.matching_tag_sets(
                scope.tag_groups(include_deleted=self.mode == 'trash'), tags.tag_system(self.view.scene),
                active=panel._active_tags, predicates=tuple(tags.parse_query(value) for value in expressions))
        if panel.search_kind in ('all', 'asset'):
            words = list(text.casefold().split())
            words.extend(str(rule.get('text', '') or '').casefold().split())
            if words:
                filters['text'] = ' '.join(words)
        return filters

    def _restore_publication_order(self, items):
        """Put the merged items back on the canonical directory id order.

        A pushed-down filter can remove the directory row that carried a
        live overlay item.  The item is still correct - it is matched
        against its own live metadata - but it would land at the end of the
        list instead of at its row's position.  The directory order is the
        ascending id order of the catalog, so sorting by the asset id
        reproduces exactly the order the un-pushed read would have produced.

        Items without an id (a brand new live asset) keep their relative
        order and stay at the end.
        """
        keyed = []
        for index, item in enumerate(items):
            identifier = getattr(item, 'save_id', None)
            if identifier is None:
                identifier = getattr(item, 'original_save_id', None)
            if identifier is None:
                keyed.append((1, '', index, item))
            else:
                try:
                    identity = (0, int(identifier))
                except (TypeError, ValueError):
                    identity = (1, str(identifier))
                keyed.append((0, identity, index, item))
        keyed.sort(key=lambda entry: (entry[0], entry[1], entry[2]))
        return [entry[3] for entry in keyed]

    def directory_build_failed(self):
        """Do not leave an in-progress label behind after a real build failure."""
        if not self.isVisible():
            return
        self.summary.setText(_('The project index could not be prepared; reopen the project to retry'))
        self.summary.show()
        if not self._preserve_catalog_display:
            self.empty.setText(_('The project index could not be prepared; reopen the project to retry'))
            self.empty.show()
            self.list.hide()

    def _refresh_catalog_assets(self):
        """Formal partial-project aggregate path: directory plus live edits.

        Querying summaries happens off the GUI thread. Proxy installation is
        sliced; proxies are never inserted into scene or partial-save sessions.
        """
        import threading
        from prism.catalog_browser import CatalogQuery, CatalogDisplayReader
        scope = self.catalog._catalog_scope()
        if scope is None:
            self._cancel_catalog_query()
            evidence = self._catalog_evidence = QueryEvidence(self._presentation_query_scope())
            evidence.record('catalog_query_started', complete=False, final=False)
            if self._preserve_catalog_display:
                evidence.finish('catalog_query_deferred', reason='index_update_pending')
                self.breadcrumb.setText(getattr(self, 'title', _('All assets')) + f" ({_('Preparing…')})")
                self.summary.setText(_('Updating assets; showing previous results temporarily'))
                self.summary.show()
                current_frame = (bool(self.model.items) and
                    getattr(self, '_catalog_model_project', None) == self.view.scene.project_id)
                self.empty.setText(_('Preparing…'))
                self.empty.setVisible(not current_frame)
                self.list.setVisible(current_frame)
                self.list.setEnabled(False)
                return
            if (getattr(self.view, '_catalog_build_active', None) is not None
                    or getattr(self.view, '_catalog_build_pending', None) is not None):
                evidence.finish('catalog_query_deferred', reason='index_build_in_progress')
                self.breadcrumb.setText(getattr(self, 'title', _('All assets')))
                self.empty.setText(_('Preparing the project index in the background…'))
                self.empty.show()
                self.list.hide()
                self.summary.setText(_('Assets will appear automatically when the index is ready'))
                self.summary.show()
                return
            evidence.finish('catalog_query_failed', reason='directory_unavailable',
                            error_type='catalog_unavailable')
            self.empty.setText(_('Project directory unavailable; retry after it is ready'))
            self.empty.show()
            self.list.hide()
            return
        self.empty.setText(_('No matching results'))
        query_key = self._catalog_query_key(scope)
        requested_digest = query_key.digest()
        pushdown = self._catalog_pushdown_filters()
        current_key = getattr(self, '_catalog_result_key', None)
        if (current_key is not None and current_key.digest() == requested_digest
                and getattr(self, '_catalog_result_items', None) is self.model.items
                and self.model.presentation_evidence is not None
                and self.model.presentation_evidence.active):
            installed_evidence = self.model.presentation_evidence
            installed_evidence.attach_trace()
            # Baseline observers may synchronously replace the current query.
            if (getattr(self, '_catalog_result_key', None) is not current_key
                    or getattr(self, '_catalog_result_items', None) is not self.model.items
                    or self.model.presentation_evidence is not installed_evidence
                    or not installed_evidence.active):
                return
            self.breadcrumb.setText(getattr(self, 'title', _('All assets')) + f' ({len(self.model.items):,})')
            self.empty.setVisible(not self.model.items)
            self.list.setVisible(bool(self.model.items))
            self.list.setEnabled(True)
            self.show_scope_info(self.model.items)
            return
        pending = getattr(self, '_catalog_pending', None)
        if (pending is not None
                and pending.get('requested_digest') == requested_digest
                and pending.get('pushdown') == pushdown
                and self._catalog_install_valid(pending)):
            # The very same query is already in flight.  Its digest covers
            # the source revision, the edit revision and every filter, the
            # pushdown covers the set of rows the directory answers, and the
            # install guard covers the project and the open identity -- so
            # this request would produce exactly the rows the running one is
            # producing.  Tearing it down would throw away the rows already
            # read and make the browser publish the same prefix again.
            self._catalog_requested_key = query_key
            self._catalog_requested_digest = requested_digest
            return
        self._cancel_catalog_query()
        self._catalog_requested_key = query_key
        self._catalog_requested_digest = requested_digest
        # A new request registers a new generation **now**, not when it
        # finishes: an older request that completes later must be recognisable
        # as older and must not overwrite this one.
        self._catalog_publication_generation += 1
        self._catalog_publication_seq = 0
        # The physical read is identified by the source revision **and** the
        # pushed-down filters: two queries that differ only in their filter
        # must not share one row stream.
        self._catalog_cache_key = (self.view.scene.project_id,
                                   query_key.source_revision,
                                   fingerprint(pushdown))
        previous_cache = getattr(self, '_catalog_scalar_cache', None)
        if previous_cache is not None and previous_cache[0] != self._catalog_cache_key:
            # The current stream answers another filter set.  It is retired,
            # not closed: a prefix checkpoint may still resume from it.
            previous_cache = None
        # Only a changed source revision makes every finished result stale.
        # A changed filter does not: the finished result is keyed by its own
        # query identity, and switching back to a previous mode must find its
        # result instead of rebuilding it.
        stale = self._catalog_results.invalidate(
            'source_revision_changed',
            predicate=lambda entry: (entry.get('query_key') is not None
                                     and entry['query_key'].source_revision
                                     != query_key.source_revision))
        if stale:
            trace_record('catalog_result_invalidated',
                         reason='source_revision_changed',
                         dropped=len(stale),
                         cache_stats=self._catalog_results.stats())
        evidence = QueryEvidence(self._presentation_query_scope(),
            source_revision=fingerprint((str(scope.project_path), query_key.project,
                                         query_key.source_revision,
                                         query_key.overlay_revision,
                                         query_key.metadata_epoch)))
        if previous_cache is not None and previous_cache[0] == self._catalog_cache_key:
            previous_evidence = getattr(previous_cache[1], 'presentation_evidence', None)
            if previous_evidence is not None:
                evidence.reuse_read(previous_evidence)
        self._catalog_evidence = evidence
        token = self._catalog_generation, self.view.scene.project_id
        evidence.record('catalog_query_started', complete=False, final=False,
                        query_digest=requested_digest,
                        pushdown_filters=sorted(pushdown.items()))
        if (not evidence.active or self._catalog_evidence is not evidence
                or token != (self._catalog_generation, self.view.scene.project_id)):
            return
        if getattr(self, '_catalog_model_project', None) != self.view.scene.project_id:
            self._catalog_model_project = self.view.scene.project_id
            self._selecting = True
            try:
                self.model.replace([])
            finally:
                self._selecting = False
        self._catalog_cancel = threading.Event()
        if previous_cache is None or previous_cache[0] != self._catalog_cache_key:
            self._catalog_proxy_cache = OrderedDict()
        self._catalog_scope = scope
        if getattr(self, '_catalog_reader_scope', None) is not scope:
            self._catalog_display_reader = CatalogDisplayReader(scope, self)
            self._catalog_reader_scope = scope
        self.list.setEnabled(False)
        self.summary.setText(_('Loading whole-project assets…'))
        self.summary.show()
        # (1) A finished result for this exact query: republish it.  No row
        #     is read, no row is evaluated, no proxy is built.
        #
        #     The completed cache is only consulted while there **is** a
        #     current result key.  "No current result" means the browser has
        #     nothing published, and then a finished result from the cache
        #     would be republished behind the user's back - instead of the
        #     query actually being asked for.  That is also what keeps a
        #     failed read explicit: with no current result, the read runs
        #     and its failure is reported.
        entry = None
        if getattr(self, '_catalog_result_key', None) is not None:
            entry = self._catalog_results.get(requested_digest)
        evidence.record('catalog_completed_cache_lookup', hit=entry is not None,
                        mode=self.mode,
                        changed_fields=([name for name in query_key.__dataclass_fields__
                                         if getattr(current_key, name) != getattr(query_key, name)]
                                        if current_key is not None else ['no_current_result']),
                        cache_stats=self._catalog_results.stats())
        if entry is not None and entry.get('items') is not None:
            if self._publish_cached_result(entry, query_key, requested_digest,
                                           evidence, token):
                return
            entry = None
        # (2) A prefix checkpoint for this exact query: the candidate list is
        #     already filtered up to a row offset; continue from there.  The
        #     checkpoint's row stream must still be open - it may be a
        #     *retired* stream, because the query it belongs to was
        #     superseded by a different filter set.
        checkpoint = self._catalog_results.get_pending(requested_digest)
        if checkpoint is not None and self._catalog_stream_is_open(checkpoint.get('rows')):
            # The stored checkpoint is an immutable snapshot: its candidate
            # list is a tuple and its overlay is a copy.  The live pending
            # below gets a **fresh** list, so the resume can never append into
            # the stored prefix - which is what used to publish the same
            # logical asset twice on a second resume of the same query.
            from prism.catalog_install import resume_install
            pending = resume_install(checkpoint,
                token=token, evidence=evidence, requested_key=query_key,
                requested_digest=requested_digest,
                project=str(self.view.scene.project_id),
                open_epoch=self._catalog_open_epoch(),
                query_generation=self._catalog_publication_generation)
            pending['install_queued'] = False
            self._catalog_match_context = pending['match_context']
            self._catalog_pending = pending
            pending['rows'].bind_evidence(evidence, self._catalog_cancel.is_set)
            evidence.reuse_install(checkpoint['evidence'])
            evidence.record('catalog_install_checkpoint_reused',
                            consumed_rows=pending['offset'], accepted_rows=len(pending['items']))
            self._queue_catalog_install(pending)
            return
        if previous_cache is not None and previous_cache[0] == self._catalog_cache_key:
            cached = previous_cache[1]
            if hasattr(cached, 'bind_evidence'):
                cached.bind_evidence(evidence, self._catalog_cancel.is_set)
                evidence.record('catalog_rows_read_evidence', **evidence.read_fields(),
                                complete=False, final=False)
            self._catalog_rows_ready((token, cached, None))
            return
        self._catalog_query_job = CatalogQuery(scope, token, self._catalog_cancel.is_set,
                                               self._catalog_rows_ready, evidence=evidence,
                                               filters=pushdown)
        self._background_jobs.start(catalog_thread_pool(), self._catalog_query_job)

    def _catalog_stream_is_open(self, rows):
        """Is this row stream still usable?

        A stream stays open while it is the current one or while it is
        retained in the pool.  A checkpoint may only resume from an open
        stream: resuming from a closed one would read rows that no longer
        exist.
        """
        if rows is None:
            return False
        cached = getattr(self, '_catalog_scalar_cache', None)
        if cached is not None and cached[1] is rows:
            return True
        return any(stream is rows for stream in self._catalog_streams.values())

    def _register_catalog_stream(self, key, rows):
        """Keep a row stream open and bound the number of open streams."""
        self._catalog_streams[key] = rows
        self._catalog_streams.move_to_end(key)
        current = getattr(self, '_catalog_scalar_cache', None)
        while len(self._catalog_streams) > CATALOG_STREAM_LIMIT:
            victim = None
            for candidate, stream in self._catalog_streams.items():
                if current is not None and current[1] is stream:
                    continue
                victim = candidate
                break
            if victim is None:
                break
            stream = self._catalog_streams.pop(victim)
            if hasattr(stream, 'close'):
                stream.close()
            # The checkpoints that were built on the closed stream can no
            # longer be resumed; they must not be kept alive either.
            self._catalog_results.invalidate(
                'row_stream_closed',
                predicate=lambda entry: entry.get('rows') is stream)
            self._catalog_results.clear_pending('row_stream_closed')

    def _close_catalog_streams(self, reason):
        """Close every open row stream and forget the checkpoints on them."""
        released = 0
        for stream in list(self._catalog_streams.values()):
            if hasattr(stream, 'close'):
                stream.close()
            released += 1
        self._catalog_streams.clear()
        self._catalog_results.clear_pending(reason)
        return released

    def _publish_cached_result(self, entry, query_key, digest, evidence, token):
        """Republish a finished result without reading, filtering or building.

        This is the hot-switch path.  It republishes the resident items, so
        the model gets exactly the rows it had before, in exactly the order
        it had before, and the attempt costs **no** row read, **no** row
        evaluation and **no** proxy creation.

        ``items is None`` means "this result is not resident"; an **empty
        list** is a legitimate complete result (an empty scope, an empty tag
        set, a query that matched nothing) and must be reusable as well.
        """
        items = entry.get('items')
        if items is None:
            return False
        republished = list(items)
        pending = dict(token=token, evidence=evidence, requested_key=query_key,
                       requested_digest=digest, items=republished, overlay={}, new=[],
                       offset=0, rows=None, match_context=self._catalog_match_context,
                       layout_snapshot=entry.get('layout_snapshot'),
                       project=str(self.view.scene.project_id),
                       open_epoch=self._catalog_open_epoch(),
                       query_generation=self._catalog_publication_generation)
        self._catalog_pending = pending
        self._prepare_catalog_layout_token(
            pending, republished, query_key,
            lambda layout_token: self._publish_cached_result_ready(
                entry, pending, republished, layout_token))
        return True

    def _publish_cached_result_ready(self, entry, pending, republished, layout_token):
        evidence = pending['evidence']
        if (not getattr(self.list, 'is_asset_grid', False)
                and entry.get('layout_token') is not None and entry['layout_token'] != layout_token):
            self._catalog_results.invalidate('cached_geometry_changed',
                                             predicate=lambda value: value is entry)
            evidence.finish('catalog_query_cancelled', cancelled=True,
                            reason='cached_geometry_changed')
            self._catalog_pending = None
            QtCore.QTimer.singleShot(0, self.refresh)
            return
        pending['layout_token'] = layout_token
        self.search_hits = []
        self._catalog_restore_layout = (None if getattr(self.list, 'is_asset_grid', False)
                                        else entry.get('layout_snapshot'))
        self._selecting = True
        try:
            self.view.scene.clearSelection()
            published = self.model.replace(republished, evidence=evidence,
                guard=lambda: self._catalog_install_valid(pending),
                layout_token=layout_token,
                publication_fields=dict(result_reused=True, evaluated_rows=0,
                                        proxies_created=0))
        finally:
            self._selecting = False
            self._catalog_restore_layout = None
        if not published:
            self._catalog_pending = None
            return False
        evidence.record('catalog_result_reused', model_rows=len(republished),
                        evaluated_rows=0, proxies_created=0, read_rows=0,
                        result_reused=True, complete=True, final=True)
        self._finish_catalog_install(pending, republished, reused=True)
        return True

    @QtCore.pyqtSlot(object)
    def _catalog_rows_ready(self, result):
        token, rows, error = result
        if token != (self._catalog_generation, self.view.scene.project_id):
            return
        evidence = getattr(self, '_catalog_evidence', None)
        if (evidence is None or not evidence.active or evidence.final
                or self._catalog_pending is not None):
            return  # Duplicate ready callbacks cannot replay the same install.
        self._background_jobs.release(self._catalog_query_job)
        self._catalog_query_job = None
        if error:
            evidence.finish('catalog_query_failed', reason='directory_read_failed',
                            error_type='catalog_unavailable')
            self._show_catalog_read_error(error)
            return
        try:
            self._catalog_rows_received(token, rows)
        except Exception as error:
            self._catalog_install_failed(self._catalog_pending, error)

    def _show_catalog_read_error(self, error):
        # A failed whole-project read must not leave the previous result on
        # screen looking like the new one.  The list was disabled when the
        # query started, the old rows are still in the model and the
        # breadcrumb still carries a count: that is exactly "the old 100
        # items, presented as the whole project".  Clear the model, say
        # what happened, and hand the controls back so the user can retry.
        self._catalog_scalar_cache = None
        self._selecting = True
        try:
            self.model.replace([])
        finally:
            self._selecting = False
        self.breadcrumb.setText(getattr(self, 'title', _('All assets')))
        self.list.setEnabled(True)
        self.empty.setText(_('Project directory unavailable; retry after it is ready'))
        self.empty.show()
        self.list.hide()
        self.summary.setText(_('Whole-project assets could not load: ') + error)
        self.summary.show()
        return


    def _catalog_rows_received(self, token, rows):
        evidence = self._catalog_evidence
        self._catalog_scalar_cache = self._catalog_cache_key, rows
        self._register_catalog_stream(self._catalog_cache_key, rows)
        covered, fresh = self.catalog._session_overlay()
        live = list(covered.values()) + list(fresh)
        overlay, new = {}, []
        for item in live:
            key = getattr(item, 'save_id', None)
            if key is None:
                key = getattr(item, 'original_save_id', None)
            if key is None:
                new.append(item)
            else:
                overlay[key] = item
        if self.mode == 'recent':
            from prism.project_resources import recent_ledger_rows
            from prism.catalog_browser import CatalogAssetProxy
            for row in recent_ledger_rows(self._catalog_scope, self._recent_usage_entries()):
                if row['id'] not in overlay:
                    overlay[row['id']] = CatalogAssetProxy(
                        row, self._catalog_scope.project_path,
                        self.view.scene, self._catalog_display_reader)
        from prism import tags
        panel = self.view.category_panel
        text, expression = panel._split_query(self.search.text())
        rule = self.saved_rule or {}
        self._catalog_match_context = dict(known=self.catalog._known_canvas_ids(),
            deleted=self.catalog._deleted_page_ids(),
            scope=set(self.scope_pages) if self.scope_pages is not None else None,
            min_rating=panel._min_rating, system=tags.tag_system(self.view.scene),
            active={str(name).casefold() for name in panel._active_tags}, words=text.casefold().split(),
            expression=tags.parse_query(expression) if expression else None,
            rule_words=rule.get('text', '').casefold().split(),
            rule_rating=int(rule.get('rating', 0)),
            rule_expression=tags.parse_query(rule['tag_expression']) if rule.get('tag_expression') else None)
        self._catalog_pending = dict(token=token, rows=rows, offset=0, items=[],
                                     overlay=overlay, new=new, evidence=evidence,
                                     requested_key=self._catalog_requested_key,
                                     requested_digest=self._catalog_requested_digest,
                                     pushdown=self._catalog_pushdown_filters(),
                                     project=str(self.view.scene.project_id),
                                     open_epoch=self._catalog_open_epoch(),
                                     query_generation=self._catalog_publication_generation,
                                     overlay_rows=0, new_rows=0, filtered_rows=0)
        self._install_catalog_rows(self._catalog_pending)

    def _catalog_matches(self, item):
        from prism import tags
        context = self._catalog_match_context
        if self.catalog.is_annotation(item):
            return False
        if self.catalog.is_deleted(item) != (self.mode == 'trash'):
            return False
        if self.mode != 'trash' and getattr(item, '_canvas_id', None) in context['deleted']:
            return False
        if context['scope'] is not None and self.catalog._effective_canvas_id(item, context['known']) not in context['scope']:
            return False
        if self.mode == 'unclassified' and not self.catalog.is_unclassified(item, context['known']):
            return False
        if self.mode == 'untagged' and self.catalog._has_valid_tags(item):
            return False
        if int(getattr(item, '_rating', 0) or 0) < max(context['min_rating'], context['rule_rating']):
            return False
        if self.mode == 'recent' and not getattr(item, '_last_used', ''):
            return False
        names = getattr(item, '_tags', ()) or ()
        if context['active'] or context['expression'] or context['rule_expression']:
            index = tags.name_index(names, context['system'])
            if not context['active'].issubset(index):
                return False
            for predicate in (context['expression'], context['rule_expression']):
                if predicate is not None and not predicate.matches(index):
                    return False
        if context['words'] or context['rule_words']:
            haystack = self.catalog._text_haystack(item, names)
            if not all(word in haystack for word in context['words'] + context['rule_words']):
                return False
        return True

    def _trim_catalog_proxy_cache(self):
        """Keep the display-proxy cache bounded **by entry count** (LRU).

        The evicted proxy is only dropped from the cache: it may still be on
        screen in the model, and it stays a read-only display proxy - it is
        never inserted into the scene and never becomes an editable object.
        A later pass over the same row rebuilds a fresh proxy from the row,
        so nothing is lost by evicting.
        """
        cache = self._catalog_proxy_cache
        while len(cache) > CATALOG_PROXY_CACHE_LIMIT:
            _key, proxy = cache.popitem(last=False)
            try:
                proxy._catalog_evicted = True
            except AttributeError:  # pragma: no cover - proxies accept attrs
                pass

    def _catalog_install_valid(self, pending):
        # A singleShot closure can outlive the native panel and its children.
        # Validate Qt ownership before reading the scene or touching controls.
        if (sip.isdeleted(self) or sip.isdeleted(self.summary)
                or sip.isdeleted(self.view)):
            return False
        if pending is None or pending is not self._catalog_pending:
            return False
        from prism.catalog_install import InstallIdentity
        identity = InstallIdentity.capture(pending)
        if not identity.matches(
                token=(self._catalog_generation, self.view.scene.project_id),
                project=self.view.scene.project_id, open_epoch=self._catalog_open_epoch(),
                query_generation=self._catalog_publication_generation):
            return False
        return bool(pending['evidence'].active)

    def _catalog_open_epoch(self):
        """The open epoch of the project the browser currently shows.

        The same project reopened is a different epoch.  ``save_preparation``
        reads the same attribute, so the browser and the save receipt agree
        on what "this open" means.
        """
        return str(getattr(self.view, '_trace_open_epoch', 0) or 0)

    def _catalog_install_progress(self, pending, *, final=False):
        evidence = pending['evidence']
        evidence.delivery('consumer_consumed', complete=final, final=final)
        evidence.record('asset_model_install_progress',
            **evidence.snapshot('consumer_consumed'), read_id=evidence.read_id,
            source_query_id=evidence.source_query_id, read_reused=evidence.read_reused,
            directory_rows_expected=len(pending['rows']), directory_rows_expected_unit='catalog_rows',
            directory_rows_expected_cumulative=False, directory_rows_expected_deduplicated=False,
            accepted_rows=len(pending['items']), accepted_rows_unit='candidate_rows',
            accepted_rows_cumulative=True, accepted_rows_deduplicated=False,
            filtered_rows=pending['filtered_rows'], filtered_rows_unit='candidate_rows',
            filtered_rows_cumulative=True, filtered_rows_deduplicated=False,
            complete=final, final=final)

    def _queue_catalog_install(self, pending, *, delay=1, progress=True):
        if not self._catalog_install_valid(pending) or pending.get('install_queued'):
            return
        pending['install_queued'] = True
        try:
            self._maybe_publish_grid_preview(pending)
            if not self._catalog_install_valid(pending):
                pending['install_queued'] = False
                return
            if progress:
                self._catalog_install_progress(pending)
            def install():
                pending['install_queued'] = False
                self._install_catalog_rows(pending)
            QtCore.QTimer.singleShot(delay, install)
        except Exception as error:
            pending['install_queued'] = False
            self._catalog_install_failed(pending, error)

    def _catalog_install_failed(self, pending, error):
        """A failed asynchronous read is a query result, never an app crash."""
        if sip.isdeleted(self) or sip.isdeleted(self.view):
            return
        if pending is not None and pending is not self._catalog_pending:
            return
        evidence = (pending or {}).get('evidence', getattr(self, '_catalog_evidence', None))
        if evidence is None or not evidence.active:
            return
        from prism.project_catalog_index import CatalogNotReady
        source_changed = isinstance(error, CatalogNotReady)
        reason = 'source_changed_during_read' if source_changed else 'installation_failed'
        evidence.finish('catalog_query_deferred' if source_changed else 'catalog_query_failed',
                        reason=reason, error_type=type(error).__name__)
        self._clear_grid_preview(pending)
        # Do not checkpoint/reuse a stream whose producer has failed.
        self._catalog_pending = None
        self._cancel_catalog_query()
        self._close_catalog_streams(reason)
        self._catalog_results.clear(reason)
        self._catalog_scalar_cache = None
        self._catalog_data_index = None
        if not source_changed:
            import logging
            logging.getLogger(__name__).exception('Project resource list installation failed')
            self._show_catalog_read_error(str(error))
            return
        self.summary.setText('工程索引正在更新，素材列表将自动恢复。')
        self.summary.show()
        self.empty.setText(_('Preparing…'))
        self.list.setEnabled(False)
        token = (self._catalog_generation, self.view.scene.project_id, self._catalog_open_epoch())
        def retry():
            if (not sip.isdeleted(self) and not sip.isdeleted(self.view)
                    and self.isVisible()
                    and token == (self._catalog_generation, self.view.scene.project_id, self._catalog_open_epoch())):
                self.refresh()
        # Let the source transaction's queued receipt reach the existing index
        # coordinator first. Refresh reuses that coordinator; no second builder.
        QtCore.QTimer.singleShot(100, retry)

    def _install_catalog_rows(self, pending=None):
        pending = self._catalog_pending if pending is None else pending
        if not self._catalog_install_valid(pending):
            return
        if (pending.get('published_items') is self.model.items
                and self.model.presentation_evidence is pending['evidence']
                and pending['evidence'].final):
            self._finish_catalog_install(pending, self.model.items)
            return
        try:
            import time
            started = time.perf_counter()
            offset = pending['offset']
            try:
                self._advance_catalog_rows(pending)
            finally:
                elapsed_ms = (time.perf_counter() - started) * 1000
                consumed = pending['offset'] - offset
                if consumed > 0 and elapsed_ms > 0:
                    # Aim below the 4ms cooperative budget, with an allocation
                    # ceiling even on a very fast machine. A GC pause reduces
                    # the next batch; it is never hidden by changing the budget.
                    pending['batch_limit'] = max(8, min(256,
                        int(consumed * 2.0 / elapsed_ms)))
                # This includes synchronous publication on the final turn.
                # GC/native construction cannot be preempted mid-row. Report
                # actual overruns instead of labelling 4ms a hard frame bound.
                if elapsed_ms > 4:
                    trace_record('catalog_install_slice_overrun',
                                 read_id=pending['evidence'].read_id,
                                 elapsed_ms=elapsed_ms, budget_ms=4,
                                 budget_kind='cooperative_between_rows',
                                 phase_at_yield=pending.get('phase', 'unknown'),
                                 directory_rows_consumed=pending['offset'] - offset,
                                 includes_synchronous_publication=True)
        except Exception as error:
            self._catalog_install_failed(pending, error)

    def _advance_catalog_rows(self, pending):
        from prism.catalog_browser import CatalogAssetProxy
        from prism.catalog_match_record import CatalogMatchRecord
        import time
        rows, offset = pending['rows'], pending['offset']
        deadline = time.perf_counter() + .004
        # A page prefix is also an allocation bound, not just a time check.
        # Small turns avoid flooding gen0 before the deadline can be checked.
        batch_end = offset + pending.get('batch_limit', 64)
        if hasattr(rows, 'try_slice'):
            batch = rows.try_slice(offset, batch_end)
            if batch is None:
                # The read has not produced a row yet.  Nothing moved, so the
                # same progress numbers would be published again; re-queue the
                # work without reporting a progress that did not happen.
                self._queue_catalog_install(pending, delay=5, progress=False)
                return
        else:
            batch = rows[offset:batch_end]
        if not self._catalog_install_valid(pending):
            return
        evidence = pending['evidence']
        # This attempt's own row count.  Never a cumulative counter: the
        # acceptance criteria forbid passing a lifetime total off as the work
        # of one query.
        pending['read_rows'] = pending.get('read_rows', 0) + len(batch)
        pending['candidate_rows'] = pending.get('candidate_rows', 0) + len(batch)
        if not hasattr(rows, 'presentation_evidence'):
            evidence.observe('sequence_access', (row['id'] for row in batch))
            evidence.delivery('sequence_access')
        pending['phase'] = 'directory_rows'
        for row in batch:
            if not self._catalog_install_valid(pending):
                return
            evidence.observe('consumer_consumed', (row['id'],))
            item = pending['overlay'].pop(row['id'], None)
            scalar_match = item is None
            if item is not None:
                pending['overlay_rows'] += 1
            if item is None:
                # Reject scalar metadata before allocating ImageRecord/Qt
                # geometry. Live/parked objects above remain authoritative.
                match_record = CatalogMatchRecord(row)
                if not self._catalog_matches(match_record):
                    pending['filtered_rows'] += 1
                    pending['offset'] += 1
                    if time.perf_counter() >= deadline:
                        break
                    continue
                requested_key = pending['requested_key']
                previous_key = getattr(self, '_catalog_result_key', None)
                # Overlay/query/metadata revisions change membership, not every
                # display record. Recheck predicates above, then reuse an unchanged
                # row from the existing model. Never reuse pixels across an
                # unproven source revision, scene/open lifetime or display reader.
                same_source = (previous_key is not None and requested_key is not None
                               and previous_key.source_revision != 'unknown'
                               and previous_key.source_revision == requested_key.source_revision)
                item = self._catalog_proxy_cache.get(row['id']) if same_source else None
                if item is None and same_source:
                    item = self._reuse_grid_proxy(row['id'], requested_key)
                if item is None and same_source:
                    position = self.list._item_rows.get((('save', row['id']), row.get('canvas_id')))
                    if position is not None and position < len(self.model.items):
                        candidate = self.model.items[position]
                        if isinstance(candidate, CatalogAssetProxy):
                            item = candidate
                if item is not None and not item.matches_catalog_row(
                        row, self._catalog_scope.project_path, self.view.scene,
                        self._catalog_display_reader):
                    item = None
                if item is None:
                    item = CatalogAssetProxy(row, self._catalog_scope.project_path,
                                             self.view.scene, self._catalog_display_reader,
                                             parsed_tags=match_record._tags)
                    pending['proxies_created'] = pending.get('proxies_created', 0) + 1
                    self._catalog_proxy_cache[row['id']] = item
                    self._trim_catalog_proxy_cache()
                else:
                    pending['proxies_reused'] = pending.get('proxies_reused', 0) + 1
                    # A proxy that was taken over from the published model is
                    # not in the LRU yet: adopt it, so the LRU can still
                    # evict it later.  (Without the adoption the LRU lookup
                    # raises KeyError, because the key was never inserted.)
                    self._catalog_proxy_cache[row['id']] = item
                    # Least recently used first: a proxy that is still on
                    # screen must not be evicted before one that is not.
                    self._catalog_proxy_cache.move_to_end(row['id'])
                    self._trim_catalog_proxy_cache()
                    if item._catalog_display_failed:
                        item._catalog_display_failed = False
            if scalar_match or self._catalog_matches(item):
                pending['items'].append(item)
            else:
                pending['filtered_rows'] += 1
            pending['offset'] += 1
            if time.perf_counter() >= deadline:
                break
        if pending['offset'] < len(rows):
            self.summary.setText('正在读取' + getattr(self, 'title', _('All assets')) + '：' + f"{min(pending['offset'], len(rows)):,}/{len(rows):,}")
            self._queue_catalog_install(pending)
            return
        if batch:
            self._queue_catalog_install(pending)
            return
        pending['phase'] = 'live_overlay'
        from prism.catalog_install import OverlayProgress
        if 'overlay_progress' not in pending:
            pending['overlay_progress'] = OverlayProgress.begin(pending['overlay'], pending['new'])
        from prism.catalog_install import consume_overlay
        batch_result = consume_overlay(pending['overlay_progress'],
            matches=self._catalog_matches,
            valid=lambda: self._catalog_install_valid(pending),
            expired=lambda: time.perf_counter() >= deadline)
        if batch_result.stop == 'invalid' or not self._catalog_install_valid(pending):
            return
        pending['overlay_progress'] = batch_result.progress
        pending['items'].extend(batch_result.accepted)
        pending['overlay_rows'] += batch_result.overlay_rows
        pending['new_rows'] += batch_result.new_rows
        pending['filtered_rows'] += batch_result.filtered_rows
        if batch_result.stop != 'exhausted':
            self._queue_catalog_install(pending)
            return
        if pending.get('pushdown') and not pending.get('publication_order_restored'):
            # A pushed-down filter can leave a live overlay item without the
            # directory row that used to carry it.  Put every item back on the
            # canonical id order so the published order is the one the
            # un-pushed read would have produced.
            pending['items'] = self._restore_publication_order(pending['items'])
            pending['publication_order_restored'] = True
        pending['phase'] = 'sort_and_other_domains'
        items = pending['items']
        panel = self.view.category_panel
        kind = panel.search_kind
        self.search_hits = []
        resource_scope = (self.mode in _RESOURCE_SCOPES
                          and self.scope_pages is None and not self.saved_rule)
        if resource_scope:
            from prism import project_resources
            requested = pending.get('requested_key')
            if requested is not None and (not self._save_projection_matches(requested)
                    or requested.overlay_revision != self._catalog_overlay_revision(self.mode)):
                self._cancel_catalog_query()
                self.schedule()
                return
            if 'resource_steps' not in pending:
                pages = panel._pages_with_implicit_defaults()
                cached_domains = getattr(panel, '_cached_resource_domains', lambda pages: None)(pages)
                domain_pending = getattr(panel, '_resource_domain_pending', None)
                if (cached_domains is None and domain_pending is not None
                        and domain_pending['key'] == panel._resource_domain_key(pages)):
                    # The sidebar is already building this exact projection.
                    # Keep the published image prefix usable and join its
                    # result instead of allocating a second search snapshot.
                    self._queue_catalog_install(pending, delay=5, progress=False)
                    return
                pending['resource_steps'] = project_resources.collect_steps(
                    self.view.scene, panel.resource_service, self.mode,
                    asset_items=items, catalog=self.catalog,
                    recent=self._recent_usage_entries(),
                    page_records=pages, domain_snapshot=cached_domains,
                    **panel.resource_scope_options(self.search.text(), kind))
            if 'resource_items' not in pending:
                try:
                    next(pending['resource_steps'])
                except StopIteration as complete:
                    pending['resource_items'] = complete.value
                else:
                    self._queue_catalog_install(pending, progress=False)
                    return
            items = pending['resource_items']
            self.search_hits = [item for item in items if isinstance(item, dict)]
        elif kind not in ('all', 'asset'):
            # Existing document/mindmap/material query remains authoritative for
            # those domains; asset candidates come from this complete directory.
            self.search_hits = [hit for hit in panel.project_search_results(self.search.text())
                                if hit['kind'] not in ('asset', 'folder', 'tag')]
            items = self.search_hits
        from prism.catalog_ordering import order_catalog_items
        from prism.project_search import item_key
        items = order_catalog_items(
            items, mode=('all' if resource_scope and self.mode == 'recent' else self.mode),
            sort_key=self.sort_key, name=asset_name,
            key=item_key, visual_keys=tuple(item_key(item) for item, _score in
                (getattr(self, 'visual_results', None) or ())),
            random_seed=getattr(self, '_random_seed', 0))
        if not self._catalog_install_valid(pending):
            return
        evidence.delivery('sequence_access', complete=True, final=True)
        evidence.record('catalog_rows_read_evidence', **evidence.read_fields(),
                        complete=True, final=True)
        self._catalog_install_progress(pending, final=True)
        if not self._catalog_install_valid(pending):
            return
        publication = dict(read_id=evidence.read_id, source_query_id=evidence.source_query_id,
                           read_reused=evidence.read_reused,
                           consumer_logical_rows=evidence.snapshot('consumer_consumed')['logical_rows'],
                           consumer_unique_rows=evidence.snapshot('consumer_consumed')['unique_rows'],
                           consumer_logical_rows_unit='row_accesses',
                           consumer_logical_rows_cumulative=True, consumer_logical_rows_deduplicated=False,
                           consumer_unique_rows_unit='asset_ids', consumer_unique_rows_cumulative=True,
                           consumer_unique_rows_deduplicated=True,
                           consumer_unique_rows_complete=evidence.snapshot('consumer_consumed')['unique_rows_complete'])
        for key, value in (('overlay_rows', pending['overlay_rows']),
                           ('new_rows', pending['new_rows']), ('search_hit_rows', len(self.search_hits))):
            publication.update({key: value, key+'_unit': 'candidate_rows',
                                key+'_cumulative': True, key+'_deduplicated': False})
        pending['published_items'] = items
        self._prepare_catalog_layout_token(
            pending, items, pending.get('requested_key'),
            lambda layout_token: self._publish_prepared_catalog_items(
                pending, items, publication, layout_token))

    def _publish_prepared_catalog_items(self, pending, items, publication, layout_token):
        evidence = pending['evidence']
        preview_view = pending.get('grid_preview_view')
        preview_scroll = (preview_view.verticalScrollBar().value()
                          if preview_view is self.list else None)
        pending['layout_token'] = layout_token
        pending['phase'] = 'model_publication'
        self._catalog_restore_layout = self._equivalent_catalog_layout(layout_token)
        self._selecting = True
        try:
            self.view.scene.clearSelection()
            published = self.model.replace(items, evidence=evidence,
                guard=lambda: self._catalog_install_valid(pending),
                layout_token=pending['layout_token'],
                publication_fields=publication)
        finally:
            self._selecting = False
            self._catalog_restore_layout = None
            self._catalog_previous_layout = None
        if not published:
            if self._catalog_install_valid(pending) and self.model._replacing:
                self._queue_catalog_install(pending)
            return
        if (preview_scroll is not None and preview_view is self.list
                and self._catalog_install_valid(pending) and self.model.items is items):
            preview_view.verticalScrollBar().setValue(preview_scroll)
        self._finish_catalog_install(pending, items)

    def _finish_catalog_install(self, pending, items, reused=False):
        if (not self._catalog_install_valid(pending) or self.model.items is not items
                or self.model.presentation_evidence is not pending['evidence']):
            return
        evidence = pending['evidence']
        evaluated_rows = (0 if reused else
                          max(0, pending['offset'] - pending.get('inherited_offset', 0)))
        proxies_created = 0 if reused else pending.get('proxies_created', 0)
        read_rows = 0 if reused else pending.get('read_rows', 0)
        evidence.record('catalog_install_work',
            inherited_rows=pending.get('inherited_offset', 0),
            evaluated_rows=evaluated_rows,
            proxies_reused=pending.get('proxies_reused', 0),
            published_rows=len(items), counter_basis='this_install_attempt')
        if hasattr(pending['rows'], 'stats'):
            evidence.record('catalog_replay_state', phase='installed',
                counter_basis='read_id_lifetime', **pending['rows'].stats())
            if not self._catalog_install_valid(pending):
                return
        query_key = pending.get('requested_key')
        digest = pending.get('requested_digest')
        # Root resource scopes retain their complete mixed result. Domain
        # edits invalidate the metadata epoch; merely switching scopes does not.
        # An **empty** result is a legitimate complete result and is kept:
        # recomputing it costs nothing, but keeping it means an empty scope
        # or an empty tag set does not rescan the catalog on every visit.
        cacheable = ((self.mode in _RESOURCE_SCOPES and self.scope_pages is None
                      and not self.saved_rule)
                     or (not self.search_hits and items is pending.get('items', items)))
        if query_key is not None and digest is not None:
            if cacheable:
                self._remember_completed_result(pending, items, query_key, digest)
            else:
                self._catalog_results.invalidate(
                    'result_mixes_live_domains',
                    predicate=lambda entry: entry.get('query_key') == query_key)
            # A finished query leaves no prefix checkpoint behind: the next
            # visit starts from the finished result, not from a half-finished
            # install.
            self._catalog_results.drop_pending(digest)
        self._preserve_catalog_display = False
        self._catalog_pending = None
        self._catalog_result_key = query_key
        self._catalog_result_digest = digest
        self._catalog_result_items = self.model.items
        data_index = pending.get('grid_data_index')
        self._catalog_data_index = (data_index if data_index is not None
                                    and data_index['items'] is items else None)
        self._end_grid_preview(pending)
        self.back.setEnabled(self.history_index > 0)
        self.forward.setEnabled(self.history_index < len(self.history)-1)
        self.breadcrumb.setText(getattr(self, 'title', _('All assets')) + f' ({len(items):,})')
        self.list.setEnabled(True)
        self.summary.setText(getattr(self, 'title', _('All assets')) + f'：{len(items):,}' + _(' · Unloaded assets support source navigation; batch editing requires loading first'))
        self.summary.show()
        self.empty.setVisible(not items)
        self.list.setVisible(bool(items))
        self.show_scope_info(items)
        self.list._publish_model_counts()
        self._catalog_publication_seq += 1
        publish_result(ResultPublication(
            query_digest=digest or '',
            match_digest=query_key.match_digest() if query_key is not None else '',
            source_digest=fingerprint(query_key.source_identity()) if query_key is not None else '',
            project=query_key.project if query_key is not None else '',
            source_revision=query_key.source_revision if query_key is not None else '',
            overlay_revision=query_key.overlay_revision if query_key is not None else '',
            mode=self.mode,
            published_rows=len(items),
            evaluated_rows=evaluated_rows,
            proxies_created=proxies_created,
            read_rows=read_rows,
            candidate_rows=pending.get('candidate_rows', 0),
            result_reused=bool(reused),
            read_reused=bool(evidence.read_reused),
            pushdown_filters=tuple(sorted((pending.get('pushdown') or {}).items())),
            complete=True,
            final=True,
            reason='result_reused' if reused else 'result_computed',
            publication_id=self._catalog_publication_id(),
            query_generation=int(pending.get('query_generation', 0)),
            publication_seq=self._catalog_publication_seq,
            open_epoch=self._catalog_open_epoch(),
            session_id=self._catalog_session_id))

    def _catalog_publication_id(self):
        """The business identity of the publication being produced.

        Deterministic on purpose: session, project, open epoch, query
        generation and the sequence inside that generation.  No wall clock
        and no trace sequence - neither of them orders publications, and the
        log's sequence does not survive a restart.
        """
        return fingerprint((self._catalog_session_id,
                            str(self.view.scene.project_id),
                            self._catalog_open_epoch(),
                            int(self._catalog_publication_generation),
                            int(self._catalog_publication_seq)))

    def show_scope_info(self, items):
        if self.list.selectionModel().hasSelection():
            return
        panel = self.view._detail_panel
        worker = getattr(self, '_storage_worker', None)
        if worker is not None:
            worker.cancelled.set()
        self._storage_pending = None
        size = None
        if len(items) > 256 and not _has_catalog_proxy(items):
            # File/stat and archive-size enumeration never block installing a
            # large scene result. Publish unknown, then only this scope's result.
            token = (self.view.scene.project_id, self.view.scene._asset_revision, id(items))
            if worker is None:
                self._start_storage_summary(items, token)
            else:
                self._storage_pending = (items, token)
        else:
            size = self.storage_bytes(items)
        panel.show_collection(getattr(self, 'title', _('All assets')), len(items), size)

    def _start_storage_summary(self, items, token):
        worker = self._storage_worker = _StorageWorker(items, token, self._storage_ready)
        self._background_jobs.start(storage_thread_pool(), worker)

    def _storage_ready(self, result):
        worker, size = result
        self._background_jobs.release(worker)
        if sip.isdeleted(self) or worker is not getattr(self, '_storage_worker', None):
            return
        self._storage_worker = None
        token = (self.view.scene.project_id, self.view.scene._asset_revision, id(self.model.items))
        pending = getattr(self, '_storage_pending', None)
        self._storage_pending = None
        if pending is not None:
            if pending[1] == token and self.isVisible():
                self._start_storage_summary(*pending)
            return
        if (worker.cancelled.is_set() or worker.token != token or not self.isVisible()
                or self.list.selectionModel().hasSelection()
                or self.view.scene.selectedItems(user_only=True)):
            return
        self.view._detail_panel.show_collection(
            getattr(self, 'title', _('All assets')), len(self.model.items), size)

    def storage_bytes(self, items):
        if _has_catalog_proxy(items):
            return None  # Do not enumerate every source BLOB size on the GUI thread.
        from prism.source_store import storage_bytes
        if not hasattr(self, '_storage_cache'):
            self._storage_cache = {}
        return storage_bytes(items, self._storage_cache)

    def select_assets(self, *_args):
        if self._selecting or getattr(self, '_catalog_preview_active', False):
            return
        if self.mode == 'trash':
            return  # Selecting trash must not load/show it or mark it recently used.
        self._selecting = True
        blocker = QtCore.QSignalBlocker(self.view.scene)
        self.view.scene.clearSelection()
        selected = [index.data(QtCore.Qt.ItemDataRole.UserRole) for index in self.list.selectedIndexes()]
        has_unloaded = any(getattr(item, '_catalog_proxy', False) for item in selected)
        if has_unloaded and len(selected) > 1:
            del blocker
            self.view.scene.selectionChanged.emit()
            self.view._detail_panel.show_collection(_('Unloaded selection: open source before batch editing'), len(selected), None)
            self._selecting = False
            return
        if has_unloaded and len(selected) == 1:
            item = selected[0]
            del blocker
            self.view.scene.selectionChanged.emit()
            if getattr(item, '_catalog_project_id', None) == self.view.scene.project_id:
                self.view._detail_panel.show_asset_reference(item)
            self._selecting = False
            return
        if len(selected) == 1 and isinstance(selected[0], dict):
            result = dict(selected[0])
            panel = self.view.category_panel
            from prism.project_search import node_path
            nodes = {node['id']: node for node in panel.resource_service.nodes if not node.get('deletedAt')}
            folder = nodes.get(result.get('folder_id'))
            if folder is not None:
                result['path'] = node_path(folder, nodes)
            del blocker
            self.view.scene.selectionChanged.emit()
            project = self.view.scene.project_id
            def locate():
                if self.view.scene.project_id == project:
                    panel.activate_project_result(result)
            self.view._detail_panel.show_search_result(result, locate, locate)
            self._selecting = False
            return
        for item in selected:
            if getattr(item, '_catalog_proxy', False):
                # A display proxy is a stand-in for an asset that is not in
                # the scene - whether it is still in the proxy cache or was
                # evicted from it.  It is resolved to the canonical asset or
                # dropped; it never becomes an editable scene object.
                if getattr(item, '_catalog_project_id', None) != self.view.scene.project_id:
                    continue
                item = self.view.ensure_asset_loaded(str(item.save_id))
                if item is None or getattr(item, '_catalog_proxy', False):
                    self.summary.setText(_('This asset is not loaded; open its source canvas'))
                    continue
            if isinstance(item, dict):
                continue
            item.setVisible(True)
            item.setSelected(True)
            item._last_used = datetime.now(timezone.utc).isoformat()
            identifier = getattr(item, 'save_id', None)
            self.view.category_panel.record_resource_usage('asset', str(id(item) if identifier is None else identifier),
                getattr(item, '_title', '') or getattr(item, 'filename', '') or '')
        del blocker
        self.view.scene.selectionChanged.emit()
        self.view._detail_panel._on_selection_changed()
        if not selected:
            self.show_scope_info(self.model.items)
        self._selecting = False

    def locate(self, index):
        item = index.data(QtCore.Qt.ItemDataRole.UserRole)
        project_id = self.view.scene.project_id
        if not self.window_host._navigation_is_committed():
            # A QModelIndex may point at another row after an async refresh.
            # Capture the item and project, not the transient row index.
            return self.window_host.request_navigation(
                lambda: self._locate_item(item, project_id))
        return self._locate_item(item, project_id)

    def _locate_item(self, item, project_id):
        if self.view.scene.project_id != project_id:
            return
        if isinstance(item, dict):
            self.view.category_panel.activate_project_result(item)
            return
        if item is None or is_deleted(item):
            return
        if getattr(item, '_catalog_proxy', False):
            if item._catalog_project_id != self.view.scene.project_id:
                return
            page_id = getattr(item, '_canvas_id', None) or 'default-canvas'
            loaded = self.view.ensure_asset_loaded(str(item.save_id))
            if loaded is None:
                self.window_host.open_workspace_page('canvas', page_id)
                self.summary.setText(_('Source canvas opened; asset type cannot be loaded individually'))
                return
            item = loaded
        if item.scene() is not self.view.scene or getattr(item, '_deleted_at', None):
            return
        window = self.window_host
        kind, page_id = self.catalog.locate(item)
        window.open_workspace_page(kind, page_id)
        self.view.scene.clearSelection()
        item.setVisible(True)
        item.setSelected(True)
        self.view.centerOn(item)

    def sort_menu(self):
        menu = QtWidgets.QMenu(self)
        for compact, title in [(False, '标准缩略图'), (True, '紧凑缩略图')]:
            action = menu.addAction(title)
            action.setCheckable(True)
            action.setChecked(self.compact == compact)
            action.triggered.connect(lambda checked=False, v=compact: self.set_layout(v))
        menu.addSeparator()
        for key, title in [('default','默认顺序'),('name','名称'),('rating','评分')]:
            action = menu.addAction(title)
            action.setCheckable(True)
            action.setChecked(self.sort_key == key)
            action.triggered.connect(lambda checked=False, value=key: self.set_sort(value))
        menu.exec(self.sort_button.mapToGlobal(QtCore.QPoint(0, 28)))

    def set_layout(self, compact):
        self.compact = compact
        self.resize_cards(self.card_size)

    def set_sort(self, key):
        self.sort_key = key
        self.refresh()

    def filter_menu(self):
        menu = QtWidgets.QMenu(self)
        menu.addAction('清除筛选', self.view.category_panel.clear_filter)
        rating = menu.addMenu('最低评分')
        for value in range(6):
            action = rating.addAction('不限' if not value else '★'*value)
            action.triggered.connect(lambda checked=False, v=value: self.set_rating(v))
        tags_menu = menu.addMenu('标签')
        for tag in sorted(self.view.scene.get_all_tags()):
            action = tags_menu.addAction(tag)
            action.setCheckable(True)
            action.setChecked(tag in self.view.category_panel._active_tags)
            action.toggled.connect(lambda checked, t=tag: self.set_tag(t, checked))
        menu.exec(QtGui.QCursor.pos())

    def quick_actions(self):
        menu = QtWidgets.QMenu(self)
        selected = self.view.scene.selectedItems(user_only=True)
        if not selected:
            action = menu.addAction('请先选择素材')
            action.setEnabled(False)
        else:
            self.view.category_panel.build_tag_menu(menu.addMenu('标签'))
            rating = menu.addMenu('评分')
            for value in range(6):
                action = rating.addAction('清除评分' if not value else '★' * value)
                action.triggered.connect(lambda checked=False, v=value: self.view._detail_panel._set_rating(v))
            menu.addAction('移到回收站', self.view.on_action_delete_items)
        menu.exec(QtGui.QCursor.pos())

    def set_rating(self, value):
        self.view.category_panel.rating_filter.setCurrentIndex(value)
        self.refresh()

    def set_tag(self, tag, checked):
        own = self.view.category_panel._active_tags
        own.add(tag) if checked else own.discard(tag)
        self.refresh()

    def restore_resources(self, items=None):
        """Restore through the existing domain services and undo command."""
        if getattr(self, '_catalog_preview_active', False):
            self.window_host.statusBar().showMessage('正在加载回收站，请稍后恢复', 4000)
            return False
        if items is None:
            items = [index.data(QtCore.Qt.ItemDataRole.UserRole) for index in self.list.selectedIndexes()]
        items = list(items)
        if not items:
            return False
        panel, scene = self.view.category_panel, self.view.scene
        from prism import commands, fileio
        if self.view.filename and fileio.is_readonly_project(scene, self.view.filename):
            self.window_host.statusBar().showMessage('只读工程不能恢复资源', 5000)
            return False
        try:
            nodes, materials, models, images = set(), [], [], []
            for item in items:
                if isinstance(item, dict):
                    kind = item.get('kind')
                    if kind == 'material':
                        materials.append(item['material_id'])
                        node = panel.resource_service._by_id(item.get('folder_id'))
                    elif kind == 'model':
                        models.append(item['model_id'])
                        node = panel.resource_service._by_id(item.get('folder_id'))
                    else:
                        node = next((n for n in panel.resource_service.nodes if n.get('pageId') == item.get('page_id')), None)
                    top = None
                    while node is not None:
                        if node.get('deletedAt') is not None:
                            top = node['id']
                        node = panel.resource_service._by_id(node.get('parentId'))
                    if top:
                        nodes.add(top)
                else:
                    canonical = self.view.ensure_asset_loaded(str(item.save_id)) if getattr(item, '_catalog_proxy', False) else item
                    if canonical is None or getattr(canonical, '_catalog_proxy', False):
                        raise ValueError('部分素材尚未加载，请先打开所属画布再恢复')
                    images.append(canonical)
            if nodes:
                panel.resource_service.restore_deleted(list(nodes))
            if materials:
                panel.material_service.restore(materials)
            if images:
                self.view.undo_stack.push(commands.RestoreItems(scene, images))
            if nodes or materials or images:
                self.view.mark_content_dirty()
            if models:
                self._restore_models(tuple(models), (scene.project_id, getattr(self.view, '_trace_open_epoch', 0)))
            self._records_changed({'kind': 'restore'})
            trace_record('resource_restore_requested', count=len(items), models=len(models), materials=len(materials), images=len(images))
            return True
        except Exception as error:
            trace_record('resource_restore_failed', error_type=type(error).__name__)
            self.window_host.statusBar().showMessage('恢复失败：' + str(error), 7000)
            return False

    def _restore_models(self, ids, identity, attempt=0):
        if identity != (self.view.scene.project_id, getattr(self.view, '_trace_open_epoch', 0)):
            return
        composition = self.window_host.model_composition
        try:
            if composition.service is None:
                if attempt == 0:
                    composition.ensure()
                if attempt >= 100:
                    raise RuntimeError('模型服务未就绪，请重试')
                self.window_host.statusBar().showMessage('正在准备模型恢复…', 2000)
                QtCore.QTimer.singleShot(50, lambda: self._restore_models(ids, identity, attempt + 1)
                                        if not sip.isdeleted(self) else None)
                return
            composition.service.patch_metadata_batch(ids, {'deleted_at': None})
            self._records_changed({'kind': 'model_restore'})
        except Exception as error:
            trace_record('resource_restore_failed', resource_kind='model', error_type=type(error).__name__)
            self.window_host.statusBar().showMessage('模型恢复失败：' + str(error), 7000)

    def context_menu(self, pos):
        if getattr(self, '_catalog_preview_active', False):
            return  # No editing a non-final query prefix.
        index = self.list.indexAt(pos)
        if index.isValid() and index not in self.list.selectedIndexes():
            self.list.selectionModel().select(index, QtCore.QItemSelectionModel.SelectionFlag.ClearAndSelect)
        selected = [idx.data(QtCore.Qt.ItemDataRole.UserRole) for idx in self.list.selectedIndexes()]
        if self.mode == 'trash':
            menu = QtWidgets.QMenu(self)
            action = menu.addAction('恢复到原位置', lambda: self.restore_resources(selected))
            action.setEnabled(bool(selected))
            menu.exec(self.list.viewport().mapToGlobal(pos))
            return
        if any(getattr(item, '_catalog_proxy', False) for item in selected):
            menu = QtWidgets.QMenu(self)
            if len(selected) == 1:
                menu.addAction('打开所属位置', lambda: self._locate_item(selected[0], self.view.scene.project_id))
            hint = menu.addAction('请先加载素材，再进行批量编辑或导出')
            hint.setEnabled(False)
            menu.exec(self.list.viewport().mapToGlobal(pos))
            return
        if getattr(self.list, 'is_asset_grid', False):
            menu = QtWidgets.QMenu(self)
            if len(selected) == 1:
                menu.addAction('打开所属位置', lambda: self._locate_item(selected[0], self.view.scene.project_id))
            if selected:
                menu.addAction('导出所选图片', self.view.on_action_export_selected_images)
                menu.addAction('移到回收站', self.view.on_action_delete_items)
            else:
                menu.addAction('全选', self.list.select_all)
            menu.exec(self.list.viewport().mapToGlobal(pos))
            return
        self.view._remove_dynamic_menu_items()
        self.view._inject_canvas_tools()
        self.view._inject_category_menu()
        self.view.context_menu.exec(self.list.viewport().mapToGlobal(pos))

    def eventFilter(self, watched, event):
        if (watched is getattr(self,'empty',None) and event.type()==QtCore.QEvent.Type.Wheel
                and event.modifiers() & QtCore.Qt.KeyboardModifier.ControlModifier):
            delta=event.angleDelta().y() or event.pixelDelta().y()
            if delta:self.zoom_slider.setValue(self.zoom_slider.value()+(12 if delta>0 else -12))
            event.accept();return True

        if watched is self.toolbar:
            if event.type() == QtCore.QEvent.Type.MouseButtonDblClick and event.button() == QtCore.Qt.MouseButton.LeftButton:
                window = self.window_host
                window.showNormal() if window.isMaximized() else window.showMaximized()
                return True
            if event.type() == QtCore.QEvent.Type.MouseButtonPress and event.button() == QtCore.Qt.MouseButton.LeftButton:
                handle = self.window_host.windowHandle()
                if handle:
                    handle.startSystemMove()
                return True
        if (watched is getattr(self, 'list', None)
                and getattr(self, '_catalog_preview_active', False)
                and event.type() == QtCore.QEvent.Type.KeyPress
                and event.key() in (QtCore.Qt.Key.Key_Delete, QtCore.Qt.Key.Key_Return, QtCore.Qt.Key.Key_Enter)):
            return True
        if watched is getattr(self, 'list', None) and event.type() == QtCore.QEvent.Type.KeyPress and event.key() in (QtCore.Qt.Key.Key_Return, QtCore.Qt.Key.Key_Enter):
            self.locate(self.list.currentIndex())
            return True
        if watched is getattr(self, 'list', None) and event.type() == QtCore.QEvent.Type.KeyPress and event.key() == QtCore.Qt.Key.Key_Delete:
            if any(getattr(idx.data(QtCore.Qt.ItemDataRole.UserRole), '_catalog_proxy', False) for idx in self.list.selectedIndexes()):
                self.summary.setText(_('Load selected assets before deleting'))
                self.summary.show()
                return True
            self.view.on_action_delete_items()
            self.refresh()
            return True
        return super().eventFilter(watched, event)

    def keyPressEvent(self, event):
        if (getattr(self, '_catalog_preview_active', False)
                and event.key() in (QtCore.Qt.Key.Key_Delete, QtCore.Qt.Key.Key_Return, QtCore.Qt.Key.Key_Enter)):
            event.accept()
            return
        if event.key() == QtCore.Qt.Key.Key_Delete:
            if any(getattr(idx.data(QtCore.Qt.ItemDataRole.UserRole), '_catalog_proxy', False) for idx in self.list.selectedIndexes()):
                self.summary.setText(_('Load selected assets before deleting'))
                self.summary.show()
                return
            self.view.on_action_delete_items()
            self.refresh()
        elif event.key() in (QtCore.Qt.Key.Key_Return, QtCore.Qt.Key.Key_Enter):
            self.locate(self.list.currentIndex())
        else:
            super().keyPressEvent(event)
