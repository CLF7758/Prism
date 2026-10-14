"""Bounded world-space tile cache shared by dense canvas views.

All scene rendering stays on the GUI thread. Fixed dyadic coordinates let
parents cover children during zoom transitions without decoding originals.
"""
import math
import time
from collections import OrderedDict
from prism.performance_trace import measured

from PyQt6 import QtCore, QtGui


class CanvasTiles(QtCore.QObject):
    SIDE = 512
    BUDGET = 64 * 1024 * 1024
    FADE = .16

    def __init__(self, view, render):
        super().__init__(view)
        self.view, self.render = view, render
        self.tiles = OrderedDict()
        self.stale = set()
        self.pending = OrderedDict()
        self.bytes = 0
        self.level = None
        self.timer = QtCore.QTimer(self)
        self.timer.setInterval(16)
        self.timer.timeout.connect(self._work)
        self.visible = set()
        self.builds = 0
        self.protected = set()
        self.coverage_missing = 0
        self._coverage_log_time = 0

    @classmethod
    def rect(cls, key):
        level, x, y = key
        side = cls.SIDE / 2.0 ** level
        return QtCore.QRectF(x * side, y * side, side, side)

    def keys(self, rect, level):
        side = self.SIDE / 2.0 ** level
        return [(level, x, y)
                for y in range(math.floor(rect.top()/side), math.ceil(rect.bottom()/side))
                for x in range(math.floor(rect.left()/side), math.ceil(rect.right()/side))]

    def prefetch_rect(self):
        rect = self.view.mapToScene(self.view.viewport().rect()).boundingRect()
        if self.level is not None and self.tiles:
            margin = self.SIDE/2.0**self.level
            return rect.adjusted(-margin, -margin, margin, margin)
        return rect

    def reset(self):
        self.timer.stop()
        self.tiles.clear()
        self.stale.clear()
        self.pending.clear()
        self.bytes = 0
        self.level = None
        self.protected.clear()

    def invalidate(self, regions=None):
        if not regions or any(r.isEmpty() for r in regions):
            self.stale.update(self.tiles)
            self.pending.clear()
            return
        for key in self.tiles:
            if any(self.rect(key).intersects(r) for r in regions):
                self.stale.add(key)
        self.view.viewport().update()

    @measured('canvas.tile_build', 8)
    def _build(self, key):
        image = QtGui.QImage(self.SIDE, self.SIDE,
                             QtGui.QImage.Format.Format_ARGB32_Premultiplied)
        image.fill(self.view.backgroundBrush().color())
        children = [(key[0]+1, key[1]*2+x, key[2]*2+y) for y in range(2) for x in range(2)]
        if key not in self.stale and all(k in self.tiles and k not in self.stale for k in children):
            painter = QtGui.QPainter(image)
            for index, child in enumerate(children):
                painter.drawImage(QtCore.QRectF(index%2*self.SIDE/2, index//2*self.SIDE/2,
                                               self.SIDE/2, self.SIDE/2), self.tiles[child][0])
            painter.end()
        else:
            self.render(image, QtCore.QRectF(image.rect()), self.rect(key))
        previous = self.tiles.pop(key, None)
        if previous:
            self.bytes -= previous[0].sizeInBytes() + (previous[2].sizeInBytes() if previous[2] else 0)
        # Retain the previous bitmap only for a bounded transition.
        old = previous[0] if previous and key in self.stale else None
        self.tiles[key] = (image, time.monotonic(), old)
        self.bytes += image.sizeInBytes() + (old.sizeInBytes() if old else 0)
        self.stale.discard(key)
        self.builds += 1
        while self.bytes > self.BUDGET and self.tiles:
            victim = next((k for k in self.tiles if k not in self.protected), None)
            if victim is None:
                victim = next((k for k in self.tiles if k in self.visible
                               and (k[0]-1, k[1]//2, k[2]//2) in self.tiles), next(iter(self.tiles)))
            value = self.tiles.pop(victim)
            self.bytes -= value[0].sizeInBytes() + (value[2].sizeInBytes() if value[2] else 0)
            self.stale.discard(victim)

    def _has_cover(self, key):
        if key in self.tiles:
            return True
        ancestor = key
        for _ in range(8):
            ancestor = (ancestor[0]-1, ancestor[1]//2, ancestor[2]//2)
            if ancestor in self.tiles:
                return True
        return all((key[0]+1, key[1]*2+x, key[2]*2+y) in self.tiles
                   for x in range(2) for y in range(2))

    def _work(self):
        start = time.monotonic()
        while self.pending and time.monotonic()-start < .004:
            key, _ = self.pending.popitem(last=False)
            if key not in self.tiles or key in self.stale:
                self._build(key)
        now = time.monotonic()
        fading = False
        for key, (image, born, old) in list(self.tiles.items()):
            if now-born >= self.FADE and old is not None:
                self.tiles[key] = (image, born, None)
                self.bytes -= old.sizeInBytes()
            elif now-born < self.FADE:
                fading = True
        self.view.viewport().update()
        if not self.pending and not fading:
            self.timer.stop()

    def _fallback(self, painter, key, target):
        ancestor = key
        for _ in range(8):
            ancestor = (ancestor[0]-1, ancestor[1]//2, ancestor[2]//2)
            value = self.tiles.get(ancestor)
            if value is not None:
                world = self.rect(ancestor)
                local = self.rect(key)
                factor = self.SIDE/world.width()
                source = QtCore.QRectF((local.x()-world.x())*factor,
                                      (local.y()-world.y())*factor,
                                      local.width()*factor, local.height()*factor)
                painter.drawImage(target, value[0], source)
                return True
        # Zooming out can reuse finer blocks even before a parent exists.
        painted = False
        world = self.rect(key)
        for child, value in list(self.tiles.items()):
            if child[0] == key[0]+1 and world.contains(self.rect(child)):
                part = self.rect(child)
                destination = QtCore.QRectF(target.x()+(part.x()-world.x())/world.width()*target.width(),
                                            target.y()+(part.y()-world.y())/world.height()*target.height(),
                                            target.width()/2, target.height()/2)
                painter.drawImage(destination, value[0])
                painted = True
        return painted

    def paint(self, painter):
        view = self.view
        rect = view.mapToScene(view.viewport().rect()).boundingRect()
        scale = abs(view.transform().m11()) * view.viewport().devicePixelRatioF()
        wanted = math.ceil(math.log2(max(scale, 1e-12)))
        if self.level is None or scale/2.0**self.level < .42 or scale/2.0**self.level > 1.05:
            self.level = wanted
        keys = self.keys(rect, self.level)
        self.visible = set(keys)
        # A coarse cover is installed first; later frames reuse it while finer
        # tiles are produced within the timer's GUI work budget.
        parents = list(dict.fromkeys((k[0]-1, k[1]//2, k[2]//2) for k in keys))
        self.protected = set(keys+parents)
        for key in keys:
            ancestor = key
            for _ in range(8):
                ancestor = (ancestor[0]-1, ancestor[1]//2, ancestor[2]//2)
                if ancestor in self.tiles:
                    self.protected.add(ancestor)
                    break
        missing = [key for key in keys if not self._has_cover(key)]
        # An unrelated cached tile must not suppress the first coarse cover
        # for this viewport. Keep coverage even while background scans are busy.
        for key in dict.fromkeys((k[0]-1, k[1]//2, k[2]//2) for k in missing):
            self._build(key)
        self.coverage_missing = sum(not self._has_cover(key) for key in keys)
        if missing and time.monotonic()-self._coverage_log_time >= 1:
            from prism.performance_trace import record
            record('render_coverage', surface=type(view).__name__, missing_before=len(missing),
                   missing_after=self.coverage_missing, visible_tiles=len(keys),
                   tile_bytes=self.bytes, scale=scale, level=self.level)
            self._coverage_log_time = time.monotonic()
        centre = rect.center()
        ordered = sorted(keys, key=lambda k: (self.rect(k).center()-centre).manhattanLength())
        neighbours = self.keys(rect.adjusted(-self.SIDE/2.0**self.level,
                                             -self.SIDE/2.0**self.level,
                                             self.SIDE/2.0**self.level,
                                             self.SIDE/2.0**self.level), self.level)
        # Drop requests from previous cameras, rather than accumulating work.
        self.pending = OrderedDict((k, None) for k in (parents+ordered+neighbours)
                                   if k not in self.tiles or k in self.stale)
        painter.fillRect(view.viewport().rect(), view.backgroundBrush())
        painter.setRenderHint(QtGui.QPainter.RenderHint.SmoothPixmapTransform)
        now = time.monotonic()
        for key in keys:
            target = view.viewportTransform().mapRect(self.rect(key))
            value = self.tiles.get(key)
            covered = self._fallback(painter, key, target)
            if value is None:
                continue
            self.tiles.move_to_end(key)
            image, born, old = value
            alpha = min(1., (now-born)/self.FADE)
            if old is not None:
                painter.drawImage(target, old)
                covered = True
            painter.save()
            painter.setOpacity(alpha if covered else 1.)
            painter.drawImage(target, image)
            painter.restore()
        if self.pending or any(now-v[1] < self.FADE for v in self.tiles.values()):
            if not self.timer.isActive():
                self.timer.start()
