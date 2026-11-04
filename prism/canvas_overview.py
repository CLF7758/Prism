"""Viewport-sized far LOD cache. The scene still owns editing and export."""
import math
import time
from PyQt6 import QtCore, QtGui

MIN_ITEMS = 2000
MIN_VISIBLE_ITEMS = 128
MAX_SCALE = 0.02
MAX_CACHE_SIDE = 2048


# shortcut: one buffered overview frame, use tiled caches when cold rebuilds dominate.
class CanvasOverviewRenderer(QtCore.QObject):
    # One GUI slice renders at most this many device rows of the scene.
    BAND_ROWS = 64
    # One GUI slice paints at most this many records (estimated).
    RECORDS_PER_BAND = 48
    # A region with at most this many records renders in a single Qt call.
    ATOMIC_RECORDS = 64
    # Upper bound on the slices one render is cut into.
    MAX_BANDS = 32
    # Wall-clock budget of one GUI turn of frame work.
    FRAME_BUDGET = .004

    def __init__(self, view):
        super().__init__(view)
        self.view = view
        self.frame = None
        from prism.canvas_tiles import CanvasTiles
        self.tiles = CanvasTiles(view, self._render,
                                 job_factory=self.create_display_tile_job)
        self.dirty = QtCore.QRectF()
        view.scene.changed.connect(self.invalidate)
        from prism.lod import cache
        cache().changed.connect(self._display_ready)
        self.display_timer = QtCore.QTimer(self)
        self.display_timer.setSingleShot(True)
        self.display_timer.setInterval(100)
        self.display_timer.timeout.connect(self._publish_details)
        self.display_dirty = QtCore.QRectF()
        self.frame_layout_revision = None
        # Camera-target generation: pan/wheel changes it so a detail pass
        # started for an older camera can be recognised as stale.
        self.generation = 0
        # The overview frame may be rendered in slices. The open job holds
        # the buffer and its generator; the timer finishes it in bounded
        # turns so no single paint renders the whole scene.
        self._frame_job = None
        self._frame_timer = QtCore.QTimer(self)
        self._frame_timer.setObjectName('canvas.overview_frame')
        self._frame_timer.setInterval(16)
        self._frame_timer.timeout.connect(self._frame_work)

    @QtCore.pyqtSlot(object)
    def _display_ready(self, item):
        from prism.item_lifecycle import is_deleted
        if (self.view.isVisible() and (self.frame is not None or self.tiles.tiles)
                and not is_deleted(item) and item.scene() is self.view.scene):
            self.display_dirty = self.display_dirty.united(item.sceneBoundingRect())
            if not self.display_timer.isActive():
                self.display_timer.start()

    def _publish_details(self):
        from prism import lod
        from prism.item_lifecycle import is_pixmap_item
        view = self.view
        if not view.isVisible():
            self.display_dirty = QtCore.QRectF()
            return
        if (self.frame is None and not self.tiles.tiles) or self.display_dirty.isEmpty():
            self.display_dirty = QtCore.QRectF()
            return
        if self.frame is None and self.tiles.tiles:
            # Publish bounded tile regions as pixels arrive. Waiting for every
            # overlapping image can never finish under a finite image budget.
            region = QtCore.QRectF(self.display_dirty)
            self.display_dirty = QtCore.QRectF()
            self.tiles.invalidate([region])
            self.queue_visible_details()
            view.viewport().update()
            return
        rect = view.mapToScene(view.viewport().rect()).boundingRect()
        scale = view.get_scale()*view.viewport().devicePixelRatioF()
        ready = not view.scene._interaction_in_progress
        if ready:
            for item in view.scene.items(rect, QtCore.Qt.ItemSelectionMode.IntersectsItemBoundingRect):
                if not item.isVisible() or not is_pixmap_item(item) or getattr(item, '_display_decode_failed', False):
                    continue
                pixels = lod.projected_pixels(item, scale, item.scale())
                if not lod.base_preview_ready(item, pixels, view):
                    ready = False
                    break
        if not ready:
            self.queue_visible_details()
            self.display_timer.start()
            return
        ready_rect = QtCore.QRectF(self.display_dirty)
        self.display_dirty = QtCore.QRectF()
        self.invalidate([ready_rect])
        view.viewport().update()

    def queue_visible_details(self):
        from prism import lod
        from prism.item_lifecycle import is_pixmap_item, is_deleted
        view = self.view
        if not view.isVisible():
            return
        rect = view.mapToScene(view.viewport().rect()).boundingRect()
        scale = view.get_scale()*view.viewport().devicePixelRatioF()
        revision = getattr(view.scene._viewport_items, 'layout_revision', 0)
        key = (rect.x(), rect.y(), rect.width(), rect.height(), scale, revision)
        if getattr(self, '_details_key', None) != key:
            self._details_key = key
            self.generation += 1  # pan/wheel changed the target; retire prior passes
            self._details_items = [item for item in view.scene.items(rect, QtCore.Qt.ItemSelectionMode.IntersectsItemBoundingRect)
                                   if item.isVisible() and is_pixmap_item(item)]
            self._details_cursor = 0
        prefix_ready = True
        for index in range(self._details_cursor, len(self._details_items)):
            item = self._details_items[index]
            if is_deleted(item) or item.scene() is not view.scene or not item.isVisible():
                self._details_key = None
                return
            pixels = lod.projected_pixels(item, scale, item.scale())
            ready = lod.base_preview_ready(item, pixels, view)
            if ready and prefix_ready:
                self._details_cursor = index+1
            elif not ready:
                prefix_ready = False
                app = QtCore.QCoreApplication.instance()
                decoder = getattr(app, '_prism_display_decoder', None)
                if decoder is not None and len(decoder.requests) >= decoder.max_pending:
                    break

    def invalidate(self, region=None):
        manager = getattr(self.view.scene, '_viewport_items', None)
        revision = (getattr(manager, 'layout_revision', None), getattr(self.view.scene, '_bounds_revision', 0))
        if self.display_dirty.isEmpty() or revision != self.frame_layout_revision:
            self.tiles.invalidate(region)
        if not region or any(rect.isEmpty() for rect in region):
            self.frame = None
            self.dirty = QtCore.QRectF()
            self._cancel_frame_job()
        elif self.frame is not None:
            for rect in region:
                self.dirty = self.dirty.united(rect.intersected(self.frame[1]))

    def _render(self, image, target, source):
        view = self.view
        painter = QtGui.QPainter(image)
        painter.setRenderHints(view.renderHints())
        painter.setClipRect(target)
        painter.fillRect(target, view.backgroundBrush())
        previous = getattr(view.scene, '_overview_recording_owner', None)
        view.scene._overview_recording_owner = view
        try:
            # Qt preserves stacking, visibility, transforms, opacity and effects.
            view.scene.render(painter, target, source,
                              QtCore.Qt.AspectRatioMode.IgnoreAspectRatio)
        finally:
            view.scene._overview_recording_owner = previous
            painter.end()

    def create_display_tile_job(self, image, target, source):
        """Yield between bounded slices of the main canvas scene.

        The main canvas paints through Qt scene items, so the only way to
        keep one GUI turn short is to render the scene in slices. Each
        slice is an ordinary ``QGraphicsScene.render`` call over one
        horizontal band of the source rectangle, so Qt stacking, clipping,
        opacity, graphics effects and the record layer keep their exact
        semantics, and the finished buffer holds the same pixels an
        unsliced render would have produced.

        The generator yields between bands, so the tile cache can drop a
        stale job (camera or content changed) at any band boundary.
        """
        return self._render_bands(image, target, source)

    def _render_bands(self, image, target, source):
        view = self.view
        painter = QtGui.QPainter(image)
        try:
            painter.setRenderHints(view.renderHints())
            painter.setClipRect(target)
            painter.fillRect(target, view.backgroundBrush())
            previous = getattr(view.scene, '_overview_recording_owner', None)
            view.scene._overview_recording_owner = view
            try:
                for band_target, band_source in self._bands(target, source):
                    started = time.perf_counter()
                    painter.setClipRect(band_target)
                    view.scene.render(painter, band_target, band_source,
                                      QtCore.Qt.AspectRatioMode.IgnoreAspectRatio)
                    if time.perf_counter()-started >= self.FRAME_BUDGET:
                        yield
            finally:
                view.scene._overview_recording_owner = previous
        finally:
            painter.end()

    def _bands(self, target, source):
        """Split a render into proportional horizontal bands."""
        count = self._band_count(target, source)
        if count <= 1:
            yield target, source
            return
        height = target.height()/count
        world = source.height()/count
        for index in range(count):
            top = index*height
            bottom = (index+1)*height
            yield (QtCore.QRectF(target.x(), target.y()+top,
                                  target.width(), bottom-top),
                   QtCore.QRectF(source.x(), source.y()+index*world,
                                 source.width(), world))

    def _band_count(self, target, source):
        """Number of slices one render is cut into.

        The count follows the number of records inside the region, so a
        dense area is cut into more slices than a sparse one and every
        slice stays inside the frame budget. The count only decides where
        the work is cut; the pixels never depend on it.
        """
        records = self._estimate_records(source)
        if records <= self.ATOMIC_RECORDS:
            return 1
        count = max(1, math.ceil(records/self.RECORDS_PER_BAND))
        count = max(count, math.ceil(max(1.0, target.height())/self.BAND_ROWS))
        return max(1, min(self.MAX_BANDS, count))

    def _estimate_records(self, source):
        """Cheap estimate of the records inside a world rectangle.

        The spatial index is bucketed, so the bucket sizes give the record
        count of the covered cells without walking every record.
        """
        manager = getattr(self.view.scene, '_viewport_items', None)
        index = getattr(manager, 'index', None)
        if index is None:
            return 0
        try:
            cells = index.keys(source)
        except (AttributeError, TypeError, ValueError):
            return 0
        if cells is None:
            return len(index.rects)
        buckets = index.buckets
        total = len(index.overflow)
        for cell in cells:
            total += len(buckets.get(cell, ()))
        # The buckets cover whole cells; scale by the covered area so a
        # tile inside one cell is not counted as the whole cell.
        cell = max(1, int(index.CELL)*int(index.CELL))
        covered = max(0.0, source.width()*source.height())
        return int(total*min(1.0, covered/cell))

    def _cancel_frame_job(self):
        job = self._frame_job
        self._frame_job = None
        if job is not None:
            try:
                job[1].close()
            except Exception:
                pass

    def _frame_work(self):
        """Finish the open frame job inside the frame budget."""
        started = time.perf_counter()
        while self._frame_job is not None \
                and time.perf_counter()-started < self.FRAME_BUDGET:
            _image, iterator = self._frame_job
            try:
                next(iterator)
            except StopIteration:
                self._frame_job = None
                self._frame_timer.stop()
                self.view.viewport().update()
                return
            except BaseException:
                self._frame_job = None
                self._frame_timer.stop()
                raise
        if self._frame_job is None:
            self._frame_timer.stop()

    def _start_frame(self, image, stored_rect):
        """Render the overview frame in bounded slices.

        A sparse frame finishes inside the call, so the diagnostic frame
        stays complete and costs one Qt call. A dense frame is finished by
        the timer in bounded slices: the buffer is filled band by band and
        the caller is never blocked by a whole scene render.
        """
        target = QtCore.QRectF(image.rect())
        iterator = self._render_bands(image, target, stored_rect)
        if self._band_count(target, stored_rect) <= 1:
            for _ in iterator:
                pass
            return
        self._cancel_frame_job()
        self._frame_job = (image, iterator)
        if not self._frame_timer.isActive():
            self._frame_timer.start()

    def _refresh_dirty(self, image, rect):
        if self.dirty.isEmpty():
            return
        manager = getattr(self.view.scene, '_viewport_items', None)
        revision = (getattr(manager, 'layout_revision', None), getattr(self.view.scene, '_bounds_revision', 0))
        if not self.display_dirty.isEmpty() and revision == self.frame_layout_revision:
            return
        sx, sy = image.width()/rect.width(), image.height()/rect.height()
        target = QtCore.QRectF((self.dirty.x()-rect.x())*sx,
                              (self.dirty.y()-rect.y())*sy,
                              self.dirty.width()*sx, self.dirty.height()*sy)
        target = target.toAlignedRect().adjusted(-2, -2, 2, 2).intersected(image.rect())
        self.dirty = QtCore.QRectF()
        if target.isEmpty():
            return
        source = QtCore.QRectF(rect.x()+target.x()/sx, rect.y()+target.y()/sy,
                              target.width()/sx, target.height()/sy)
        self._render(image, QtCore.QRectF(target), source)
        self.frame_layout_revision = revision

    def eligible(self):
        view = self.view
        transform = view.transform()
        if (view.scene._item_count < MIN_ITEMS or transform.m11() <= 0
                or transform.m12() != 0 or transform.m21() != 0
                or not math.isclose(transform.m22(), transform.m11())
                or view.viewport().width() <= 0 or view.viewport().height() <= 0
                or view.scene.crop_item or view.scene.edit_item):
            return False
        if transform.m11() <= MAX_SCALE:
            return True
        rect = view.mapToScene(view.viewport().rect()).boundingRect()
        if self.frame is not None:
            _, stored_rect, scale = self.frame
            if (view.scene._interaction_in_progress
                    or (stored_rect.contains(rect) and 0.5 <= transform.m11()/scale <= 1.25)):
                return True
        # Small source images can be densely packed even at a larger view scale.
        return len(view.scene.items(rect, QtCore.Qt.ItemSelectionMode.IntersectsItemBoundingRect)) >= MIN_VISIBLE_ITEMS

    def image_for_view(self):
        view = self.view
        rect = view.mapToScene(view.viewport().rect()).boundingRect()
        scale = view.get_scale()
        if self.frame is not None:
            image, stored_rect, stored_scale = self.frame
            if (view.scene._interaction_in_progress
                    or (stored_rect.contains(rect) and 0.5 <= scale/stored_scale <= 1.25)):
                self._refresh_dirty(image, stored_rect)
                return image, stored_rect
        stored_rect = rect.adjusted(-rect.width()/4, -rect.height()/4,
                                    rect.width()/4, rect.height()/4)
        dpr = view.viewport().devicePixelRatioF()
        width = max(1, math.ceil(stored_rect.width()*scale*dpr))
        height = max(1, math.ceil(stored_rect.height()*scale*dpr))
        factor = min(1, MAX_CACHE_SIDE/max(width, height))
        image = QtGui.QImage(max(1, int(width*factor)), max(1, int(height*factor)),
                             QtGui.QImage.Format.Format_ARGB32_Premultiplied)
        image.fill(view.backgroundBrush().color())
        self._start_frame(image, stored_rect)
        self.dirty = QtCore.QRectF()
        self.frame = image, stored_rect, scale
        self.frame_layout_revision = (getattr(getattr(view.scene, '_viewport_items', None), 'layout_revision', None),
                                      getattr(view.scene, '_bounds_revision', 0))
        return image, stored_rect

    def paint(self, painter):
        self.tiles.paint(painter)
        self.frame_layout_revision = (getattr(self.view.scene._viewport_items, 'layout_revision', None),
                                      getattr(self.view.scene, '_bounds_revision', 0))
        rect = QtCore.QRectF()
        for item in self.view.scene.selectedItems():
            if item.isVisible():
                rect = rect.united(item.sceneBoundingRect())
        if not rect.isEmpty():
            rect = self.view.viewportTransform().mapRect(rect)
            painter.setPen(QtGui.QPen(QtGui.QColor(240, 240, 242)))
            painter.setBrush(QtCore.Qt.BrushStyle.NoBrush)
            painter.drawRect(rect)
            painter.setBrush(QtGui.QColor(240, 240, 242))
            for point in (rect.topLeft(), rect.topRight(), rect.bottomLeft(), rect.bottomRight()):
                painter.drawEllipse(point, 4, 4)
