"""Bounded world-space tile cache shared by dense canvas views.

All scene rendering stays on the GUI thread. Fixed dyadic coordinates let
parents cover children during zoom transitions without decoding originals.
"""
import math
import time
from collections import OrderedDict
from prism.performance_trace import measured

from PyQt6 import QtCore, QtGui
from prism import asset_paint_evidence as paint_evidence


class CanvasTiles(QtCore.QObject):
    SIDE = 512
    BUDGET = 64 * 1024 * 1024
    FADE = .16
    # A GUI turn must not install an unbounded number of freshly rendered
    # tiles; cap each timer tick so heavy zoom/pan cannot freeze input.
    MAX_BUILDS_PER_FRAME = 4
    # Wall-clock budget of one GUI turn of tile work. A sliced renderer
    # hands the turn back before this runs out, so a single frame can
    # never spend an unbounded amount of time inside one paint.
    FRAME_BUDGET = .004
    # A GUI turn must also advance a bounded number of render slices. One
    # slice is the renderer's own bounded unit -- ``_draw_tile_slices``
    # draws at most 128 records before it yields -- so this cap is the
    # counting form of the "at most 128 records per GUI turn" contract, and
    # it holds even when a slice costs far less than the wall-clock budget
    # can notice. A trivial delegate would otherwise finish a whole cold
    # library inside one turn, because the time budget alone cannot see it.
    MAX_SLICE_STEPS_PER_FRAME = 1

    def __init__(self, view, render, job_factory=None):
        super().__init__(view)
        self.view, self.render = view, render
        # Optional renderer protocol: ``job_factory(image, target, source)``
        # returns a generator that yields between bounded render slices, or
        # ``None`` when the content cannot be sliced. The view protocol
        # (``view.create_display_tile_job``) is used when no factory is
        # given, so the material canvas keeps its own slicing.
        self._job_factory = job_factory
        self.tiles = OrderedDict()
        self.stale = set()
        self.pending = OrderedDict()
        self.bytes = 0
        self.level = None
        self.timer = QtCore.QTimer(self)
        self.timer.setObjectName('canvas.build_tiles')
        self.timer.setInterval(16)
        self.timer.timeout.connect(self._work)
        self.visible = set()
        self.builds = 0
        # Observable per-turn work counters, so the "one GUI turn draws at
        # most 128 records" contract can be asserted instead of inferred.
        self.last_frame_steps = 0
        self.last_frame_builds = 0
        self.protected = set()
        self.coverage_missing = 0
        self._coverage_log_time = 0
        self.context = None
        self.contexts = OrderedDict()
        # ``generation`` tracks the camera target (pan/zoom/level); ``revision``
        # tracks content (model/layout/source). They invalidate different work.
        self.generation = 0
        self.revision = None
        self._camera_key = None
        self._jobs = {}
        from prism.resource_budget import shared_cache_budget
        self._budget = shared_cache_budget()
        self._budget.register(self, category='canvas_tiles', usage='budget_bytes',
                              trim='_trim_cache_to', priority=50)

    def budget_bytes(self):
        # self.bytes includes the single active incremental buffer.
        return self.bytes + sum(state[1] for state in self.contexts.values())

    def _trim_cache_to(self, target_bytes):
        while self.contexts and self.budget_bytes() > target_bytes:
            self.contexts.popitem(last=False)
        # Keep current cover and the in-progress buffer. The shared budget
        # reports pinned excess rather than dropping visible pictures.
        visible = self.view.isVisible()
        for key in list(self.tiles):
            if self.budget_bytes() <= target_bytes:
                break
            if visible and (key in self.protected or key in self.visible):
                continue
            image, _born, old, _revision = self.tiles.pop(key)
            self.bytes -= image.sizeInBytes() + (old.sizeInBytes() if old else 0)
            self.stale.discard(key)

    def switch_context(self, context):
        """Reuse a canvas's last pixels while its visible details refresh."""
        if context == self.context:
            return
        self.timer.stop()
        self._cancel_jobs()
        if self.tiles:
            self.contexts[self.context] = (self.tiles, self.bytes, self.level)
        self.tiles, self.bytes, self.level = self.contexts.pop(context, (OrderedDict(), 0, None))
        self.context = context
        # Cached pixels give immediate coverage, but edits and newly decoded
        # previews must still refresh them when this canvas is revisited.
        self.stale = set(self.tiles)
        self.pending.clear()
        self.visible.clear()
        self.protected.clear()
        self._trim_contexts()
        self._budget.changed(self)

    def _trim_contexts(self):
        total = self.bytes + sum(state[1] for state in self.contexts.values())
        while total > self.BUDGET and self.contexts:
            _context, state = self.contexts.popitem(last=False)
            total -= state[1]

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
        self._cancel_jobs()
        self.timer.stop()
        self.tiles.clear()
        self.stale.clear()
        self.pending.clear()
        self.bytes = 0
        self.level = None
        self.protected.clear()
        self.contexts.clear()
        self.context = None
        self.revision = None
        self._camera_key = None

    def _cancel_jobs(self, regions=None):
        for key in list(self._jobs):
            if not regions or any(self.rect(key).intersects(r) for r in regions):
                image, iterator, _revision, _generation = self._jobs.pop(key)
                iterator.close()
                self.bytes -= image.sizeInBytes()

    def invalidate(self, regions=None):
        self._cancel_jobs(None if regions and any(r.isEmpty() for r in regions) else regions)
        if not regions or any(r.isEmpty() for r in regions):
            self.stale.update(self.tiles)
            self.pending.clear()
            return
        for key in self.tiles:
            if any(self.rect(key).intersects(r) for r in regions):
                self.stale.add(key)
        self.view.viewport().update()

    def content_revision(self):
        """Content version that invalidates tiles on model/layout/source change.

        The tile key stays geometric ``(level, x, y)`` because parent/child
        cover and fallback math depends on it. Each tile records the revision
        it was built against; a tile whose revision lags the current one is
        treated as absent so stale pixels can never survive a model or source
        change. Scene views expose a layout+bounds revision; the material
        canvas exposes its model identity, card count and packed bounds.
        """
        view = self.view
        # 主 View 上 `scene` 是属性（PrismGraphicsScene），AssetCanvas 上是
        # QGraphicsView.scene() 方法。两种都要兼容，否则主画布 paint 会崩。
        scene = getattr(view, 'scene', None)
        if callable(scene):
            scene = scene()
        manager = getattr(scene, '_viewport_items', None)
        layout = getattr(manager, 'layout_revision', 0)
        bounds = getattr(scene, '_bounds_revision', 0)
        model = getattr(view, '_model', None)
        cards = getattr(view, 'cards', None)
        content = getattr(view, '_content_bounds', None)
        if callable(content):
            # 主 View 上 `_content_bounds` 是方法；AssetCanvas 上是 QRectF 属性。
            content = content()
        return (layout, bounds, getattr(view, '_render_layout_revision', 0),
                id(model) if model is not None else 0,
                len(cards) if cards is not None else 0,
                (content.x(), content.y(), content.width(), content.height()) if content is not None else 0)

    def _fresh(self, key):
        """True when the tile exists and was built for the current content."""
        value = self.tiles.get(key)
        return value is not None and value[3] == self.revision

    @measured('canvas.tile_build', 8)
    def _build(self, key):
        image = QtGui.QImage(self.SIDE, self.SIDE,
                             QtGui.QImage.Format.Format_ARGB32_Premultiplied)
        image.fill(self.view.backgroundBrush().color())
        paint_evidence.observe_background(self.view, image, self.view.backgroundBrush().color())
        children = [(key[0]+1, key[1]*2+x, key[2]*2+y) for y in range(2) for x in range(2)]
        if key not in self.stale and all(self._fresh(k) and k not in self.stale for k in children):
            painter = QtGui.QPainter(image)
            with paint_evidence.resource_scope(self.view, image):
                for index, child in enumerate(children):
                    target = QtCore.QRectF(index%2*self.SIDE/2, index//2*self.SIDE/2,
                                          self.SIDE/2, self.SIDE/2)
                    painter.drawImage(target, self.tiles[child][0])
                    paint_evidence.observe_image(self.view, painter, target, self.tiles[child][0],
                                                 category='tile', render_revision=self.tiles[child][3])
            painter.end()
            paint_evidence.generated(self.view, image, complete=True)
        else:
            self.render(image, QtCore.QRectF(image.rect()), self.rect(key))
        self._install(key, image)

    def _install(self, key, image):
        previous = self.tiles.pop(key, None)
        if previous:
            self.bytes -= previous[0].sizeInBytes() + (previous[2].sizeInBytes() if previous[2] else 0)
        # Retain the previous bitmap only for a bounded transition.
        old = previous[0] if previous and key in self.stale else None
        self.tiles[key] = (image, time.monotonic(), old, self.revision)
        self.bytes += image.sizeInBytes() + (old.sizeInBytes() if old else 0)
        self.stale.discard(key)
        self.builds += 1
        while self.bytes > self.BUDGET and self.tiles:
            # Preserve a bounded whole-view cover during deep zoom. A return
            # to far view must not synchronously rebuild an evicted coarse
            # region while newer detailed tiles occupy the LRU cache.
            anchor_count = min(4, self.BUDGET // (self.SIDE*self.SIDE*4) // 4)
            anchors = set(sorted(self.tiles, key=lambda key: key[0])[:anchor_count])
            victim = next((k for k in self.tiles if k not in self.protected and k not in anchors), None)
            if victim is None:
                victim = next((k for k in self.tiles if k in self.visible
                               and (k[0]-1, k[1]//2, k[2]//2) in self.tiles), next(iter(self.tiles)))
            value = self.tiles.pop(victim)
            self.bytes -= value[0].sizeInBytes() + (value[2].sizeInBytes() if value[2] else 0)
            self.stale.discard(victim)
        self._trim_contexts()
        self._budget.changed(self)
        paint_evidence.installed(self.view, image, resource_kind='tile', tile_key=list(key),
                                 render_revision=self.revision, retains_old=old is not None)

    def _job_maker(self):
        """Return the factory that yields bounded render slices, if any.

        ``create_display_tile_job`` on the view is the material canvas
        protocol; ``job_factory`` is the renderer protocol used by the main
        canvas overview. Both return a generator that yields between
        bounded slices, or ``None`` when the content cannot be sliced.
        """
        if self._job_factory is not None:
            return self._job_factory
        return getattr(self.view, 'create_display_tile_job', None)

    def _begin_job(self, key):
        """Open the buffer for ``key`` without rendering into it yet.

        The generator only runs when it is advanced, so opening a job is
        cheap and safe to do from a paint: the timer then fills the buffer
        inside the frame budget.
        """
        create = self._job_maker()
        if create is None or self._jobs:
            return False
        image = QtGui.QImage(self.SIDE, self.SIDE,
                             QtGui.QImage.Format.Format_ARGB32_Premultiplied)
        image.fill(QtCore.Qt.GlobalColor.transparent)
        iterator = create(image, QtCore.QRectF(image.rect()), self.rect(key))
        if iterator is None:
            return False
        self._jobs[key] = image, iterator, self.revision, self.generation
        self.bytes += image.sizeInBytes()
        self._budget.changed(self)
        return True

    def _advance_job(self, key):
        """Run one bounded slice of the open job; True when it is installed."""
        image, iterator, revision, generation = self._jobs[key]
        if revision != self.revision or generation != self.generation:
            # Stale work must give way: the camera moved or the content
            # changed, so the buffer is dropped instead of published.
            self._cancel_jobs()
            return False
        try:
            next(iterator)
        except StopIteration:
            self._jobs.pop(key)
            self.bytes -= image.sizeInBytes()
            # Partial buffers are transparent so existing cover remains visible.
            # The final tile has the exact background used by synchronous Qt.
            painter = QtGui.QPainter(image)
            painter.setCompositionMode(QtGui.QPainter.CompositionMode.CompositionMode_DestinationOver)
            painter.fillRect(image.rect(), self.view.backgroundBrush())
            painter.end()
            paint_evidence.observe_background(self.view, image, self.view.backgroundBrush())
            self._install(key, image)
            return True
        except BaseException:
            self._cancel_jobs()
            raise
        return False

    def _step_build(self, key):
        """Resume one bounded GUI slice for ``key``.

        A sliced renderer consumes one slice per call and reports False
        until the tile is complete, so a single tile may span several GUI
        turns. The budget and the cancellation checks then apply to every
        slice instead of only between whole tiles.
        """
        if key in self._jobs:
            return self._advance_job(key)
        create = self._job_maker()
        if create is None:
            self._build(key)
            return True
        children = [(key[0]+1, key[1]*2+x, key[2]*2+y) for y in range(2) for x in range(2)]
        if key not in self.stale and all(self._fresh(child) and child not in self.stale for child in children):
            self._build(key)  # Four existing Qt images need no sliced rerender.
            return True
        if self._jobs:
            return False  # Only one extra tile buffer may be resident.
        if not self._begin_job(key):
            self._build(key)
            return True
        return self._advance_job(key)

    def _has_cover(self, key):
        if self._fresh(key):
            return True
        ancestor = key
        for _ in range(8):
            ancestor = (ancestor[0]-1, ancestor[1]//2, ancestor[2]//2)
            if self._fresh(ancestor):
                return True
        children = [(key[0]+1, key[1]*2+x, key[2]*2+y)
                    for x in range(2) for y in range(2)]
        # Packed asset views have finite image bounds. Empty quadrants outside
        # them need only the background, not a synchronous whole-library draw.
        bounds = getattr(self.view, '_content_bounds', None)
        if isinstance(bounds, (QtCore.QRectF, QtCore.QRect)) and not bounds.isEmpty():
            children = [child for child in children if self.rect(child).intersects(bounds)]
        return all(self._fresh(child) for child in children)

    def _work(self):
        start = time.monotonic()
        generation = self.generation
        waiting = False
        built = 0
        # Every path that consumes this turn's budget counts as one step:
        # finishing a tile and advancing one render slice both do. Counting
        # only finished tiles let a sliced tile span one GUI turn without
        # limit, because a slice reports False until its tile completes.
        steps = 0
        while self.pending and steps < self.MAX_SLICE_STEPS_PER_FRAME \
                and built < self.MAX_BUILDS_PER_FRAME \
                and time.monotonic()-start < self.FRAME_BUDGET:
            if self.generation != generation:
                break  # camera/level changed since the queue was built
            ready = getattr(self.view, 'can_build_display_tile', None)
            key = next(iter(self._jobs), None)
            if key is None:
                key = next((key for key in self.pending if ready is None
                        or ready(key) or key in self.visible and not self._has_cover(key)), None)
            if key is None:
                waiting = True
                break
            steps += 1
            if not self._fresh(key) or key in self.stale:
                # A sliced renderer consumes one bounded slice per call and
                # reports False until the tile is complete. The turn keeps
                # its budget and advances the same job again, bounded by
                # ``MAX_SLICE_STEPS_PER_FRAME`` and the frame budget, so one
                # frame can never paint more than one renderer's worth of
                # records no matter how cheap a slice is.
                if not self._step_build(key):
                    continue
                built += 1
            self.pending.pop(key, None)
        self.last_frame_steps, self.last_frame_builds = steps, built
        now = time.monotonic()
        fading = False
        for key, (image, born, old, _revision) in list(self.tiles.items()):
            if now-born >= self.FADE and old is not None:
                self.tiles[key] = (image, born, None, _revision)
                self.bytes -= old.sizeInBytes()
            elif now-born < self.FADE:
                fading = True
        self.view.viewport().update()
        if (not self.pending and not self._jobs or waiting) and not fading:
            self.timer.stop()

    def _fallback(self, painter, key, target):
        ancestor = key
        for _ in range(8):
            ancestor = (ancestor[0]-1, ancestor[1]//2, ancestor[2]//2)
            value = self.tiles.get(ancestor)
            if value is not None and value[3] == self.revision:
                world = self.rect(ancestor)
                local = self.rect(key)
                factor = self.SIDE/world.width()
                source = QtCore.QRectF((local.x()-world.x())*factor,
                                      (local.y()-world.y())*factor,
                                      local.width()*factor, local.height()*factor)
                painter.drawImage(target, value[0], source)
                paint_evidence.observe_image(self.view, painter, target, value[0], source,
                    category='ancestor', stale=ancestor in self.stale, render_revision=value[3],
                    force_unknown_geometry=not paint_evidence.axis_aligned(self.view.viewportTransform()))
                return True
        # Zooming out can reuse finer blocks even before a parent exists.
        painted = False
        world = self.rect(key)
        for child, value in list(self.tiles.items()):
            if child[0] == key[0]+1 and world.contains(self.rect(child)) and value[3] == self.revision:
                part = self.rect(child)
                destination = QtCore.QRectF(target.x()+(part.x()-world.x())/world.width()*target.width(),
                                            target.y()+(part.y()-world.y())/world.height()*target.height(),
                                            target.width()/2, target.height()/2)
                painter.drawImage(destination, value[0])
                paint_evidence.observe_image(self.view, painter, destination, value[0],
                    category='child', stale=child in self.stale, render_revision=value[3],
                    force_unknown_geometry=not paint_evidence.axis_aligned(self.view.viewportTransform()))
                painted = True
        return painted

    def _start_cover(self, coarse):
        """Install the coarse cover that this viewport is missing.

        Only a renderer that cannot slice is served here. Opening a sliced
        buffer inside a paint would leave an unfinished buffer resident, and
        the next camera change would have to drop it again while the paint
        immediately opened its replacement; the queue already lists the
        coarse keys and the timer starts on them below, so the sliced path
        gets the same cover one bounded step later.

        A renderer without slicing cannot be handed the cover in pieces, so
        this paint installs the whole coarse set. It is already bounded --
        it holds one parent per uncovered visible tile, so its size tracks
        the viewport's tile count and not the library -- and deferring it
        leaves a cold viewport black until the timer catches up, which is
        the regression this branch guards against.
        """
        if self._jobs or self._job_maker() is not None:
            return  # Sliced cover is queued and built by the timer.
        for key in coarse:
            if self._fresh(key) and key not in self.stale:
                continue
            self._build(key)

    @staticmethod
    def _view_scale(view):
        """Device pixels per scene unit, rotation aware.

        ``m11`` alone underestimates a rotated or skewed view, so the tile
        level would be coarser than the screen can show. This is the same
        measure ``lod.shown_pixels`` uses for the image level, so the tile
        grid and the decoded images stay consistent.
        """
        transform = view.transform()
        scale = max(math.hypot(transform.m11(), transform.m12()),
                    math.hypot(transform.m21(), transform.m22()))
        return scale*view.viewport().devicePixelRatioF()

    def paint(self, painter):
        view = self.view
        prepare = getattr(view, 'prepare_display_tiles', None)
        if prepare is not None:
            prepare()
        revision = self.content_revision()
        if revision != self.revision:
            # Model replaced or layout/source changed since these tiles were
            # built. Mark every cached pixel stale so no old content survives.
            self._cancel_jobs()
            self.revision = revision
            self.stale.update(self.tiles)
            self.pending.clear()
        rect = view.mapToScene(view.viewport().rect()).boundingRect()
        scale = self._view_scale(view)
        wanted = math.ceil(math.log2(max(scale, 1e-12)))
        if self.level is None or scale/2.0**self.level < .42 or scale/2.0**self.level > 1.05:
            self.level = wanted
        camera = (rect.x(), rect.y(), rect.width(), rect.height(), scale, self.level)
        if camera != self._camera_key:
            self._cancel_jobs()
            self._camera_key = camera
            self.generation += 1
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
        coarse = list(dict.fromkeys((k[0]-1, k[1]//2, k[2]//2) for k in missing))
        # A paint may never build the whole coarse cover. Building every
        # coarse tile here is what turned one cold viewport into a multi
        # second stall; the cover is now started here and finished by the
        # timer inside the frame budget.
        self._start_cover(coarse)
        self.coverage_missing = sum(not self._has_cover(key) for key in keys)
        if missing and time.monotonic()-self._coverage_log_time >= 1:
            from prism.performance_trace import record
            record('render_coverage', surface=type(view).__name__, missing_before=len(missing),
                   missing_after=self.coverage_missing, visible_tiles=len(keys),
                   tile_bytes=self.bytes, scale=scale, level=self.level)
            self._coverage_log_time = time.monotonic()
        # The focal point (mouse) ranks first, then the visible set by
        # distance, then a bounded ring of neighbours for panning.
        focus = view.mapToScene(getattr(view, '_detail_focus', view.viewport().rect().center()))
        ordered = sorted(keys, key=lambda k: (self.rect(k).center()-focus).manhattanLength())
        neighbours = self.keys(rect.adjusted(-self.SIDE/2.0**self.level,
                                             -self.SIDE/2.0**self.level,
                                             self.SIDE/2.0**self.level,
                                             self.SIDE/2.0**self.level), self.level)
        # Drop requests from previous cameras, rather than accumulating work.
        self.pending = OrderedDict((k, None) for k in (coarse+parents+ordered+neighbours)
                                   if not self._fresh(k) or k in self.stale)
        painter.fillRect(view.viewport().rect(), view.backgroundBrush())
        painter.setRenderHint(QtGui.QPainter.RenderHint.SmoothPixmapTransform)
        now = time.monotonic()
        for key in keys:
            target = view.viewportTransform().mapRect(self.rect(key))
            value = self.tiles.get(key)
            if value is not None and value[3] != self.revision:
                value = None  # stale content must not be painted
            covered = self._fallback(painter, key, target)
            if value is None:
                continue
            self.tiles.move_to_end(key)
            image, born, old, _revision = value
            alpha = min(1., (now-born)/self.FADE)
            if old is not None:
                painter.drawImage(target, old)
                paint_evidence.observe_image(view, painter, target, old, category='old_tile',
                    stale=True, render_revision=None,
                    force_unknown_geometry=not paint_evidence.axis_aligned(view.viewportTransform()))
                covered = True
            painter.save()
            painter.setOpacity(alpha if covered else 1.)
            painter.drawImage(target, image)
            paint_evidence.observe_image(view, painter, target, image, category='tile',
                stale=key in self.stale, render_revision=_revision,
                force_unknown_geometry=not paint_evidence.axis_aligned(view.viewportTransform()))
            painter.restore()
        # Publish real pixels already drawn by the bounded job; transparent
        # areas leave the last full cover in place instead of erasing it.
        for key, (image, _iterator, revision, _generation) in self._jobs.items():
            if revision == self.revision:
                target = view.viewportTransform().mapRect(self.rect(key))
                painter.drawImage(target, image)
                paint_evidence.observe_image(view, painter, target, image, category='incremental',
                    render_revision=revision,
                    force_unknown_geometry=not paint_evidence.axis_aligned(view.viewportTransform()))
        if self.pending or self._jobs or any(now-v[1] < self.FADE for v in self.tiles.values()):
            if not self.timer.isActive():
                self.timer.start()
