"""Shared compact rectangle packing with bounded work per group.

纯尺寸计算：不导入 Qt，也不物化任何图形对象，调用方可在后台 worker 中
直接调用本模块；Qt 项的安装/摆放由调用方（AssetCanvas.arrange 等）负责分片。

布局复用与局部失效（交接书 5A）：
- pack_sizes(sizes) 是纯函数：sizes 的顺序编码了排序与范围版本，值编码了
  尺寸/crop 几何版本；因此 tuple(sizes) 是"无上下文"时的退化布局键。
- 整体缓存（_pack_cache）按该键复用完整结果：同一范围重复进入 O(1) 命中，
  不依赖 pack 递归 LRU 掩盖上层全量重排。
- 小组缓存（_group_cache）按 <=GROUP 的 sizes 元组复用：新增/删除/移动少量
  图元时，仅变化的组与顶层组合重算，未变化的组直接命中 —— 局部失效。
- StableLayoutCache 供上层传入完整键（project/epoch、范围 ID 版本、排序、
  布局模式、尺寸/crop 几何版本、必要视口宽度）复用"位置 + 空间索引"；它只
  服务自动布局，绝不缓存或覆盖用户手工摆放、撤销或选择。
"""
import math
import threading
from collections import OrderedDict

import rpack


# 稳定分组大小：小组结果可跨页/跨范围复用，是实现局部失效的最小粒度。
GROUP = 16
_packing_lock = threading.RLock()

# 布局计算串行锁（显式互斥）。
#
# 背景 worker（prepare_layout）和 GUI 线程（arrange）共享同一份小组/整体缓存，
# 两者并发调用 pack_sizes 会让同一批尺寸被重复计算，也让缓存状态在两次读取
# 之间漂移。这里用一把显式互斥把"取缓存 -> 计算 -> 写缓存"整段串行化。
#
# 锁内只允许纯计算：不得在锁内做 GUI 发布（Qt 信号、模型 reset、绘制）。
# 锁的持有者只能是后台布局 worker 或 GUI 的同步 arrange，两者都不会在锁内
# 触碰 QWidget/场景。
_layout_lock = threading.RLock()


class LayoutCancelled(Exception):
    """A superseded pure-data layout must not be installed."""


def check_cancelled(cancelled):
    if cancelled is not None and cancelled():
        raise LayoutCancelled()

# 小组缓存容量：键最多 GROUP 项，占用约 1~2 KB/条；10 万图 / 16 ≈ 6250 组，
# 放宽到 8192 避免翻页时把上页小组逐出、回来再全量重排。
_GROUP_CACHE_CAPACITY = 8192

# 整体缓存容量与单键元素数上限。整体键 = 完整 sizes 元组，随范围增大而增大；
# 超过 _PACK_CACHE_MAX_KEY 项的大键复用价值低且占用大（数万项 tuple 数十 MB 级），
# 不写入整体缓存，只靠小组缓存实现局部失效。
_PACK_CACHE_CAPACITY = 16
_PACK_CACHE_MAX_KEY = 2048


class _Lru:
    """有界 LRU：get 命中提升为最近使用；put 超容淘汰最久未用。"""

    def __init__(self, capacity):
        self.capacity = max(1, int(capacity))
        self._data = OrderedDict()
        self.hits = 0
        self.misses = 0
        self._lock = threading.RLock()

    def get(self, key, default=None):
        with self._lock:
            try:
                value = self._data.pop(key)
            except KeyError:
                self.misses += 1
                return default
            self._data[key] = value
            self.hits += 1
            return value

    def put(self, key, value):
        with self._lock:
            self._data.pop(key, None)
            self._data[key] = value
            while len(self._data) > self.capacity:
                self._data.popitem(last=False)

    def clear(self):
        with self._lock:
            self._data.clear()

    def __len__(self):
        with self._lock:
            return len(self._data)


_group_cache = _Lru(_GROUP_CACHE_CAPACITY)
_pack_cache = _Lru(_PACK_CACHE_CAPACITY)


def _pack_small(sizes, cancelled=None):
    """打包 <=GROUP 的小组；纯计算，无缓存。sizes 为 (w,h) 元组序列。"""
    if not sizes:
        return (), (0, 0)
    side = max(max(max(w, h) for w, h in sizes),
               math.ceil(math.sqrt(sum(w*h for w, h in sizes))))
    while True:
        check_cancelled(cancelled)
        try:
            with _packing_lock:
                check_cancelled(cancelled)
                positions = rpack.pack(sizes, max_width=side, max_height=side)
            break
        except rpack.PackingImpossibleError:
            side = math.ceil(side*1.2)
    return tuple(positions), rpack.bbox_size(sizes, positions)


def _pack_group(sizes, cancelled=None):
    """打包一个 <=GROUP 的小组；结果入小组缓存以便局部复用。"""
    key = tuple(sizes)
    check_cancelled(cancelled)
    cached = _group_cache.get(key)
    if cached is not None:
        return cached
    positions, bounds = _pack_small(key, cancelled)
    result = positions, bounds
    _group_cache.put(key, result)
    return result


def _pack_sizes(sizes, cancelled=None):
    """按 GROUP 分层打包。sizes 为 (w,h) 元组序列。

    小组与顶层组合都经 _pack_group 缓存：规模增大时只有新增/变化的组重算，
    未变化的组直接命中，避免每次全量重排造成画面跳动。
    """
    if len(sizes) <= GROUP:
        return _pack_group(sizes, cancelled)
    groups = [_pack_group(sizes[start:start+GROUP], cancelled)
              for start in range(0, len(sizes), GROUP)]
    # Recurse through bounded groups at every level. Passing all parent
    # rectangles to rpack again makes the top level unbounded for 100k rows.
    origins, bounds = _pack_sizes(tuple(group[1] for group in groups), cancelled)
    positions = []
    for (group, _), (ox, oy) in zip(groups, origins):
        check_cancelled(cancelled)
        positions.extend((x+ox, y+oy) for x, y in group)
    return tuple(positions), bounds


