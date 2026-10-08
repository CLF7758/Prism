# This file is part of Prism.
#
# Prism is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# Prism is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with Prism.  If not, see <https://www.gnu.org/licenses/>.

"""Classes for items that are added to the scene by the user (images,
text).
"""

from collections import defaultdict
from functools import cached_property
import logging
import os
from pathlib import Path
import sqlite3
import tempfile
import time

from PyQt6 import QtCore, QtGui, QtWidgets, sip
from PyQt6.QtCore import Qt, QUrl


def _configure_media_backend():
    """Pick a working QtMultimedia backend.

    Some PyQt6 wheels ship an ffmpeg backend plugin whose FFmpeg DLLs
    are missing, so the plugin fails to load. Fall back to the Windows
    Media Foundation backend in that case; without a backend
    QMediaPlayer cannot be created at all.
    """
    loader = QtCore.QPluginLoader(
        QtCore.QLibraryInfo.path(
            QtCore.QLibraryInfo.LibraryPath.PluginsPath)
        + '/multimedia/ffmpegmediaplugin.dll')
    if not loader.load():
        logging.getLogger(__name__).debug(
            'ffmpeg media backend unavailable, '
            'falling back to windows backend')
        os.environ['QT_MEDIA_BACKEND'] = 'windows'


_configure_media_backend()

from PyQt6.QtMultimedia import QMediaPlayer, QAudioOutput, QVideoSink

from prism import commands
from prism import thumbnail_cache, image_decode
from prism.config import PrismSettings
from prism.constants import COLORS
from prism.glb_preview import (FAST_QUALITY_TRIANGLES, GLBError,
                               GLBPreviewWindow, load_mesh, parse_glb,
                               parse_gltf, render_mesh)
from prism.i18n import _
from prism.selection import SelectableMixin


logger = logging.getLogger(__name__)

# ── Color group definitions (11 groups) ─────────────────────────
COLOR_GROUPS = [
    {"id": "red",    "label": "Red", "color": "#d95c62"},
    {"id": "orange", "label": "Orange", "color": "#d99046"},
    {"id": "yellow", "label": "Yellow", "color": "#d6bc48"},
    {"id": "green",  "label": "Green", "color": "#65a873"},
    {"id": "cyan",   "label": "Cyan", "color": "#54aeb4"},
    {"id": "blue",   "label": "Blue", "color": "#5d8fe8"},
    {"id": "purple", "label": "Purple", "color": "#9a7be6"},
    {"id": "pink",   "label": "Pink", "color": "#d579a5"},
    {"id": "black",  "label": "Black", "color": "#232626"},
    {"id": "white",  "label": "White", "color": "#dfe4e1"},
    {"id": "gray",   "label": "Gray", "color": "#828986"},
]


def _qimage_to_rgba(image):
    """Copy a QImage into an (h, w, 4) uint8 array, or None if empty."""
    import numpy as np

    converted = image.convertToFormat(QtGui.QImage.Format.Format_RGBA8888)
    width, height = converted.width(), converted.height()
    if not width or not height:
        return None
    buffer = converted.bits().asstring(converted.bytesPerLine() * height)
    return np.frombuffer(buffer, dtype=np.uint8).reshape(
        height, converted.bytesPerLine())[:, :width * 4].reshape(
            height, width, 4)


def _hsl_arrays(r, g, b):
    """Vectorised _rgb_to_hsl over uint8 arrays."""
    import numpy as np

    r = r.astype(np.float32) / 255.0
    g = g.astype(np.float32) / 255.0
    b = b.astype(np.float32) / 255.0
    mx = np.maximum(np.maximum(r, g), b)
    mn = np.minimum(np.minimum(r, g), b)
    lightness = (mx + mn) / 2.0
    delta = mx - mn
    chroma = delta > 0

    saturation = np.zeros_like(lightness)
    upper = chroma & (lightness > 0.5)
    lower = chroma & ~upper
    saturation[upper] = delta[upper] / np.maximum(
        2.0 - mx[upper] - mn[upper], 1e-6)
    saturation[lower] = delta[lower] / np.maximum(
        mx[lower] + mn[lower], 1e-6)

    hue = np.zeros_like(lightness)
    safe = np.maximum(delta, 1e-6)
    is_red = chroma & (mx == r)
    is_green = chroma & (mx == g) & ~is_red
    is_blue = chroma & ~is_red & ~is_green
    hue[is_red] = ((g[is_red] - b[is_red]) / safe[is_red]
                   + np.where(g[is_red] < b[is_red], 6.0, 0.0))
    hue[is_green] = (b[is_green] - r[is_green]) / safe[is_green] + 2.0
    hue[is_blue] = (r[is_blue] - g[is_blue]) / safe[is_blue] + 4.0
    return hue * 60.0, saturation, lightness


_COLOR_INDEX = {group['id']: index
                for index, group in enumerate(COLOR_GROUPS)}


def _color_group_indices(hue, saturation, lightness):
    """Vectorised _color_group_from_rgb, as COLOR_GROUPS indices.

    The scalar version is an if/elif chain, so the conditions are applied
    here in the same order and each one only claims the pixels that no
    earlier condition took.
    """
    import numpy as np

    result = np.full(hue.shape, _COLOR_INDEX['gray'], dtype=np.uint8)
    taken = np.zeros(hue.shape, dtype=bool)

    def claim(mask, name):
        nonlocal taken
        target = mask & ~taken
        result[target] = _COLOR_INDEX[name]
        taken |= target

    claim((lightness < 0.12)
          | ((lightness < 0.22) & (saturation < 0.25)), 'black')
    claim((lightness > 0.88) & (saturation < 0.15), 'white')
    claim((saturation < 0.10)
          | ((saturation < 0.18) & (lightness > 0.30) & (lightness < 0.75)),
          'gray')
    claim((hue < 20) | (hue >= 335), 'red')
    claim(hue < 46, 'orange')
    claim(hue < 75, 'yellow')
    claim(hue < 165, 'green')
    claim(hue < 200, 'cyan')
    claim(hue < 260, 'blue')
    claim(hue < 295, 'purple')
    claim(hue < 335, 'pink')
    return result


def _rgb_to_hsl(r, g, b):
    r, g, b = r / 255.0, g / 255.0, b / 255.0
    mx, mn = max(r, g, b), min(r, g, b)
    l = (mx + mn) / 2.0
    if mx == mn:
        h = s = 0.0
    else:
        d = mx - mn
        s = d / (2.0 - mx - mn) if l > 0.5 else d / (mx + mn)
        if mx == r:
            h = (g - b) / d + (6.0 if g < b else 0.0)
        elif mx == g:
            h = (b - r) / d + 2.0
        else:
            h = (r - g) / d + 4.0
        h *= 60.0
    return h, s, l


def _color_group_from_rgb(r, g, b):
    h, s, l = _rgb_to_hsl(r, g, b)
    # Black: very dark pixels
    if l < 0.12:
        return "black"
    if l < 0.22 and s < 0.25:
        return "black"
    # White: very bright, near-neutral
    if l > 0.88 and s < 0.15:
        return "white"
    # Gray: low saturation, mid brightness
    if s < 0.10:
        return "gray"
    if s < 0.18 and l > 0.30 and l < 0.75:
        return "gray"
    # Chromatic: classify by hue range
    if h < 20 or h >= 335:
        return "red"
    if h < 46:
        return "orange"
    if h < 75:
        return "yellow"
    if h < 165:
        return "green"
    if h < 200:
        return "cyan"
    if h < 260:
        return "blue"
    if h < 295:
        return "purple"
    return "pink"


item_registry = {}

VIDEO_EXTENSIONS = {
    '.mp4', '.mov', '.avi', '.mkv', '.webm', '.wmv', '.flv',
    '.m4v', '.3gp', '.ts',
}

VIDEO_EMBED_THRESHOLD_MB = 50

#: Longest side of the image used for colour analysis and perceptual
#: hashing.  Both throw away almost all of the detail anyway, and
#: working from a small copy keeps them from touching the full image.
ANALYSIS_MAX_SIDE = 128

#: How much full resolution image data may sit in memory at once, in
#: pixels (about four bytes each).  Beyond this the images that were
#: drawn longest ago are dropped back to their preview; they decode
#: again in a couple of milliseconds when the view returns to them.
LOADED_PIXEL_BUDGET = 96 * 1024 * 1024

#: Full images to load before the budget is checked again, and how many
#: recently drawn images are never dropped.
BUDGET_CHECK_INTERVAL = 40
KEEP_RECENT_IMAGES = 24

#: Opening a project that has no previews yet decodes every image.  A
#: few seconds after the first painting, memory is looked at once so
#: that the images nobody is reading do not stay resident.
OPENING_CHECK_DELAY_MS = 6000


def register_item(cls):
    item_registry[cls.TYPE] = cls
    return cls


def sort_by_filename(items):
    """Order items by filename.

    Items with a filename (ordered by filename) first, then items
    without a filename but with a save_id follow (ordered by
    save_id), then remaining items in the order that they have
    been inserted into the scene.
    """

    items_by_filename = []
    items_by_save_id = []
    items_remaining = []

    for item in items:
        if getattr(item, 'filename', None):
            items_by_filename.append(item)
        elif getattr(item, 'save_id', None):
            items_by_save_id.append(item)
        else:
            items_remaining.append(item)

    items_by_filename.sort(key=lambda x: x.filename)
    items_by_save_id.sort(key=lambda x: x.save_id)
    return items_by_filename + items_by_save_id + items_remaining


