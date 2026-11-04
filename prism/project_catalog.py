"""全项目素材查询服务（方案 §5）。

统一为左栏导航数字、资源树计数、全项目结果列表和状态栏提供同一套谓词，
避免 `media_library` / `project_search` / `resource_tree_model` 各自写一套
过滤条件导致口径漂移（顶部"全部素材 22" vs 父目录"1562"就是这么来的）。

设计要点
--------

* **只读元数据，不解码图像。** 1562 张图的计数不应触发 1562 次原图读取，
  否则平移/缩放/拖动过程中每一次 `scene.changed` 都会卡顿。所有谓词只看
  `_tags` / `_rating` / `_canvas_id` / `_deleted_at` / `_title` / `_notes`
  / `filename` 这些内存里已经有的属性。

* **活跃素材定义**（方案 §5.1）：
  - 有 `save_id` 属性（`scene.items_for_save()` 的口径，见 scene.py:539）
  - `TYPE` 不在 `ANNOTATION_TYPES = ('path', 'text')` 里（画笔线和文字
    注释不是素材）
  - 未软删除（`_deleted_at` 为 None，Stage C 引入的字段；缺失视为活跃）

* **无归属素材**（方案 §5.3）：桌面样本 `参考.prism` 里有 1 张图缺少
  `canvasId`。`items.load_metadata_from_data()` 会把缺失默认为
  `'default-canvas'`（items.py:413），所以运行时 `_canvas_id` 通常不会是
  None。但为了防御 `_canvas_id` 为 None、或指向的画布已不在工程里的情况，
  统一归入 `UNASSIGNED_CANVAS` 桶：
  - 全项目数量、未标签数量各计一次
  - 切换当前画布不漂移
  - 结果列表里路径显示为"未归属"
  - 定位时若原画布已消失，回落到 `default-canvas`（存在）或第一个画布

* **同一对象不重复计数**：用 `id(item)` 作稳定身份（`save_id` 未保存前
  可能是 None）。`scene.items()` 本身不会返回同一对象两次，所以直接枚举
  即可；额外的去重只在合并多个来源（例如归档 + 场景）时才需要。

* **不按文件名/内容去重**（方案 §5.1）：一张图出现在两个画布且项目存了
  两个独立对象，算两份实例。

* **不扫描磁盘参考目录**（方案 §5.1）：项目已导入的素材与文件夹里尚未
  导入的文件是不同概念。
"""

from __future__ import annotations

import collections
import heapq
import inspect
import json
import logging
import os
import sys
import threading
from dataclasses import dataclass, field
from typing import Iterable, Iterator, Optional

from prism.catalog_match_record import CatalogMatchRecord
from prism.project_catalog_index import (CatalogNotReady, PKG3_UNKNOWN,
                                        SCOPE_SNAPSHOT_EVENT,
                                        build_scope_snapshot,
                                        classify_scope_mode, pkg3_span,
                                        record_pkg3)

logger = logging.getLogger(__name__)


# ── C3：目录口径的命中/未命中计数点 ──────────────────────────────────────
#
# `performance_trace.record()` 在没有活动 trace 时是 no-op，所以计数点不能只
# 靠 trace：这里另有一份**永远活着**的计数器 + 可注入的观察者，验收组在没
# 有 trace 的测试里也能断言"C3 有没有被触发"。
#
# 事件名固定为 `catalog_scope_hit` / `catalog_scope_miss`；miss 一定带
# `reason`，来源互不混淆：
#
# * `no_partial_source`  —— 场景不是按页打开。**这是 T1 上线前的历史分支**：
#   T1 解除绑定以后这个 reason 不再产生，留在文档里是为了对照基线。
# * `no_scope_provider`  —— 没有接线（`scope_provider` 是 None）。
# * `provider_error`      —— provider 抛异常。
# * `no_scope`            —— provider 返回 None（没有目录实例）。
# * `not_ready`           —— 目录存在但不是 ready（缺失/过期/建库未完成）。
#
# 观察入口：
#
# * `catalog_scope_counters()` —— 计数快照，永远可用（生产路径与测试都能调）。
# * `reset_catalog_scope_counters()` —— 清零，测试用。
# * `add_catalog_scope_observer(cb)` / `remove_catalog_scope_observer(cb)`
#   —— `cb(event, **fields)`，在生产路径上同步调用。
# * trace 文件（`Prism-operations-<pid>.jsonl`）里的同名事件，只在有活动
#   trace 时才有。

_SCOPE_COUNTER_LOCK = threading.Lock()
_SCOPE_COUNTERS: 'collections.Counter' = collections.Counter()
_SCOPE_OBSERVERS: list = []


def catalog_scope_counters() -> dict:
    """C3 计数快照。

    返回形如 `{'catalog_scope_hit': 12, 'catalog_scope_miss': 3,
    'catalog_scope_miss:not_ready': 3}` 的字典。没有活动 trace 时也照样
    有数据——这是它和 `performance_trace.record()` 的区别。
    """
    with _SCOPE_COUNTER_LOCK:
        return dict(_SCOPE_COUNTERS)


def reset_catalog_scope_counters() -> None:
    """清零 C3 计数（测试用；生产路径不调用）。"""
    with _SCOPE_COUNTER_LOCK:
        _SCOPE_COUNTERS.clear()


def add_catalog_scope_observer(callback) -> None:
    """注册一个 C3 观察者：`callback(event, **fields)`。"""
    if callback not in _SCOPE_OBSERVERS:
        _SCOPE_OBSERVERS.append(callback)


def remove_catalog_scope_observer(callback) -> None:
    """注销一个 C3 观察者。"""
    try:
        _SCOPE_OBSERVERS.remove(callback)
    except ValueError:
        pass


def _record_scope_event(event: str, **fields) -> None:
    """记一条 C3 事件：计数器永远更新，trace 与观察者尽力而为。

    观察者/trace 抛异常绝不能影响计数本身，也不许影响调用方的结果。
    """
    reason = fields.get('reason')
    with _SCOPE_COUNTER_LOCK:
        _SCOPE_COUNTERS[event] += 1
        if reason:
            _SCOPE_COUNTERS['%s:%s' % (event, reason)] += 1
    module = sys.modules.get('prism.performance_trace')
    if module is not None:
        try:
            module.record(event, **fields)
        except Exception:
            logger.exception('catalog scope trace failed')
    for callback in list(_SCOPE_OBSERVERS):
        try:
            callback(event, **fields)
        except Exception:
            logger.exception('catalog scope observer failed')


def _scope_project_name(scope) -> str:
    """目录所属工程的文件名（trace 里不要写整条绝对路径）。"""
    try:
        return os.path.basename(str(scope.project_path))
    except Exception:
        return ''


# ── pkg3：这次查询到底是什么范围（规格 §6） ────────────────────────
#
# `query()` 是"全部素材 / 未分类 / 未标签 / 默认画布 / 普通页 / 搜索"六种口径
# 在**内存判定**这一侧的唯一入口，所以范围快照必须在这里落一次：这里的谓词
# 就是最终判定，口径不会和下推层漂移。
#
# 两个硬约束照旧：
#
# * **只描述已经算出来的东西。** `disk_rows` 是这次真的从目录读的行数，
#   `entries` 是内存判定活下来的行数，两者都是现成的；**不为了日志再查一次**
#   （没有额外的 `count()`、没有额外的遍历）。
# * **有效范围和请求范围分开写。** `requested_mode` 是调用方要的口径，
#   `effective_mode` 是六个口径里**唯一**生效的那一个，附
#   `effective_mode_reason`：为什么它不是另外五个。空范围绝不写成"全部"。

def _pkg3_scope_identity(*, text='', tag_expression='', untagged_only=False,
                         canvas_scope=None, known=None, kind_filter=None):
    """§6 的范围身份字段（六个口径互不合并）。"""
    words = str(text or '').strip()
    if untagged_only:
        mode = 'untagged'
    elif canvas_scope:
        mode = 'all'
    else:
        mode = 'all'
    canvases = set(canvas_scope) if canvas_scope is not None else None
    effective, reason = classify_scope_mode(
        mode, canvas_scope, search_text=words,
        saved_rule=tag_expression or None)
    fields = dict(
        requested_mode=('untagged' if untagged_only else
                        'page' if canvas_scope else 'all'),
        effective_mode=effective,
        effective_mode_reason=reason,
        search_active=bool(words or tag_expression),
        search_kind=(PKG3_UNKNOWN if kind_filter is None else kind_filter),
        default_canvas_selected=bool(
            canvases is not None and canvases <= {'default-canvas'}),
        current_page_kind=PKG3_UNKNOWN,
        current_page_id=(sorted(canvases)[0]
                         if canvases and len(canvases) == 1 else PKG3_UNKNOWN))
    return fields, canvases, known


