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
_REVISION_UNSET = object()


def pixmap_source_revision(item, pixmap):
    """Pixel provenance, not the current metadata revision of a fallback."""
    store = cache()
    if store is None or pixmap is None:
        return None
    return store.pixel_revisions.get((id(logical_item(item)), pixmap.cacheKey()))


def _saved_catalog_revision(item, kind, payload):
    """Only an exact payload installed by the verified catalog reader is known."""
    provenance = getattr(item, '_catalog_' + kind + '_provenance', None)
    if (getattr(item, '_catalog_proxy', False) and isinstance(payload, list)
            and len(payload) == 3 and provenance is not None
            and tuple(payload) == provenance[0]
            and getattr(item, '_display_source_revision', None) == provenance[1]):
        return provenance[1]
    return None


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
        self.pixel_revisions = OrderedDict()
        self.bytes = 0
        self.budget = DISPLAY_BUDGET_BYTES
        self.overviews = OrderedDict()
        self.overview_bytes = 0
        self.overview_budget = 2*1024*1024
        from prism.resource_budget import shared_cache_budget
        self._cache_budget = shared_cache_budget()
        self._cache_budget.register(self, category='image_display', usage='_budget_bytes',
                                    priority=80)

    def _budget_bytes(self):
        return self.bytes + self.overview_bytes

    def _trim_cache_to(self, target):
        while self.overviews and self._budget_bytes() > target:
            old, _pixmap = self.overviews.popitem(last=False)
            self.overview_bytes -= old[0]*old[1]*4
        if self._budget_bytes() <= target:
            return
        # Snapshot the oldest-first keys once. Repeatedly searching from the
        # beginning rescanned every pinned entry for each evicted item.
        for key in tuple(self.entries):
            if self._budget_bytes() <= target:
                break
            entry = self.entries.get(key)
            if entry is None or self._entry_visible(entry[0]()):
                continue
            self.entries.pop(key)
            self.bytes -= entry[2]
            owner = entry[0]()
            if owner is not None and not is_deleted(owner):
                preview = getattr(owner, '_preview_pixmap', None)
                if preview is not None and preview.cacheKey() == entry[1].cacheKey():
                    owner._preview_pixmap = None
                    owner._grayscale_cache = None

    @staticmethod
    def _entry_visible(item):
        if item is None or is_deleted(item):
            return False
        reference = getattr(item, '_display_budget_owner', None)
        view = reference() if reference is not None else None
        if (view is None or (isinstance(view, QtCore.QObject) and sip.isdeleted(view))
                or not getattr(view, 'isVisible', lambda: False)()):
            return False
        needs = getattr(view, 'needs_display', None)
        return bool(needs and needs(item))

    def saved_overview(self, value):
        key = _overview_key(value)
        if key is None:
            return None
        result = self.overviews.get(key)
        if result is not None:
            self.overviews.move_to_end(key)
            self._cache_budget.touch(self)
            return QtGui.QPixmap(result)
        data = _decode_overview(value)
        if data is None:
            return None
        width, height, _encoded = key
        image = QtGui.QImage(data, width, height, width*4,
                             QtGui.QImage.Format.Format_RGBA8888).copy()
        result = QtGui.QPixmap.fromImage(image)
        self.overviews[key] = result
        self.overview_bytes += width*height*4
        while self.overviews and (self.overview_bytes > self.overview_budget or len(self.overviews)>8192):
            old, _pixmap = self.overviews.popitem(last=False)
            self.overview_bytes -= old[0]*old[1]*4
        self._cache_budget.changed(self)
        return QtGui.QPixmap(result)

    def get(self, item, level, *, touch=True):
        item = logical_item(item)
        key = source_key(item)+(level,)
        entry = self.entries.get(key)
        if entry is None or entry[0]() is not item:
            return None
        if touch:
            self.entries.move_to_end(key)
            self._cache_budget.touch(self)
        return entry[1]

    def remember_pixels(self, item, pixmap, source_revision):
        pixel_key = (id(logical_item(item)), pixmap.cacheKey())
        if source_revision is None:
            # Unknown is absence of proof, not a permanent provenance entry.
            # Explicitly unknown input must also revoke any older proof for
            # these shared pixels; a later verified install can establish it.
            self.pixel_revisions.pop(pixel_key, None)
            return
        self.pixel_revisions[pixel_key] = source_revision
        self.pixel_revisions.move_to_end(pixel_key)
        while len(self.pixel_revisions) > 8192:
            self.pixel_revisions.popitem(last=False)

    def store(self, item, level, pixmap, notify=True, *, source_revision=_REVISION_UNSET):
        item = logical_item(item)
        # Decoder installs are revision-checked by the caller. Internal LOD
        # resizes explicitly inherit their input's provenance, including None.
        actual = (getattr(item, '_display_source_revision', None)
                  if source_revision is _REVISION_UNSET else source_revision)
        self.remember_pixels(item, pixmap, actual)
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
        self._cache_budget.changed(self)
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
            self.store(item, 192, preview, notify=notify,
                       source_revision=pixmap_source_revision(item, preview))

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
    if owner is not None:
        reference = getattr(item, '_display_budget_owner', None)
        if reference is None or reference() is not owner:
            try:
                item._display_budget_owner = weakref.ref(owner)
            except (TypeError, AttributeError):
                pass  # lightweight non-Qt adapters need not support weak references
    preview = getattr(item, '_preview_pixmap', None)
    full = native_pixmap(item)
    if getattr(item, 'TYPE', None) in ('video', 'glb'):
        # Their archive source is a movie/model, never an image. In particular
        # it must not reach image_decode.source_snapshot or image LOD requests.
        saved = thumbnail_pixmap(item, level_for_pixels(pixels))
        return saved if saved is not None and not saved.isNull() else full
    if getattr(item, '_preview_adjusted', False):
        return full if full is not None and not full.isNull() else preview
    if pixels <= OVERVIEW_MAX_PIXELS:
        # The saved overview already has at least as many pixels as this
        # image occupies on screen. Drawing it is real image content, not a
        # placeholder. Do not queue 192px decodes for 100k sub-pixel images.
        for candidate in (preview, full):
            if candidate is not None and not candidate.isNull():
                return candidate
        overview = overview_pixmap(item)
        if overview is not None and not overview.isNull():
            return overview
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
                store.store(item, level, result, notify=False,
                            source_revision=pixmap_source_revision(item, candidate))
                return result
    # Native getter bypasses the original-on-demand/export API.
    fallback = preview if preview is not None and not preview.isNull() else full
    if fallback is None or fallback.isNull():
        if owner is None:
            fallback = thumbnail_pixmap(item, level)
    if (fallback is None or fallback.isNull()) and (is_record(item) or isinstance(item, QtWidgets.QGraphicsPixmapItem)):
        fallback = overview_pixmap(item)
    if full is not None and not full.isNull() and (fallback is None or max(full.width(), full.height()) > max(fallback.width(), fallback.height())):
        fallback = full
    if fallback is not None and not fallback.isNull() and max(fallback.width(), fallback.height()) >= level:
        result = fallback.scaled(level, level, QtCore.Qt.AspectRatioMode.KeepAspectRatio,
                                 QtCore.Qt.TransformationMode.SmoothTransformation)
        if store:
            store.store(item, level, result,
                        source_revision=pixmap_source_revision(item, fallback))
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
            # Request one screen-appropriate level. The old path queued a
            # small preview and then a second 192px-or-larger decode for the
            # same image, wasting work during dense mid-distance views. The
            # dispatcher re-checks needs_display at install time, so no guard
            # is needed here.
            request_display(item, level, owner=owner)
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
        store.store(item, cache_level, result, notify=False,
                    source_revision=_saved_catalog_revision(item, 'thumbnail', value))
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
            store = cache()
            if store is not None:
                store.remember_pixels(item, result, pixmap_source_revision(item, source))
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


