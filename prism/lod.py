"""Bounded display-only image levels. Original bytes/export pixels stay intact."""
import base64
import binascii
from collections import OrderedDict
import math
import weakref
from PyQt6 import QtCore, QtGui, QtWidgets, sip
from prism.item_lifecycle import logical_item, is_record, native_pixmap
from prism.item_lifecycle import is_deleted

OVERVIEW_MAX_PIXELS = 8
LEVELS = (32, 64, 192, 512, 1024, 2048, 4096)
DISPLAY_BUDGET_BYTES = 96 * 1024 * 1024


def level_for_pixels(pixels):
    return next((level for level in LEVELS if pixels <= level), LEVELS[-1])


def source_key(item):
    item = logical_item(item)
    return (id(item), getattr(item, '_display_source_revision', 0),
            getattr(item, '_source_path', None), getattr(item, '_prism_source', None),
            bool(getattr(item, '_preview_adjusted', False)))


class DisplayCache(QtCore.QObject):
    changed = QtCore.pyqtSignal(object)

    def __init__(self, app):
        super().__init__(app)
        self.entries = OrderedDict()
        self.bytes = 0
        self.budget = DISPLAY_BUDGET_BYTES

    def get(self, item, level):
        item = logical_item(item)
        key = source_key(item)+(level,)
        entry = self.entries.get(key)
        if entry is None or entry[0]() is not item:
            return None
        self.entries.move_to_end(key)
        return entry[1]

    def store(self, item, level, pixmap, notify=True):
        item = logical_item(item)
        key = source_key(item)+(level,)
        entry = self.entries.get(key)
        if entry is not None and entry[1].cacheKey() == pixmap.cacheKey():
            self.entries.move_to_end(key)
            return
        size = pixmap.width()*pixmap.height()*4
        old = self.entries.pop(key, None)
        if old:
            self.bytes -= old[2]
        if size > self.budget:
            return
        self.entries[key] = (weakref.ref(item), pixmap, size)
        self.bytes += size
        while self.bytes > self.budget and self.entries:
            _, entry = self.entries.popitem(last=False)
            self.bytes -= entry[2]
            owner = entry[0]()
            if owner is not None and not is_deleted(owner):
                preview = getattr(owner, '_preview_pixmap', None)
                if preview is not None and preview.cacheKey() == entry[1].cacheKey():
                    owner._preview_pixmap = None
                    owner._grayscale_cache = None
        if notify:
            self.changed.emit(item)

    def track_preview(self, item, notify=True):
        overview_pixmap(item)
        preview = getattr(item, '_preview_pixmap', None)
        if preview is not None and not preview.isNull() and not getattr(item, '_preview_adjusted', False):
            retain_thumbnail(item, preview)
        if (preview is not None and not preview.isNull()
                and not getattr(item, '_preview_adjusted', False)
                and (getattr(item, '_source_blob', None)
                     or getattr(item, '_source_path', None)
                     or getattr(item, '_prism_source', None))):
            self.store(item, 192, preview, notify=notify)

    def invalidate(self, item):
        item = logical_item(item)
        for key in [key for key in self.entries if key[0] == id(item)]:
            self.bytes -= self.entries.pop(key)[2]


def cache():
    app = QtWidgets.QApplication.instance()
    if app is None:
        return None
    result = getattr(app, '_prism_lod_cache', None)
    if result is None:
        result = app._prism_lod_cache = DisplayCache(app)
    return result