def _pkg3_current_page(scene, attribute):
    """当前页面属性（kind/id），拿不到就 unknown。只读属性，不触发任何加载。"""
    value = getattr(scene, attribute, None)
    return value if isinstance(value, str) and value else PKG3_UNKNOWN


#: 资源树选中 id 由 `widgets/asset_browser` 提供：它是唯一的 Qt 侧持有者，
#: 本模块不引入 Qt，也不去遍历树（那是一次 O(可见行) 的额外工作）。
PKG3_RESOURCE_TREE_UNKNOWN = 'asset_browser_only'


def _pkg3_telemetry(scope, scene=None):
    """目录状态 + 指纹 + 页面身份，全部来自 scope 已经有的能力。

    优先用 ``scope.telemetry_state()``（读层给的、**带缓存**的状态，含建库
    状态与 ``total``）；读层还没接上时退回 ``scope.source_state()``，那是
    stat-only 的，两次指纹变了就说明缓存不可信。两条路都拿不到就写 unknown。
    """
    fields = dict(catalog_state=PKG3_UNKNOWN,
                  catalog_state_reason='no_scope',
                  source_fingerprint=PKG3_UNKNOWN,
                  cache_fingerprint=PKG3_UNKNOWN,
                  cache_revision=PKG3_UNKNOWN,
                  expected_total=PKG3_UNKNOWN,
                  expected_total_known=False,
                  cache_revision_source=PKG3_UNKNOWN,
                  project=PKG3_UNKNOWN,
                  current_page_kind=(_pkg3_current_page(scene, '_current_page_kind')
                                    if scene is not None else PKG3_UNKNOWN),
                  current_page_id=(_pkg3_current_page(scene, '_current_page_id')
                                   if scene is not None else PKG3_UNKNOWN),
                  resource_tree_selected_id=PKG3_RESOURCE_TREE_UNKNOWN)
    if scope is None:
        return fields
    provider = getattr(scope, 'telemetry_state', None)
    try:
        state = dict(provider()) if callable(provider) else dict(scope.source_state())
    except Exception:
        return fields
    if callable(provider):
        fields.update(state)
        return fields
    # 读层还没接上：只有 stat-only 的两个指纹。源变了缓存就是 stale。
    source = state.get('source')
    cache = state.get('cache')
    fields['source_fingerprint'] = (source if source is not None
                                    else PKG3_UNKNOWN)
    fields['cache_fingerprint'] = (cache if cache is not None else PKG3_UNKNOWN)
    if cache is None:
        fields['catalog_state'] = 'missing'
        fields['catalog_state_reason'] = 'cache_file_absent'
    elif getattr(scope, 'ready', False):
        fields['catalog_state'] = 'ready'
        fields['catalog_state_reason'] = 'ready_property'
    else:
        fields['catalog_state'] = 'stale'
        fields['catalog_state_reason'] = 'ready_property_false'
    try:
        fields['project'] = _scope_project_name(scope)
    except Exception:
        pass
    return fields


#: `scope.iter_query` 是否接受 `max_rows` 总量上限（按 scope 的类型缓存）。
#:
#: **一次性探测并缓存**：能力探测不能变成每次查询的反射开销，也不能因为
#: A-2 的文件还没落地就抛异常。
_SCOPE_MAX_ROWS_SUPPORT: dict = {}


def _scope_supports_max_rows(scope) -> bool:
    """目录读取接口是否接受 `max_rows`（具名形参）。

    只认**具名** `max_rows` 形参：`iter_query(**filters)` 那种写法会把
    `max_rows` 当成未知 filter 透传下去（scope 层会抛 `TypeError`），
    不算支持。探测失败（拿不到签名）一律当"不支持"——不支持时的行为是
    "不前推"，而不是"崩"。
    """
    cls = type(scope)
    cached = _SCOPE_MAX_ROWS_SUPPORT.get(cls)
    if cached is None:
        try:
            parameters = inspect.signature(cls.iter_query).parameters
        except (TypeError, ValueError):
            cached = False
        else:
            cached = 'max_rows' in parameters
        _SCOPE_MAX_ROWS_SUPPORT[cls] = cached
    return cached


def _normalize_max_rows(value):
    """`max_rows` 的契约：`None`（无上限）或 ≥1 的整数。

    与 scope 层 `project_open_scope._as_max_rows` **同契约**：非整数抛
    `TypeError`，小于 1 抛 `ValueError`。上限必须是"至少一行"——传 0 或
    负数几乎一定是调用方把参数搞错了，宁可报错也不要静默给出空结果。
    """
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError('max_rows must be an integer or None, got %r'
                        % type(value).__name__)
    if value < 1:
        raise ValueError('max_rows must be at least 1, got %r' % (value,))
    return value


def _as_canvas_ids(canvas_ids):
    """归一化 `canvas_scope`：**裸字符串 = 一个画布 id**。

    三层口径必须一致：scope 层 `project_open_scope._as_canvas_ids`、
    索引层 `project_catalog_index._canvas_id_tuple`、本层。目录侧会
    `tuple(canvas_ids)`，字符串一旦被拆成逐字符元组，
    `canvas_id IN ('s','t','r',…)` → **0 行而且不报错**——静默错答，
    比崩溃难查得多（本项目已经在索引层与 scope 层各被咬过一次）。

    * `None` → `None`（不限画布）；
    * 裸字符串 → **一个**画布 id（单元素元组），不是字符集合；
    * 其它可迭代对象 → 逐项取元组；
    * 不可迭代、或条目不是字符串（`None` 除外）→ `TypeError`。

    （`bytes` 不接受：它的条目是整数，不是画布 id。）
    """
    if canvas_ids is None:
        return None
    if isinstance(canvas_ids, str):
        return (canvas_ids,)
    try:
        ids = tuple(canvas_ids)
    except TypeError:
        raise TypeError('canvas_scope must be a string or an iterable of ids, '
                        'got %r' % type(canvas_ids).__name__) from None
    for value in ids:
        if value is not None and not isinstance(value, str):
            raise TypeError('canvas_scope entries must be strings or None, '
                            'got %r' % type(value).__name__)
    return ids


#: 画笔线和文字注释的 TYPE。它们不是素材：没有分类/标签/评分/文件名，
#: 按素材规则算永远"不匹配"。与 `media_library.ANNOTATION_TYPES` 保持
#: 一致（那里用字符串元组避免循环 import，这里同理）。
ANNOTATION_TYPES = ('path', 'text')

#: 无归属素材的虚拟画布 ID。不会与任何真实 pageId 冲突（真实 pageId 是
#: uuid4().hex 或 'default-canvas'/'default-mindmap'/'default-document'）。
UNASSIGNED_CANVAS = '__unassigned__'

#: 结果列表里"未归属"的显示文本。
UNASSIGNED_LABEL = '未归属'


@dataclass
class AssetResult:
    """全项目结果列表的一行。

    `item` 是场景里的图元对象（可能已被 sip 删除，消费方要用
    `sip.isdeleted` 守卫）。其余字段是渲染时需要的元数据快照，避免结果
    列表在构建后再去反复访问图元属性。

    按页打开时，`item` 也可能是**未加载**素材的只读显示代理
    （`prism.catalog_browser.CatalogAssetProxy`，`_catalog_proxy` 为真）：
    它带有目录里的标量元数据，但不允许选中/编辑/保存。消费方定位这类行要
    先按 `item.save_id` 走 `view.ensure_asset_loaded()`。
    """
    item: object
    title: str
    path: str
    canvas_id: str
    page_kind: str = 'canvas'
    tags: list = field(default_factory=list)
    rating: int = 0
    notes: str = ''
    filename: str = ''
    deleted_at: Optional[str] = None

    @property
    def is_unassigned(self) -> bool:
        return self.canvas_id == UNASSIGNED_CANVAS

    @property
    def is_directory_only(self) -> bool:
        """这一行来自磁盘目录，还没有对应的场景对象。"""
        return bool(getattr(self.item, '_catalog_proxy', False))


@dataclass(frozen=True)
class _Predicates:
    """一次查询的全部过滤条件。

    内存分支和目录分支**共用**同一个判定函数（`_matches`），两套口径不可能
    各写一遍然后漂移。标签表达式保留 `prism.tags.parse_query` 的完整语义，
    绝不用 SQL `LIKE` 简化替代。
    """

    words: tuple = ()
    tags: object = None
    system: object = None
    canvases: Optional[set] = None
    untagged: bool = False
    rating: int = 0
    deleted: bool = False


class _NoDirectoryDisplay:
    """`CatalogAssetProxy` 的显示读取器占位实现。

    结果列表/以图搜图只需要标量元数据与 `_prism_source`，不画缩略图，所以
    这里不挂 `CatalogDisplayReader`（那需要 Qt 父对象）。必须是一个可被
    `weakref.ref` 的对象。
    """

    def request(self, item, owner):
        pass