class PrismItemMixin(SelectableMixin):
    """Base for all items added by the user."""

    def set_pos_center(self, pos):
        """Sets the position using the item's center as the origin point."""

        self.setPos(pos - self.center_scene_coords)

    def has_selection_outline(self):
        return self.isSelected()

    def has_selection_handles(self):
        return (self.isSelected()
                and self.scene()
                and self.scene().has_single_selection())

    def selection_action_items(self):
        """The items affected by selection actions like scaling and rotating.
        """
        return [self]

    def on_selected_change(self, value):
        if (value and self.scene()
                and not self.scene().signalsBlocked()
                and not self.scene().has_selection()
                and not self.scene().active_mode is None):
            self.bring_to_front()

    # ── Metadata: categories, tags, notes ──────────────────────────────

    @property
    def categories(self):
        return self._categories

    @categories.setter
    def categories(self, value):
        self._categories = list(value) if value else []
        self._notify_metadata_changed()

    @property
    def tags(self):
        return self._tags

    @tags.setter
    def tags(self, value):
        self._tags = list(value) if value else []
        self._notify_metadata_changed()

    @property
    def rating(self):
        return self._rating

    @rating.setter
    def rating(self, value):
        self._rating = max(0, min(5, int(value or 0)))
        self._notify_metadata_changed()

    @property
    def notes(self):
        return self._notes

    @notes.setter
    def notes(self, value):
        self._notes = value or ''
        self._notify_metadata_changed()

    @property
    def title(self):
        return self._title

    @title.setter
    def title(self, value):
        self._title = value or ''
        self._notify_metadata_changed()

    @property
    def canvas_id(self):
        """素材属于哪个画布页 —— 拖到左栏另一个画布时改的就是它。

        `ChangeMetadata` 靠这个名字读旧值，所以它得是个真属性，
        不能只留一个 `_canvas_id`。
        """
        return self._canvas_id

    @canvas_id.setter
    def canvas_id(self, value):
        self._canvas_id = value
        self._notify_metadata_changed()

    def init_metadata(self):
        """Initialise metadata fields with empty defaults."""
        self._group_id = None
        self._group_note = ''
        if not hasattr(self, '_categories'):
            self._categories = []
        if not hasattr(self, '_tags'):
            self._tags = []
        if not hasattr(self, '_rating'):
            self._rating = 0
        if not hasattr(self, '_notes'):
            self._notes = ''
        if not hasattr(self, '_title'):
            self._title = ''
        if not hasattr(self, '_canvas_id'):
            self._canvas_id = 'default-canvas'

    def _notify_metadata_changed(self):
        if self.scene() and hasattr(self.scene(), 'metadata_changed'):
            self.scene().metadata_changed.emit()

    def get_metadata_save_data(self):
        return {'categories': list(self._categories),
                'tags': list(self._tags),
                'rating': self._rating,
                'notes': self._notes,
                'title': self._title,
                'canvasId': self._canvas_id,
                'groupId': self._group_id,
                'groupNote': self._group_note}

    def load_metadata_from_data(self, data):
        self._group_id = data.get('groupId')
        self._group_note = data.get('groupNote', '')
        self._categories = list(data.get('categories', []))
        self._tags = list(data.get('tags', []))
        self._rating = max(0, min(5, int(data.get('rating', 0) or 0)))
        self._notes = data.get('notes', '')
        self._title = data.get('title', '')
        self._canvas_id = data.get('canvasId', 'default-canvas')

    def update_from_data(self, **kwargs):
        self.save_id = kwargs.get('save_id', self.save_id)
        self.setPos(kwargs.get('x', self.pos().x()),
                    kwargs.get('y', self.pos().y()))
        # Each of the setters below logs and, for scale/rotation, walks the
        # item transform.  Opening a scene calls this for every item, always
        # with the value the item already has, so setting it again is pure
        # overhead - worth a comparison on projects with a thousand items.
        z = kwargs.get('z')
        if z is not None and z != self.zValue():
            self.setZValue(z)
        scale = kwargs.get('scale')
        if scale is not None and scale != self.scale():
            self.setScale(scale)
        rotation = kwargs.get('rotation')
        if rotation is not None and rotation != self.rotation():
            self.setRotation(rotation)
        if kwargs.get('flip', 1) != self.flip():
            self.do_flip()
        data = kwargs.get('data', {})
        self.load_metadata_from_data(data)


def _small_image(source, max_side):
    """A QImage of source, no larger than max_side on its longest side.

    Scaling a pixmap keeps Qt from copying the original pixels into an
    image first, which matters when the original is large.
    """
    width, height = source.width(), source.height()
    if width <= 0 or height <= 0:
        return None
    longest = max(width, height)
    if longest > max_side:
        scale = max_side / longest
        size = QtCore.QSize(max(1, round(width * scale)),
                            max(1, round(height * scale)))
        source = source.scaled(
            size, Qt.AspectRatioMode.KeepAspectRatio,
            Qt.TransformationMode.SmoothTransformation)
    if isinstance(source, QtGui.QPixmap):
        return source.toImage()
    return source


