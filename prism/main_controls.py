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
import os

from PyQt6 import QtCore, QtGui
from PyQt6.QtCore import QUrl
from PyQt6.QtCore import Qt

from prism import commands, widgets
from prism.i18n import _
from prism.items import PrismPixmapItem
from prism import fileio


logger = logging.getLogger(__name__)

# Common image extensions (avoids QImageReader.canRead which crashes on non-image files)
_IMAGE_EXTS = {'.png', '.jpg', '.jpeg', '.gif', '.bmp', '.webp', '.tiff', '.tif',
               '.svg', '.ico', '.jfif', '.jpe', '.ppm', '.pgm', '.pbm', '.xbm', '.xpm',
               '.psd', '.exr', '.hdr',
               # 3D models: dropped like images, previewed as their own item
               '.glb', '.gltf'}


class MainControlsMixin:
    """Basic controls shared by the main view and the welcome overlay:

    * Right-click menu
    * Dropping files
    * Moving the window without title bar
    """

    def init_main_controls(self, main_window):
        self.main_window = main_window
        self.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.customContextMenuRequested.connect(
            self.control_target.on_context_menu)
        self.setAcceptDrops(True)
        self.movewin_active = False

    def on_action_movewin_mode(self):
        if self.movewin_active:
            # Pressing the same shortcut again should end the action
            self.exit_movewin_mode()
        else:
            self.enter_movewin_mode()

    @property
    def viewport_or_self(self):
        if hasattr(self, 'viewport'):
            return self.viewport()
        return self

    def enter_movewin_mode(self):
        logger.debug('Entering movewin mode')
        self.setMouseTracking(True)
        self.movewin_active = True
        self.viewport_or_self.setCursor(Qt.CursorShape.SizeAllCursor)
        self.event_start = QtCore.QPointF(self.cursor().pos())
        if hasattr(self, 'disable_mouse_events'):
            self.disable_mouse_events()

    def exit_movewin_mode(self):
        logger.debug('Exiting movewin mode')
        self.setMouseTracking(False)
        self.movewin_active = False
        self.viewport_or_self.unsetCursor()
        if hasattr(self, 'enable_mouse_events'):
            self.enable_mouse_events()

    def dragEnterEvent(self, event):
        mimedata = event.mimeData()
        logger.debug(f'Drag enter event: {mimedata.formats()}')
        if mimedata.hasUrls():
            event.acceptProposedAction()
        elif mimedata.hasImage():
            event.acceptProposedAction()
        else:
            msg = _('Attempted drop not an image or image too big')
            logger.info(msg)
            widgets.PrismNotification(self.control_target, msg)

    def dragMoveEvent(self, event):
        event.acceptProposedAction()

    def dropEvent(self, event):
        mimedata = event.mimeData()
        logger.debug(f'Handling file drop: {mimedata.formats()}')
        pos = QtCore.QPoint(round(event.position().x()),
                            round(event.position().y()))
        if mimedata.hasUrls():
            logger.debug(f'Found dropped urls: {mimedata.urls()}')
            if not self.control_target.scene.items():
                path = mimedata.urls()[0]
                if (path.isLocalFile()
                        and fileio.is_prism_file(path.toLocalFile())):
                    self.control_target.open_from_file(path.toLocalFile())
                    return

            # Separate folders from files, collect media with per-folder categories
            from PyQt6.QtCore import QFileInfo
            urls = []
            folder_categories = {}  # {normalized_folder_path: category_name}
            #: 有媒体的目录 -> 目录名。拖进来的文件夹会按这个结构变成画布。
            folder_trees = {}
            for url in mimedata.urls():
                if not url.isLocalFile():
                    urls.append(url)
                    continue
                fi = QFileInfo(url.toLocalFile())
                if fi.isDir():
                    folder_path = os.path.normpath(fi.absoluteFilePath())
                    folder_name = fi.fileName()
                    folder_categories[folder_path] = folder_name
                    try:
                        for root, dirs, files in os.walk(folder_path):
                            root = os.path.normpath(root)
                            for f in files:
                                filepath = os.path.join(root, f)
                                ext = os.path.splitext(f)[1].lower()
                                if ext in fileio.VIDEO_EXTENSIONS or ext in _IMAGE_EXTS:
                                    urls.append(QUrl.fromLocalFile(filepath))
                                    # 这个目录里有媒体，它就会变成一个画布。
                                    # 只记目录名：层级关系由路径本身决定，
                                    # 不必再建一棵树。
                                    folder_trees.setdefault(
                                        root, os.path.basename(root))
                    except Exception:
                        logger.debug(f'Error walking folder: {folder_path}')
                else:
                    urls.append(url)

            if not urls:
                logger.info('No media files found in dropped folder(s)')
                return
            # 没有文件夹时**不传** folder_trees：这条调用的签名被别人的测试
            # 钉着（test_view.py:test_drop_when_url），能用默认值就别去动它。
            if folder_trees:
                self.control_target.do_insert_images(
                    urls, pos, folder_categories=folder_categories or None,
                    folder_trees=folder_trees)
            else:
                self.control_target.do_insert_images(
                    urls, pos, folder_categories=folder_categories or None)
        elif mimedata.hasImage():
            img = QtGui.QImage(mimedata.imageData())
            item = PrismPixmapItem(img)
            pos = self.control_target.mapToScene(pos)
            self.control_target.undo_stack.push(
                commands.InsertItems(self.control_target.scene, [item], pos))
        else:
            logger.info('Drop not an image')

    def mousePressEventMainControls(self, event):
        if self.movewin_active:
            self.exit_movewin_mode()
            event.accept()
            return True

        action, inverted =\
            self.control_target.keyboard_settings.mouse_action_for_event(event)
        if action == 'movewindow':
            self.enter_movewin_mode()
            event.accept()
            return True

    def mouseMoveEventMainControls(self, event):
        if self.movewin_active:
            pos = self.mapToGlobal(event.position())
            delta = pos - self.event_start
            self.event_start = pos
            self.main_window.move(self.main_window.x() + int(delta.x()),
                                  self.main_window.y() + int(delta.y()))
            event.accept()
            return True

    def mouseReleaseEventMainControls(self, event):
        if self.movewin_active:
            self.exit_movewin_mode()
            event.accept()
            return True

    def keyPressEventMainControls(self, event):
        if self.movewin_active:
            self.exit_movewin_mode()
            event.accept()
            return True
