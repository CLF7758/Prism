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

import logging
import os.path

from PyQt6 import QtCore, QtGui, QtWidgets

from prism import commands
from prism.config import PrismSettings
from prism.fileio.errors import PrismFileIOError
from prism.fileio.image import load_image
from prism.fileio.sql import (SQLiteIO, is_prism_file, is_readonly_project,
                               request_readonly_project,
                               load_deferred_board_extras,
                               read_page_body, read_attachment,
                               StoredBody, StoredAttachment)
from prism.glb_preview import GLB_EXTENSIONS
from prism.items import (PrismGlbItem, PrismPixmapItem, PrismVideoItem,
                         VIDEO_EXTENSIONS)

from prism.performance_trace import open_phase


__all__ = [
    'is_prism_file',
    'is_video_file',
    'load_prism',
    'save_prism',
    'load_images',
    'load_media',
    'ThreadedLoader',
    'PrismFileIOError',
    'is_readonly_project',
    'request_readonly_project',
    # The deferred bodies. The interface layer asks for these through this
    # package; it must not reach into `prism.fileio.sql` for them.
    'load_deferred_board_extras',
    'read_page_body',
    'read_attachment',
    'StoredBody',
    'StoredAttachment',
]


def is_video_file(path):
    """Check if a file path points to a video file."""
    ext = os.path.splitext(str(path))[1].lower()
    return ext in VIDEO_EXTENSIONS

logger = logging.getLogger(__name__)


def _read_local_source(path):
    """Read imported bytes so the project can preserve the original file."""
    if not isinstance(path, (str, os.PathLike)):
        return None
    try:
        with open(path, 'rb') as handle:
            return handle.read()
    except (OSError, TypeError):
        logger.debug('Could not retain source bytes for %s', path,
                     exc_info=True)
        return None


def load_prism(filename, scene, worker=None, scope=None):
    """Load Prism native file.

    ``scope`` 是 item id 的序列：给定时只读这一页（有界按页打开），
    未给定时是全量读取（旧行为）。按页读会在 scene 上登记
    ``_loaded_scope_ids`` 与 ``_partial_project_source``，保存必须走部分快照。

    Opening a 51.81 GiB project used to leave one unbreakable number in the
    log. The two stages are timed separately so a slow open says which half
    was slow: connecting/reading the archive, or turning rows into items.
    """
    logger.info(f'Loading from file {filename}...')
    with open_phase('read_archive', filename=str(filename),
                    paged=scope is not None,
                    source_bytes=(os.path.getsize(str(filename))
                                  if os.path.exists(str(filename)) else None)):
        io = SQLiteIO(filename, scene, readonly=True, worker=worker)
        try:
            return io.read(scope=scope)
        finally:
            io._close_connection()


def _start_background_materialization(scene):
    """Start extracting the embedded videos a save is about to write.

    The bytes of an embedded video live inside the .prism archive and have
    to be pulled out before they can be written into the new file.  Doing
    that on the thread that saves would keep it busy for the whole
    extraction, so the work is handed to ``source_store``'s background
    service here, before the write starts.  ``SQLiteIO.insert_item`` then
    finds the extracted file waiting for it and only has to copy it.
    """
    if scene is None:
        return
    try:
        # ``PrismScene.items()`` sorts every item of the scene, which is
        # far too much work for this scan; the base implementation hands
        # out the plain list.
        items = QtWidgets.QGraphicsScene.items(scene)
    except (AttributeError, RuntimeError, TypeError):
        return
    for item in items:
        if getattr(item, 'TYPE', None) != 'video':
            continue
        if not hasattr(item, 'save_id'):
            # Only the items that are actually written are worth extracting.
            continue
        request = getattr(item, 'request_materialization', None)
        if not callable(request):
            continue
        try:
            request()
        except Exception:                       # noqa: BLE001
            logger.debug('Could not start the background materialization',
                         exc_info=True)


def save_prism(filename, scene, create_new=False, worker=None, board_snapshot=None,
               partial_snapshot=None):
    """Save Prism native file."""
    from prism.storage.managed import is_directory_target, root_for
    if is_directory_target(filename) or root_for(filename) is not None:
        from prism.storage.managed_io import save_directory
        return save_directory(filename, scene, worker=worker, board_snapshot=board_snapshot,
                              partial_snapshot=partial_snapshot)
    logger.info(f'Saving to file {filename}...')
    logger.debug(f'Create new: {create_new}')
    # 内嵌视频的原始字节要先从存档里提出来。写入之前就在后台开始提取，
    # 这样写盘线程走到 _ensure_materialized() 时字节多半已经就位，
    # 提取本身也从来不在调用保存的那条线程上发生。
    _start_background_materialization(scene)
    io = SQLiteIO(filename, scene, create_new, worker=worker,
                  board_snapshot=board_snapshot, partial_snapshot=partial_snapshot)
    receipt = io.write()
    logger.info('End save')
    return receipt