def release_stale_pixmaps(scene, budget_pixels=LOADED_PIXEL_BUDGET,
                          keep_recent=KEEP_RECENT_IMAGES):
    """Drop full size images that have not been drawn for a while.

    Only images loaded from a project file can be dropped: they still
    have the compressed original to decode from again.  Whatever the
    user looked at last is kept, so scrolling back and forth does not
    turn into repeated decoding.

    A second pass releases the compressed source bytes for items whose
    preview is in hand and whose blob can be re-read from disk or from
    the .prism archive.  This bounds memory for projects with tens of
    thousands of images.
    """
    if scene is None:
        return 0
    loaded = []
    total = 0
    for item in scene.items():
        if not isinstance(item, PrismPixmapItem):
            continue
        if not item.has_original_source():
            continue
        if not item.has_full_pixmap():
            continue
        size = item._image_size
        pixels = max(1, size.width() * size.height())
        loaded.append((getattr(item, '_last_painted', 0.0), pixels, item))
        total += pixels
    released = 0
    if total > budget_pixels:
        loaded.sort(key=lambda entry: entry[0])
        for _stamp, pixels, item in loaded[:max(0, len(loaded) - keep_recent)]:
            if total <= budget_pixels:
                break
            if item.release_full_pixmap():
                total -= pixels
                released += 1
    # Second pass: release compressed source bytes for items that
    # already dropped their full pixmap and can re-read the blob
    # on demand (via _source_path or _prism_source).
    blob_released = 0
    for item in scene.items():
        if not isinstance(item, PrismPixmapItem):
            continue
        if item.has_full_pixmap():
            continue
        if item.release_source_blob():
            blob_released += 1
    if released:
        logger.debug('Released %d full size images (%d megapixels left)',
                     released, total // (1024 * 1024))
    if blob_released:
        logger.debug('Released %d source blobs', blob_released)
    return released + blob_released


def _schedule_budget_check(scene):
    """Look at the memory budget once enough images have been decoded.

    Deferred to the event loop: dropping an image while the scene is
    painting it would be asking for trouble.
    """
    if scene is None or sip.isdeleted(scene):
        return
    count = getattr(scene, '_prism_budget_counter', 0) + 1
    if count < BUDGET_CHECK_INTERVAL:
        scene._prism_budget_counter = count
        return
    if getattr(scene, '_prism_budget_scheduled', False):
        return
    scene._prism_budget_counter = 0
    scene._prism_budget_scheduled = True
    QtCore.QTimer.singleShot(0, lambda: _run_budget_check(scene))


def _run_budget_check(scene):
    if scene is None or sip.isdeleted(scene):
        return
    scene._prism_budget_scheduled = False
    try:
        release_stale_pixmaps(scene)
    except Exception:
        # Housekeeping must never take a session down with it
        logger.debug('Could not release full size images', exc_info=True)


def _schedule_opening_check(scene):
    """Look at memory once, shortly after a scene starts being drawn.

    A project without cached previews is opened by decoding everything;
    waiting until the first painting is done means the first check does
    not fight with the opening itself.
    """
    if scene is None or sip.isdeleted(scene):
        return
    if getattr(scene, '_prism_opening_check', False):
        return
    scene._prism_opening_check = True
    QtCore.QTimer.singleShot(
        OPENING_CHECK_DELAY_MS, lambda: _run_budget_check(scene))


@register_item
class PrismPixmapItem(PrismItemMixin, QtWidgets.QGraphicsPixmapItem):
    """Class for images added by the user."""

    TYPE = 'pixmap'
    CROP_HANDLE_SIZE = 15
    _shared_settings = None

    @classmethod
    def _get_settings(cls):
        if cls._shared_settings is None:
            cls._shared_settings = PrismSettings()
        return cls._shared_settings

    def __init__(self, image, filename=None, **kwargs):
        super().__init__(QtGui.QPixmap.fromImage(image))
        self.save_id = None
        self.filename = filename
        # The full size image is not always in memory: projects store
        # their images compressed, and decoding all of them is what made
        # opening a large project slow.  _preview_pixmap holds a small
        # version for painting, _source_blob the compressed original it
        # can be decoded from again.  _image_size is the size of that
        # original and therefore the one piece of geometry that is
        # always valid.
        self._preview_pixmap = None
        self._display_decode_request = None
        self._display_decode_failed = False
        self._source_blob = None
        self._source_format = None
        self._source_path = None
        self._prism_source = None
        # A display adjustment is a derived preview only.  Keeping this
        # state separate from the pixmap is what lets exports distinguish
        # the untouched source from the currently visible pixels.
        self._preview_adjusted = False
        self._image_size = QtWidgets.QGraphicsPixmapItem.pixmap(self).size()
        self._perceptual_hash = None
        #: When this image was drawn last, used to decide what to unload
        self._last_painted = 0.0
        self.reset_crop()
        logger.trace('Initialized %s', self)
        self.is_image = True
        self.crop_mode = False
        self.init_selectable()
        self.grayscale = False
        self.color_group = None
        self.dominant_color = None
        self.init_metadata()

    @classmethod
    def create_from_data(self, **kwargs):
        item = kwargs.pop('item')
        data = kwargs.pop('data', {})
        item.filename = item.filename or data.get('filename')
        if 'crop' in data:
            item.crop = QtCore.QRectF(*data['crop'])
        item.setOpacity(data.get('opacity', 1))
        item.grayscale = data.get('grayscale', False)
        item.color_group = data.get('colorGroup')
        item.dominant_color = data.get('dominantColor')
        item._perceptual_hash = data.get('hash')
        item._source_format = item._normalise_format(
            data.get('sourceFormat')) or item._format_from_filename(
                item.filename)
        return item

    def __str__(self):
        size = self._image_size
        return (f'Image "{self.filename}" {size.width()} x {size.height()}')

    @property
    def crop(self):
        return self._crop

    @crop.setter
    def crop(self, value):
        logger.trace('Setting crop for %s to %s', self, value)
        self.prepareGeometryChange()
        self._crop = value
        self.update()

    @property
    def grayscale(self):
        return self._grayscale

    @grayscale.setter
    def grayscale(self, value):
        value = bool(value)
        if value == getattr(self, '_grayscale', None):
            return
        self._grayscale = value
        self._grayscale_cache = None
        app = QtWidgets.QApplication.instance()
        if app is not None and QtCore.QThread.currentThread() == app.thread():
            self._sync_grayscale_effect()
        self.update()

    def _sync_grayscale_effect(self):
        # Qt converts the pixels actually drawn at the current view scale.
        # No full-resolution decode or NumPy conversion during a UI toggle.
        # A saved image can be loaded on the IO thread. Install its QObject
        # effect when the GUI thread adds the item to the scene.
        current = self.graphicsEffect()
        if self.grayscale and not isinstance(current, QtWidgets.QGraphicsColorizeEffect):
            effect = QtWidgets.QGraphicsColorizeEffect()
            effect.setColor(QtGui.QColor('black'))
            effect.setStrength(1.0)
            self.setGraphicsEffect(effect)
        elif not self.grayscale and isinstance(current, QtWidgets.QGraphicsColorizeEffect):
            self.setGraphicsEffect(None)

    @property
    def _grayscale_pixmap(self):
        """Full-resolution grayscale pixels only for explicit export/sample."""
        if not self.grayscale:
            return None
        if self._grayscale_cache is None:
            from prism.fileio.image import qt_grayscale_image
            image = qt_grayscale_image(self.pixmap().toImage())
            self._grayscale_cache = QtGui.QPixmap.fromImage(image)
        return self._grayscale_cache

    @_grayscale_pixmap.setter
    def _grayscale_pixmap(self, pixmap):
        self._grayscale_cache = pixmap

    def sample_color_at(self, pos):
        ipos = self.mapFromScene(pos)
        if self.grayscale:
            pm = self._grayscale_pixmap
        else:
            pm = self.pixmap()
        img = pm.toImage()

        color = img.pixelColor(int(ipos.x()), int(ipos.y()))
        if color.alpha():
            return color

    def bounding_rect_unselected(self):
        if self.crop_mode:
            # While cropping, the whole image is the item's geometry.
            # Take the size from the stored one rather than from the
            # pixmap, which may only hold the preview.
            size = self._image_size
            return QtCore.QRectF(0, 0, size.width(), size.height())
        else:
            return self.crop

    def get_extra_save_data(self):
        data = {'filename': self.filename,
                'opacity': getattr(self, '_filter_original_opacity', self.opacity()),
                'grayscale': self.grayscale,
                'crop': [self.crop.topLeft().x(),
                         self.crop.topLeft().y(),
                         self.crop.width(),
                         self.crop.height()],
                'imageWidth': self._image_size.width(),
                'imageHeight': self._image_size.height()}
        if self.color_group:
            data['colorGroup'] = self.color_group
        if self.dominant_color:
            data['dominantColor'] = self.dominant_color
        if self._source_format:
            data['sourceFormat'] = self._source_format
        # Kept in the file so that looking for duplicates does not have to
        # hash every image again - it is the same picture either way.
        hash_value = self._perceptual_hash
        if hash_value is not None:
            data['hash'] = hash_value
        data.update(self.get_metadata_save_data())
        return data

    @property
    def perceptual_hash(self):
        """Difference hash of this image, computed once and remembered."""
        if self._perceptual_hash is None:
            from prism.similarity import difference_hash
            image = self.analysis_image(64)
            if image is None:
                return None
            self._perceptual_hash = difference_hash(image)
        return self._perceptual_hash

    def get_filename_for_export(self, imgformat, save_id_default=None):
        save_id = self.save_id or save_id_default
        assert save_id is not None

        if self.filename:
            basename = os.path.splitext(os.path.basename(self.filename))[0]
            return f'{save_id:04}-{basename}.{imgformat}'
        else:
            return f'{save_id:04}.{imgformat}'

    def get_imgformat(self, img):
        """Determines the format for storing this image."""

        formt = self._get_settings().valueOrDefault(
            'Items/image_storage_format')

        if formt == 'best':
            # Images with alpha channel and small images are stored as png
            if (img.hasAlphaChannel()
                    or (img.height() < 500 and img.width() < 500)):
                formt = 'png'
            else:
                formt = 'jpg'

        logger.trace(f'Found format {formt} for {self}')
        return formt

    def pixmap_to_bytes(self, apply_grayscale=False, apply_crop=False):
        """Convert the pixmap data to PNG bytestring."""
        barray = QtCore.QByteArray()
        buffer = QtCore.QBuffer(barray)
        buffer.open(QtCore.QIODevice.OpenModeFlag.WriteOnly)
        if apply_grayscale and self.grayscale:
            pm = self._grayscale_pixmap
        else:
            # Silently decodes the original if only the preview is in
            # memory: saving and exporting must never write a preview
            # where the full image belongs.
            pm = self.pixmap()

        if apply_crop:
            pm = pm.copy(self.crop.toRect())

        img = pm.toImage()
        imgformat = self.get_imgformat(img)
        img.save(buffer, imgformat.upper(), quality=90)
        return (barray.data(), imgformat)

    @staticmethod
    def _normalise_format(value):
        if not value:
            return None
        value = str(value).lower().strip()
        return value[1:] if value.startswith('.') else value

    @staticmethod
    def _format_from_filename(filename):
        if not filename:
            return None
        return PrismPixmapItem._normalise_format(
            os.path.splitext(str(filename))[1])

    def set_source_blob(self, blob, source_format=None):
        """Keep the bytes that were imported as the lossless source.

        The canvas may use a decoded or adjusted QPixmap, but this payload
        is never rewritten merely because the project is saved or the
        preview controls change.  It is also intentionally independent of
        the current storage-format preference.
        """
        self._grayscale_cache = None
        self._display_decode_request = None
        self._display_decode_failed = False
        if blob is None:
            self._source_blob = None
            self._source_format = None
            self._source_path = None
            self._prism_source = None
            self._preview_adjusted = False
            return
        self._source_blob = bytes(blob)
        self._source_format = (
            self._normalise_format(source_format)
            or self._format_from_filename(self.filename)
            or self._format_from_bytes(self._source_blob)
            or 'png')
        self._preview_adjusted = False

    @staticmethod
    def _format_from_bytes(blob):
        signatures = ((b'\x89PNG\r\n\x1a\n', 'png'),
                      (b'\xff\xd8\xff', 'jpg'),
                      (b'GIF87a', 'gif'),
                      (b'GIF89a', 'gif'),
                      (b'RIFF', 'webp'),
                      (b'BM', 'bmp'))
        for signature, format_name in signatures:
            if blob.startswith(signature):
                return format_name
        return None

    def original_bytes(self):
        """Return the imported bytes, or ``None`` for generated images.

        When the source blob was deferred (only the file path or the
        .prism archive reference was kept during import/load), read the
        original bytes from disk on demand.  This keeps import memory
        usage low while still preserving the exact original file bytes
        when saving.
        """
        if self._source_blob is not None:
            return self._source_blob
        if self._source_path and os.path.isfile(self._source_path):
            try:
                with open(self._source_path, 'rb') as f:
                    self._source_blob = f.read()
                if self._source_format is None:
                    self._source_format = (
                        self._format_from_filename(self._source_path)
                        or self._format_from_bytes(self._source_blob)
                        or 'png')
                return self._source_blob
            except (OSError, TypeError):
                logger.debug('Could not read deferred source %s',
                             self._source_path, exc_info=True)
        if self._prism_source:
            prism_path, item_id = self._prism_source
            try:
                import sqlite3
                conn = sqlite3.connect(
                    Path(prism_path).resolve().as_uri() + '?mode=ro', uri=True)
                try:
                    row = conn.execute(
                        'SELECT data FROM sqlar WHERE item_id = ?',
                        (item_id,)).fetchone()
                finally:
                    conn.close()
                if row and row[0]:
                    self._source_blob = row[0]
                    if self._source_format is None:
                        self._source_format = (
                            self._format_from_bytes(self._source_blob)
                            or 'png')
                    return self._source_blob
            except Exception:
                logger.debug('Could not read deferred source from %s',
                             prism_path, exc_info=True)
        return None

    def original_format(self):
        return self._source_format or self._format_from_filename(self.filename)

    def has_original_source(self):
        return bool(self._source_blob) or (
            self._source_path and os.path.isfile(self._source_path)) or (
            self._prism_source is not None)

    def materialize_source(self):
        """Force deferred source bytes into memory.

        Call before the source file might disappear (e.g. staging
        directory cleanup in zip imports).  After this call the blob
        lives in ``_source_blob`` regardless of whether the file still
        exists.
        """
        if self._source_blob is not None:
            return
        self.original_bytes()

    def set_preview_pixmap(self, pixmap, adjusted=True):
        """Replace display pixels without throwing away the original.

        Used by exposure/channel controls.  ``setPixmap`` remains the
        destructive operation for callers that intentionally replace an
        image's contents.
        """
        self._grayscale_cache = None
        self._display_decode_request = None
        self._display_decode_failed = False
        QtWidgets.QGraphicsPixmapItem.setPixmap(self, pixmap)
        if not pixmap.isNull() and self._source_blob is None:
            self._image_size = pixmap.size()
        self._preview_adjusted = bool(adjusted)
        self.update()

    def pixmap(self):
        """The full size image, decoded on demand.

        Callers that need real pixels - saving, exporting, cropping,
        copying - use this and get the original back.  Painting goes
        through _pixmap_for_painting() instead, which is happy with the
        preview until the image is shown large.
        """
        pixmap = QtWidgets.QGraphicsPixmapItem.pixmap(self)
        if pixmap.isNull() and self.has_original_source():
            self._decode_source_blob()
            pixmap = QtWidgets.QGraphicsPixmapItem.pixmap(self)
        return pixmap

    def _decode_source_blob(self):
        """Decode the stored original; the crop is deliberately kept."""
        self._grayscale_cache = None
        self._display_decode_request = None
        try:
            result = image_decode.decode(*image_decode.source_snapshot(self))
        except (OSError, sqlite3.Error):
            logger.debug('Could not decode original image', exc_info=True)
            return False
        if result is None:
            return False
        image, _size = result
        self._display_decode_failed = False
        pixmap = QtGui.QPixmap.fromImage(image)
        if self._image_size != pixmap.size():
            self.prepareGeometryChange()
            self._image_size = pixmap.size()
            if self.crop.isEmpty():
                self.reset_crop()
        QtWidgets.QGraphicsPixmapItem.setPixmap(self, pixmap)
        # Decoding images is what fills up memory, so this is where the
        # budget is worth looking at.
        _schedule_budget_check(self.scene())
        return True

    def ensure_full_pixmap(self):
        """Make sure the full size image is in memory."""
        return not self.pixmap().isNull()

    def has_full_pixmap(self):
        return not QtWidgets.QGraphicsPixmapItem.pixmap(self).isNull()

    def release_full_pixmap(self):
        """Drop the full size image, keeping the preview for painting.

        Used to bound memory on projects with many images: what is off
        screen does not need to sit in RAM at full resolution, and can
        be decoded again when the view comes back to it.
        """
        if not self.has_original_source() or self._preview_pixmap is None:
            # Nothing to fall back to, or nothing was loaded from a file
            return False
        if QtWidgets.QGraphicsPixmapItem.pixmap(self).isNull():
            return False
        self._grayscale_cache = None
        QtWidgets.QGraphicsPixmapItem.setPixmap(self, QtGui.QPixmap())
        return True

    def release_source_blob(self):
        """Drop the compressed source bytes if they can be re-read later.

        Keeps the preview pixmap for painting.  The blob can be
        re-fetched on demand via :meth:`original_bytes` from either
        ``_source_path`` (imported from a file) or ``_prism_source``
        (loaded from a .prism archive).
        """
        if self._source_blob is None:
            return False
        if self._preview_pixmap is None:
            return False
        can_reread = (
            (self._source_path and os.path.isfile(self._source_path))
            or self._prism_source is not None)
        if not can_reread:
            return False
        self._source_blob = None
        return True

    def load_stored_image(self, blob):
        """Load an image stored in a project file.

        Uses the preview cache to avoid decoding the original: what the
        canvas shows on opening is a small image anyway, and the full
        resolution one is only decoded once it is needed.

        When *blob* is ``None`` the item is set up for deferred loading:
        the caller must have set ``_prism_source`` so that
        :meth:`original_bytes` can fetch the bytes on demand.
        """
        if blob is None:
            # Deferred: blob will be read from .prism archive on demand
            # via _prism_source (set by the caller in sql.py).
            return True
        self.set_source_blob(blob, self._source_format)
        key = thumbnail_cache.digest(blob)
        preview = thumbnail_cache.cache().load(key)
        if preview is not None:
            self._apply_preview(preview, blob)
            return True
        image, size = thumbnail_cache.decode_preview_image(blob)
        if image is None:
            return False
        QtWidgets.QGraphicsPixmapItem.setPixmap(self, QtGui.QPixmap())
        self._preview_pixmap = QtGui.QPixmap.fromImage(image)
        self._image_size = size
        self._source_blob = bytes(blob)
        self.reset_crop()
        # The preview is in hand now; hand the blob to the cache builder
        # so the next open does not even have to do this much.
        thumbnail_cache.defer_preview(key, blob)
        return True

    def _apply_preview(self, preview, blob):
        """Use a cached preview and defer decoding the original."""
        QtWidgets.QGraphicsPixmapItem.setPixmap(self, QtGui.QPixmap())
        self._preview_pixmap = QtGui.QPixmap.fromImage(preview.image)
        self._image_size = preview.size
        self._source_blob = bytes(blob)
        self.reset_crop()

    def setPixmap(self, pixmap):
        self._grayscale_cache = None
        self._display_decode_request = None
        self._display_decode_failed = False
        QtWidgets.QGraphicsPixmapItem.setPixmap(self, pixmap)
        if not pixmap.isNull():
            # A new image is the full one now; any preview is stale, and
            # the stored original (if any) no longer matches it.
            self._image_size = pixmap.size()
            self._preview_pixmap = None
            self._source_blob = None
            self._source_format = None
            self._source_path = None
            self._prism_source = None
            self._perceptual_hash = None
            self._preview_adjusted = False
        self.reset_crop()

    def pixmap_from_bytes(self, data):
        """Set image pimap from a bytestring."""
        pixmap = QtGui.QPixmap()
        pixmap.loadFromData(data)
        self.setPixmap(pixmap)
        self.set_source_blob(data)

    def _pixmap_for_painting(self, painter):
        """Return (pixmap, source rect) to draw the cropped image.

        The preview stands in for the original as long as it is at least
        as detailed as what ends up on screen; past that the original is
        decoded, which is what makes zooming in on a big project load
        only the images that are actually looked at.
        """
        pixmap = QtWidgets.QGraphicsPixmapItem.pixmap(self)
        if not pixmap.isNull():
            return pixmap, QtCore.QRectF(self.crop)
        preview = self._preview_pixmap
        if preview is not None:
            if not self._preview_is_detailed_enough(painter, preview):
                image_decode.request_display(self)
            return preview, self._preview_source_rect(preview)
        scene = self.scene()
        busy = scene and (scene._import_in_progress or scene._interaction_in_progress)
        if not busy and (self._source_blob or self._source_path or self._prism_source):
            # Painting only queues immutable source references. File reads and
            # decoding run off-thread; QPixmap creation returns to the GUI.
            image_decode.request_display(self, thumbnail_cache.PREVIEW_MAX_SIDE)
        return pixmap, QtCore.QRectF(self.crop)

    def _preview_is_detailed_enough(self, painter, preview):
        scene = self.scene()
        if scene and (scene._import_in_progress or scene._interaction_in_progress):
            return True
        size = self._image_size
        longest = max(size.width(), size.height())
        if longest <= 0:
            return True
        transform = painter.combinedTransform()
        shown = longest * (transform.m11() ** 2 + transform.m12() ** 2) ** 0.5
        if shown <= max(preview.width(), preview.height()):
            return True
        # Painting must never stat a source file. A stored reference is
        # enough to attempt rehydration; the actual read handles missing files.
        return not (self._source_blob or self._source_path or self._prism_source)

    def _preview_source_rect(self, preview):
        size = self._image_size
        if size.width() <= 0 or size.height() <= 0:
            return QtCore.QRectF(preview.rect())
        scale_x = preview.width() / size.width()
        scale_y = preview.height() / size.height()
        crop = self.crop
        return QtCore.QRectF(
            crop.x() * scale_x, crop.y() * scale_y,
            crop.width() * scale_x, crop.height() * scale_y)

    def create_copy(self):
        item = PrismPixmapItem(QtGui.QImage(), self.filename)
        item.setPixmap(self.pixmap())
        if self._source_blob is not None:
            item.set_source_blob(self._source_blob, self._source_format)
            item._preview_adjusted = self._preview_adjusted
        item.setPos(self.pos())
        item.setZValue(self.zValue())
        item.setScale(self.scale())
        item.setRotation(self.rotation())
        item.setOpacity(self.opacity())
        item.grayscale = self.grayscale
        item.color_group = self.color_group
        item.dominant_color = self.dominant_color
        if self.flip() == -1:
            item.do_flip()
        item.crop = self.crop
        item._categories = list(self._categories)
        item._tags = list(self._tags)
        item._rating = self._rating
        item._notes = self._notes
        item._title = getattr(self, '_title', '')
        return item

    @cached_property
    def color_gamut(self):
        logger.debug(f'Calculating color gamut for {self}')
        gamut = defaultdict(int)
        img = self._analysis_image()
        if img is None or img.isNull():
            return gamut
        # Don't evaluate every pixel for larger images:
        step = max(1, int(max(img.width(), img.height()) / 1000))
        logger.debug(f'Considering every {step}. row/column')

        # Not actually faster than solution below :(
        # ptr = img.bits()
        # size = img.sizeInBytes()
        # pixelsize = int(img.sizeInBytes() / img.width() / img.height())
        # ptr.setsize(size)
        # for pixel in batched(ptr, n=pixelsize):
        #     r, g, b, alpha = tuple(map(ord, pixel))
        #     if 5 < alpha and 5 < r < 250 and 5 < g < 250 and 5 < b < 250:
        #         # Only consider pixels that aren't close to
        #         # transparent, white or black
        #         rgb = QtGui.QColor(r, g, b)
        #         gamut[rgb.hue(), rgb.saturation()] += 1

        for i in range(0, img.width(), step):
            for j in range(0, img.height(), step):
                rgb = img.pixelColor(i, j)
                rgbtuple = (rgb.red(), rgb.blue(), rgb.green())
                if (5 < rgb.alpha()
                        and min(rgbtuple) < 250 and max(rgbtuple) > 5):
                    # Only consider pixels that aren't close to
                    # transparent, white or black
                    gamut[rgb.hue(), rgb.saturation()] += 1

        logger.debug(f'Got {len(gamut)} color gamut values')
        return gamut


    def _analysis_image(self):
        """A small version of the image, enough for colour statistics.

        The colour analysis runs for every image in a project and throws
        away most of the detail anyway; using the preview keeps opening
        a large project from decoding originals just to look at them.
        """
        return self.analysis_image()

    def analysis_image(self, max_side=ANALYSIS_MAX_SIDE):
        """A small QImage of this image, for analysis and hashing.

        Full resolution is never needed for either: the preview is used
        when there is one, and otherwise the pixmap is scaled down
        directly rather than copied into a full size QImage first.
        """
        preview = self._preview_pixmap
        if preview is not None and not preview.isNull():
            return _small_image(preview, max_side)
        pixmap = self.pixmap()
        if pixmap.isNull():
            return None
        return _small_image(pixmap, max_side)

    def analyze_color_group(self):
        """Analyse the image and assign self.color_group / self.dominant_color."""
        import numpy as np

        img = self._analysis_image()
        if img is None or img.isNull():
            self.color_group = 'gray'
            self.dominant_color = '#828986'
            return
        max_side = 72
        w, h = img.width(), img.height()
        longest = max(w, h)
        if longest > max_side:
            scale = max_side / longest
            img = img.scaled(
                max(1, int(w * scale)), max(1, int(h * scale)),
                Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.FastTransformation)

        # Vectorised: the per-pixel loop this replaces called Qt once per
        # pixel and cost ~6.5ms per image, which added up to about ten
        # seconds on a project holding 1500 items.
        sample = _qimage_to_rgba(img)
        if sample is None:
            self.color_group = 'gray'
            self.dominant_color = '#828986'
            return
        opaque = sample[:, :, 3] >= 180
        if not opaque.any():
            self.color_group = 'gray'
            self.dominant_color = '#828986'
            return

        red = sample[:, :, 0].astype(np.float32)
        green = sample[:, :, 1].astype(np.float32)
        blue = sample[:, :, 2].astype(np.float32)
        hue, saturation, lightness = _hsl_arrays(
            sample[:, :, 0], sample[:, :, 1], sample[:, :, 2])
        index = _color_group_indices(hue, saturation, lightness)

        weights = np.where(opaque, 1.0, 0.0)
        neutral = np.isin(index, [_COLOR_INDEX[name]
                                  for name in ('black', 'white', 'gray')])
        weights *= np.where(neutral, 0.72, 1.0)
        weights *= np.where(saturation > 0.5, 1.9, 1.0)

        totals = np.zeros(len(COLOR_GROUPS), dtype=np.float64)
        np.add.at(totals, index.ravel(), weights.ravel())
        if not totals.any():
            self.color_group = 'gray'
            self.dominant_color = '#828986'
            return

        best_index = int(np.argmax(totals))
        best = COLOR_GROUPS[best_index]['id']
        total = totals[best_index]
        channels = []
        for channel in (red, green, blue):
            weighted = np.zeros(len(COLOR_GROUPS), dtype=np.float64)
            np.add.at(weighted, index.ravel(),
                      (channel * weights).ravel())
            channels.append(int(weighted[best_index] / total))
        self.color_group = best
        self.dominant_color = '#%02x%02x%02x' % tuple(channels)

    def copy_to_clipboard(self, clipboard):
        clipboard.setPixmap(self.pixmap())

    def reset_crop(self):
        size = self._image_size
        self.crop = QtCore.QRectF(0, 0, size.width(), size.height())

    @property
    def crop_handle_size(self):
        return self.fixed_length_for_viewport(self.CROP_HANDLE_SIZE)

    def crop_handle_topleft(self):
        topleft = self.crop_temp.topLeft()
        return QtCore.QRectF(
            topleft.x(),
            topleft.y(),
            self.crop_handle_size,
            self.crop_handle_size)

    def crop_handle_bottomleft(self):
        bottomleft = self.crop_temp.bottomLeft()
        return QtCore.QRectF(
            bottomleft.x(),
            bottomleft.y() - self.crop_handle_size,
            self.crop_handle_size,
            self.crop_handle_size)

    def crop_handle_bottomright(self):
        bottomright = self.crop_temp.bottomRight()
        return QtCore.QRectF(
            bottomright.x() - self.crop_handle_size,
            bottomright.y() - self.crop_handle_size,
            self.crop_handle_size,
            self.crop_handle_size)

    def crop_handle_topright(self):
        topright = self.crop_temp.topRight()
        return QtCore.QRectF(
            topright.x() - self.crop_handle_size,
            topright.y(),
            self.crop_handle_size,
            self.crop_handle_size)

    def crop_handles(self):
        return (self.crop_handle_topleft,
                self.crop_handle_bottomleft,
                self.crop_handle_bottomright,
                self.crop_handle_topright)

    def crop_edge_top(self):
        topleft = self.crop_temp.topLeft()
        return QtCore.QRectF(
            topleft.x() + self.crop_handle_size,
            topleft.y(),
            self.crop_temp.width() - 2 * self.crop_handle_size,
            self.crop_handle_size)

    def crop_edge_left(self):
        topleft = self.crop_temp.topLeft()
        return QtCore.QRectF(
            topleft.x(),
            topleft.y() + self.crop_handle_size,
            self.crop_handle_size,
            self.crop_temp.height() - 2 * self.crop_handle_size)

    def crop_edge_bottom(self):
        bottomleft = self.crop_temp.bottomLeft()
        return QtCore.QRectF(
            bottomleft.x() + self.crop_handle_size,
            bottomleft.y() - self.crop_handle_size,
            self.crop_temp.width() - 2 * self.crop_handle_size,
            self.crop_handle_size)

    def crop_edge_right(self):
        topright = self.crop_temp.topRight()
        return QtCore.QRectF(
            topright.x() - self.crop_handle_size,
            topright.y() + self.crop_handle_size,
            self.crop_handle_size,
            self.crop_temp.height() - 2 * self.crop_handle_size)

    def crop_edges(self):
        return (self.crop_edge_top,
                self.crop_edge_left,
                self.crop_edge_bottom,
                self.crop_edge_right)

    def get_crop_handle_cursor(self, handle):
        """Gets the crop cursor for the given handle."""

        is_topleft_or_bottomright = handle in (
            self.crop_handle_topleft, self.crop_handle_bottomright)
        return self.get_diag_cursor(is_topleft_or_bottomright)

    def get_crop_edge_cursor(self, edge):
        """Gets the crop edge cursor for the given edge."""

        top_or_bottom = edge in (
            self.crop_edge_top, self.crop_edge_bottom)
        sideways = (45 < self.rotation() < 135
                    or 225 < self.rotation() < 315)

        if top_or_bottom is sideways:
            return Qt.CursorShape.SizeHorCursor
        else:
            return Qt.CursorShape.SizeVerCursor

    def draw_crop_rect(self, painter, rect):
        """Paint a dotted rectangle for the cropping UI."""
        pen = QtGui.QPen(QtGui.QColor(255, 255, 255))
        pen.setWidth(2)
        pen.setCosmetic(True)
        painter.setPen(pen)
        painter.drawRect(rect)
        pen.setColor(QtGui.QColor(0, 0, 0))
        pen.setStyle(Qt.PenStyle.DotLine)
        painter.setPen(pen)
        painter.drawRect(rect)

    def paint(self, painter, option, widget):
        # Remembered so that the memory budget knows which images the
        # user is actually looking at.
        self._last_painted = time.monotonic()
        _schedule_opening_check(self.scene())
        if abs(painter.combinedTransform().m11()) < 2:
            # We want image smoothing, but only for images where we
            # are not zoomed in a lot. This is to ensure that for
            # example icons and pixel sprites can be viewed correctly.
            scene = self.scene()
            painter.setRenderHint(painter.RenderHint.SmoothPixmapTransform,
                                  not (scene and scene._interaction_in_progress))

        if self.crop_mode:
            self.paint_debug(painter, option, widget)

            # Darken image outside of cropped area.  Cropping needs real
            # pixels, so make sure the original is in memory.
            self.ensure_full_pixmap()
            painter.drawPixmap(0, 0, self.pixmap())
            path = QtWidgets.QGraphicsPixmapItem.shape(self)
            path.addRect(self.crop_temp)
            color = QtGui.QColor(0, 0, 0)
            color.setAlpha(100)
            painter.setBrush(QtGui.QBrush(color))
            painter.setPen(Qt.PenStyle.NoPen)
            painter.drawPath(path)
            painter.setBrush(QtGui.QBrush())

            for handle in self.crop_handles():
                self.draw_crop_rect(painter, handle())
            self.draw_crop_rect(painter, self.crop_temp)
        else:
            pm, source = self._pixmap_for_painting(painter)
            if not pm.isNull():
                painter.drawPixmap(self.crop, pm, source)
            self.paint_selectable(painter, option, widget)

    def enter_crop_mode(self):
        logger.debug(f'Entering crop mode on {self}')
        self.prepareGeometryChange()
        self.crop_mode = True
        self.crop_temp = QtCore.QRectF(self.crop)
        self.crop_mode_move = None
        self.crop_mode_event_start = None
        self.grabKeyboard()
        self.update()
        self.scene().crop_item = self

    def exit_crop_mode(self, confirm):
        logger.debug(f'Exiting crop mode with {confirm} on {self}')
        if confirm and self.crop != self.crop_temp:
            self.scene().undo_stack.push(
                commands.CropItem(self, self.crop_temp))
        self.prepareGeometryChange()
        self.crop_mode = False
        self.crop_temp = None
        self.crop_mode_move = None
        self.crop_mode_event_start = None
        self.ungrabKeyboard()
        self.update()
        self.scene().crop_item = None

    def keyPressEvent(self, event):
        if event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
            self.exit_crop_mode(confirm=True)
        elif event.key() == Qt.Key.Key_Escape:
            self.exit_crop_mode(confirm=False)
        else:
            super().keyPressEvent(event)

    def hoverMoveEvent(self, event):
        if not self.crop_mode:
            return super().hoverMoveEvent(event)

        for handle in self.crop_handles():
            if handle().contains(event.pos()):
                self.set_cursor(self.get_crop_handle_cursor(handle))
                return
        for edge in self.crop_edges():
            if edge().contains(event.pos()):
                self.set_cursor(self.get_crop_edge_cursor(edge))
                return
        self.unset_cursor()

    def mousePressEvent(self, event):
        if not self.crop_mode:
            return super().mousePressEvent(event)

        event.accept()
        for handle in self.crop_handles():
            # Click into a handle?
            if handle().contains(event.pos()):
                self.crop_mode_event_start = event.pos()
                self.crop_mode_move = handle
                return
        for edge in self.crop_edges():
            # Click into an edge handle?
            if edge().contains(event.pos()):
                self.crop_mode_event_start = event.pos()
                self.crop_mode_move = edge
                return
        # Click not in handle, end cropping mode:
        self.exit_crop_mode(
            confirm=self.crop_temp.contains(event.pos()))

    def ensure_point_within_crop_bounds(self, point, handle):
        """Returns the point, or the nearest point within the pixmap."""

        if handle == self.crop_handle_topleft:
            topleft = QtCore.QPointF(0, 0)
            bottomright = self.crop_temp.bottomRight()
        if handle == self.crop_handle_bottomleft:
            topleft = QtCore.QPointF(0, self.crop_temp.top())
            bottomright = QtCore.QPointF(
                self.crop_temp.right(), self.pixmap().size().height())
        if handle == self.crop_handle_bottomright:
            topleft = self.crop_temp.topLeft()
            bottomright = QtCore.QPointF(
                self.pixmap().size().width(), self.pixmap().size().height())
        if handle == self.crop_handle_topright:
            topleft = QtCore.QPointF(self.crop_temp.left(), 0)
            bottomright = QtCore.QPointF(
                self.pixmap().size().width(), self.crop_temp.bottom())
        if handle == self.crop_edge_top:
            topleft = QtCore.QPointF(0, 0)
            bottomright = QtCore.QPointF(
                self.pixmap().size().width(), self.crop_temp.bottom())
        if handle == self.crop_edge_bottom:
            topleft = QtCore.QPointF(0, self.crop_temp.top())
            bottomright = QtCore.QPointF(
                self.pixmap().size().width(), self.pixmap().size().height())
        if handle == self.crop_edge_left:
            topleft = QtCore.QPointF(0, 0)
            bottomright = QtCore.QPointF(
                self.crop_temp.right(), self.pixmap().size().height())
        if handle == self.crop_edge_right:
            topleft = QtCore.QPointF(self.crop_temp.left(), 0)
            bottomright = QtCore.QPointF(
                self.pixmap().size().width(), self.pixmap().size().height())

        point.setX(min(bottomright.x(), max(topleft.x(), point.x())))
        point.setY(min(bottomright.y(), max(topleft.y(), point.y())))

        return point

    def mouseMoveEvent(self, event):
        if self.crop_mode and self.crop_mode_event_start:
            diff = event.pos() - self.crop_mode_event_start
            if self.crop_mode_move == self.crop_handle_topleft:
                new = self.ensure_point_within_crop_bounds(
                    self.crop_temp.topLeft() + diff, self.crop_mode_move)
                self.crop_temp.setTopLeft(new)
            if self.crop_mode_move == self.crop_handle_bottomleft:
                new = self.ensure_point_within_crop_bounds(
                    self.crop_temp.bottomLeft() + diff, self.crop_mode_move)
                self.crop_temp.setBottomLeft(new)
            if self.crop_mode_move == self.crop_handle_bottomright:
                new = self.ensure_point_within_crop_bounds(
                    self.crop_temp.bottomRight() + diff, self.crop_mode_move)
                self.crop_temp.setBottomRight(new)
            if self.crop_mode_move == self.crop_handle_topright:
                new = self.ensure_point_within_crop_bounds(
                    self.crop_temp.topRight() + diff, self.crop_mode_move)
                self.crop_temp.setTopRight(new)
            if self.crop_mode_move == self.crop_edge_top:
                new = self.ensure_point_within_crop_bounds(
                    self.crop_temp.topLeft() + diff, self.crop_mode_move)
                self.crop_temp.setTop(new.y())
            if self.crop_mode_move == self.crop_edge_left:
                new = self.ensure_point_within_crop_bounds(
                    self.crop_temp.topLeft() + diff, self.crop_mode_move)
                self.crop_temp.setLeft(new.x())
            if self.crop_mode_move == self.crop_edge_bottom:
                new = self.ensure_point_within_crop_bounds(
                    self.crop_temp.bottomLeft() + diff, self.crop_mode_move)
                self.crop_temp.setBottom(new.y())
            if self.crop_mode_move == self.crop_edge_right:
                new = self.ensure_point_within_crop_bounds(
                    self.crop_temp.topRight() + diff, self.crop_mode_move)
                self.crop_temp.setRight(new.x())
            self.update()
            self.crop_mode_event_start = event.pos()
            event.accept()
        else:
            super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event):
        if self.crop_mode:
            self.crop_mode_move = None
            self.crop_mode_event_start = None
            event.accept()
        else:
            super().mouseReleaseEvent(event)