def display_pixmap(item, pixels, owner=None):
    """Never calls item.pixmap(): that API may synchronously decode an original."""
    item = logical_item(item)
    preview = getattr(item, '_preview_pixmap', None)
    full = native_pixmap(item)
    if getattr(item, '_preview_adjusted', False):
        return full if full is not None and not full.isNull() else preview
    level = level_for_pixels(pixels)
    size = getattr(item, '_image_size', QtCore.QSize())
    original = max(size.width(), size.height())
    needed = min(level, original) if original > 0 else level
    store = cache()
    result = store.get(item, level) if store else None
    if result is not None and max(result.width(), result.height()) >= needed:
        return result
    if store:
        for higher in LEVELS:
            candidate = store.get(item, higher) if higher > level else None
            if candidate is not None and max(candidate.width(), candidate.height()) >= needed:
                result = candidate.scaled(level, level, QtCore.Qt.AspectRatioMode.KeepAspectRatio,
                                           QtCore.Qt.TransformationMode.SmoothTransformation)
                store.store(item, level, result, notify=False)
                return result
    # Native getter bypasses the original-on-demand/export API.
    fallback = preview if preview is not None and not preview.isNull() else full
    if fallback is None or fallback.isNull():
        if owner is None:
            fallback = thumbnail_pixmap(item, level)
        else:
            # Painting must not synchronously decompress hundreds of previews.
            from prism.image_decode import request_display
            if getattr(item, '_display_thumbnail', None) is not None or hasattr(item, '_prism_source'):
                request_display(item, min(192, level), owner=owner)
    if (fallback is None or fallback.isNull()) and (is_record(item) or isinstance(item, QtWidgets.QGraphicsPixmapItem)):
        fallback = overview_pixmap(item)
    if full is not None and not full.isNull() and (fallback is None or max(full.width(), full.height()) > max(fallback.width(), fallback.height())):
        fallback = full
    if fallback is not None and not fallback.isNull() and max(fallback.width(), fallback.height()) >= level:
        result = fallback.scaled(level, level, QtCore.Qt.AspectRatioMode.KeepAspectRatio,
                                 QtCore.Qt.TransformationMode.SmoothTransformation)
        if store:
            store.store(item, level, result)
        return result
    if store:
        for other in reversed(LEVELS):
            candidate = store.get(item, other)
            if candidate is not None:
                fallback = candidate
                break
    if fallback is None or fallback.isNull() or (max(fallback.width(), fallback.height()) < needed):
        if getattr(item, '_source_blob', None) or getattr(item, '_source_path', None) or getattr(item, '_prism_source', None):
            from prism.image_decode import request_display
            request_display(item, max(192, level), owner=owner)
    return fallback


def retain_thumbnail(item, pixmap):
    """Keep a usable preview across reopening and display-cache eviction."""
    value = getattr(item, '_display_thumbnail', None)
    side = min(192, max(pixmap.width(), pixmap.height()))
    if isinstance(value, list) and len(value) == 3 and type(value[0]) is int and type(value[1]) is int and max(value[:2]) >= side:
        return
    from prism.thumbnail_cache import make_preview, encode_preview
    image = make_preview(pixmap.toImage(), 192)
    encoded = encode_preview(image)
    if encoded:
        item._display_thumbnail = [image.width(), image.height(), base64.b64encode(encoded).decode('ascii')]


def base_preview_ready(item, pixels, owner):
    if pixels <= OVERVIEW_MAX_PIXELS or getattr(item, '_display_decode_failed', False):
        return True
    size = item._image_size
    needed = min(192, max(size.width(), size.height()), level_for_pixels(pixels))
    preview = display_pixmap(item, needed, owner)
    return preview is not None and not preview.isNull() and max(preview.width(), preview.height()) >= needed


def thumbnail_pixmap(item, level=64):
    value = getattr(item, '_display_thumbnail', None)
    if (not isinstance(value, list) or len(value) != 3 or type(value[0]) is not int
            or type(value[1]) is not int or not 0 < value[0] <= 192 or not 0 < value[1] <= 192
            or not isinstance(value[2], str) or len(value[2]) > 220000):
        return None
    store = cache()
    cached = store.get(item, level) if store else None
    if cached is not None:
        return cached
    try:
        blob = base64.b64decode(value[2], validate=True)
    except (ValueError, binascii.Error):
        return None
    buffer = QtCore.QBuffer()
    buffer.setData(blob)
    buffer.open(QtCore.QIODevice.OpenModeFlag.ReadOnly)
    reader = QtGui.QImageReader(buffer)
    if reader.size() != QtCore.QSize(value[0], value[1]):
        return None
    if max(value[:2]) > level:
        scaled = reader.size().scaled(level, level, QtCore.Qt.AspectRatioMode.KeepAspectRatio)
        reader.setScaledSize(QtCore.QSize(max(1, scaled.width()), max(1, scaled.height())))
    image = reader.read()
    if image.isNull():
        return None
    result = QtGui.QPixmap.fromImage(image)
    if store:
        original = getattr(item, '_image_size', QtCore.QSize())
        cache_level = level if max(value[:2]) >= level or max(original.width(), original.height()) <= max(value[:2]) else level_for_pixels(max(value[:2]))
        store.store(item, cache_level, result, notify=False)
    return result


