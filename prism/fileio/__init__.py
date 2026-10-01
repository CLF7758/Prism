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

from PyQt6 import QtCore

from prism import commands
from prism.config import PrismSettings
from prism.fileio.errors import PrismFileIOError
from prism.fileio.image import load_image
from prism.fileio.sql import SQLiteIO, is_prism_file
from prism.glb_preview import GLB_EXTENSIONS
from prism.items import (PrismGlbItem, PrismPixmapItem, PrismVideoItem,
                         VIDEO_EXTENSIONS)


__all__ = [
    'is_prism_file',
    'is_video_file',
    'load_prism',
    'save_prism',
    'load_images',
    'load_media',
    'ThreadedLoader',
    'PrismFileIOError',
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


def load_prism(filename, scene, worker=None):
    """Load Prism native file."""
    logger.info(f'Loading from file {filename}...')
    io = SQLiteIO(filename, scene, readonly=True, worker=worker)
    return io.read()


def save_prism(filename, scene, create_new=False, worker=None):
    """Save Prism native file."""
    logger.info(f'Saving to file {filename}...')
    logger.debug(f'Create new: {create_new}')
    io = SQLiteIO(filename, scene, create_new, worker=worker)
    io.write()
    logger.info('End save')


def load_images(filenames, pos, scene, worker):
    """Add images to existing scene."""

    errors = []
    items = []
    worker.begin_processing.emit(len(filenames))
    for i, filename in enumerate(filenames):
        logger.info(f'Loading image from file {filename}')
        img, filename = load_image(filename)
        worker.progress.emit(i)
        if img.isNull():
            logger.info(f'Could not load file {filename}')
            errors.append(filename)
            continue

        item = PrismPixmapItem(img, filename)
        item.set_source_blob(_read_local_source(filename))
        item.analyze_color_group()
        item.set_pos_center(pos)
        scene.add_item_later({'item': item, 'type': 'pixmap'}, selected=True)
        items.append(item)
        if worker.canceled:
            break
        # Give main thread time to process items:
        worker.msleep(10)

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


def load_media(filenames, pos, scene, worker):
    """Add images, videos and 3D models to existing scene."""
    from PyQt6.QtCore import QUrl

    errors = []
    items = []
    worker.begin_processing.emit(len(filenames))
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
            embed = (mode == 'embed'
                     or (mode == 'auto' and size_mb <= threshold_mb))
            video_blob = None
            if embed:
                with open(local_path, 'rb') as f:
                    video_blob = f.read()
            item = PrismVideoItem(video_url=video_url,
                                filename=local_path,
                                video_blob=video_blob)
            item._is_reference = not embed
            item.set_pos_center(pos)
            scene.add_item_later(
                {'item': item, 'type': 'video'}, selected=True)
            items.append(item)
        elif ext in GLB_EXTENSIONS:
            logger.info(f'Loading 3D model from file {local_path}')
            item = _create_glb_item(local_path)
            if item is None:
                errors.append(local_path)
                continue
            item.set_pos_center(pos)
            scene.add_item_later(
                {'item': item, 'type': 'glb'}, selected=True)
            items.append(item)
        else:
            logger.info(f'Loading image from file {filename}')
            img, fn = load_image(filename)
            if img.isNull():
                logger.info(f'Could not load file {filename}')
                errors.append(str(filename))
                continue
            item = PrismPixmapItem(img, fn)
            item.set_source_blob(_read_local_source(local_path))
            item.set_pos_center(pos)
            scene.add_item_later(
                {'item': item, 'type': 'pixmap'}, selected=True)
            items.append(item)

        if worker.canceled:
            break
        worker.msleep(20)

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