@register_item
class PrismTextItem(PrismItemMixin, QtWidgets.QGraphicsTextItem):
    """Class for text added by the user."""

    TYPE = 'text'

    def __init__(self, text=None, color=None, font=None, size=None,
                 bold=False, italic=False, **kwargs):
        super().__init__(text or "Text")
        self.save_id = None
        logger.trace('Initialized %s', self)
        self.is_image = False
        self.init_selectable()
        self.is_editable = True
        self.edit_mode = False
        self._apply_style(color=color, font=font, size=size,
                          bold=bold, italic=italic)
        self.init_metadata()

    def _apply_style(self, *, color=None, font=None, size=None,
                     bold=False, italic=False):
        """把样式设上去。缺哪一项就用默认值。

        **旧工程里的文字没有这些字段**（那几项是后来加的），所以每一项
        都要能独立缺省 —— 不能因为没存颜色就整段用不了。
        """
        qfont = self.font()
        if font:
            qfont.setFamily(str(font))
        if size:
            try:
                qfont.setPointSizeF(float(size))
            except (TypeError, ValueError):
                logger.warning('Ignoring bad text size %r', size)
        qfont.setBold(bool(bold))
        qfont.setItalic(bool(italic))
        self.setFont(qfont)
        self.setDefaultTextColor(
            QtGui.QColor(color) if color
            else QtGui.QColor(*COLORS['Scene:Text']))

    #: 样式字段 —— 存和读共用一处，免得两边写岔。
    STYLE_FIELDS = ('color', 'font', 'size', 'bold', 'italic')

    def style(self):
        """当前的样式，形状和存盘一致（可以直接喂回 `_apply_style`）。"""
        qfont = self.font()
        return {
            'color': self.defaultTextColor().name(),
            'font': qfont.family(),
            'size': qfont.pointSizeF(),
            'bold': qfont.bold(),
            'italic': qfont.italic(),
        }

    def set_style(self, **changes):
        """改样式（属性面板用的）。只传要改的那几项。"""
        merged = self.style()
        merged.update({key: value for key, value in changes.items()
                       if key in self.STYLE_FIELDS})
        # pointSizeF() 在没有点尺寸时返回 -1，那种当作"没设字号"
        if not merged.get('size') or merged['size'] <= 0:
            merged['size'] = None
        self._apply_style(**merged)

    @classmethod
    def create_from_data(cls, **kwargs):
        data = kwargs.get('data', {})
        item = cls(**data)
        return item

    def __str__(self):
        txt = self.toPlainText()[:40]
        return (f'Text "{txt}"')

    def get_extra_save_data(self):
        # 样式也存下来。以前只存文字，所以颜色字号改了也留不住 ——
        # 保存再打开就变回默认色。
        data = {'text': self.toPlainText()}
        data.update(self.style())
        data.update(self.get_metadata_save_data())
        return data

    def contains(self, point):
        return self.boundingRect().contains(point)

    def paint(self, painter, option, widget):
        painter.setPen(Qt.PenStyle.NoPen)
        color = QtGui.QColor(0, 0, 0)
        color.setAlpha(40)
        brush = QtGui.QBrush(color)
        painter.setBrush(brush)
        painter.drawRect(QtWidgets.QGraphicsTextItem.boundingRect(self))
        option.state = QtWidgets.QStyle.StateFlag.State_Enabled
        super().paint(painter, option, widget)
        self.paint_selectable(painter, option, widget)

    def create_copy(self):
        item = PrismTextItem(self.toPlainText())
        item.setPos(self.pos())
        item.setZValue(self.zValue())
        item.setScale(self.scale())
        item.setRotation(self.rotation())
        if self.flip() == -1:
            item.do_flip()
        item._categories = list(self._categories)
        item._tags = list(self._tags)
        item._rating = self._rating
        item._notes = self._notes
        item._title = getattr(self, '_title', '')
        return item

    def enter_edit_mode(self):
        logger.debug(f'Entering edit mode on {self}')
        if self.edit_mode or self.scene() is None:
            return
        self.edit_mode = True
        self.old_text = self.toPlainText()
        self.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextEditorInteraction)
        self.scene().edit_item = self
        self.setFocus(Qt.FocusReason.OtherFocusReason)

    def exit_edit_mode(self, commit=True):
        logger.debug(f'Exiting edit mode on {self}')
        if not self.edit_mode or self.scene() is None:
            return
        self.edit_mode = False
        # reset selection:
        self.setTextCursor(QtGui.QTextCursor(self.document()))
        self.setTextInteractionFlags(Qt.TextInteractionFlag.NoTextInteraction)
        self.scene().edit_item = None
        if commit:
            self.scene().undo_stack.push(
                commands.ChangeText(self, self.toPlainText(), self.old_text))
            if not self.toPlainText().strip():
                logger.debug('Removing empty text item')
                self.scene().undo_stack.push(
                    commands.DeleteItems(self.scene(), [self]))
        else:
            self.setPlainText(self.old_text)

    def has_selection_handles(self):
        return super().has_selection_handles() and not self.edit_mode

    def keyPressEvent(self, event):
        if (event.key() in (Qt.Key.Key_Enter, Qt.Key.Key_Return)
                and event.modifiers() == Qt.KeyboardModifier.NoModifier):
            self.exit_edit_mode()
            event.accept()
            return
        if (event.key() == Qt.Key.Key_Escape
                and event.modifiers() == Qt.KeyboardModifier.NoModifier):
            self.exit_edit_mode(commit=False)
            event.accept()
            return
        super().keyPressEvent(event)

    def copy_to_clipboard(self, clipboard):
        clipboard.setText(self.toPlainText())


