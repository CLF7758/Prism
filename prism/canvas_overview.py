"""Viewport-sized far LOD cache. The scene still owns editing and export."""
import math
from PyQt6 import QtCore, QtGui

MIN_ITEMS = 2000
MIN_VISIBLE_ITEMS = 128
MAX_SCALE = 0.02
MAX_CACHE_SIDE = 2048


# shortcut: one buffered overview frame, use tiled caches when cold rebuilds dominate.
class CanvasOverviewRenderer(QtCore.QObject):
    def __init__(self, view):
        super().__init__(view)
        self.view = view
        self.frame = None
        from prism.canvas_tiles import CanvasTiles
        self.tiles = CanvasTiles(view, self._render)
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
                size = item._image_size
                if not lod.base_preview_ready(item, max(size.width(), size.height())*item.scale()*scale, view):
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
        from prism.item_lifecycle import is_pixmap_item
        view = self.view
        if not view.isVisible():
            return
        rect = view.mapToScene(view.viewport().rect()).boundingRect()
        scale = view.get_scale()*view.viewport().devicePixelRatioF()
        for item in view.scene.items(rect, QtCore.Qt.ItemSelectionMode.IntersectsItemBoundingRect):
            if not is_pixmap_item(item):
                continue
            size = item._image_size
            pixels = max(size.width(), size.height())*item.scale()*scale
            if pixels > lod.OVERVIEW_MAX_PIXELS:
                lod.base_preview_ready(item, pixels, view)
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
        self._render(image, QtCore.QRectF(image.rect()), stored_rect)
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
