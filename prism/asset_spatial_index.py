"""World-space record index; no Qt graphics objects or image bytes.

纯几何索引：只存 record -> sceneBoundingRect 的分桶映射，不持有任何图形对象
或图像字节。几何变化按 record 粒度局部更新（update 只动单个矩形的桶），并累计
受影响区域供上层做局部失效；布局复用时可连同快照一起恢复，避免重复进入同一
范围时逐条 update 全量矩形。
"""
import math
from PyQt6 import QtCore


class AssetSpatialIndex:
    CELL = 4096
    MAX_CELLS = 64

    def __init__(self):
        self.rects = {}
        self.buckets = {}
        self.overflow = set()
        self.extent = QtCore.QRectF()
        # 局部失效：自上次 take_dirty()/discard_dirty() 以来，几何变化的联合区域。
        self._dirty = QtCore.QRectF()

    def cells(self, rect):
        left, right = math.floor(rect.left()/self.CELL), math.floor(rect.right()/self.CELL)
        top, bottom = math.floor(rect.top()/self.CELL), math.floor(rect.bottom()/self.CELL)
        return left, right, top, bottom

    def keys(self, rect):
        left, right, top, bottom = self.cells(rect)
        if (right-left+1)*(bottom-top+1) > self.MAX_CELLS:
            return None
        return [(x, y) for x in range(left, right+1) for y in range(top, bottom+1)]

    def _mark_dirty(self, rect):
        # QRectF 是值类型，united 返回新对象；仅几何变化时调用，成本 O(1)。
        if rect is not None and not rect.isEmpty():
            self._dirty = self._dirty.united(rect)

    def remove(self, record):
        previous = self.rects.pop(record, None)
        if previous is None:
            return
        rect, keys = previous
        if keys is None:
            self.overflow.discard(record)
        else:
            for key in keys:
                bucket = self.buckets[key]
                bucket.discard(record)
                if not bucket:
                    del self.buckets[key]
        self._mark_dirty(rect)

    def update(self, record):
        rect = record.sceneBoundingRect()
        previous = self.rects.get(record)
        if previous is not None and previous[0] == rect:
            return False
        self.remove(record)
        keys = self.keys(rect)
        self.rects[record] = QtCore.QRectF(rect), keys
        if keys is None:
            self.overflow.add(record)
        else:
            for key in keys:
                self.buckets.setdefault(key, set()).add(record)
        self.extent = self.extent.united(rect)
        self._mark_dirty(rect)
        return True

    def query(self, rect):
        left, right, top, bottom = self.cells(rect)
        area = (right-left+1)*(bottom-top+1)
        if area > max(1, len(self.buckets)*2):
            candidates = self.rects
        else:
            candidates = set(self.overflow)
            for x in range(left, right+1):
                for y in range(top, bottom+1):
                    candidates.update(self.buckets.get((x, y), ()))
        return [record for record in candidates if self.rects[record][0].intersects(rect)]

    # ---- 局部失效与布局复用（5A） ----

    def take_dirty(self):
        """返回并清空自上次调用以来的几何变化联合区域（供局部失效）。

        上层可在几何变化后只失效/重绘该区域，而非整个场景。无变化时返回空
        QRectF（isNull() 为 True）。
        """
        rect = self._dirty
        self._dirty = QtCore.QRectF()
        return rect

    def discard_dirty(self):
        """丢弃累计的脏区（例如已整场景失效，无需局部区域）。"""
        self._dirty = QtCore.QRectF()

    def snapshot(self):
        """返回索引的不可变浅快照，供布局缓存连同"位置 + 空间索引"一起保存。

        重复进入同一范围时 restore(snapshot) 以 O(records) 恢复，避免逐条
        update 全量矩形。QRectF 为值类型、keys/buckets/overflow 为拷贝，后续
        对索引的修改不会污染快照。

        占用估计：O(records) 内存（每 record 一个 QRectF + 桶键引用），与索引
        本体同量级；只应对"重复进入、有界规模"的范围做快照，超大范围应复用
        位置而非保留整份索引副本。
        """
        return (
            {record: (QtCore.QRectF(rect), None if keys is None else list(keys))
             for record, (rect, keys) in self.rects.items()},
            {key: set(records) for key, records in self.buckets.items()},
            set(self.overflow),
            QtCore.QRectF(self.extent),
        )

    def restore(self, snapshot):
        """用 snapshot() 的结果整体恢复索引；会清空当前脏区。"""
        rects, buckets, overflow, extent = snapshot
        self.rects = {record: (QtCore.QRectF(rect), None if keys is None else list(keys))
                      for record, (rect, keys) in rects.items()}
        self.buckets = {key: set(records) for key, records in buckets.items()}
        self.overflow = set(overflow)
        self.extent = QtCore.QRectF(extent)
        self._dirty = QtCore.QRectF()