#: 画笔的默认颜色和粗细。用户在悬浮工具条上改。
DEFAULT_PEN_COLOR = '#ff5050'
DEFAULT_PEN_WIDTH = 2.0

#: 绘制工具的种类。同一时间只有一种是激活的（互斥）。
DRAW_TOOL_PEN = 'pen'          # 自由画笔：跟着鼠标走，留下轨迹
DRAW_TOOL_LINE = 'line'        # 直线：按下定起点，松开定终点
#: **能出现在存盘数据里**的工具 —— 只有会留下图元的那两种。橡皮擦是
#: 一种*操作*，它不产生任何东西，所以不在这个元组里。
DRAW_TOOLS = (DRAW_TOOL_PEN, DRAW_TOOL_LINE)

#: 界面上的三种模式。橡皮擦只在这里 —— 它删东西，不留东西。
DRAW_TOOL_ERASER = 'eraser'
DRAW_MODES = (DRAW_TOOL_PEN, DRAW_TOOL_LINE, DRAW_TOOL_ERASER)

#: 线条端点（箭头）的四种样子。
ARROW_NONE = 'none'            # 普通直线，两头都没有箭头
ARROW_START = 'start'          # 箭头在起点
ARROW_END = 'end'              # 箭头在终点
ARROW_BOTH = 'both'            # 两头都有
ARROW_STYLES = (ARROW_NONE, ARROW_START, ARROW_END, ARROW_BOTH)

