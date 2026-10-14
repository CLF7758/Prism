"""World-space record index; no Qt graphics objects or image bytes."""
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

    def cells(self, rect):
        left, right = math.floor(rect.left()/self.CELL), math.floor(rect.right()/self.CELL)
        top, bottom = math.floor(rect.top()/self.CELL), math.floor(rect.bottom()/self.CELL)
        return left, right, top, bottom

    def keys(self, rect):
        left, right, top, bottom = self.cells(rect)
        if (right-left+1)*(bottom-top+1) > self.MAX_CELLS:
            return None
        return [(x, y) for x in range(left, right+1) for y in range(top, bottom+1)]

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