def _overview_key(value):
    if not isinstance(value, list) or len(value) != 3:
        return None
    width, height, encoded = value
    if (type(width) is not int or type(height) is not int
            or not 0 < width <= OVERVIEW_MAX_PIXELS
            or not 0 < height <= OVERVIEW_MAX_PIXELS
            or not isinstance(encoded, str) or len(encoded) > 344):
        return None
    return width, height, encoded


def _decode_overview(value):
    key = _overview_key(value)
    if key is None:
        return None
    try:
        data = base64.b64decode(key[2], validate=True)
    except (ValueError, binascii.Error):
        return None
    return data if len(data) == key[0]*key[1]*4 else None


def valid_overview(value):
    return _decode_overview(value) is not None


def restore_overview(item, value):
    """Share immutable saved pixels; never read an original to restore them."""
    store = cache()
    if store is None:
        return
    result = store.saved_overview(value)
    if result is not None:
        item._overview_pixmap = result
        item._overview_revision = getattr(item, '_display_source_revision', 0)
        store.remember_pixels(item, result, _saved_catalog_revision(item, 'overview', value))


def source_rect(item, pixmap):
    if hasattr(item, '_preview_source_rect') and not getattr(item, 'crop', QtCore.QRectF()).isEmpty():
        return item._preview_source_rect(pixmap)
    # Passive image records intentionally do not allocate a graphics item
    # merely to call a private method. Their crop needs the same mapping.
    crop = getattr(item, 'crop', QtCore.QRectF())
    size = getattr(item, '_image_size', QtCore.QSize())
    if not crop.isEmpty() and size.width() > 0 and size.height() > 0:
        return QtCore.QRectF(crop.x()*pixmap.width()/size.width(),
                            crop.y()*pixmap.height()/size.height(),
                            crop.width()*pixmap.width()/size.width(),
                            crop.height()*pixmap.height()/size.height())
    return QtCore.QRectF(pixmap.rect())


