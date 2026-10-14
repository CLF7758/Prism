"""Recycle editable Qt images, while passive records keep their exact stacking."""
import bisect
import time
from PyQt6 import QtCore, QtGui, QtWidgets, sip
from prism.asset_spatial_index import AssetSpatialIndex
from prism.item_lifecycle import logical_item
from prism.items import PrismPixmapItem

MAX_EDIT_ITEMS = 512
MIN_EDIT_PIXELS = 16


class MaterializedImageItem(PrismPixmapItem):
    def paint(self, painter, option, widget=None):
        scene = self.scene()
        if scene is not None and getattr(scene, '_exporting_records', False):
            RecordLayer.draw_record(painter, self._asset_record, None, True)
        else:
            super().paint(painter, option, widget)

    def itemChange(self, change, value):
        result = super().itemChange(change, value)
        record = getattr(self, '_asset_record', None)
        if record is not None and record.scene() is not None:
            manager = record.scene()._viewport_items
            if not manager.reconciling:
                if change == self.GraphicsItemChange.ItemSelectedHasChanged:
                    record._selected = bool(value)
                    manager.selection(record, bool(value))
                manager.changed(record, True)
        return result

    def update(self, *args):
        super().update(*args)
        record = getattr(self, '_asset_record', None)
        if record is not None and record.scene() is not None:
            record.scene()._viewport_items.changed(record, True)


def materialize_image(record):
    item = MaterializedImageItem(QtGui.QImage())
    position, scale, rotation, flip = record.pos(), record.scale(), record.rotation(), record.flip()
    z, opacity, visible, selected = record.zValue(), record.opacity(), record.isVisible(), record.isSelected()
    item.__dict__ = record.__dict__
    record._graphics = item
    item.setFlags(record._flags)
    item.setAcceptHoverEvents(True)
    QtWidgets.QGraphicsItem.setPos(item, position)
    QtWidgets.QGraphicsItem.setTransform(item, QtGui.QTransform.fromScale(flip, 1))
    QtWidgets.QGraphicsItem.setScale(item, scale)
    QtWidgets.QGraphicsItem.setRotation(item, rotation)
    QtWidgets.QGraphicsItem.setZValue(item, z)
    QtWidgets.QGraphicsItem.setOpacity(item, opacity)
    QtWidgets.QGraphicsItem.setVisible(item, visible)
    QtWidgets.QGraphicsItem.setSelected(item, selected)
    if record._derived_pixmap is not None:
        QtWidgets.QGraphicsPixmapItem.setPixmap(item, record._derived_pixmap)
    item._sync_grayscale_effect()
    return item


class RecordLayer(QtWidgets.QGraphicsObject):
    _is_record_layer = True

    def __init__(self, manager, lower, upper):
        super().__init__()
        self.manager, self.lower, self.upper = manager, lower, upper
        self.setAcceptedMouseButtons(QtCore.Qt.MouseButton.NoButton)
        self.setFlag(self.GraphicsItemFlag.ItemUsesExtendedStyleOption)

    def boundingRect(self):
        return self.manager.extent

    def paint(self, painter, option, widget=None):
        manager = self.manager
        records, keys = manager.visible(option.exposedRect)
        first = bisect.bisect_right(keys, self.lower)
        last = bisect.bisect_left(keys, self.upper)
        owner = getattr(manager.scene, '_overview_recording_owner', None)
        if owner is None and widget is not None:
            owner = next((v for v in manager.scene.views() if v.viewport() is widget), None)
        exporting = owner is None and widget is None
        for record in records[first:last]:
            if record._graphics is not None or not record.isVisible():
                continue
            painter.save()
            try:
                painter.setWorldTransform(record.sceneTransform(), True)
                painter.setOpacity(painter.opacity()*record.opacity())
                self.draw_record(painter, record, owner, exporting)
            finally:
                painter.restore()

    @staticmethod
    def draw_record(painter, record, owner, exporting):
        from prism import image_decode, lod
        if exporting:
            # Export pixels bypass display LOD and release each decoded image.
            from prism.item_lifecycle import native_pixmap
            adjusted = native_pixmap(record) if record._preview_adjusted else None
            if adjusted is not None and not adjusted.isNull():
                decoded = adjusted.toImage(), record._image_size
            else:
                decoded = image_decode.decode(*image_decode.source_snapshot(record))
            if decoded is not None:
                image, _size = decoded
                if record.grayscale:
                    from prism.fileio.image import qt_grayscale_image
                    image = qt_grayscale_image(image)
                painter.drawImage(record.crop, image, record.crop)
            return
        pixels = lod.shown_pixels(record, painter)
        pixmap = lod.overview_pixmap(record) if pixels <= 8 else lod.display_pixmap(record, pixels, owner)
        if pixmap is None or pixmap.isNull():
            painter.fillRect(record.crop, QtGui.QColor(record.dominant_color or '#757B82'))
            return
        if record.grayscale:
            from prism.fileio.image import qt_grayscale_image
            pixmap = QtGui.QPixmap.fromImage(qt_grayscale_image(pixmap.toImage()))
        painter.setRenderHint(QtGui.QPainter.RenderHint.SmoothPixmapTransform, pixels > 8)
        painter.drawPixmap(record.crop, pixmap, lod.source_rect(record, pixmap))