def _load_image_item(filename, as_record=False):
    retained = None
    local = filename if isinstance(filename, str) else (
        filename.toLocalFile() if filename.isLocalFile() else None)
    if local and os.path.isfile(local):
        from prism.source_store import retain_file
        from prism.thumbnail_cache import PREVIEW_MAX_SIDE
        try:
            retained = retain_file(local)
        except OSError:
            logger.exception('Could not preserve imported source %s', local)
            return None
        reader = QtGui.QImageReader(retained)
        reader.setAutoTransform(True)
        size = reader.size()
        if size.isValid() and not size.isEmpty():
            rotation = bool(reader.transformation().value & 4)
            if as_record:
                from prism.asset_records import ImageRecord
                if rotation:
                    size.transpose()
                item = ImageRecord({'data': {'filename': os.path.normpath(local),
                    'imageWidth': size.width(), 'imageHeight': size.height()}}, None)
                item._source_path = retained
                return item
            if max(size.width(), size.height()) > PREVIEW_MAX_SIDE:
                reader.setScaledSize(size.scaled(
                    PREVIEW_MAX_SIDE, PREVIEW_MAX_SIDE,
                    QtCore.Qt.AspectRatioMode.KeepAspectRatio))
            image = reader.read()
            if not image.isNull():
                if rotation:
                    size.transpose()
                item = PrismPixmapItem(QtGui.QImage(), os.path.normpath(local))
                item._preview_pixmap = QtGui.QPixmap.fromImage(image)
                item._image_size = size
                item._source_path = retained
                item.reset_crop()
                return item
    if local:
        if retained is None:
            return None
        # Decode the verified owned copy, never re-read a possibly changed
        # external source after retaining it.
        image, _ = load_image(retained)
        name = os.path.normpath(local)
        original_blob = None
    else:
        image, name, original_blob = load_image(filename, with_source=True)
        if not original_blob:
            return None
    if image.isNull():
        return None
    item = PrismPixmapItem(image, name)
    if retained is not None:
        item._source_path = retained
    else:
        from urllib.parse import urlsplit
        extension = os.path.splitext(urlsplit(name).path)[1]
        item.set_source_blob(original_blob, item._format_from_bytes(original_blob) or extension)
    return item


def load_images(filenames, pos, scene, worker):
    """Add images to existing scene."""

    errors = []
    items = []
    worker.begin_processing.emit(len(filenames))
    from prism.asset_records import MIN_VIRTUAL_IMAGES
    virtual_images = len(filenames) >= MIN_VIRTUAL_IMAGES
    for i, filename in enumerate(filenames):
        logger.info(f'Loading image from file {filename}')
        item = _load_image_item(filename, as_record=True) if virtual_images else _load_image_item(filename)
        if i % 32 == 0 or i == len(filenames)-1:
            worker.progress.emit(i)
        if item is None:
            logger.info(f'Could not load file {filename}')
            errors.append(filename)
            continue

        item.set_pos_center(pos)
        scene.add_item_later({'item': item, 'type': 'pixmap'},
                             selected=len(filenames) <= 200)
        items.append(item)
        if worker.canceled:
            break
        # Give main thread time to process items:
        if (i + 1) % 32 == 0:
            worker.msleep(1)

    scene.undo_stack.push(
        commands.InsertItems(scene, items, ignore_first_redo=True))
    worker.finished.emit('', errors)




def _create_glb_item(path):
    """Build a GLB item, embedding the model when the settings say so.

    Returns ``None`` when the file cannot be read at all. A model that reads
    but fails to parse still yields an item, which then shows a placeholder
    tile instead of silently disappearing.
    """
    try:
        size_mb = os.path.getsize(path) / (1024 * 1024)
    except OSError as exc:
        logger.info(f'Could not stat 3D model {path}: {exc}')
        return None
    settings = PrismSettings()
    mode = settings.valueOrDefault('Items/glb_embed_mode')
    threshold_mb = settings.valueOrDefault('Items/glb_embed_threshold_mb')
    embed = mode == 'embed' or (mode == 'auto' and size_mb <= threshold_mb)
    glb_blob = None
    if embed:
        try:
            with open(path, 'rb') as handle:
                glb_blob = handle.read()
        except OSError as exc:
            logger.info(f'Could not read 3D model {path}: {exc}')
            return None
    item = PrismGlbItem(filename=path, glb_blob=glb_blob, glb_path=path)
    item._is_reference = not embed
    return item