#: 线型：实线 / 虚线 / 点线。工具条上「直线」那个下拉里选。
LINE_SOLID = 'solid'
LINE_DASHED = 'dashed'
LINE_DOTTED = 'dotted'
LINE_STYLES = (LINE_SOLID, LINE_DASHED, LINE_DOTTED)

LINE_STYLE_PENS = {
    LINE_SOLID: Qt.PenStyle.SolidLine,
    LINE_DASHED: Qt.PenStyle.DashLine,
    LINE_DOTTED: Qt.PenStyle.DotLine,
}


@register_item
class PrismPathItem(PrismItemMixin, QtWidgets.QGraphicsPathItem):
    """画笔画出来的一条线 —— 自由笔画或者直线（可以带箭头）。

    **为什么需要这个类**：画笔原来走的是 `scene.addPath()`，那产生的是
    裸的 `QGraphicsPathItem` —— 它没有 `save_id`，而 `items_for_save()`
    只收有 `save_id` 的（`scene.py` 里写着 "items that have a save_id
    attribute"）。所以**画完一存盘就消失**，用户白画。

    另外原来画完立刻把 `ItemIsSelectable` / `ItemIsMovable` 设成 False，
    所以画错的线既选不中也删不掉，只能撤销。

    点列表是存盘的形状 —— 比序列化 `QPainterPath` 简单，而且读回来时
    按点重建就行。没点或只有一个点时画一个圆点，"点一下"也得留下东西。

    **直线只留首尾两点**：`tool == DRAW_TOOL_LINE` 时 `_points` 就是
    `[起点, 终点]`，拖动过程中的中间点不进来 —— 那是预览用的临时状态，
    不该进存盘数据。
    """

    TYPE = 'path'

    def __init__(self, points=None, color=None, width=None,
                 tool=None, arrow=None, line_style=None, **kwargs):
        super().__init__()
        self.save_id = None
        # **这些要在 `logger.trace` 之前设好。** `%s` 会调 `__str__`，
        # 而 `__str__` 读 `_points` —— 顺序反了的话 `__init__` 中途抛
        # AttributeError，`_rebuild()` 没跑到，路径就是空的（画出来
        # 什么都没有），而且 logging 在格式化失败时会递归报错。
        self._points = [tuple(point) for point in (points or [])]
        self._color = str(color or DEFAULT_PEN_COLOR)
        self._width = float(DEFAULT_PEN_WIDTH if width is None else width)
        # 存盘数据可能是旧版本写的（没有这几个字段），也可能被手改坏了
        # —— 认不出来就当自由笔画、实线、不带箭头，不抛。
        self._tool = tool if tool in DRAW_TOOLS else DRAW_TOOL_PEN
        self._arrow = arrow if arrow in ARROW_STYLES else ARROW_NONE
        self._line_style = (line_style if line_style in LINE_STYLES
                            else LINE_SOLID)
        logger.trace('Initialized %s', self)
        self.is_image = False
        self.init_selectable()
        self._rebuild()
        self.init_metadata()

    def __str__(self):
        # `getattr` 兜底：这个对象可能在 `_points` 设好之前就被打印
        # （比如初始化中途出错时的日志）。
        return f'Path ({len(getattr(self, "_points", []) or [])} points)'

    @classmethod
    def create_from_data(cls, **kwargs):
        return cls(**kwargs.get('data', {}))

    # ── 形状 ────────────────────────────────────────────────────────

    def _rebuild(self):
        """按点列表重建路径和画笔。"""
        path = QtGui.QPainterPath()
        if len(self._points) == 1:
            # 单点：画个圆点。只 moveTo 不 lineTo 的话路径是空的，
            # 用户点一下画布什么都看不到。
            x, y = self._points[0]
            radius = max(self._width, 1.0) / 2.0
            path.addEllipse(QtCore.QPointF(x, y), radius, radius)
        elif self._points:
            path.moveTo(*self._points[0])
            for point in self._points[1:]:
                path.lineTo(*point)
        self.setPath(path)

        pen = QtGui.QPen(QtGui.QColor(self._color), max(0.5, self._width))
        pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
        pen.setStyle(LINE_STYLE_PENS.get(self._line_style,
                                         Qt.PenStyle.SolidLine))
        self.setPen(pen)

    def set_points(self, points):
        """整条重设（画的过程中每移动一次调一次）。"""
        self._points = [tuple(point) for point in points]
        self._rebuild()

    def points(self):
        return list(self._points)

    def tool(self):
        return self._tool

    def arrow(self):
        return self._arrow

    # ── 样式（颜色 / 粗细 / 工具 / 箭头）────────────────────────────

    def style(self):
        return {'color': self._color, 'width': self._width,
                'tool': self._tool, 'arrow': self._arrow,
                'line_style': self._line_style}

    def set_style(self, color=None, width=None, tool=None, arrow=None,
                  line_style=None):
        """改样式。只传要改的那几项。"""
        if color:
            self._color = str(color)
        if width is not None:
            self._width = float(width)
        if tool in DRAW_TOOLS:
            self._tool = tool
        if arrow in ARROW_STYLES:
            self._arrow = arrow
        if line_style in LINE_STYLES:
            self._line_style = line_style
        self._rebuild()

    # ── 箭头 ────────────────────────────────────────────────────────

    def _arrow_size(self):
        """箭头多大 —— 跟着线宽走，但不能小到看不见。"""
        return max(8.0, self._width * 4.0)

    def _endpoints(self):
        """首尾两点。点数不够时给 None。"""
        if len(self._points) < 2:
            return None
        start = QtCore.QPointF(*self._points[0])
        end = QtCore.QPointF(*self._points[-1])
        return start, end

    def _paint_arrow_head(self, painter, tail, tip):
        """在 `tip` 处画一个指向 `tip` 的箭头，方向从 `tail` 指过来。"""
        size = self._arrow_size()
        dx = tip.x() - tail.x()
        dy = tip.y() - tail.y()
        length = (dx * dx + dy * dy) ** 0.5
        if length < 1e-6:
            # 起点和终点重合 —— 线的方向是没定义的，硬画会得到一个
            # 乱七八糟的多边形。用户"点了一下没拖"就会走到这里。
            return
        # 线很短时把箭头按比例缩小，不然它会比线还长，看起来像一团墨
        size = min(size, length * 0.8)
        ux, uy = dx / length, dy / length
        base_x, base_y = tip.x() - ux * size, tip.y() - uy * size
        half = size * 0.42
        left = QtCore.QPointF(base_x - uy * half, base_y + ux * half)
        right = QtCore.QPointF(base_x + uy * half, base_y - ux * half)
        painter.drawPolygon(QtGui.QPolygonF([tip, left, right]))

    def _paint_arrows(self, painter):
        """画两端的箭头（按当前 arrow 样式）。"""
        if self._arrow not in (ARROW_START, ARROW_END, ARROW_BOTH):
            return
        ends = self._endpoints()
        if ends is None:
            return
        start, end = ends
        painter.save()
        painter.setPen(Qt.PenStyle.NoPen)
        # 箭头和线**同色同粗细** —— 需求里明确要求，也是唯一不会看错的
        painter.setBrush(QtGui.QBrush(QtGui.QColor(self._color)))
        if self._arrow in (ARROW_END, ARROW_BOTH):
            self._paint_arrow_head(painter, start, end)
        if self._arrow in (ARROW_START, ARROW_BOTH):
            self._paint_arrow_head(painter, end, start)
        painter.restore()

    # ── 绘制 ────────────────────────────────────────────────────────

    def paint(self, painter, option, widget):
        # `option.state` 要打开 Enabled，不然 Qt 会把颜色调淡
        # （它以为这个图元被禁用了）。
        option.state = QtWidgets.QStyle.StateFlag.State_Enabled
        super().paint(painter, option, widget)
        self._paint_arrows(painter)
        # 选中框和缩放手柄 —— 少了这一句，选中的线看不出被选中了
        self.paint_selectable(painter, option, widget)

    # ── 存盘 ────────────────────────────────────────────────────────

    def get_extra_save_data(self):
        data = {
            'points': [[float(x), float(y)] for x, y in self._points],
            'color': self._color,
            'width': self._width,
            'tool': self._tool,
            'arrow': self._arrow,
            'line_style': self._line_style,
        }
        data.update(self.get_metadata_save_data())
        return data

    def contains(self, point):
        return self.shape().contains(point)

    def bounding_rect_unselected(self):
        """从**路径本身**算包围盒，不经过 Qt 的 `QGraphicsPathItem`。

        **不能调 `super()`，也不能调 `QtWidgets.QGraphicsPathItem.
        boundingRect(self)`。** 那两条路都会让 Qt 去问 `shape()`，而 PyQt
        会把那个虚函数调用**转发回 Python**（`SelectableMixin.shape()`），
        它又问回 `bounding_rect_unselected()` —— 互相调用到栈溢出，进程
        直接以 `0xC0000409` 崩掉，连 Python 异常都来不及抛。

        `self.path()` 返回的是 `QPainterPath`（纯数据），它的
        `boundingRect()` 不会再回调过来，所以这里断得干净。
        """
        rect = self.path().boundingRect()
        if rect.isNull():
            # 一个点都没有：给个能画选中框的最小矩形，
            # 返回空矩形的话选中框和手柄都画不出来。
            return QtCore.QRectF(-2, -2, 4, 4)
        # 笔宽要算进去，不然细线点不中
        margin = max(0.5, self._width) / 2.0 + 1.0
        return rect.marginsAdded(
            QtCore.QMarginsF(margin, margin, margin, margin))