def overview_pixmap(item):
    """An at-most 8px thumbnail survives eviction of larger display levels."""
    item = logical_item(item)
    revision = getattr(item, '_display_source_revision', 0)
    if is_record(item) and item._overview_payload is not None and item._overview_pixmap is None:
        restore_overview(item, item._overview_payload)
    if getattr(item, '_overview_revision', None) != revision:
        item._overview_pixmap = None
        item._overview_revision = revision
    result = getattr(item, '_overview_pixmap', None)
    if result is None:
        source = getattr(item, '_preview_pixmap', None)
        if getattr(item, '_preview_adjusted', False) or source is None:
            source = native_pixmap(item)
        if source is not None and not source.isNull():
            result = source.scaled(OVERVIEW_MAX_PIXELS, OVERVIEW_MAX_PIXELS,
                                   QtCore.Qt.AspectRatioMode.KeepAspectRatio,
                                   QtCore.Qt.TransformationMode.FastTransformation)
            item._overview_pixmap = result
    return result


def overview_data(item):
    """Persist only available original display pixels; never decode for saving."""
    item = logical_item(item)
    if is_record(item) and item._overview_payload is not None and not item._preview_adjusted:
        value = item._overview_payload
        if valid_overview(value):
            return value
    if (getattr(item, '_preview_adjusted', False)
            or not (getattr(item, '_source_blob', None)
                    or getattr(item, '_source_path', None)
                    or getattr(item, '_prism_source', None))):
        return None
    pixmap = overview_pixmap(item)
    if pixmap is None or pixmap.isNull():
        return None
    image = pixmap.toImage().convertToFormat(QtGui.QImage.Format.Format_RGBA8888)
    data = image.constBits().asstring(image.sizeInBytes())
    return [image.width(), image.height(), base64.b64encode(data).decode('ascii')]


def valid_overview(value):
    if not isinstance(value, list) or len(value) != 3:
        return False
    width, height, encoded = value
    if (type(width) is not int or type(height) is not int
            or not 0 < width <= OVERVIEW_MAX_PIXELS
            or not 0 < height <= OVERVIEW_MAX_PIXELS
            or not isinstance(encoded, str) or len(encoded) > 344):
        return False
    try:
        data = base64.b64decode(encoded, validate=True)
    except (ValueError, binascii.Error):
        return False
    if len(data) != width*height*4:
        return False
    return True


def restore_overview(item, value):
    """Optional project thumbnails are bounded before allocating pixel data."""
    if not valid_overview(value):
        return
    width, height, encoded = value
    data = base64.b64decode(encoded, validate=True)
    image = QtGui.QImage(data, width, height, width*4,
                         QtGui.QImage.Format.Format_RGBA8888).copy()
    item._overview_pixmap = QtGui.QPixmap.fromImage(image)
    item._overview_revision = getattr(item, '_display_source_revision', 0)


def source_rect(item, pixmap):
    if hasattr(item, '_preview_source_rect') and not getattr(item, 'crop', QtCore.QRectF()).isEmpty():
        return item._preview_source_rect(pixmap)
    return QtCore.QRectF(pixmap.rect())


def shown_pixels(item, painter):
    size = item._image_size
    transform = painter.combinedTransform()
    scale = max(math.hypot(transform.m11(), transform.m12()), math.hypot(transform.m21(), transform.m22()))
    return max(size.width(), size.height())*scale


def draw_image(painter, item, rect, owner=None, grayscale=False):
    """Shared thumbnail rendering, including the original crop and gray mode."""
    crop = getattr(item, 'crop', QtCore.QRectF())
    size = crop.size() if not crop.isEmpty() else QtCore.QSizeF(getattr(item, '_image_size', QtCore.QSize()))
    if size.isEmpty():
        pm = getattr(item, '_preview_pixmap', None)
        size = QtCore.QSizeF(pm.size()) if pm is not None else QtCore.QSizeF(1, 1)
    fitted = size.scaled(rect.size(), QtCore.Qt.AspectRatioMode.KeepAspectRatio)
    target = QtCore.QRectF(QtCore.QPointF(), fitted)
    target.moveCenter(rect.center())
    screen = painter.combinedTransform().mapRect(target)
    original = getattr(item, '_image_size', QtCore.QSize())
    pixels = max(screen.width(), screen.height()) * max(original.width(), original.height(), 1)/max(size.width(), size.height(), 1)
    pm = display_pixmap(item, pixels, owner)
    if pm is None or pm.isNull():
        return False
    if grayscale or getattr(item, 'grayscale', False):
        pm = QtGui.QPixmap.fromImage(pm.toImage().convertToFormat(QtGui.QImage.Format.Format_Grayscale8))
    painter.drawPixmap(target, pm, source_rect(item, pm))
    return True