def pack_sizes(sizes, *, cancelled=None):
    """打包矩形；返回 (list[positions], bounds)。签名不变，语义不变。

    结果经整体缓存复用：同一范围（同排序、同尺寸/crop 几何版本）重复进入时
    直接命中，不再全量重排。返回的 positions 为每次新建的 list，bounds 为
    不可变元组，缓存值不会被调用方修改。

    线程安全：整段计算在模块级 ``_layout_lock`` 内串行执行，后台布局 worker
    与 GUI 线程不会并发跑同一个 pack。锁内只有纯计算，不做任何 GUI 发布。
    """
    with _layout_lock:
        return _pack_sizes_locked(sizes, cancelled=cancelled)


def _pack_sizes_locked(sizes, *, cancelled=None):
    key = tuple(sizes)
    check_cancelled(cancelled)
    cached = _pack_cache.get(key) if len(key) <= _PACK_CACHE_MAX_KEY else None
    if cached is not None:
        positions, bounds = cached
        return list(positions), bounds
    positions, bounds = _pack_sizes(key, cancelled)
    check_cancelled(cancelled)
    if len(key) <= _PACK_CACHE_MAX_KEY:
        _pack_cache.put(key, (positions, bounds))
    return list(positions), bounds


def layout_key(project_epoch, range_version, sort_key, mode,
               geometry_version, viewport_width):
    """构造 5A 布局键；所有分量必须 hashable。见 StableLayoutCache。"""
    return (project_epoch, range_version, sort_key, mode,
            geometry_version, viewport_width)


def _cache_locked(method):
    from functools import wraps
    @wraps(method)
    def locked(self, *args, **kwargs):
        with self._lock:
            return method(self, *args, **kwargs)
    return locked


class StableLayoutCache:
    """键控布局缓存（交接书 5A），供上层复用"位置 + 空间索引"。

    只在自动布局路径上使用；手工摆放（用户拖动、arrange_selected、撤销/重做）
    不得经过本缓存，本缓存也不写回任何图元位置 —— 它只回放纯计算得到的相对
    位置，因此不可能覆盖用户手工布局、撤销或选择。

    key 组成（由 layout_key 构造）：
        project_epoch    项目/纪元：换项目或同项目重开（epoch 变化）即失效
        range_version    范围 ID 版本：页码/目录/筛选范围变化即失效
        sort_key         排序：排序变化必须重排
        mode             布局模式：visual / canvas / optimal 等
        geometry_version 尺寸/crop 几何版本：卡片尺寸或裁剪变化即失效
        viewport_width   必要视口宽度：影响换行/网格列数

    占用估计：单条 ≈ 键(7 元组) + positions(每项 1 个 (x,y) 元组) + bounds，
    约 32 + 64*len(sizes) 字节量级。

    淘汰规则：LRU 淘汰最久未用；容量按条目数 + 缓存元素总数双重上限，
    单条元素数超过 max_elements 的巨型布局不写入（复用价值低且会挤掉其它范围）。

    失效事件：put 同键覆盖（geometry_version 推进即换新键，旧键自然失效）；
    invalidate 按谓词删除；clear 整体清空。调用方在项目关闭/切换时必须 clear，
    避免跨项目复用旧范围位置。
    """

    def __init__(self, capacity=16, max_elements=65536):
        self.capacity = max(1, int(capacity))
        self.max_elements = max(1, int(max_elements))
        self._data = OrderedDict()
        self._elements = 0
        self._lock = threading.RLock()

    @_cache_locked
    def get(self, key):
        """命中返回 (positions, bounds)；未命中返回 None。positions 为副本。"""
        entry = self._data.get(key)
        if entry is None:
            return None
        self._data.move_to_end(key)
        positions, bounds, _count = entry
        return list(positions), bounds

    @_cache_locked
    def put(self, key, positions, bounds):
        positions = tuple(positions)
        count = len(positions)
        if count > self.max_elements:
            return
        if key in self._data:
            self._elements -= self._data[key][2]
            del self._data[key]
        self._data[key] = (positions, bounds, count)
        self._elements += count
        while self._data and (len(self._data) > self.capacity
                              or self._elements > self.max_elements):
            _key, (_p, _b, _count) = self._data.popitem(last=False)
            self._elements -= _count

    def layout(self, key, sizes):
        """按键复用；未命中时计算并写入缓存。返回 (positions, bounds)。

        计算发生在缓存锁之外：打包可能耗时很久（十万图），持锁计算会让
        其它线程的缓存读写一起排队，也避免与模块级 ``_layout_lock`` 形成
        交叉持锁。
        """
        hit = self.get(key)
        if hit is not None:
            return hit
        positions, bounds = pack_sizes(sizes)
        self.put(key, positions, bounds)
        return positions, bounds

    @_cache_locked
    def invalidate(self, predicate):
        """删除所有使 predicate(key) 为真的条目；用于范围/项目级失效。"""
        for key in [k for k in self._data if predicate(k)]:
            self._elements -= self._data[key][2]
            del self._data[key]

    @_cache_locked
    def clear(self):
        self._data.clear()
        self._elements = 0

    @_cache_locked
    def __len__(self):
        return len(self._data)