def load_media_with_originals(filenames, pos, scene, worker):
    return load_media(filenames, pos, scene, worker, force_video_embed=True)


def load_media(filenames, pos, scene, worker, force_video_embed=False, item_metadata=None):
    """Add images, videos and 3D models to existing scene."""
    from PyQt6.QtCore import QUrl

    errors = []
    items = []
    worker.begin_processing.emit(len(filenames))
    from prism.asset_records import MIN_VIRTUAL_IMAGES
    virtual_images = len(filenames) >= MIN_VIRTUAL_IMAGES
    metadata_offsets = {}
    def packet(item, kind, path):
        result = {'item':item, 'type':kind}
        choices = (item_metadata or {}).get(path, ())
        if choices:
            number = metadata_offsets.get(path, 0)
            metadata_offsets[path] = number + 1
            data = dict(choices[number])
            from prism.item_lifecycle import is_record
            if is_record(item):
                item._extra_data.update(data)
                item.load_metadata_from_data(data)
                item._display_thumbnail = data.get('displayThumbnail')
                item._overview_payload = data.get('displayOverview')
            else:
                result['data'] = data  # Hydrated on the GUI before scene insertion.
        return result
    for i, filename in enumerate(filenames):
        if i % 50 == 0 or i == len(filenames) - 1:
            worker.progress.emit(i)

        if isinstance(filename, str):
            local_path = os.path.normpath(filename)
        elif hasattr(filename, 'isLocalFile') and filename.isLocalFile():
            local_path = os.path.normpath(filename.toLocalFile())
        elif hasattr(filename, 'toLocalFile'):
            local_path = os.path.normpath(filename.toLocalFile())
        else:
            local_path = str(filename)

        ext = os.path.splitext(local_path)[1].lower()

        if ext in VIDEO_EXTENSIONS:
            logger.info(f'Loading video from file {local_path}')
            video_url = QUrl.fromLocalFile(local_path)
            size_mb = os.path.getsize(local_path) / (1024 * 1024)
            settings = PrismSettings()
            mode = settings.valueOrDefault('Items/video_embed_mode')
            threshold_mb = settings.valueOrDefault(
                'Items/video_embed_threshold_mb')
            embed = (force_video_embed or mode == 'embed'
                     or (mode == 'auto' and size_mb <= threshold_mb))
            video_blob = None
            if embed and not force_video_embed:
                with open(local_path, 'rb') as f:
                    video_blob = f.read()
            item = PrismVideoItem(video_url=video_url,
                                filename=local_path,
                                video_blob=video_blob)
            item._is_reference = not embed
            if force_video_embed:
                from prism.source_store import retain_file
                item._video_source_path = retain_file(local_path)
                item._video_url = QUrl.fromLocalFile(item._video_source_path)
            item.set_pos_center(pos)
            scene.add_item_later(
                packet(item, 'video', local_path), selected=len(filenames) <= 200)
            items.append(item)
        elif ext in GLB_EXTENSIONS:
            logger.info(f'Loading 3D model from file {local_path}')
            item = _create_glb_item(local_path)
            if item is None:
                errors.append(local_path)
                continue
            item.set_pos_center(pos)
            scene.add_item_later(
                packet(item, 'glb', local_path), selected=len(filenames) <= 200)
            items.append(item)
        else:
            logger.info(f'Loading image from file {filename}')
            item = _load_image_item(filename, as_record=True) if virtual_images else _load_image_item(filename)
            if item is None:
                logger.info(f'Could not load file {filename}')
                errors.append(str(filename))
                continue
            item.set_pos_center(pos)
            scene.add_item_later(
                packet(item, 'pixmap', local_path), selected=len(filenames) <= 200)
            items.append(item)

        if worker.canceled:
            break
        if (i + 1) % 50 == 0:
            worker.msleep(1)

    scene.undo_stack.push(
        commands.InsertItems(scene, items, ignore_first_redo=True))
    worker.finished.emit('', errors)


class ThreadedIO(QtCore.QThread):
    """Dedicated thread for loading and saving."""

    progress = QtCore.pyqtSignal(int)
    finished = QtCore.pyqtSignal(str, list)
    begin_processing = QtCore.pyqtSignal(int)
    user_input_required = QtCore.pyqtSignal(str)

    def __init__(self, func, *args, **kwargs):
        super().__init__()
        self.func = func
        self.args = args
        self.kwargs = kwargs
        self.kwargs['worker'] = self
        self.canceled = False

    def run(self):
        self.func(*self.args, **self.kwargs)

    def on_canceled(self):
        self.canceled = True