_NO_DIRECTORY_DISPLAY = _NoDirectoryDisplay()


def _order_key(item) -> str:
    """(path, title) 相同的一行之间的**明确** tie-break。

    有 `save_id` 的行按保存 id 排（与目录行的主键同一坐标系），还没保存的
    新对象排在前面并保持 `sorted` 的稳定性。不返回 `id()`：对象地址在同一
    次会话之外没有意义，而且会把结果顺序绑到内存分配上。
    """
    key = getattr(item, 'save_id', None)
    if key is None:
        key = getattr(item, 'original_save_id', None)
    return '' if key is None else str(key)


class ProjectAssetCatalog:
    """全项目素材查询服务。

    参数
    ----
    scene:
        `PrismScene` 实例。计数和查询都从 `scene.items_for_save()` 枚举。
    resource_service:
        可选的 `ResourceTreeService`。用来把 canvas_id 映射到资源树路径
        （"画布 / 参考 / 子文件夹"）。缺失时路径退化为画布标题或"未归属"。
    page_title_resolver:
        可选的可调用对象 `(canvas_id) -> str`，返回画布标题。默认从
        `scene.workspace_pages` 里查。

    线程安全
    --------
    非线程安全。所有方法都应在 GUI 线程调用。计数走 300 ms 防抖
    （`MediaLibraryPanel._recount_timer`），不要在 `scene.changed` 的每
    一次发射里都调。
    """

    def __init__(self, scene, resource_service=None, page_title_resolver=None,
                 scope_provider=None):
        self.scene = scene
        self.resource_service = resource_service
        self._page_title_resolver = page_title_resolver
        # 可选：返回 `ProjectOpenScope` 的可调用对象（通常来自 view 的按页
        # 打开目录）。非空且 ready 时，计数/查询走只读目录而非 scene 内存，
        # 保证「按页打开后左栏数字/资源树计数仍是全项目口径」。
        self._scope_provider = scope_provider

    # ── 谓词 ─────────────────────────────────────────────────

    @staticmethod
    def is_annotation(item) -> bool:
        """画笔线 / 文字注释。它们不是素材。"""
        return getattr(item, 'TYPE', '') in ANNOTATION_TYPES

    @staticmethod
    def is_deleted(item) -> bool:
        """软删除状态。Stage C 引入 `_deleted_at` 字段；缺失视为活跃。"""
        return getattr(item, '_deleted_at', None) is not None

    @classmethod
    def is_active(cls, item) -> bool:
        """活跃素材：有 save_id、非注释、未软删除。

        注意：`scene.items_for_save()` 已经过滤了 `hasattr(i, 'save_id')`，
        所以从它枚举出来的 item 不需要再检查 save_id。但 `is_active` 也
        会被外部直接调用（例如传入 `scene.items()` 的元素），所以保留
        完整检查。
        """
        if not (hasattr(item, 'save_id') or getattr(item, '_asset_source', None)):
            return False
        if cls.is_annotation(item):
            return False
        if cls.is_deleted(item):
            return False
        return True

    # ── 枚举 ─────────────────────────────────────────────────

    def asset_items(self):
        # Metadata queries do not need the scene's expensive stacking sort.
        # Native items and virtual records share the existing asset iterator.
        iterate = getattr(self.scene, 'iter_assets', self.scene.items)
        for item in iterate():
            if hasattr(item, 'save_id') or getattr(item, '_asset_source', None):
                yield item

    def active_items(self) -> Iterator:
        """全项目活跃素材生成器。

        跨所有画布和嵌套文件夹，不随当前画布切换而变为局部数量。
        """
        deleted_pages = self._deleted_page_ids()
        for item in self.asset_items():
            if self.is_active(item) and getattr(item, '_canvas_id', None) not in deleted_pages:
                yield item

    def deleted_items(self) -> Iterator:
        """全项目软删除素材生成器（回收站里的图片）。"""
        for item in self.asset_items():
            if (hasattr(item, 'save_id') or getattr(item, '_asset_source', None)) and not self.is_annotation(item) \
                    and self.is_deleted(item):
                yield item

    # ── 计数 ─────────────────────────────────────────────────

    def _catalog_scope(self):
        """可用且 ready 的只读目录范围，否则 ``None``。

        目录 ready 时，计数与检索一律走目录（T1）：目录是只读基线，本次
        会话的 dirty 覆盖由 `_catalog_count_snapshot` / `_directory_rows` 的
        overlay 语义压过去——已加载/驻留的对象压过磁盘行，新建未保存的对象
        单独计入，两边的判定共用同一个 `_matches`，口径不会漂移。目录不可用
        或抛异常时一律回退 scene 内存，绝不拿目录的「准备中」冒充「0」。

        C3：每次判定都记一条 `catalog_scope_hit` 或 `catalog_scope_miss`
        （miss 带 reason）。计数入口见模块级 `catalog_scope_counters()`。
        """
        partial = bool(getattr(self.scene, '_partial_project_source', None))
        provider = getattr(self, '_scope_provider', None)
        if provider is None:
            self._scope_miss('no_scope_provider', partial=partial)
            return None
        try:
            scope = provider()
        except Exception:
            logger.exception('scope_provider failed; whole-project counts unavailable')
            self._scope_miss('provider_error', partial=partial)
            return None
        if scope is None:
            self._scope_miss('no_scope', partial=partial)
            return None
        if not getattr(scope, 'ready', False):
            self._scope_miss('not_ready', partial=partial)
            return None
        self._scope_hit(scope, partial=partial)
        return scope

    @staticmethod
    def _scope_hit(scope, *, partial=False):
        _record_scope_event('catalog_scope_hit', partial=partial,
                            project=_scope_project_name(scope))

    @staticmethod
    def _scope_miss(reason, *, partial=False):
        _record_scope_event('catalog_scope_miss', reason=reason, partial=partial)

    def _catalog_count_snapshot(self, scope, *, trash_assets_only=False):
        """Disk aggregate minus covered rows plus current canonical edits.

        Loaded and parked objects replace disk rows, never count twice.
        Unknown canvas IDs belong to unassigned; trashed pages hide children
        consistently in every active counter.
        """
        try:
            groups = scope.grouped_counts()
            live = list(self.asset_items())
            session = getattr(self.scene, '_partial_project_session', None)
            residency = getattr(session, '_residency', None)
            overlay, new, seen = {}, [], set()
            for item in live + list(getattr(residency, 'parked', {}).values()):
                if id(item) in seen:
                    continue
                seen.add(id(item))
                key = getattr(item, 'save_id', None)
                if key is None:
                    key = getattr(item, 'original_save_id', None)
                if key is None:
                    new.append(item)
                else:
                    overlay[key] = item
            disk, ids = [], list(overlay)
            covered = self._covered_groups(scope, ids, groups)
            if covered is None:
                # 能力缺失（scope 还没有批量摘要接口）：退回逐条读行，
                # 口径与下面的桶计数**逐项相同**。
                for start in range(0, len(ids), 1024):
                    disk.extend(scope.summaries(ids[start:start+1024]))
            result = dict(total=0, unclassified=0, untagged=0, trash=0,
                          visible=0, canvases={})
            known, deleted_pages = self._known_canvas_ids(), self._deleted_page_ids()

            all_deleted = 0

            def apply(cid, deleted, untagged, amount):
                nonlocal all_deleted
                if deleted:
                    all_deleted += amount
                if cid in deleted_pages:
                    return
                if deleted:
                    result['trash'] += amount
                    return
                effective = cid if cid == 'default-canvas' or cid in known else UNASSIGNED_CANVAS
                result['total'] += amount
                result['untagged'] += amount * bool(untagged)
                result['unclassified'] += amount * (effective in ('default-canvas', UNASSIGNED_CANVAS))
                result['canvases'][effective] = result['canvases'].get(effective, 0) + amount

            for cid, deleted, untagged, amount in groups:
                apply(cid, deleted, untagged, amount)
            if covered is None:
                for row in disk:
                    if row['kind'] not in ANNOTATION_TYPES:
                        names = json.loads(row['tags'] or '[]')
                        apply(row['canvas_id'], row['deleted_at'] is not None,
                              not any(str(name).strip() for name in names), -1)
            else:
                # 一次往返拿到的桶计数：逐桶减掉"被本次会话覆盖"的那些磁盘行。
                # `apply` 对 amount 是**线性**的（每一支都只做 `+= amount`），
                # 所以"按桶减 total"与"逐行减 1"完全等价——两条路径口径不会漂。
                for cid, deleted, untagged, amount in covered:
                    apply(cid, deleted, untagged, -amount)
            for item in list(overlay.values()) + new:
                if not self.is_annotation(item):
                    apply(getattr(item, '_canvas_id', None), self.is_deleted(item),
                          not self._has_valid_tags(item), 1)
            if trash_assets_only:
                return all_deleted
            result['canvases'] = {cid: count for cid, count in result['canvases'].items() if count}
            result['trash'] += len(self._top_level_trashed_nodes())
            result['visible'] = sum(1 for item in live if self.is_active(item)
                                    and getattr(item, '_canvas_id', None) not in deleted_pages
                                    and item.isVisible())
            return result
        except Exception:
            logger.exception('catalog count unavailable; whole-project counters are unknown')
            return None

    def tag_distribution(self):
        """Active whole-project tag sets plus live/parked unsaved replacements.

        None means unavailable, never a resident-only whole-project answer.
        Return tag sets with multiplicities, retaining intersection semantics.
        """
        from prism.search_candidates import catalog_tag_names
        scope = self._catalog_scope()
        if scope is None and getattr(self.scene, '_partial_project_source', None):
            return None
        deleted_pages = self._deleted_page_ids()
        result = collections.Counter()

        def apply(names, amount):
            key = tuple(sorted(set(str(name).strip() for name in names if str(name).strip())))
            if key:
                result[key] += amount

        overlay, fresh = self._session_overlay()
        try:
            if scope is not None:
                for cid, raw, amount in scope.tag_groups():
                    if cid not in deleted_pages:
                        apply(catalog_tag_names(raw), amount)
                ids = tuple(overlay)
                # Disk summaries do not change when only live metadata changes.
                key = (scope.source_state(), ids)
                previous = getattr(self, '_tag_overlay_rows', None)
                if previous is None or previous[0] != key:
                    disk = []
                    for start in range(0, len(ids), 1024):
                        disk.extend(scope.summaries(ids[start:start+1024]))
                    previous = self._tag_overlay_rows = (key, tuple(disk))
                for row in previous[1]:
                    if (row['kind'] not in ANNOTATION_TYPES
                            and row['deleted_at'] is None
                            and row['canvas_id'] not in deleted_pages):
                        apply(catalog_tag_names(row['tags']), -1)
            for item in list(overlay.values()) + fresh:
                if self.is_active(item) and getattr(item, '_canvas_id', None) not in deleted_pages:
                    apply(getattr(item, '_tags', []) or [], 1)
            return {names: count for names, count in result.items() if count > 0}
        except Exception:
            logger.exception('Whole-project tag distribution unavailable')
            return None

    def _covered_groups(self, scope, ids, groups):
        """被本次会话覆盖的那批磁盘行的**计数桶**；能力缺失时返回 ``None``。

        计数侧要回答的是"这批 id 里有多少行在哪个画布 / 删没删 / 有没有
        标签"——用**计数器**回答，不要一行一个素材地读回来。

        两条**等价**的路径，按"哪边更便宜"选（选哪条只影响耗时，不影响
        数字，两边的桶口径完全一致）：

        * `summaries_buckets(ids)`：读**被覆盖**的那些行，成本 ∝ 覆盖数。
        * `summaries_uncovered(ids)`：读**没被覆盖**的那些行，成本 ∝ 剩余
          数。全量打开时覆盖 = 全库 ⇒ 这一边**几乎是空的** ⇒ 一次范围扫描
          就完事（这正是侧栏计数在"打开整个工程"时的主要成本）。
          它的桶要从聚合数里减出来（聚合 − 未覆盖 = 被覆盖），所以**总数
          仍然来自聚合数**，口径与另一条路径完全一样。

        两条都没有：返回 ``None``，调用方退回每 1024 个 id 一次
        `summaries()` 的旧路径——**口径不变，只差往返次数**。
        """
        if not ids:
            return ()
        if self._uncovered_is_cheaper(scope, len(ids)):
            uncovered = self._uncovered_groups(scope, ids)
            if uncovered is not None:
                return self._subtract_buckets(groups, uncovered)
        buckets = getattr(scope, 'summaries_buckets', None)
        if callable(buckets):
            try:
                return tuple(buckets(ids))
            except Exception:
                logger.exception('scope.summaries_buckets failed; '
                                 'falling back to per-row summaries')
                return None
        grouped = getattr(scope, 'summaries_grouped', None)
        if callable(grouped):
            try:
                return self._group_summary_rows(grouped(ids))
            except Exception:
                logger.exception('scope.summaries_grouped failed; '
                                 'falling back to per-row summaries')
                return None
        return None

    @staticmethod
    def _uncovered_is_cheaper(scope, covered_count):
        """覆盖得多不多到值得改读"没被覆盖"的那一边。

        被覆盖的那一边成本 ∝ 覆盖数；没被覆盖的那一边成本 ∝ 全库 − 覆盖数。
        覆盖过半时改读另一边更便宜。判断不出来（没有 `total()`）时保守
        走原来那条。
        """
        total = getattr(scope, 'total', None)
        if not callable(total):
            return False
        try:
            whole = total()
        except Exception:
            return False
        if not isinstance(whole, int) or whole <= 0:
            return False
        return covered_count * 2 > whole

    @staticmethod
    def _uncovered_groups(scope, ids):
        """没被会话覆盖的那批磁盘行的**计数桶**；能力缺失时返回 ``None``。

        scope 层没转发这个接口时，退到目录层自己的
        `ProjectCatalogIndex.summaries_uncovered`（A-3）——只读调用，
        不绕过任何写路径。
        """
        uncovered = getattr(scope, 'summaries_uncovered', None)
        if not callable(uncovered):
            index = getattr(scope, 'index', None)
            uncovered = getattr(index, 'summaries_uncovered', None)
        if not callable(uncovered):
            return None
        try:
            return tuple(uncovered(ids))
        except Exception:
            logger.exception('scope.summaries_uncovered failed; '
                             'falling back to per-row summaries')
            return None

    @staticmethod
    def _subtract_buckets(groups, uncovered):
        """聚合桶 − 未覆盖桶 = 被覆盖的桶。

        口径与 `scope.grouped_counts()` 同构：`(canvas_id, deleted,
        untagged, total)`。聚合里没有、但未覆盖里有的桶（预聚合与行不一致
        时才会发生）按负数记，交给 `apply` 时等于把它们**加回来**——总数
        仍然是行口径的真值。
        """
        rest = collections.Counter()
        for canvas, deleted, untagged, total in uncovered:
            rest[(canvas, bool(deleted), bool(untagged))] += int(total)
        seen = set()
        covered = []
        for canvas, deleted, untagged, total in groups:
            key = (canvas, bool(deleted), bool(untagged))
            seen.add(key)
            amount = int(total) - rest.get(key, 0)
            if amount:
                covered.append((canvas, bool(deleted), bool(untagged), amount))
        for key, amount in rest.items():
            if key not in seen and amount:
                covered.append((key[0], key[1], key[2], -amount))
        return tuple(covered)

    @staticmethod
    def _group_summary_rows(rows):
        """批量逐行摘要 → 计数桶（规则与旧逐条路径**逐字相同**）。"""
        totals = collections.Counter()
        for row in rows:
            if row.get('kind') in ANNOTATION_TYPES:
                continue
            names = json.loads(row.get('tags') or '[]')
            untagged = not any(str(name).strip() for name in names)
            key = (row.get('canvas_id'), row.get('deleted_at') is not None,
                   untagged)
            totals[key] += 1
        return tuple((canvas, deleted, untagged, total)
                     for (canvas, deleted, untagged), total in totals.items())

    def count_snapshot(self):
        """全项目计数快照。

        目录 ready 时走目录口径（T1）：`scope` 已经解析过，这里不再重复
        判定，也不会重复记 C3 事件。
        """
        return self._counts_with_scope(self._catalog_scope())

    def _counts_with_scope(self, scope):
        """`scope` 已解析时的计数快照。

        * 目录可用 → 目录口径（disk aggregate − 覆盖行 + 当前编辑）。
        * 目录不可用且是按页打开 → 未知（None）。已加载的一页不是全项目
          答案，调用方渲染成 "Preparing…"，绝不当成 0。
        * 目录不可用且是全量打开 → scene 内存口径。
        """
        if scope is not None:
            snapshot = self._catalog_count_snapshot(scope)
            if snapshot is not None:
                return snapshot
        if getattr(self.scene, '_partial_project_source', None):
            # A loaded page is not a whole-project count. Preserve unknown until
            # the matching directory becomes ready; callers display Preparing.
            return dict(total=None, unclassified=None, untagged=None, trash=None,
                        visible=sum(1 for item in self.active_items() if item.isVisible()),
                        canvases=None, complete=False)
        return self._scene_count_snapshot()

    def _scene_count_snapshot(self):
        """全量打开、目录不可用时的 scene 内存口径。"""
        result = dict(total=0, unclassified=0, untagged=0, trash=0, visible=0, canvases={})
        known = self._known_canvas_ids()
        deleted_pages = self._deleted_page_ids()
        for item in self.asset_items():
            if self.is_annotation(item):
                continue
            if self.is_deleted(item):
                if getattr(item, '_canvas_id', None) not in deleted_pages:
                    result['trash'] += 1
                continue
            if getattr(item, '_canvas_id', None) in deleted_pages:
                continue
            result['total'] += 1
            result['visible'] += item.isVisible()
            result['untagged'] += not self._has_valid_tags(item)
            cid = self._effective_canvas_id(item, known)
            result['unclassified'] += cid in ('default-canvas', UNASSIGNED_CANVAS)
            result['canvases'][cid] = result['canvases'].get(cid, 0)+1
        result['trash'] += len(self._top_level_trashed_nodes())
        return result

    def total_count(self) -> int:
        """活跃素材总数（方案 §5.1）。"""
        scope = self._catalog_scope()
        if scope is not None:
            snapshot = self._catalog_count_snapshot(scope)
            if snapshot is not None:
                return snapshot['total']
        if getattr(self.scene, '_partial_project_source', None):
            # 目录不可用：已加载的一页不是全项目总数，必须是未知而不是 0。
            return None
        return sum(1 for _ in self.active_items())

    def unclassified_count(self):
        scope = self._catalog_scope()
        if scope is not None:
            snapshot = self._catalog_count_snapshot(scope)
            if snapshot is not None:
                return snapshot['unclassified']
        if getattr(self.scene, '_partial_project_source', None):
            return None
        known = self._known_canvas_ids()
        return sum(self.is_unclassified(i, known) for i in self.active_items())

    def is_unclassified(self, item, known=None):
        return self._effective_canvas_id(item, known) in ('default-canvas', UNASSIGNED_CANVAS)

    def untagged_count(self) -> int:
        """无有效素材标签的活跃素材数。

        "有效标签"的判定：`_tags` 列表非空，且至少有一个非空白字符串。
        页面标签不能自动代替图片标签（方案 §5.1）。
        """
        scope = self._catalog_scope()
        if scope is not None:
            snapshot = self._catalog_count_snapshot(scope)
            if snapshot is not None:
                return snapshot['untagged']
        if getattr(self.scene, '_partial_project_source', None):
            return None
        count = 0
        for item in self.active_items():
            if not self._has_valid_tags(item):
                count += 1
        return count

    @staticmethod
    def _has_valid_tags(item) -> bool:
        tags = getattr(item, '_tags', None) or []
        return any(str(t).strip() for t in tags)

    def canvas_counts(self) -> dict:
        """`{canvas_id: count}` 字典，含 `UNASSIGNED_CANVAS` 桶。

        资源树父文件夹的递归汇总由 `ResourceTreeModel.set_asset_counts()`
        基于这个字典做（它已经实现了文件夹层级的 roll-up，见
        test_eagle_sidebars.test_tree_counts_roll_up_nested_canvas_folders）。
        """
        scope = self._catalog_scope()
        if scope is not None:
            snapshot = self._catalog_count_snapshot(scope)
            if snapshot is not None:
                return snapshot['canvases']
        if getattr(self.scene, '_partial_project_source', None):
            return None
        counts: dict = {}
        known = self._known_canvas_ids()
        for item in self.active_items():
            cid = self._effective_canvas_id(item, known)
            counts[cid] = counts.get(cid, 0) + 1
        return counts

    def trash_top_level_count(self) -> int:
        """回收站顶层删除记录数（方案 §7.1 口径）。

        - 独立删除的图片：每张记 1
        - 删除整个页面/文件夹：记 1 条（内部图片数在行内另显示，不累加
          到徽标）

        实现上：
        - 素材级：统计软删除图片中"不属于任何已删除页面/文件夹"的数量。
          属于已删除页面的图片算在页面那条记录里，不重复计。
        - 页面级：`resource_service.trashed_nodes()` 里 `parentId` 为 None
          或父节点未删除的节点数（顶层记录）。
        """
        scope = self._catalog_scope()
        if scope is not None:
            snapshot = self._catalog_count_snapshot(scope)
            if snapshot is not None:
                return snapshot['trash']
        if getattr(self.scene, '_partial_project_source', None):
            return None
        deleted_page_ids = self._deleted_page_ids()
        asset_count = 0
        for item in self.deleted_items():
            cid = getattr(item, '_canvas_id', None)
            if cid in deleted_page_ids:
                # 这张图属于一个被整体删除的页面/文件夹，算在那条记录里
                continue
            asset_count += 1
        page_count = len(self._top_level_trashed_nodes())
        return asset_count + page_count

    def trash_asset_count(self) -> int:
        """回收站里素材总数（含被整体删除的页面内部的素材）。

        用于回收站面板"素材"页签的条目数显示，与 `trash_top_level_count`
        的徽标口径不同。
        """
        scope = self._catalog_scope()
        if scope is not None:
            value = self._catalog_count_snapshot(scope, trash_assets_only=True)
            if value is not None:
                return value
        if getattr(self.scene, '_partial_project_source', None):
            return None
        return sum(1 for _ in self.deleted_items())

    def _deleted_page_ids(self) -> set:
        """当前处于软删除状态的页面/文件夹 pageId 集合。"""
        if self.resource_service is None:
            return set()
        ids = set()
        for node in self.resource_service.trashed_nodes():
            page_id = node.get('pageId')
            if page_id:
                ids.add(page_id)
            # 文件夹本身没有 pageId，但它下面的页面会被一起软删除。
            # `trashed_nodes()` 已经返回所有软删除节点（含子节点），所以
            # 这里不需要额外递归。
        return ids

    def _top_level_trashed_nodes(self) -> list:
        """顶层删除记录：parentId 为 None 或父节点未删除的软删除节点。"""
        if self.resource_service is None:
            return []
        trashed = list(self.resource_service.trashed_nodes())
        trashed_ids = {n['id'] for n in trashed}
        top = []
        for node in trashed:
            parent_id = node.get('parentId')
            if not parent_id or parent_id not in trashed_ids:
                top.append(node)
        return top

    # ── 查询 ─────────────────────────────────────────────────

    #: 目录分支一次读多少行。`ProjectOpenScope.iter_query` 自己按这个大小做
    #: keyset 分页，所以十万级项目也不会一次性把目录读进内存。
    DIRECTORY_PAGE_LIMIT = 512

    def query(self, text: str = '', tag_expression: str = '',
              untagged_only: bool = False, min_rating: int = 0,
              canvas_scope: Optional[Iterable] = None,
              include_deleted: bool = False,
              limit: Optional[int] = None, *, cancel=None,
              max_rows: Optional[int] = None) -> list:
        """全项目素材查询，返回 `AssetResult` 列表。

        两个数据源，一套口径
        --------------------

        * 目录 ready：走 `_directory_rows`——只读目录 + 本次会话的
          live/parked 覆盖层，所以侧栏计数和结果列表说的是同一批素材。
          目录是只读基线，会话里的编辑由覆盖层压过去（口径见
          `_catalog_count_snapshot`，两处不能漂移）。
        * 目录不可用：走 `_scene_rows`——场景内存就是全项目。
        * 目录不可用**且**是按页打开：抛 `CatalogNotReady`。已加载的一页
          **不是**全项目答案，把它当成"没有素材"渲染就是静默说谎（缺陷 B）。

        limit 前置（为什么能前置、为什么不会漏结果）
        -------------------------------------------

        排序键是 `(画布路径, 标题, tie-break)`。画布路径来自资源树，目录表
        里没有这一列，所以**排序**没法下推；能下推的是**过滤**：目录索引层
        支持 `canvas_ids / untagged_only / min_rating / text`，而这四个谓词
        在目录侧都是内存侧 `_matches` 谓词的**子集**（逐条论证见
        `_pushdown_filters`）。所以顺序是"先下推过滤 → 再逐行用 `_matches`
        判定 → 最后只对活下来的行排序和截断"：

        * 被下推过滤掉的行，一定也是内存判定不匹配的行 → 不会漏结果。
        * 标签表达式**不能**下推（目录表只存原始 tags JSON，没有层级/同义词
          列），所以带标签表达式的查询仍然逐行判定，但其余过滤照样下推。
        * `limit` 不为 None 时只保留最小的 limit 个，而且**只有活下来的那些
          才真正造 `AssetResult` 和显示代理**（代理是 Qt 对象，十万行里
          命中几百行时不必建十万个）。

        `max_rows` 的语义（总量上限，**不前推给目录读取**）
        ----------------------------------------------

        `max_rows` 是**结果集**的总量上限，与 scope 层
        `ProjectOpenScope.iter_query(max_rows=…)` / 消费层
        `CatalogRows(max_rows=…)` 同一个名字，但**本层不把它前推给目录
        读取**：

        * 目录读取（`iter_query`）是 **按 `id` 升序** 的 keyset 分页；
          本层结果的排序键是 **`(画布路径, 标题, tie-break)`**。两者不同源
          ⇒ 前推拿到的是"按 id 序的前 N 行"，不是"按排序键的前 N 行"，
          **那是静默返回错的结果，比慢更糟**。
        * 所以 `max_rows` 只作**物化阶段**的总量上限：按排序键取前
          `max_rows` 个。**读取量仍由下推决定**（`_pushdown_filters`）。
        * 验收判据：`max_rows >= 真实命中数` 时，返回结果集与不传
          `max_rows` 时**逐项相同**（见 `tests/test_project_catalog_max_rows.py`）。

        参数
        ----
        text:
            普通文字搜索。多个词都要命中（空格 = 与）。匹配 `_title` /
            `filename` / `_notes` / `_tags`。大小写不敏感。
        tag_expression:
            标签查询语法（`tag:xxx` / `xxx:*` / `a | b` / `a -b` / `-*`）。
            委托 `prism.tags.parse_query` 解析；目录分支逐行用同一个
            表达式判定，不用 SQL `LIKE` 近似。
        untagged_only:
            只返回无有效标签的素材。
        min_rating:
            最低评分（0 = 不限）。
        canvas_scope:
            限定画布 ID 集合。None = 全项目。`UNASSIGNED_CANVAS` 可作为
            集合成员，表示包含无归属素材。**裸字符串 = 一个画布 id**
            （不是字符集合）——口径与 scope 层、索引层一致：字符串被拆成
            逐字符元组会得到"0 行且不报错"的静默空答案。条目不是字符串
            （`None` 除外）一律 `TypeError`。
        include_deleted:
            是否包含软删除素材。默认 False（活跃素材）。True 时**只**返回
            删除项（回收站视图）。
        limit:
            最多返回多少条。None = 不限。先按 `(画布路径, 标题, tie-break)`
            排序再截断，所以同一个查询每次给出的前缀是同一批。
        cancel:
            目录分支的可取消回调（`ProjectOpenScope.iter_query` 的契约）。
        max_rows:
            **总量上限**：结果集最多多少条。`None` = 不限。必须是 ≥1 的
            整数（与 scope 层 `_as_max_rows` 同契约：非整数 `TypeError`，
            小于 1 `ValueError`）。与 `limit` 作用在**同一个排序键**上，
            最终条数 = 两者中更小的那个。

        返回
        ----
        `list[AssetResult]`，按 (canvas 路径, 标题, 保存 id) 排序，便于分组
        显示。未加载的素材行 `item` 是只读显示代理，见 `AssetResult`。
        """
        from prism import tags as tags_module

        cap = _normalize_max_rows(max_rows)
        scope = self._catalog_scope()
        if scope is None and getattr(self.scene, '_partial_project_source', None):
            raise CatalogNotReady(
                'Whole-project directory is unavailable; the loaded page is not '
                'a whole-project result set')
        canvas_ids = _as_canvas_ids(canvas_scope)
        predicates = _Predicates(
            words=tuple(word.casefold() for word in (text or '').split() if word),
            tags=tags_module.parse_query(tag_expression) if tag_expression else None,
            system=tags_module.tag_system(self.scene),
            canvases=set(canvas_ids) if canvas_ids is not None else None,
            untagged=bool(untagged_only),
            rating=int(min_rating or 0),
            deleted=bool(include_deleted))
        known, trashed = self._known_canvas_ids(), self._deleted_page_ids()
        # pkg3: 范围身份在这里算一次，因为这里的谓词就是最终判定。
        identity, canvases, _known = _pkg3_scope_identity(
            text=text, tag_expression=tag_expression,
            untagged_only=untagged_only, canvas_scope=canvas_ids, known=known)
        fallback_reason = None
        disk_rows = PKG3_UNKNOWN
        source = 'scene'
        if scope is None:
            entries = self._scene_rows(predicates, known, trashed)
        else:
            source = 'catalog'
            pushdown = self._pushdown_filters(predicates, known, scope)
            entries, disk_rows = self._directory_rows(scope, predicates, known,
                                                      trashed, cancel, pushdown,
                                                      cap)
            if disk_rows == 0 and 'canvas_ids' in pushdown:
                # 下推为空：要么这个画布本来就是空的，要么目录的画布坐标系
                # 和场景不是一回事（目录 canvas_id 与 page id 不同源）。
                # 后者绝不能靠"下推为空"就回答"没有素材"——内存分支兜底，
                # 并且把这件事记成可观测事件。
                _record_scope_event('catalog_pushdown_fallback',
                                    reason='canvas_scope_unmatched')
                fallback_reason = 'canvas_scope_unmatched'
                source = 'catalog+scene'
                entries = self._merge_entries(
                    entries, self._scene_rows(predicates, known, trashed))
        # pkg3: 一次 aggregate 阶段的范围快照。字段全部取自上面已经算好的值
        # （`disk_rows`、`entries`、`known`），**没有**为日志多查一次目录。
        telemetry = _pkg3_telemetry(scope, getattr(self, 'scene', None))
        with pkg3_span(SCOPE_SNAPSHOT_EVENT, stage='aggregate',
                       **dict(identity, **{'scope_pages': canvases},
                              **telemetry)) as snapshot_span:
            results = self._materialize(entries, limit, cap)
            snapshot_span.set(
                disk_candidates=disk_rows,
                logical_result_count=len(results),
                deduped_count=len(entries) - len(results)
                if len(results) <= len(entries) else PKG3_UNKNOWN,
                filter_hits=len(entries),
                source_path=source,
                fallback_reason=fallback_reason,
                known_canvases=len(known) if known is not None else PKG3_UNKNOWN,
                project=telemetry.get('project'))
            # `build_scope_snapshot` always returns a dict that already carries
            # a `stage` key, and the inner mapping sets it to 'aggregate'.
            # Passing `stage=` again here raised
            # "got multiple values for keyword argument 'stage'".
            record_pkg3(SCOPE_SNAPSHOT_EVENT,
                        **build_scope_snapshot(
                            canvases, **dict(
                                identity,
                                **dict(telemetry,
                                       stage='aggregate',
                                       disk_candidates=disk_rows,
                                       logical_result_count=len(results),
                                       filter_hits=len(entries),
                                       deduped_count=(len(entries) - len(results)
                                                      if len(results) <= len(entries)
                                                      else PKG3_UNKNOWN),
                                       fallback_reason=fallback_reason,
                                       known_canvases=(
                                           len(known) if known is not None
                                           else PKG3_UNKNOWN),
                                       source_path=source))))
        return results

    # ── 下推（limit 前置的前提） ────────────────────────────────

    def _pushdown_filters(self, predicates, known, scope):
        """能**证明不漏结果**的下推过滤器。

        目录索引层（`project_catalog_index._predicate`）支持
        `canvas_ids / untagged_only / min_rating / text`。下推的前提是：
        目录侧的谓词必须是内存侧 `_matches` 谓词的**子集**——被下推过滤掉
        的行，内存判定也一定不匹配，所以不会漏结果。逐条论证：

        * `untagged_only`：目录的 `prism_untagged(tags)` 与内存的
          `_has_valid_tags()` 读同一份 tags JSON、同一判定（空串不算标签），
          等价。
        * `min_rating`：目录的 `rating>=?` 与内存的 `rating < r → 不匹配`
          等价（目录 rating 与 `CatalogMatchRecord._rating` 同源同 clamp）。
        * `text`：目录搜 title/notes/filename/tags 四列；内存侧
          `_text_haystack()` 对**目录行**也正好是这四列——
          `asset_search_text()` 在 `CatalogMatchRecord` 上取不到
          `_source_url` / `_categories` / `toPlainText`（都是空串），而词
          不含空格所以不会跨字段匹配。等价。
        * `canvas_ids`：**有条件**，见 `_pushdown_canvas_ids`。
        * 标签表达式（`predicates.tags`）：**不能**下推——目录表只存原始
          tags JSON，没有层级/同义词列，`tags.parse_query` 的完整语义留在
          内存侧逐行判定。
        """
        filters = {}
        if predicates.untagged:
            filters['untagged_only'] = True
        if predicates.rating:
            filters['min_rating'] = int(predicates.rating)
        if predicates.words:
            filters['text'] = ' '.join(predicates.words)
        if predicates.canvases is not None:
            canvas_ids = self._pushdown_canvas_ids(predicates.canvases, known, scope)
            if canvas_ids is not None:
                filters['canvas_ids'] = canvas_ids
        return filters

    def _pushdown_canvas_ids(self, canvases, known, scope):
        """能安全下推的画布 id 元组；不能安全下推时返回 ``None``。

        * 请求里含 `UNASSIGNED_CANVAS` → 不能下推：内存侧把"目录里没有的
          画布"也算进无归属桶，而目录侧只能按原始 `canvas_id` 过滤，下推会
          漏掉那部分行。
        * 目录与场景的画布坐标系不一致 → 不能下推（`_canvas_ids_aligned`）。
        * **字符串绝不能直接传下去**：`project_catalog_index._predicate()` 的
          `tuple(canvas_ids)` 会把 `'stress-canvas-0000'` 拆成逐字符元组 →
          `IN ('s','t','r',…)` → 0 行而且不报错。`query()` 上游已经用
          `_as_canvas_ids()` 归一化过了（裸字符串 = 一个画布 id）；这里再兜
          一层，防止将来有人直接调这个私有方法。
        """
        if isinstance(canvases, (str, bytes)):
            canvases = (canvases,)
        ids = tuple(canvases)
        if UNASSIGNED_CANVAS in ids:
            return None
        if not self._canvas_ids_aligned(scope, known):
            return None
        # 内存侧的有效画布 id 只可能是 known ∪ {'default-canvas'} ∪
        # {UNASSIGNED_CANVAS}；请求里的未知 id 匹配不到任何行，剔掉它
        # 只是缩小下推范围，不会改变结果集。
        return tuple(cid for cid in ids
                     if cid == 'default-canvas' or cid in known)

    def _canvas_ids_aligned(self, scope, known):
        """目录里的 `canvas_id` 与场景的 page id 是否同一坐标系。

        目录的 canvas_id 来自素材 JSON 的 `canvasId`，场景的 page id 来自
        workspace_pages，两者本该同源（真实工程实测 1000/1000 完全重合）。
        这里做一个便宜的交叉检查：目录里出现过的画布（去掉隐式的
        'default-canvas'）和场景已知画布没有交集时，判定为坐标系不一致，
        这时**不下推** canvas_ids。
        """
        try:
            groups = scope.grouped_counts()
        except Exception:
            # 读不出来就不阻断下推：`_matches` 仍然是最终判定。
            return True
        catalog_ids = {cid for cid, *_ in groups if cid}
        catalog_ids.discard('default-canvas')
        if not catalog_ids:
            return True
        return bool(catalog_ids & known)

    def _pushdown_max_rows(self, scope, max_rows):
        """能不能把总量上限前推给目录读取？**当前口径：不能。**

        目录读取（`ProjectOpenScope.iter_query`）是 **按 `id` 升序** 的
        keyset 分页；本层结果的排序键是 **`(画布路径, 标题, tie-break)`**。
        两者**不同源** ⇒ 前推 `max_rows` 拿到的是"按 id 序的前 N 行"，不是
        "按排序键的前 N 行"——**静默返回错的结果，比慢更糟**。

        所以这里**不前推**：`max_rows` 只作**物化阶段**的总量上限（见
        `_materialize`），**读取量仍由下推决定**（`_pushdown_filters`）。
        可证明等价的前推条件（目录读取顺序 == 结果排序键）在当前排序口径
        下是**空集**：画布路径来自资源树，目录表里没有这一列。

        **能力探测仍然保留**：A-2 的 `iter_query(max_rows=…)` 落地与否，
        本层都不崩、不改行为；探测结果与"不前推"的理由都记成可观测事件，
        便于验收组核对"没有偷偷前推"。
        """
        if scope is None or max_rows is None:
            return None
        if not _scope_supports_max_rows(scope):
            # A-2 未落地：不前推，行为与本轮上线前完全一致。
            return None
        _record_scope_event('catalog_max_rows_not_pushed',
                            reason='sort_key_not_id_order', max_rows=max_rows)
        return None

    # ── 结果条目 ───────────────────────────────────────────────

    def _scene_rows(self, predicates, known, trashed):
        """`(排序键, 构造函数, 去重键)` 列表：目录不可用时只有场景内存。"""
        paths, rows = {}, []
        for item in self.asset_items():
            self._scene_entry(rows, item, paths, predicates, known, trashed)
        return rows

    def _scene_entry(self, rows, item, paths, predicates, known, trashed):
        """一个 live/parked 对象 → 最多一行结果。

        先算排序键、最后才造 `AssetResult`：真正进不了结果的那一行不必
        付造对象的代价。
        """
        if self.is_annotation(item):
            return
        deleted = self.is_deleted(item)
        item_tags = list(getattr(item, '_tags', []) or [])
        rating = int(getattr(item, '_rating', 0) or 0)
        cid = self._effective_canvas_id(item, known)
        if not self._matches(item, item_tags, rating, cid, deleted, predicates, trashed):
            return
        title = getattr(item, '_title', '') or getattr(item, 'filename', '') or '未命名'
        rows.append((self._row_sort_key(cid, title, paths) + (_order_key(item),),
                     self._result_builder(item, cid, item_tags, rating, deleted, paths),
                     ('live', id(item))))

    def _directory_rows(self, scope, predicates, known, trashed, cancel=None,
                        pushdown=None, max_rows=None):
        """磁盘目录 + live/parked 覆盖层，同一个 save_id 只出一行。

        合并规则（和 `_catalog_count_snapshot` 完全一致，两处口径不能漂移）：

        * 已加载/驻留的对象**压过**磁盘行。用户没保存的标签、评分、标题、
          归属都以内存为准，旧目录行整个丢掉——哪怕内存版本不满足过滤条件，
          也不能让旧行"复活"回来。
        * 同一 `save_id` 只出一行：命中磁盘行时弹出覆盖层条目，被弹出的不再
          从覆盖层末尾重复产出。
        * 目录里没有对应行的会话对象（新建未保存、恢复出来的）正常产出。
        * 逐行用 `_matches` 判定，语义与内存分支逐字相同。

        返回 `(结果条目, 读到的磁盘行数)`：行数用来判断"下推是否为空"。
        """
        overlay, fresh = self._session_overlay()
        paths, rows = {}, []
        signature = self._directory_signature(scope)
        disk_rows = 0
        kwargs = dict(pushdown or {})
        forward = self._pushdown_max_rows(scope, max_rows)
        if forward is not None:
            kwargs['max_rows'] = forward
        for row in scope.iter_query(include_deleted=predicates.deleted,
                                    limit=self.DIRECTORY_PAGE_LIMIT, cancel=cancel,
                                    **kwargs):
            disk_rows += 1
            key = str(row['id'])
            loaded = overlay.pop(key, None)
            if loaded is not None:
                self._scene_entry(rows, loaded, paths, predicates, known, trashed)
                continue
            record = CatalogMatchRecord(row)
            item_tags = list(record._tags)
            cid = self._effective_canvas_id(record, known)
            deleted = record._deleted_at is not None
            if not self._matches(record, item_tags, record._rating, cid, deleted,
                                 predicates, trashed):
                continue
            # 显示代理是 Qt 对象：等这一行真的活下来再分配。
            # 目录与场景路径同口径：显式 title 优先，其次 filename。
            title = row.get('title') or row.get('filename') or '未命名'
            rows.append((self._row_sort_key(cid, title, paths) + (key,),
                         self._directory_result_builder(row, scope, signature, cid,
                                                        item_tags, record._rating,
                                                        deleted, paths),
                         ('row', key)))
        for item in list(overlay.values()) + fresh:
            self._scene_entry(rows, item, paths, predicates, known, trashed)
        return rows, disk_rows

    def _row_sort_key(self, cid, title, paths):
        """`(画布路径, 标题)`：与旧实现的排序键逐字一致。"""
        if cid not in paths:
            paths[cid] = self.canvas_path(cid)
        return (paths[cid], str(title).casefold())

    def _result_builder(self, item, cid, item_tags, rating, deleted, paths):
        """延迟造 `AssetResult` 的闭包。"""
        return lambda: self._result(item, cid, item_tags, rating, deleted, paths)

    def _directory_result_builder(self, row, scope, signature, cid, item_tags,
                                  rating, deleted, paths):
        """延迟造目录行的 `AssetResult`（代理也一起延迟）。"""
        def build():
            proxy = self._directory_item(row, scope, signature)
            return self._result(proxy, cid, item_tags, rating, deleted, paths)
        return build

    @staticmethod
    def _merge_entries(entries, extra):
        """按去重键合并两组结果条目，已有的一条优先。"""
        if not extra:
            return entries
        seen = {entry[2] for entry in entries}
        merged = list(entries)
        for entry in extra:
            if entry[2] not in seen:
                seen.add(entry[2])
                merged.append(entry)
        return merged

    @staticmethod
    def _materialize(entries, limit, max_rows=None):
        """排序 + 截断 + 造结果对象。

        `entries` 是 `((画布路径, 标题, tie-break), 构造函数, 去重键)`。
        `limit` 不为 None 时只保留最小的 limit 个（`heapq.nsmallest` 与
        `sorted(...)[:limit]` 等价，且同样稳定），而且只有留下来的那些才
        真正造 `AssetResult`。

        `max_rows` 是**总量上限**，作用在**同一个排序键**上：先按排序键取
        前 `max_rows` 个，再套用 `limit`（负数 = 去掉末尾若干个，沿用旧
        语义）。它**不是**"目录读多少行"——目录读取按 `id` 升序，与排序键
        不同源，前推会拿到错的前 N 行（理由见 `_pushdown_max_rows`）。

        `max_rows >= 真实命中数` 时，这里不做任何截断 ⇒ 结果集与不传
        `max_rows` 时**逐项相同**。
        """
        if max_rows is not None and len(entries) > max_rows:
            entries = heapq.nsmallest(max_rows, entries, key=lambda entry: entry[0])
        if limit is None:
            entries.sort(key=lambda entry: entry[0])
            return [entry[1]() for entry in entries]
        if limit < 0:
            # 保留旧的 `results[:limit]` 语义（负数 = 去掉末尾若干个）。
            entries.sort(key=lambda entry: entry[0])
            return [entry[1]() for entry in entries[:limit]]
        top = heapq.nsmallest(limit, entries, key=lambda entry: entry[0])
        return [entry[1]() for entry in top]

    def _matches(self, item, item_tags, rating, cid, deleted, predicates, trashed) -> bool:
        """live 对象和目录标量记录共用的唯一判定函数。"""
        if deleted and not predicates.deleted:
            return False
        if not deleted and predicates.deleted:
            # include_deleted=True 时只返回删除项（回收站视图）
            return False
        if cid in trashed:
            return False
        if predicates.canvases is not None and cid not in predicates.canvases:
            return False
        if predicates.untagged and self._has_valid_tags(item):
            return False
        if predicates.rating and rating < predicates.rating:
            return False
        if predicates.tags is not None:
            from prism import tags as tags_module
            index = tags_module.name_index(item_tags, predicates.system)
            if not predicates.tags.matches(index):
                return False
        if predicates.words:
            haystack = self._text_haystack(item, item_tags)
            if not all(word in haystack for word in predicates.words):
                return False
        return True

    def _result(self, item, cid, item_tags, rating, deleted, paths) -> AssetResult:
        """`_build_result` + 画布路径缓存（一个画布只拼一次路径）。"""
        if cid not in paths:
            paths[cid] = self.canvas_path(cid)
        return self._build_result(item, cid, item_tags, rating, deleted, paths[cid])

    def _session_overlay(self):
        """`({save_id: item}, [还没有 save_id 的对象])`。

        和 `_catalog_count_snapshot` 的拆分完全相同：计数与结果列表必须对
        "哪一行属于谁" 有同一个答案。
        """
        session = getattr(self.scene, '_partial_project_session', None)
        residency = getattr(session, '_residency', None)
        overlay, fresh, seen = {}, [], set()
        for item in (list(self.asset_items()) +
                     list(getattr(residency, 'parked', {}).values())):
            if id(item) in seen:
                continue
            seen.add(id(item))
            key = getattr(item, 'save_id', None)
            if key is None:
                key = getattr(item, 'original_save_id', None)
            if key is None:
                fresh.append(item)
            else:
                overlay[str(key)] = item
        return overlay, fresh

    def _directory_signature(self, scope):
        """显示代理缓存的身份：同一个工程文件 + 同一份目录字节。"""
        return (str(scope.project_path), str(scope.source_state()))

    def _directory_item(self, row, scope, signature):
        """未加载素材的只读显示代理，按目录主键缓存复用。"""
        cached = getattr(self, '_directory_items', None)
        if cached is None or cached[0] != signature:
            cached = self._directory_items = (signature, {})
        key = str(row['id'])
        item = cached[1].get(key)
        if item is None:
            from prism.catalog_browser import CatalogAssetProxy
            item = cached[1][key] = CatalogAssetProxy(
                row, scope.project_path, self.scene, _NO_DIRECTORY_DISPLAY)
        return item

    @staticmethod
    def _text_haystack(item, item_tags) -> str:
        from prism.project_search import asset_search_text
        return (asset_search_text(item) + ' ' + ' '.join(str(t) for t in item_tags)).casefold()

    def _build_result(self, item, canvas_id, item_tags, rating, deleted, path=None) -> AssetResult:
        title = (getattr(item, '_title', '') or
                 getattr(item, 'filename', '') or
                 '未命名')
        if path is None:
            path = self.canvas_path(canvas_id)
        return AssetResult(
            item=item,
            title=str(title),
            path=path,
            canvas_id=canvas_id,
            page_kind='canvas',
            tags=list(item_tags),
            rating=rating,
            notes=str(getattr(item, '_notes', '') or ''),
            filename=str(getattr(item, 'filename', '') or ''),
            deleted_at=getattr(item, '_deleted_at', None) if deleted else None,
        )

    # ── 画布归属与路径 ───────────────────────────────────────

    def _effective_canvas_id(self, item, known=None) -> str:
        """素材的有效画布 ID。

        - `_canvas_id` 为 None → UNASSIGNED_CANVAS
        - `_canvas_id` 指向的画布已不在工程里 → UNASSIGNED_CANVAS
        - 否则返回 `_canvas_id`

        "画布已不在工程里"的判定：`scene.workspace_pages` 里没有对应 id
        且不是 'default-canvas'（default-canvas 是隐式默认，即使
        workspace_pages 里没有也可能存在素材，见
        `_pages_with_implicit_defaults`）。
        """
        cid = getattr(item, '_canvas_id', None)
        if cid is None:
            return UNASSIGNED_CANVAS
        if known is None:
            known = self._known_canvas_ids()
        if cid in known or cid == 'default-canvas':
            return cid
        return UNASSIGNED_CANVAS

    def _known_canvas_ids(self) -> set:
        ids = set()
        for page in getattr(self.scene, 'workspace_pages', []) or []:
            if page.get('kind') == 'canvas':
                ids.add(page.get('id'))
        # workspace_nodes 里的 canvas 分区页面也算
        if self.resource_service is not None:
            for node in self.resource_service.nodes:
                if node.get('section') == 'canvas' and node.get('pageId'):
                    ids.add(node['pageId'])
        return ids

    def canvas_path(self, canvas_id: str) -> str:
        """画布在资源树里的可读路径，如 "参考 / ARC Raiders"。

        UNASSIGNED_CANVAS 返回 "未归属"。
        """
        if canvas_id == UNASSIGNED_CANVAS:
            return UNASSIGNED_LABEL
        if self.resource_service is None:
            return self._page_title(canvas_id)
        # 从 resource_service 里找 pageId 对应的节点，向上拼路径
        node = None
        for n in self.resource_service.nodes:
            if n.get('pageId') == canvas_id:
                node = n
                break
        if node is None:
            return self._page_title(canvas_id)
        parts = []
        current = node
        seen = set()
        while current is not None and current['id'] not in seen:
            seen.add(current['id'])
            parts.append(current.get('title') or '')
            parent_id = current.get('parentId')
            if not parent_id:
                break
            current = self.resource_service._by_id(parent_id)
        parts.reverse()
        return ' / '.join(p for p in parts if p) or self._page_title(canvas_id)

    def _page_title(self, canvas_id: str) -> str:
        if self._page_title_resolver is not None:
            try:
                title = self._page_title_resolver(canvas_id)
                if title:
                    return str(title)
            except Exception:
                logger.exception('page_title_resolver failed for %s', canvas_id)
        for page in getattr(self.scene, 'workspace_pages', []) or []:
            if page.get('id') == canvas_id:
                return str(page.get('title') or canvas_id)
        if canvas_id == 'default-canvas':
            return '默认画布'
        return canvas_id

    # ── 定位 ─────────────────────────────────────────────────

    def locate(self, item) -> tuple:
        """返回 `(page_kind, page_id)`，用于导航到素材所在画布。

        - 素材有明确画布且画布存在 → ('canvas', canvas_id)
        - 素材无归属或画布已消失 → ('canvas', fallback_canvas_id)
          fallback 优先 'default-canvas'（若工程里有素材属于它），否则
          第一个已知画布，否则 None（调用方应提示用户选择有效画布）。
        """
        cid = getattr(item, '_canvas_id', None)
        known = self._known_canvas_ids()
        if cid and (cid in known or cid == 'default-canvas'):
            return ('canvas', cid)
        fallback = self._fallback_canvas_id()
        return ('canvas', fallback)

    def _fallback_canvas_id(self) -> Optional[str]:
        # default-canvas 是隐式默认页：`_pages_with_implicit_defaults` 总是
        # 支持打开它来承载素材（见 media_library.py），即使 workspace_pages
        # 里没有显式注册。所以它是无归属素材的首选回落，不要求它出现在
        # _known_canvas_ids() 里。
        return 'default-canvas'