@register_item
class PrismErrorItem(PrismItemMixin, QtWidgets.QGraphicsTextItem):
    """Class for displaying error messages when an item can't be loaded
    from a bee file.

    This item will be displayed instead of the original item. It won't
    save to bee files. The original item will be preserved in the bee
    file, unless this item gets deleted by the user, or a new bee file
    is saved.
    """

    TYPE = 'error'

    def __init__(self, text=None, **kwargs):
        super().__init__(text or "Text")
        self.original_save_id = None
        logger.trace('Initialized %s', self)
        self.is_image = False
        self.init_selectable()
        self.is_editable = False
        self.setDefaultTextColor(QtGui.QColor(*COLORS['Scene:Text']))
        self.init_metadata()

    @classmethod
    def create_from_data(cls, **kwargs):
        data = kwargs.get('data', {})
        item = cls(**data)
        return item

    def __str__(self):
        txt = self.toPlainText()[:40]
        return (f'Error "{txt}"')

    def contains(self, point):
        return self.boundingRect().contains(point)

    def paint(self, painter, option, widget):
        painter.setPen(Qt.PenStyle.NoPen)
        color = QtGui.QColor(200, 0, 0)
        brush = QtGui.QBrush(color)
        painter.setBrush(brush)
        painter.drawRect(QtWidgets.QGraphicsTextItem.boundingRect(self))
        option.state = QtWidgets.QStyle.StateFlag.State_Enabled
        super().paint(painter, option, widget)
        self.paint_selectable(painter, option, widget)

    def update_from_data(self, **kwargs):
        self.original_save_id = kwargs.get('save_id', self.original_save_id)
        self.setPos(kwargs.get('x', self.pos().x()),
                    kwargs.get('y', self.pos().y()))
        self.setZValue(kwargs.get('z', self.zValue()))
        self.setScale(kwargs.get('scale', self.scale()))
        self.setRotation(kwargs.get('rotation', self.rotation()))

    def create_copy(self):
        item = PrismErrorItem(self.toPlainText())
        item.setPos(self.pos())
        item.setZValue(self.zValue())
        item.setScale(self.scale())
        item.setRotation(self.rotation())
        return item

    def flip(self, *args, **kwargs):
        """Returns the flip value (1 or -1)"""
        # Never display error messages flipped
        return 1

    def do_flip(self, *args, **kwargs):
        """Flips the item."""
        # Never flip error messages
        pass

    def copy_to_clipboard(self, clipboard):
        clipboard.setText(self.toPlainText())


GLB_THUMBNAIL_SIZE = 640


@register_item
class PrismGlbItem(PrismItemMixin, QtWidgets.QGraphicsPixmapItem):
    """A glTF/GLB model, drawn as a rendered thumbnail on the canvas.

    The mesh is never rendered into the canvas itself: Prism's canvas is a
    QGraphicsView and nesting a second OpenGL context inside it is fragile.
    The interactive preview lives in its own window instead, opened by
    double-clicking the item.
    """

    TYPE = 'glb'

    def __init__(self, filename=None, glb_blob=None, glb_path=None):
        """Create a 3D model item.

        :param filename: Original filename for display and saving
        :param glb_blob: Raw model bytes (for embedded storage)
        :param glb_path: Path to the model on disk (reference storage)
        """
        super().__init__()
        self.save_id = None
        self.filename = filename
        self.is_image = False
        self.is_editable = False
        self._glb_blob = glb_blob
        self._glb_path = glb_path
        self._is_reference = False
        self._mesh = None
        self._load_error = None
        self._preview_window = None
        self.init_selectable()
        self._build_thumbnail()
        logger.trace('Initialized %s', self)

    def __str__(self):
        return f'3D model "{self.filename}"'

    # ── Model access ─────────────────────────────────────────

    def _ensure_mesh(self):
        """Parse the model once, remembering the first failure.

        Only :class:`~prism.glb_preview.GLBError` is caught: an unreadable
        model must leave the item usable rather than taking the canvas down.
        """
        if self._mesh is not None or self._load_error is not None:
            return self._mesh
        try:
            if self._glb_blob:
                if (self.filename or '').lower().endswith('.gltf'):
                    self._mesh = parse_gltf(self._glb_blob)
                else:
                    self._mesh = parse_glb(self._glb_blob)
            elif self._glb_path:
                self._mesh = load_mesh(self._glb_path)
            else:
                raise GLBError('no model data')
        except GLBError as exc:
            self._load_error = str(exc)
            logger.info(f'Could not read 3D model {self.filename}: {exc}')
        return self._mesh

    def _build_thumbnail(self):
        mesh = self._ensure_mesh()
        if mesh is None:
            self._set_placeholder()
            return
        image = render_mesh(
            mesh, QtCore.QSize(GLB_THUMBNAIL_SIZE, GLB_THUMBNAIL_SIZE),
            max_triangles=FAST_QUALITY_TRIANGLES, antialiasing=False)
        self.setPixmap(QtGui.QPixmap.fromImage(image))

    def _set_placeholder(self):
        """Neutral tile so an unreadable model still shows on the canvas."""
        size = GLB_THUMBNAIL_SIZE
        pixmap = QtGui.QPixmap(size, size)
        pixmap.fill(QtGui.QColor(48, 48, 54))
        painter = QtGui.QPainter(pixmap)
        try:
            painter.setRenderHint(QtGui.QPainter.RenderHint.Antialiasing)
            painter.setPen(QtGui.QPen(QtGui.QColor(190, 190, 200, 170), 6))
            inset = size * 0.28
            painter.drawRect(QtCore.QRectF(
                inset, inset, size - 2 * inset, size - 2 * inset))
        finally:
            painter.end()
        self.setPixmap(pixmap)

    def _notify(self, message):
        """Best-effort in-app notification, falling back to the log.

        ``prism.widgets`` is imported lazily because it pulls in modules that
        import this one.
        """
        logger.info(message)
        try:
            from prism.widgets import PrismNotification
        except ImportError:     # pragma: no cover - circular import guard
            return
        scene = self.scene()
        views = scene.views() if scene is not None else []
        if views:
            PrismNotification(views[0], message)

    def open_preview(self):
        """Show (or refresh) the interactive preview window."""
        mesh = self._ensure_mesh()
        if mesh is None:
            self._notify(
                _('The 3D model could not be read: {reason}').format(
                    reason=self._load_error or _('unknown error')))
            return
        if self._preview_window is None:
            parent = None
            scene = self.scene()
            views = scene.views() if scene is not None else []
            if views:
                parent = views[0]
            name = os.path.basename(self.filename) if self.filename else ''
            self._preview_window = GLBPreviewWindow(mesh, title=name,
                                                    parent=parent)
        else:
            self._preview_window.surface.set_mesh(mesh)
        self._preview_window.show()
        self._preview_window.raise_()
        self._preview_window.activateWindow()

    # ── Persistence ──────────────────────────────────────────

    @classmethod
    def create_from_data(cls, **kwargs):
        item = kwargs.pop('item')
        data = kwargs.pop('data', {})
        item.filename = item.filename or data.get('filename')
        item.setOpacity(data.get('opacity', 1))
        if 'isReference' in data:
            item._is_reference = data['isReference']
        if data.get('modelPath'):
            item._glb_path = data['modelPath']
        return item

    def get_extra_save_data(self):
        data = {
            'filename': self.filename,
            'opacity': self.opacity(),
            'isReference': self._is_reference,
        }
        if self._is_reference and self._glb_path:
            data['modelPath'] = os.path.abspath(self._glb_path)
        return data

    def get_filename_for_export(self, imgformat, save_id_default=None):
        save_id = self.save_id or save_id_default
        assert save_id is not None
        if self.filename:
            basename = os.path.splitext(os.path.basename(self.filename))[0]
            ext = os.path.splitext(self.filename)[1] or '.glb'
            return f'{save_id:04}-{basename}{ext}'
        return f'{save_id:04}.glb'


