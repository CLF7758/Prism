"""Bounded canvas-group display composites; originals and hit testing stay intact.

The existing delegate and Qt painter render each group in short GUI turns.
This adds a parent level above image LOD without another rendering engine.
"""
from collections import OrderedDict
import math
import time
from PyQt6 import QtCore, QtGui, QtWidgets
from prism import asset_paint_evidence as paint_evidence


class CanvasComposites(QtCore.QObject):
    MIN_ITEMS = 96
    BUDGET = 32*1024*1024
    MAX_EDGE = 1024
    DETAIL_PIXELS = 40
    # Cap how many whole-group composites a single GUI turn may install; the
    # remaining groups continue on later ticks instead of stalling input.
    MAX_GROUPS_PER_FRAME = 3

    def __init__(self, host):
        super().__init__(host)
        self.host = host
        self.groups = []
        self.row_group = {}
        # Composite keys are ``(group, wanted, epoch)``. ``epoch`` is the
        # content generation: model replacement or a full source change bumps
        # it so every key derived from the old epoch misses and no stale
        # composite can be painted. ``generation`` separately tracks camera
        # movement so in-flight builds can drop at interrupt points.
        self.cache = OrderedDict()
        self.by_group = {}
        self.stale = set()
        self.pending = OrderedDict()
        self.refresh_jobs = set()
        self.bytes = 0
        self.building = False
        self.draws = 0
        self.builds = 0
        self.dirty = []
        self.published = 0.0
        self.camera_key = None
        self.camera_changed = 0.0
        self.focus_published = False
        self.epoch = 0
        self.generation = 0
        self._content_key = None
        self.timer = QtCore.QTimer(self)
        self.timer.setObjectName('canvas.group_composites')
        self.timer.setInterval(16)
        self.timer.timeout.connect(self._work)
        from prism.resource_budget import shared_cache_budget
        self._budget = shared_cache_budget()
        self._budget.register(self, category='canvas_composites', usage='budget_bytes',
                              trim='_trim_cache_to', priority=40)

    def budget_bytes(self):
        return self.bytes + sum(image.sizeInBytes() for image, _cursor in self.pending.values()
                                if image is not None)

    def _trim_cache_to(self, target_bytes):
        camera = self.host.mapToScene(self.host.viewport().rect()).boundingRect()
        for key in list(self.cache):
            if self.budget_bytes() <= target_bytes:
                break
            if self.host.isVisible() and self.groups[key[0]][0].intersects(camera):
                continue
            self.bytes -= self.cache.pop(key).sizeInBytes()
            self.stale.discard(key)
            self.by_group[key[0]].discard(key)
            if not self.by_group[key[0]]:
                del self.by_group[key[0]]

    def reset(self):
        self.timer.stop()
        # Committed geometry can be held by an inactive query snapshot.
        self.groups = []
        self.row_group = {}
        self.cache.clear()
        self.by_group.clear()
        self.stale.clear()
        self.pending.clear()
        self.refresh_jobs.clear()
        self.bytes = 0
        self.dirty.clear()
        self.epoch += 1

    def arrange(self):
        self.reset()
        host = self.host
        if len(host.cards) < self.MIN_ITEMS or getattr(host.parent(), 'mode', None) == 'visual':
            return
        groups = OrderedDict()
        # Group by ROW, not by card: a card facade is built on demand, and a
        # group that held every card of the library would keep them all
        # alive and defeat the bounded residency of the canvas.
        for row in range(len(host.cards)):
            item = host.model().items[row]
            if isinstance(item, dict):
                continue
            canvas = getattr(item, '_canvas_id', None)
            groups.setdefault(canvas, []).append(row)
        for canvas, members in groups.items():
            # A canvas with thousands of pictures also needs bounded child
            # groups. A giant bitmap must never become an unbounded texture.
            for start in range(0, len(members), 128):
                rows = members[start:start+128]
                bounds = QtCore.QRectF()
                for row in rows:
                    bounds = bounds.united(host._card_rect(row))
                index = len(self.groups)
                self.groups.append((bounds, tuple(rows)))
                for row in rows:
                    self.row_group[row] = index
        if len(self.row_group) != len(host.cards):
            self.reset()

    def _content_key_for_view(self):
        """Derive a content identity from the host for defence in depth.

        ``arrange``/``reset`` normally handle model replacement, but if a path
        misses them this key still retires stale composites on the next paint.
        """
        host = self.host
        model = getattr(host, '_model', None)
        cards = getattr(host, 'cards', None)
        content = getattr(host, '_content_bounds', None)
        return (getattr(host, '_render_layout_revision', 0),
                id(model) if model is not None else 0,
                len(cards) if cards is not None else 0,
                (content.x(), content.y(), content.width(), content.height()) if content is not None else 0)

    def invalidate(self, row=None, preserve=False):
        group = self.row_group.get(row) if row is not None else None
        if not preserve:
            if group is None:
                # Model or whole-source change: retire the current epoch so
                # every stale composite key misses and nothing old is drawn.
                self.epoch += 1
                self.cache.clear()
                self.by_group.clear()
                self.stale.clear()
                self.pending.clear()
                self.refresh_jobs.clear()
                self.bytes = 0
                return
            # A single row changed: only its group's pixels are stale.
            keys = [key for key in self.cache if key[0] == group]
            for key in keys:
                self.bytes -= self.cache.pop(key).sizeInBytes()
                self.stale.discard(key)
                self.by_group[key[0]].discard(key)
                if not self.by_group[key[0]]:
                    del self.by_group[key[0]]
            for key in list(self.pending):
                if key[0] == group:
                    del self.pending[key]
                    self.refresh_jobs.discard(key)
            return
        # preserve=True: detail arrival. Keep the existing pixels as fallback
        # so the group never flashes blank while a refresh is sliced in.
        for key in [key for key in self.cache if group is None or key[0] == group]:
            self.stale.add(key)
        # A job may contain old pixels when a source/colour/crop changes.
        for key in list(self.pending):
            if group is None or key[0] == group:
                if self.pending[key][1]:
                    # Detail arriving during a sliced build must not restart
                    # the entire group after every image. Finish this cover,
                    # then refresh it once if pixels already painted changed.
                    self.refresh_jobs.add(key)

    def ready_for(self, rect):
        """One region can refine while other visible groups are still building."""
        return not any(self.groups[key[0]][0].intersects(rect) and not self.by_group.get(key[0])
                       for key in self.pending)

    def paint(self, painter, viewport):
        if self.building or not self.groups:
            return None
        host = self.host
        content_key = self._content_key_for_view()
        if content_key != self._content_key:
            # Model replaced or layout repacked without a reset reaching us:
            # retire the epoch instead of painting stale composites.
            self._content_key = content_key
            self.epoch += 1
            self.cache.clear()
            self.by_group.clear()
            self.stale.clear()
            self.pending.clear()
            self.refresh_jobs.clear()
            self.bytes = 0
        # Tile buffers can be recorded at a different scale from the camera.
        # Detail selection must follow screen pixels, not buffer resolution.
        scale = abs(host.transform().m11())
        if host.gridSize().width()*scale >= self.DETAIL_PIXELS:
            self.pending.clear()
            self.refresh_jobs.clear()
            self.timer.stop()
            return None
        remaining = []
        camera = host.mapToScene(host.viewport().rect()).boundingRect()
        camera_key = (camera.x(), camera.y(), camera.width(), camera.height(), host.transform().m11(), self.epoch)
        moved = camera_key != self.camera_key
        if moved:
            self.camera_key = camera_key
            self.camera_changed = time.monotonic()
            self.focus_published = False
            self.generation += 1
        for group, (bounds, rows) in enumerate(self.groups):
            if not bounds.intersects(viewport):
                continue
            # A tile records only a slice of the viewport. Pick one group
            # level for the real camera, not a new level per recording tile.
            camera_scale = abs(host.transform().m11())*host.viewport().devicePixelRatioF()
            edge = max(bounds.width(), bounds.height())*camera_scale
            wanted = max(32, 2**math.ceil(math.log2(max(1, edge))))
            if wanted > self.MAX_EDGE:
                if painter is not None:
                    remaining.extend(rows)
                continue
            key = group, wanted, self.epoch
            image = self.cache.get(key)
            available = key
            if image is None and self.by_group.get(group):
                available = max(self.by_group[group], key=lambda entry: entry[1])
                image = self.cache[available]
            if image is not None:
                self.cache.move_to_end(available)
                if painter is not None:
                    painter.drawImage(bounds, image)
                    paint_evidence.observe_image(host, painter, bounds, image, category='group',
                        stale=available in self.stale, render_revision=list(available))
                    self.draws += 1
            else:
                if painter is not None:
                    remaining.extend(rows)
            # Reuse a previous resolution while preparing this camera. That
            # lets a local tile redraw completed groups without repainting
            # every image in all its still-unfinished neighbours.
            if image is None or available[1] < wanted or available in self.stale:
                if key not in self.pending:
                    for old in list(self.pending):
                        if old[0] == group:
                            del self.pending[old]
                            self.refresh_jobs.discard(old)
                    self.pending[key] = (None, 0)
        # Keep only cameras/resolutions still requested, so fast wheel input
        # cannot grow an obsolete build queue without a bound.
        for key in list(self.pending):
            if not self.groups[key[0]][0].intersects(camera):
                del self.pending[key]
                self.refresh_jobs.discard(key)
        if moved and self.pending and host.gridSize().width()*abs(host.transform().m11()) > 8:
            focus = host.mapToScene(getattr(host, '_detail_focus', host.viewport().rect().center()))
            self.pending = OrderedDict(sorted(self.pending.items(), key=lambda pair:
                (self.groups[pair[0][0]][0].center()-focus).manhattanLength()))
        if self.pending and not self.timer.isActive():
            self.timer.start()
        return remaining

    def _work(self):
        host = self.host
        if not host.isVisible():
            self.timer.stop()
            return
        # Interactive panning/zooming reuses existing pixels. New group
        # snapshots are background GUI work after the camera briefly settles.
        if time.monotonic()-self.camera_changed < .15:
            return
        started = time.perf_counter()
        generation = self.generation
        changed = []
        self.building = True
        try:
            completed = 0
            while self.pending and time.perf_counter()-started < .004 \
                    and completed < self.MAX_GROUPS_PER_FRAME:
                if self.generation != generation:
                    break  # camera moved; drop stale composite work
                key = next(iter(self.pending))
                image, cursor = self.pending[key]
                bounds, rows = self.groups[key[0]]
                if image is None:
                    width = max(1, math.ceil(key[1]*bounds.width()/max(bounds.width(), bounds.height())))
                    height = max(1, math.ceil(key[1]*bounds.height()/max(bounds.width(), bounds.height())))
                    image = QtGui.QImage(width, height, QtGui.QImage.Format.Format_ARGB32_Premultiplied)
                    image.fill(QtCore.Qt.GlobalColor.transparent)
                painter = QtGui.QPainter(image)
                painter.setRenderHints(host.renderHints())
                painter.scale(image.width()/bounds.width(), image.height()/bounds.height())
                painter.translate(-bounds.left(), -bounds.top())
                option = QtWidgets.QStyleOptionViewItem()
                option.font = host.font()
                option.state = QtWidgets.QStyle.StateFlag.State_Enabled
                try:
                    with paint_evidence.resource_scope(host, image, 'group'):
                        # At most a small slice of a group in each GUI turn.
                        while cursor < len(rows):
                            # The facade is built here, for the rows this
                            # group actually paints, and dropped again when
                            # the canvas evicts it.
                            card = host.cards[rows[cursor]]
                            painter.save()
                            painter.translate(card._x, card._y)
                            option.rect = card.boundingRect().toAlignedRect()
                            with paint_evidence.delegate_scope(host, card, painter):
                                host.delegate.paint(painter, option, host.model().index(card.row))
                            painter.restore()
                            cursor += 1
                            if time.perf_counter()-started >= .004:
                                break
                finally:
                    painter.end()
                paint_evidence.generated(host, image, complete=cursor == len(rows))
                if cursor == len(rows):
                    self.pending.pop(key)
                    previous = self.cache.pop(key, None)
                    if previous is not None:
                        self.bytes -= previous.sizeInBytes()
                    self.cache[key] = image
                    if key in self.refresh_jobs:
                        self.stale.add(key)
                        self.refresh_jobs.discard(key)
                    else:
                        self.stale.discard(key)
                    self.by_group.setdefault(key[0], set()).add(key)
                    self.bytes += image.sizeInBytes()
                    self.builds += 1
                    completed += 1
                    paint_evidence.installed(host, image, resource_kind='group',
                        render_revision=list(key), stale=key in self.stale)
                    changed.append(bounds)
                    while self.bytes > self.BUDGET and self.cache:
                        _old, value = self.cache.popitem(last=False)
                        self.stale.discard(_old)
                        self.by_group[_old[0]].discard(_old)
                        if not self.by_group[_old[0]]:
                            del self.by_group[_old[0]]
                        self.bytes -= value.sizeInBytes()
                else:
                    self.pending[key] = image, cursor
        finally:
            self.building = False
        self._budget.changed(self)
        self.dirty.extend(changed)
        now = time.monotonic()
        publish = []
        if self.dirty and not self.pending:
            publish = self.dirty[:]
        elif (self.dirty and not self.focus_published
              and host.gridSize().width()*abs(host.transform().m11()) > 8):
            focus = host.mapToScene(getattr(host, '_detail_focus', host.viewport().rect().center()))
            publish = [bounds for bounds in self.dirty if bounds.contains(focus)]
        if publish:
            # Show the focal region promptly, then let the bounded decoder
            # finish instead of interrupting it with repeated whole-tile draws.
            host._tiles.invalidate(publish)
            self.dirty = [bounds for bounds in self.dirty if bounds not in publish]
            self.focus_published = True
            self.published = now
            host.viewport().update()
        if not self.pending:
            self.timer.stop()