def drawn_region_size(item):
    """Size of the source region that is actually drawn (crop-aware)."""
    crop = getattr(item, 'crop', QtCore.QRectF())
    if not crop.isEmpty() and crop.width() > 0 and crop.height() > 0:
        return QtCore.QSizeF(crop.size())
    return QtCore.QSizeF(getattr(item, '_image_size', QtCore.QSize()))


def projected_pixels(item, view_scale, item_scale=1.0):
    """Source pixels needed to draw ``item`` at ``view_scale`` device pixels
    per scene unit, honouring the drawn crop instead of the full original.

    ``view_scale`` already includes the view's device pixel ratio; ``item_scale``
    is the item's own scale. Only the cropped region is ever painted, so the
    decode level follows that region's longer edge, not the whole original.
    """
    size = drawn_region_size(item)
    longest = max(size.width(), size.height(), 1.0)
    return longest * max(float(item_scale), 0.0) * max(float(view_scale), 0.0)


def shown_pixels(item, painter, owner=None):
    size = drawn_region_size(item)
    transform = painter.combinedTransform()
    scale = max(math.hypot(transform.m11(), transform.m12()), math.hypot(transform.m21(), transform.m22()))
    if owner is not None and getattr(owner, '_overview_renderer', None) is not None:
        # Coarse cover tiles are temporary display buffers. Select image LOD
        # for the actual viewport, otherwise every coarse/fine tile requests
        # a different tiny preview and canvases repeatedly look pixelated.
        scale = max(scale, abs(owner.get_scale()*item.scale())*owner.viewport().devicePixelRatioF())
    return max(size.width(), size.height())*scale


def draw_image(painter, item, rect, owner=None, grayscale=False):
    """Shared thumbnail rendering, including the original crop and gray mode."""
    crop = getattr(item, 'crop', QtCore.QRectF())
    dimensions = getattr(item, '_video_dimensions', None) if getattr(item, 'TYPE', None) == 'video' else None
    size = crop.size() if not crop.isEmpty() else QtCore.QSizeF(dimensions or getattr(item, '_image_size', QtCore.QSize()))
    if size.isEmpty():
        pm = getattr(item, '_preview_pixmap', None)
        size = QtCore.QSizeF(pm.size()) if pm is not None else QtCore.QSizeF(1, 1)
    fitted = size.scaled(rect.size(), QtCore.Qt.AspectRatioMode.KeepAspectRatio)
    target = QtCore.QRectF(QtCore.QPointF(), fitted)
    target.moveCenter(rect.center())
    screen = painter.combinedTransform().mapRect(target)
    screen_pixels = max(screen.width(), screen.height())
    if owner is not None and hasattr(owner, '_composites'):
        # A coarse tile/group painter can be much smaller than the current
        # viewport. Decode for the camera the user sees, not that buffer.
        camera_scale = abs(owner.transform().m11())*owner.viewport().devicePixelRatioF()
        screen_pixels = max(target.width(), target.height())*camera_scale
    # `target` already maps the drawn source region (crop-aware) onto the
    # device, so the pixels it covers are exactly the source pixels the decode
    # level must satisfy (1:1). Scaling it up by the full original extent
    # over-requests huge levels for heavily cropped images.
    pixels = screen_pixels
    pm = display_pixmap(item, pixels, owner)
    hook = getattr(owner, '_observe_asset_pixmap', None)
    missing_hook = getattr(owner, '_observe_asset_missing', None)
    if pm is None or pm.isNull():
        if missing_hook is not None:
            try:
                missing_hook(painter, target)
            except Exception:
                pass
        return False
    actual_revision = None
    if hook is not None:
        try:
            actual_revision = pixmap_source_revision(item, pm)
        except Exception:
            pass
    if grayscale or getattr(item, 'grayscale', False):
        pm = QtGui.QPixmap.fromImage(pm.toImage().convertToFormat(QtGui.QImage.Format.Format_Grayscale8))
    # Directory/search cards paint outside the item's graphics transform.
    # Apply its display state locally without leaking into adjacent cards.
    painter.save()
    painter.setOpacity(painter.opacity() * item.opacity())
    if item.flip() < 0:
        painter.translate(target.center())
        painter.scale(-1, 1)
        painter.translate(-target.center())
    source = source_rect(item, pm)
    painter.drawPixmap(target, pm, source)
    if hook is not None:
        try:
            hook(painter, item, target, pm, source, pixels, actual_revision)
        except Exception:
            pass
    painter.restore()
    return True