@register_item
class PrismVideoItem(PrismItemMixin, QtWidgets.QGraphicsPixmapItem):
    """Class for video items added by the user.

    Displays a thumbnail (first frame or placeholder) when not playing.
    When playing, updates the pixmap from QMediaPlayer video frames.
    """

    TYPE = 'video'

    def __init__(self, video_url=None, filename=None, video_blob=None):
        """Create a video item.

        :param video_url: QUrl pointing to the video file on disk
        :param filename: Original filename for display/save purposes
        :param video_blob: Raw video bytes (for embedded storage)
        """
        super().__init__()
        self.save_id = None
        self.filename = filename
        self.is_image = False
        self.is_editable = False
        self.init_selectable()

        self._video_url = video_url
        self._video_blob = video_blob
        self._is_reference = False
        self._playing = False
        self._loop = False
        self._muted = False
        self._player = None
        self._player_source = None
        self._audio_output = None
        self._video_sink = None
        self._tmp_video_file = None
        self._control_bar = None
        self._thumb_player = None
        self._thumb_audio = None
        self._thumb_sink = None
        self._current_frame = None

        self._generate_placeholder()
        # Thumbnail capture deferred to itemChange(ItemSceneHasChanged)
        # to ensure the Qt event loop is running and media backend is ready

        if filename:
            logger.trace('Initialized %s', self)

    def _generate_placeholder(self):
        """Create a placeholder thumbnail with a play icon."""
        w, h = 640, 360
        pm = QtGui.QPixmap(w, h)
        pm.fill(QtGui.QColor(50, 50, 50))
        p = QtGui.QPainter(pm)
        p.setRenderHint(QtGui.QPainter.RenderHint.Antialiasing)
        p.setBrush(QtGui.QColor(200, 200, 200, 180))
        p.setPen(Qt.PenStyle.NoPen)
        cx, cy = w // 2, h // 2
        r = 35
        p.drawEllipse(QtCore.QPointF(cx, cy), r, r)
        p.setBrush(QtGui.QColor(50, 50, 50))
        tri = QtGui.QPolygonF([
            QtCore.QPointF(cx - 10, cy - 18),
            QtCore.QPointF(cx - 10, cy + 18),
            QtCore.QPointF(cx + 18, cy),
        ])
        p.drawPolygon(tri)
        p.end()
        self.setPixmap(pm)

    def _capture_thumbnail(self):
        """Asynchronously capture the first frame as thumbnail.

        Uses a temporary QMediaPlayer with an explicit QVideoSink.
        The first valid frame that the sink delivers becomes the
        thumbnail; the temp player is torn down right after.
        """
        player = QMediaPlayer()
        audio = QAudioOutput()
        audio.setMuted(True)
        audio.setVolume(0)
        sink = QVideoSink()
        player.setAudioOutput(audio)
        player.setVideoOutput(sink)
        player.setSource(self._video_url)

        state = {'done': False}

        def teardown():
            for signal, slot in (
                    (player.mediaStatusChanged, on_media_status),
                    (player.errorOccurred, on_error),
                    (sink.videoFrameChanged, on_video_frame)):
                try:
                    signal.disconnect(slot)
                except (TypeError, RuntimeError):
                    pass
            player.deleteLater()
            sink.deleteLater()
            audio.deleteLater()
            # Drop our references so later cleanup() won't touch the
            # deleted C++ objects.
            if self._thumb_player is player:
                self._thumb_player = None
            if self._thumb_audio is audio:
                self._thumb_audio = None
            if self._thumb_sink is sink:
                self._thumb_sink = None

        def on_video_frame(frame):
            if state['done']:
                return
            if frame.isValid():
                state['done'] = True
                img = frame.toImage()
                if not img.isNull():
                    self.setPixmap(QtGui.QPixmap.fromImage(img))
                    if self._control_bar:
                        self._control_bar.reposition()
                    self.update()
                player.stop()
                teardown()

        def on_error(error, error_string):
            logger.debug(f'Thumbnail capture error: '
                         f'{error} {error_string}')
            state['done'] = True
            teardown()

        def on_media_status(status):
            if status == QMediaPlayer.MediaStatus.EndOfMedia:
                # No usable frame was delivered; give up quietly and
                # keep the placeholder.
                state['done'] = True
                teardown()

        sink.videoFrameChanged.connect(on_video_frame)
        player.mediaStatusChanged.connect(on_media_status)
        player.errorOccurred.connect(on_error)
        player.play()
        self._thumb_player = player
        self._thumb_audio = audio
        self._thumb_sink = sink

    @classmethod
    def create_from_data(cls, **kwargs):
        item = kwargs.pop('item')
        data = kwargs.pop('data', {})
        item.filename = item.filename or data.get('filename')
        item.setOpacity(data.get('opacity', 1))
        if 'loop' in data:
            item._loop = data['loop']
        if 'mute' in data:
            item._muted = data['mute']
        if 'isReference' in data:
            item._is_reference = data['isReference']
        if data.get('videoPath'):
            path = data['videoPath']
            if os.path.exists(path):
                item._video_url = QUrl.fromLocalFile(path)
                item._capture_thumbnail()
            else:
                logger.warning(f'Video file missing: {path}')
        return item

    def __str__(self):
        return f'Video "{self.filename}"'

    def get_extra_save_data(self):
        data = {
            'filename': self.filename,
            'opacity': getattr(self, '_filter_original_opacity', self.opacity()),
            'loop': self._loop,
            'mute': self._muted,
            'isReference': self._is_reference,
        }
        if self._is_reference and self._video_url:
            path = self._video_url.toLocalFile()
            if path:
                data['videoPath'] = os.path.abspath(path)
        return data

    def get_filename_for_export(self, imgformat, save_id_default=None):
        save_id = self.save_id or save_id_default
        assert save_id is not None
        if self.filename:
            basename = os.path.splitext(
                os.path.basename(self.filename))[0]
            ext = os.path.splitext(self.filename)[1] or '.mp4'
            return f'{save_id:04}-{basename}{ext}'
        return f'{save_id:04}.mp4'

    # ── Playback ─────────────────────────────────────────────

    def play(self):
        """Start or resume video playback."""
        if not self._video_url:
            return
        path = self._video_url.toLocalFile()
        if not os.path.exists(path):
            logger.warning(f'Video file not found: {path}')
            return

        if self._video_blob and not self._tmp_video_file:
            ext = os.path.splitext(path)[1] or '.mp4'
            self._tmp_video_file = tempfile.NamedTemporaryFile(
                suffix=ext, delete=False)
            self._tmp_video_file.write(self._video_blob)
            self._tmp_video_file.close()
            self._player_source = QUrl.fromLocalFile(
                self._tmp_video_file.name)
        else:
            self._player_source = self._video_url

        if self._player is None:
            self._player = QMediaPlayer()
            self._audio_output = QAudioOutput()
            self._video_sink = QVideoSink()
            self._player.setAudioOutput(self._audio_output)
            self._player.setVideoOutput(self._video_sink)
            self._audio_output.setMuted(self._muted)
            self._player.setLoops(
                QMediaPlayer.Loops.Infinite
                if self._loop
                else QMediaPlayer.Loops.Once)
            self._player.setSource(self._player_source)
            self._video_sink.videoFrameChanged.connect(
                self._on_video_frame)
            self._player.mediaStatusChanged.connect(
                self._on_media_status)

        self._player.play()
        self._playing = True
        if self._control_bar:
            self._control_bar.show()
            self._control_bar.update_state(True)
        self.update()

    def pause(self):
        """Pause video playback."""
        if self._player:
            self._player.pause()
        self._playing = False
        if self._control_bar:
            self._control_bar.update_state(False)
        self.update()

    def toggle_play(self):
        """Toggle between play and pause."""
        if self._playing:
            self.pause()
        else:
            if (self._player
                    and self._player.position()
                    >= self._player.duration()):
                self._player.setPosition(0)
            self.play()

    def set_muted(self, muted):
        self._muted = muted
        if self._audio_output:
            self._audio_output.setMuted(muted)

    def set_loop(self, loop):
        self._loop = loop
        if self._player:
            self._player.setLoops(
                QMediaPlayer.Loops.Infinite
                if loop
                else QMediaPlayer.Loops.Once)

    def seek(self, position_ms):
        if self._player:
            self._player.setPosition(position_ms)

    def position(self):
        if self._player:
            return self._player.position()
        return 0

    def duration(self):
        if self._player:
            return self._player.duration()
        return 0

    # ── Internal slots ───────────────────────────────────────

    def _on_video_frame(self, frame):
        if frame.isValid():
            img = frame.toImage()
            if not img.isNull():
                self._current_frame = QtGui.QPixmap.fromImage(img)
                self.update()

    def _on_media_status(self, status):
        if status == QMediaPlayer.MediaStatus.EndOfMedia:
            if not self._loop:
                self._playing = False
                if self._control_bar:
                    self._control_bar.update_state(False)
                self.update()

    # ── Painting ─────────────────────────────────────────────

    def paint(self, painter, option, widget):
        if abs(painter.combinedTransform().m11()) < 2:
            painter.setRenderHint(
                painter.RenderHint.SmoothPixmapTransform)

        if self._playing and self._current_frame:
            painter.drawPixmap(
                self.boundingRect().toRect(),
                self._current_frame,
                self._current_frame.rect())
        else:
            painter.drawPixmap(
                self.boundingRect().toRect(),
                self.pixmap(),
                self.pixmap().rect())

            # Centered play button overlay (YouTube-style)
            rect = self.boundingRect()
            cx = rect.center().x()
            cy = rect.center().y()
            radius = min(rect.width(), rect.height()) * 0.12
            radius = max(radius, 18)

            # Semi-transparent dark circle background
            painter.setBrush(QtGui.QColor(0, 0, 0, 140))
            painter.setPen(Qt.PenStyle.NoPen)
            painter.drawEllipse(QtCore.QPointF(cx, cy), radius, radius)

            # White play triangle
            tri_r = radius * 0.45
            offset_x = tri_r * 0.2  # slight right offset for optical center
            triangle = QtGui.QPolygonF([
                QtCore.QPointF(cx - tri_r + offset_x, cy - tri_r),
                QtCore.QPointF(cx - tri_r + offset_x, cy + tri_r),
                QtCore.QPointF(cx + tri_r + offset_x, cy),
            ])
            painter.setBrush(QtGui.QColor(255, 255, 255, 220))
            painter.setPen(Qt.PenStyle.NoPen)
            painter.drawPolygon(triangle)

        # Never draw selection outline on video items
        option.state &= ~QtWidgets.QStyle.StateFlag.State_Selected


    def has_selection_outline(self):
        return False
    # ── Copy / clipboard ─────────────────────────────────────

    def create_copy(self):
        item = PrismVideoItem(self._video_url, self.filename,
                            self._video_blob)
        item._is_reference = self._is_reference
        item._loop = self._loop
        item._muted = self._muted
        item.load_metadata_from_data(self.get_metadata_save_data())
        item.setPos(self.pos())
        item.setZValue(self.zValue())
        item.setScale(self.scale())
        item.setRotation(self.rotation())
        item.setOpacity(self.opacity())
        if self.flip() == -1:
            item.do_flip()
        return item

    def copy_to_clipboard(self, clipboard):
        if self.filename:
            clipboard.setText(self.filename)

    # ── Cleanup ──────────────────────────────────────────────

    def cleanup(self):
        """Stop playback and remove temporary files."""
        if self._player:
            self._player.stop()
            try:
                self._player.mediaStatusChanged.disconnect()
            except (TypeError, RuntimeError):
                pass
            self._player = None
            self._audio_output = None
        if self._video_sink:
            try:
                self._video_sink.videoFrameChanged.disconnect()
            except (TypeError, RuntimeError):
                pass
            self._video_sink = None
        if self._thumb_player:
            self._thumb_player.stop()
            try:
                self._thumb_player.mediaStatusChanged.disconnect()
                self._thumb_player.positionChanged.disconnect()
            except (TypeError, RuntimeError):
                pass
            self._thumb_player = None
            self._thumb_audio = None
        if self._thumb_sink:
            try:
                self._thumb_sink.videoFrameChanged.disconnect()
            except (TypeError, RuntimeError):
                pass
            self._thumb_sink = None
        if self._tmp_video_file:
            try:
                os.unlink(self._tmp_video_file.name)
            except OSError:
                pass
            self._tmp_video_file = None
        if self._control_bar:
            self._control_bar.hide()

    def itemChange(self, change, value):
        if change == QtWidgets.QGraphicsItem.GraphicsItemChange.ItemPositionHasChanged:
            # Force scene to repaint old area to avoid ghosting
            if self._playing and self.scene():
                self.scene().update(self.sceneBoundingRect())
        elif change == QtWidgets.QGraphicsItem.GraphicsItemChange.ItemSceneChange:
            if self._playing:
                self.cleanup()
            if value is None and self._control_bar:
                # Item leaves the scene; drop the control bar with it
                self._control_bar.hide()
                self._control_bar.deleteLater()
                self._control_bar = None
        elif (change
              == QtWidgets.QGraphicsItem.GraphicsItemChange.ItemSceneHasChanged):
            if self.scene() and self._control_bar is None:
                from prism.widgets.video_controls import (
                    PrismVideoControlProxy)
                self._control_bar = PrismVideoControlProxy(self)
            # Capture thumbnail now that item is in scene and event loop is active
            if self._video_url and self.pixmap().size() == QtCore.QSize(640, 360):
                QtCore.QTimer.singleShot(100, self._capture_thumbnail)
        return super().itemChange(change, value)

    def hoverEnterEvent(self, event):
        super().hoverEnterEvent(event)
        if self._control_bar and self._playing:
            self._control_bar.show()

    def hoverLeaveEvent(self, event):
        super().hoverLeaveEvent(event)
        if self._control_bar and not self._playing:
            self._control_bar.hide()
