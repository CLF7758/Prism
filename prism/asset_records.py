"""Lightweight image state. Editing pixels and Qt handles are materialized on demand."""
import copy
import weakref
from PyQt6 import QtCore, QtGui, QtWidgets
from prism.image_source import ImageSourceMixin
from prism.item_metadata import ItemMetadataMixin
from prism.selection import with_anchor

MIN_VIRTUAL_IMAGES = 2000


class ImageRecord(ImageSourceMixin, ItemMetadataMixin):
    _is_asset_record = True
    TYPE = 'pixmap'
    is_image = True
    is_editable = False
    GraphicsItemFlag = QtWidgets.QGraphicsItem.GraphicsItemFlag

    def __init__(self, data, source):
        extra = data.get('data', {})
        self.save_id = data.get('save_id')
        self.filename = extra.get('filename')
        self._extra_data = copy.deepcopy(extra)
        self._position = QtCore.QPointF(data.get('x', 0), data.get('y', 0))
        self._scale = data.get('scale', 1)
        self._rotation = data.get('rotation', 0)
        self._flip = data.get('flip', 1)
        self._z = data.get('z', 0)
        self._opacity = extra.get('opacity', 1)
        self._visible = True
        self._selected = False
        self._graphics = None
        self._scene_ref = None
        self._disposed = False
        self._sequence = 0
        self._asset_record = self
        self._source_blob = None
        self._source_pinned = False
        self._source_path = None
        self._prism_source = source
        self._source_format = self._normalise_format(extra.get('sourceFormat')) or self._format_from_filename(self.filename)
        self._image_size = QtCore.QSize(extra['imageWidth'], extra['imageHeight'])
        self._crop = QtCore.QRectF(*extra['crop']) if 'crop' in extra else QtCore.QRectF(QtCore.QPointF(), QtCore.QSizeF(self._image_size))
        self._grayscale = bool(extra.get('grayscale', False))
        self._grayscale_cache = None
        self._preview_pixmap = None
        self._overview_pixmap = None
        self._overview_payload = extra.get('displayOverview')
        self._display_thumbnail = extra.get('displayThumbnail')
        self._derived_pixmap = None
        self._preview_adjusted = False
        self._display_source_revision = 0
        self._display_decode_request = None
        self._display_decode_failed = False
        self._perceptual_hash = extra.get('hash')
        self._last_painted = 0
        self.color_group = extra.get('colorGroup')
        self.dominant_color = extra.get('dominantColor')
        self.crop_mode = False
        self.active_mode = None
        self.viewport_scale = 1
        self._flags = (self.GraphicsItemFlag.ItemIsMovable | self.GraphicsItemFlag.ItemIsSelectable |
                       self.GraphicsItemFlag.ItemSendsGeometryChanges)
        self.init_metadata()
        self.load_metadata_from_data(extra)
        self.apply_deleted_state()

    def scene(self):
        return self._scene_ref() if self._scene_ref is not None else None

    def attach(self, scene):
        self._scene_ref = weakref.ref(scene)
        self._disposed = False

    def graphics_item(self):
        if self._disposed:
            raise RuntimeError('The asset belongs to a cleared scene')
        if self._graphics is None:
            scene = self.scene()
            if scene is None:
                from prism.viewport_items import materialize_image
                materialize_image(self)
            else:
                scene._viewport_items.materialize(self)
        return self._graphics

    def __getattr__(self, name):
        # Metadata probes must never allocate a graphics object.
        if name.startswith('_'):
            raise AttributeError(name)
        from prism.items import PrismPixmapItem
        if not hasattr(PrismPixmapItem, name):
            raise AttributeError(name)
        value = getattr(PrismPixmapItem, name)
        if callable(value):
            return lambda *args, **kwargs: getattr(self.graphics_item(), name)(*args, **kwargs)
        return getattr(self.graphics_item(), name)

    def pos(self):
        return self._graphics.pos() if self._graphics is not None else QtCore.QPointF(self._position)

    def x(self): return self.pos().x()
    def y(self): return self.pos().y()
    def scale(self): return self._graphics.scale() if self._graphics is not None else self._scale
    def rotation(self): return self._graphics.rotation() if self._graphics is not None else self._rotation
    def zValue(self): return self._graphics.zValue() if self._graphics is not None else self._z
    def flip(self): return self._graphics.flip() if self._graphics is not None else self._flip
    def opacity(self): return self._graphics.opacity() if self._graphics is not None else self._opacity
    def isVisible(self): return self._graphics.isVisible() if self._graphics is not None else self._visible
    def isSelected(self): return self._graphics.isSelected() if self._graphics is not None else self._selected
    def flags(self): return self._flags
    def parentItem(self): return None

    def on_view_scale_change(self):
        if self._graphics is not None:
            self._graphics.on_view_scale_change()

    def _changed(self, geometry=False):
        scene = self.scene()
        if scene is not None:
            scene._viewport_items.changed(self, geometry)

    def update(self, *args):
        if self._graphics is not None:
            QtWidgets.QGraphicsPixmapItem.update(self._graphics, *args)
        self._changed()

    def prepareGeometryChange(self):
        if self._graphics is not None:
            self._graphics.prepareGeometryChange()

    def setPos(self, x, y=None):
        self._position = QtCore.QPointF(x) if y is None else QtCore.QPointF(x, y)
        if self._graphics is not None:
            self._graphics.setPos(self._position)
        self._changed(True)

    def moveBy(self, x, y): self.setPos(self.pos()+QtCore.QPointF(x, y))

    @with_anchor
    def setScale(self, value):
        if value > 0:
            self._scale = value
            if self._graphics is not None:
                QtWidgets.QGraphicsItem.setScale(self._graphics, value)
            self._changed(True)

    @with_anchor
    def setRotation(self, value):
        self._rotation = value % 360
        if self._graphics is not None:
            QtWidgets.QGraphicsItem.setRotation(self._graphics, self._rotation)
        self._changed(True)

    @with_anchor
    def do_flip(self, vertical=False):
        self._flip = -self.flip()
        if self._graphics is not None:
            self._graphics.setTransform(QtGui.QTransform.fromScale(self._flip, 1))
        if vertical:
            self.setRotation(self.rotation()+180)
        self._changed(True)

    def setZValue(self, value):
        self._z = value
        if self._graphics is not None:
            self._graphics.setZValue(value)
        scene = self.scene()
        if scene is not None:
            scene.max_z = max(scene.max_z, value)
            scene.min_z = min(scene.min_z, value)
        self._changed(True)

    def bring_to_front(self):
        self.setZValue(self.scene().max_z+self.scene().Z_STEP)

    def setOpacity(self, value):
        self._opacity = max(0, min(1, value))
        if self._graphics is not None:
            self._graphics.setOpacity(self._opacity)
        self.update()

    def setVisible(self, value):
        if bool(value) == self.isVisible():
            return
        self._visible = bool(value)
        if not value:
            self.setSelected(False)
        if self._graphics is not None:
            self._graphics.setVisible(value)
        self._changed(True)

    def setSelected(self, value):
        value = bool(value) and bool(self._flags & self.GraphicsItemFlag.ItemIsSelectable) and self.isVisible()
        if value == self.isSelected():
            return
        self._selected = value
        scene = self.scene()
        if scene is not None:
            scene._viewport_items.selection(self, value)
        if self._graphics is not None:
            self._graphics.setSelected(value)
        elif scene is not None:
            scene.selectionChanged.emit()
        self.update()

    def setFlag(self, flag, enabled=True):
        self._flags = self._flags | flag if enabled else self._flags & ~flag
        if self._graphics is not None:
            self._graphics.setFlag(flag, enabled)

    @property
    def crop(self): return self._crop

    @crop.setter
    def crop(self, value):
        self.prepareGeometryChange()
        self._crop = QtCore.QRectF(value)
        self._changed(True)

    @property
    def grayscale(self): return self._grayscale

    @grayscale.setter
    def grayscale(self, value):
        self._grayscale = bool(value)
        self._grayscale_cache = None
        if self._graphics is not None:
            self._graphics._sync_grayscale_effect()
        self.update()

    def reset_crop(self):
        self.crop = QtCore.QRectF(QtCore.QPointF(), QtCore.QSizeF(self._image_size))

    def apply_deleted_state(self):
        if self._deleted_at is not None:
            self.setSelected(False)
            self.setFlag(self.GraphicsItemFlag.ItemIsSelectable, False)
            self.setVisible(False)

    def bounding_rect_unselected(self): return QtCore.QRectF(self._crop)
    def boundingRect(self):
        return self._graphics.boundingRect() if self._graphics is not None else self.bounding_rect_unselected()

    @property
    def width(self): return self._crop.width()
    @property
    def height(self): return self._crop.height()
    @property
    def center(self): return self._crop.center()
    @property
    def center_scene_coords(self): return self.mapToScene(self.center)
    @property
    def corners_scene_coords(self):
        return [self.mapToScene(p) for p in (self._crop.topLeft(), self._crop.topRight(), self._crop.bottomRight(), self._crop.bottomLeft())]

    def sceneTransform(self):
        if self._graphics is not None:
            return self._graphics.sceneTransform()
        transform = QtGui.QTransform()
        transform.translate(self._position.x(), self._position.y())
        transform.scale(self._flip, 1)
        transform.rotate(self._rotation)
        transform.scale(self._scale, self._scale)
        return transform

    def mapToScene(self, point): return self.sceneTransform().map(point)
    def mapFromScene(self, point): return self.sceneTransform().inverted()[0].map(point)
    def mapRectToScene(self, rect): return self.sceneTransform().mapRect(rect)
    def sceneBoundingRect(self): return self.mapRectToScene(self.boundingRect())
    def shape(self):
        path = QtGui.QPainterPath()
        path.addRect(self.boundingRect())
        return path
    def set_pos_center(self, position): self.setPos(position-self.center_scene_coords+self.pos())

    def get_extra_save_data(self):
        from prism.items import PrismPixmapItem
        data = dict(self._extra_data)
        for key in ('displayOverview', 'displayThumbnail', 'hash', 'sourceFormat', 'colorGroup', 'dominantColor'):
            data.pop(key, None)
        data.update(PrismPixmapItem.get_extra_save_data(self))
        if self._deleted_at is None:
            data.pop('deletedAt', None)
        return data

    def _preview_source_rect(self, pixmap):
        from prism.items import PrismPixmapItem
        return PrismPixmapItem._preview_source_rect(self, pixmap)

    def _get_settings(self):
        from prism.config import PrismSettings
        scene = self.scene()
        return scene.settings if scene is not None else PrismSettings()

    def get_imgformat(self, image):
        from prism.items import PrismPixmapItem
        return PrismPixmapItem.get_imgformat(self, image)

    def pixmap_to_bytes(self, apply_grayscale=False, apply_crop=False):
        from prism import image_decode
        from prism.fileio.errors import PrismFileIOError
        pixmap = self._derived_pixmap
        if self._graphics is not None:
            pixmap = QtWidgets.QGraphicsPixmapItem.pixmap(self._graphics)
        if pixmap is not None and not pixmap.isNull():
            image = pixmap.toImage()
        else:
            decoded = image_decode.decode(*image_decode.source_snapshot(self))
            if decoded is None:
                raise PrismFileIOError(msg='Cannot read original image', filename=self.filename)
            image, _size = decoded
        if apply_grayscale and self.grayscale:
            from prism.fileio.image import qt_grayscale_image
            image = qt_grayscale_image(image)
        if apply_crop:
            image = image.copy(self.crop.toRect())
        buffer = QtCore.QBuffer()
        buffer.open(QtCore.QIODevice.OpenModeFlag.WriteOnly)
        image_format = self.get_imgformat(image)
        if not image.save(buffer, image_format.upper(), quality=90):
            raise PrismFileIOError(msg='Cannot encode image', filename=self.filename)
        return bytes(buffer.data()), image_format

    def analysis_image(self, max_side=128):
        from prism.items import _small_image
        preview = self._preview_pixmap or self._derived_pixmap
        if preview is not None and not preview.isNull():
            return _small_image(preview, max_side)
        from prism import image_decode
        decoded = image_decode.decode(*image_decode.source_snapshot(self), max_side=max_side)
        return decoded[0] if decoded is not None else None

    def _analysis_image(self): return self.analysis_image()

    def analyze_color_group(self):
        from prism.items import PrismPixmapItem
        return PrismPixmapItem.analyze_color_group(self)

    @property
    def perceptual_hash(self):
        from prism.items import PrismPixmapItem
        return PrismPixmapItem.perceptual_hash.fget(self)

    def has_full_pixmap(self):
        from prism.item_lifecycle import native_pixmap
        return not native_pixmap(self).isNull()

    def original_bytes(self):
        blob = super().original_bytes()
        if not self._source_pinned and (self._source_path or self._prism_source):
            self._source_blob = None
        return blob

    def materialize_source(self):
        self._source_pinned = True
        if self._source_blob is None:
            self._source_blob = super().original_bytes()

    def release_source_blob(self):
        if not self._source_pinned and (self._source_path or self._prism_source):
            self._source_blob = None
            return True
        return False

    def release_full_pixmap(self):
        if self._graphics is None:
            return False
        return self._graphics.release_full_pixmap()

    def create_copy(self):
        data = {'data': self.get_extra_save_data(), 'x': self.x(), 'y': self.y(),
                'z': self.zValue(), 'scale': self.scale(), 'rotation': self.rotation(), 'flip': self.flip()}
        result = ImageRecord(data, self._prism_source)
        result._source_path, result._source_blob = self._source_path, self._source_blob
        result._source_pinned = self._source_pinned
        from prism.item_lifecycle import native_pixmap
        result._derived_pixmap = native_pixmap(self) if self._preview_adjusted or not self.has_original_source() else None
        result._preview_adjusted = self._preview_adjusted
        result._preview_pixmap, result._overview_pixmap = self._preview_pixmap, self._overview_pixmap
        return result

    def __str__(self):
        return f'Image record {self.save_id}: {self.filename}'