class ViewportItems(QtCore.QObject):
    def __init__(self, scene):
        super().__init__(scene)
        self.scene = scene
        self.records = {}
        self.index = AssetSpatialIndex()
        self.materialized = set()
        self.selected = set()
        self.layers = []
        self.extent = QtCore.QRectF()
        self.reconciling = False
        self.generation = 0
        self.layout_revision = 0
        self._sync_key = None
        self._visibility = {}
        self._visible_cache = None
        self._layer_signature = None
        self.timer = QtCore.QTimer(self)
        self.timer.setSingleShot(True)
        self.timer.timeout.connect(self.refresh)

    def schedule(self):
        if not self.timer.isActive():
            self.timer.start(16)

    def add(self, record):
        if not self.records:
            # Virtual records have their own index; Qt's BSP must not index
            # the frequently recycled editors and overlapping record layers.
            self.scene.setItemIndexMethod(QtWidgets.QGraphicsScene.ItemIndexMethod.NoIndex)
        self.records[id(record)] = record
        self.layout_revision += 1
        record.attach(self.scene)
        self.index.update(record)
        self.selection(record, record.isSelected())
        self.changed(record, True)

    def remove(self, record):
        old = self.index.rects.get(record)
        self.recycle(record)
        self.records.pop(id(record), None)
        self.index.remove(record)
        self.selected.discard(record)
        self._visibility.pop(record, None)
        self.layout_revision += 1
        record._scene_ref = None
        self.generation += 1
        self._layer_signature = None
        if old:
            self.scene.update(old[0])
        self.schedule()

    def clear(self):
        self.timer.stop()
        self.reconciling = True
        try:
            for record in list(self.records.values()):
                self.recycle(record)
                record._scene_ref = None
                record._disposed = True
            for layer in self.layers:
                QtWidgets.QGraphicsScene.removeItem(self.scene, layer)
                sip.delete(layer)
        finally:
            self.reconciling = False
        self.records.clear()
        self.selected.clear()
        self.materialized.clear()
        self.layers.clear()
        self.index = AssetSpatialIndex()
        self._visibility.clear()
        self._sync_key = None
        self.layout_revision += 1
        self.extent = QtCore.QRectF()
        self._layer_signature = None
        self._visible_cache = None
        self.generation += 1

    def selection(self, record, selected):
        changed = (record in self.selected) != selected
        self.selected.add(record) if selected else self.selected.discard(record)
        if changed:
            self._sync_key = None
            self.schedule()

    def changed(self, record, geometry=False):
        if self.reconciling or id(record) not in self.records:
            return
        old = self.index.rects.get(record)
        changed = self.index.update(record) if geometry else False
        visibility = record.isVisible()
        if changed or self._visibility.get(record) != visibility:
            self.layout_revision += 1
        self._visibility[record] = visibility
        if changed:
            self.scene._invalidate_view_bounds()
        if self.extent != self.index.extent:
            for layer in self.layers:
                layer.prepareGeometryChange()
            self.extent = QtCore.QRectF(self.index.extent)
        self.generation += 1
        region = record.sceneBoundingRect()
        if old is not None:
            region = region.united(old[0])
        self.scene.update(region)
        self.schedule()

    def visible(self, rect):
        key = (rect.x(), rect.y(), rect.width(), rect.height(), self.generation)
        if self._visible_cache is None or self._visible_cache[0] != key:
            records = sorted(self.index.query(rect), key=self.stack_key)
            self._visible_cache = key, records, [self.stack_key(r) for r in records]
        return self._visible_cache[1:]

    def in_area(self, path, mode):
        result = set()
        bounding = mode in (QtCore.Qt.ItemSelectionMode.ContainsItemBoundingRect,
                            QtCore.Qt.ItemSelectionMode.IntersectsItemBoundingRect)
        contains = mode in (QtCore.Qt.ItemSelectionMode.ContainsItemShape,
                            QtCore.Qt.ItemSelectionMode.ContainsItemBoundingRect)
        for record in self.index.query(path.boundingRect()):
            if not record.isVisible():
                continue
            outline = QtGui.QPainterPath()
            if bounding:
                outline.addRect(record.sceneBoundingRect())
            else:
                outline = record.sceneTransform().map(record.shape())
            matches = path.contains(outline) if contains else path.intersects(outline)
            if matches:
                result.add(record)
        return result

    @staticmethod
    def stack_key(item):
        item = logical_item(item)
        return item.zValue(), getattr(item, '_sequence', 0)

    def materialize(self, record):
        if record._graphics is not None:
            return record._graphics
        previous = self.reconciling
        self.reconciling = True
        blocked = self.scene.blockSignals(True)
        try:
            item = materialize_image(record)
            if record.scene() is self.scene:
                QtWidgets.QGraphicsScene.addItem(self.scene, item)
            self.materialized.add(record)
            self.index.update(record)
            self._layer_signature = None
            self.generation += 1
            return item
        finally:
            self.scene.blockSignals(blocked)
            self.reconciling = previous

    def recycle(self, record):
        item = record._graphics
        if item is None:
            return
        record._position, record._scale = item.pos(), item.scale()
        record._rotation, record._flip, record._z = item.rotation(), item.flip(), item.zValue()
        record._opacity, record._visible, record._selected = item.opacity(), item.isVisible(), item.isSelected()
        if record._preview_adjusted or not record.has_original_source():
            record._derived_pixmap = QtWidgets.QGraphicsPixmapItem.pixmap(item)
        record._grayscale_cache = None
        record.release_source_blob()
        record._display_decode_request = None
        record._graphics = None
        self.materialized.discard(record)
        QtWidgets.QGraphicsScene.removeItem(self.scene, item)
        sip.delete(item)
        self.index.update(record) if id(record) in self.records else None
        self._layer_signature = None
        self.generation += 1

    def refresh(self):
        views = [view for view in self.scene.views() if view.isVisible()]
        if views:
            self.sync(views[0])
            views[0].viewport().update()

    def sync(self, view):
        if self.reconciling or not self.records:
            return
        rect = view.mapToScene(view.viewport().rect()).boundingRect()
        key = (rect.x(), rect.y(), rect.width(), rect.height(),
               view.transform().m11(), self.layout_revision, self.scene._interaction_in_progress, getattr(view, '_overview_active', False))
        if key == self._sync_key:
            self.rebuild_layers()
            return
        previous = self.reconciling
        self.reconciling = True
        blocked = self.scene.blockSignals(True)
        try:
            if not self.scene._interaction_in_progress:
                margin = 128/max(abs(view.transform().m11()), .000001)
                candidates = [r for r in self.index.query(rect.adjusted(-margin, -margin, margin, margin)) if r.isVisible()]
                detailed = [r for r in candidates if max(r.width, r.height)*r.scale()*abs(view.transform().m11()) >= MIN_EDIT_PIXELS]
                desired = set(detailed if len(detailed) <= MAX_EDIT_ITEMS else [r for r in detailed if r.isSelected()][:MAX_EDIT_ITEMS])
                if getattr(view, '_overview_active', False):
                    # Group selection operates on records and a live overlay;
                    # it must not create hundreds of overlapping Qt layers.
                    desired = {r for r in detailed if r in self.selected} if len(self.selected) == 1 else set()
                for item in (self.scene.crop_item, self.scene.edit_item, self.scene.mouseGrabberItem()):
                    record = logical_item(item) if item is not None else None
                    if id(record) in self.records:
                        desired.add(record)
                for record in list(self.materialized-desired):
                    self.recycle(record)
                started = time.perf_counter()
                for record in desired-self.materialized:
                    self.materialize(record)
                    if (time.perf_counter()-started)*1000 >= 6:
                        break
                if desired-self.materialized:
                    self.schedule()
                    key = None
            self.rebuild_layers()
            self._sync_key = key
        finally:
            self.scene.blockSignals(blocked)
            self.reconciling = previous

    def rebuild_layers(self):
        native = [item for item in QtWidgets.QGraphicsScene.items(self.scene)
                  if not getattr(item, '_is_record_layer', False) and item.isVisible() and item.parentItem() is None]
        native.sort(key=self.stack_key)
        signature = tuple((id(item), self.stack_key(item)) for item in native)
        if signature == self._layer_signature:
            return
        for before, after in reversed(list(zip(native, native[1:]))):
            if before.zValue() == after.zValue():
                before.stackBefore(after)
        for layer in self.layers:
            QtWidgets.QGraphicsScene.removeItem(self.scene, layer)
            sip.delete(layer)
        self.layers = []
        previous = (-float('inf'), -float('inf'))
        for next_item in native+[None]:
            upper = self.stack_key(next_item) if next_item is not None else (float('inf'), float('inf'))
            layer = RecordLayer(self, previous, upper)
            if next_item is not None:
                layer.setZValue(upper[0] if not self.layers else (previous[0]+upper[0])/2)
            else:
                layer.setZValue(0 if not self.layers else previous[0]+.0001)
            QtWidgets.QGraphicsScene.addItem(self.scene, layer)
            if next_item is not None and layer.zValue() == next_item.zValue():
                layer.stackBefore(next_item)
            self.layers.append(layer)
            previous = upper
        self._layer_signature = signature
