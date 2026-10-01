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

"""Mind map editor hosted in a QWebEngineView.

The editing surface is simple-mind-map (MIT), shipped unmodified under
``prism/assets/mindmap``. Python and the page talk over QWebChannel: Python
pushes the board's tree in and receives every change back as JSON, which is
then stored in the board file.

Node pictures never live in the tree.  An imported XMind brings its images
inline as ``data:`` URLs, and a whole sheet of them would be megabytes of
base64 inside the board file; the tree keeps a short ``prism://attach/N``
reference instead and the bytes go to the scene's attachment table, the
same place the document editor keeps its images.  The page is handed the
bytes it needs as a lookup table when a tree is loaded.

The page is loaded lazily on first use, because spinning up Chromium costs
noticeable time at startup and most sessions never open the mind map.
"""

import json
import logging
import os
import time

# Chromium needs these before QtWebEngine initialises; without them a
# headless or remote session fails to create a context at all.
os.environ.setdefault(
    'QTWEBENGINE_CHROMIUM_FLAGS',
    '--disable-gpu --disable-software-rasterizer --no-sandbox '
    '--disable-features=RendererCodeIntegrity')

from PyQt6 import QtCore, QtGui, QtWidgets  # noqa: E402

from prism.fileio.attachments import (  # noqa: E402
    ATTACHMENT_PREFIX, MAX_ATTACHMENT_EDGE, attachment_for, attachment_url,
    decode_data_url, encode_data_url, node_data)
from prism.fileio.mindmap_export import MindMapExportError  # noqa: E402
from prism.i18n import _  # noqa: E402
from prism.widgets import components  # noqa: E402

try:                                            # pragma: no cover - optional
    from PyQt6 import QtWebChannel, QtWebEngineCore, QtWebEngineWidgets
    WEBENGINE_AVAILABLE = True
except ImportError:                             # pragma: no cover
    QtWebChannel = None
    QtWebEngineCore = None
    QtWebEngineWidgets = None
    WEBENGINE_AVAILABLE = False


logger = logging.getLogger(__name__)


MINDMAP_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    'assets', 'mindmap')

#: Formats Chromium renders as they are; anything else is re-encoded.
PASSTHROUGH_EXTENSIONS = {
    '.png': 'image/png',
    '.jpg': 'image/jpeg',
    '.jpeg': 'image/jpeg',
    '.gif': 'image/gif',
    '.webp': 'image/webp',
    '.bmp': 'image/bmp',
    '.svg': 'image/svg+xml',
}


def content_signature(tree):
    """A comparable form of a tree ignoring where the view was scrolled."""
    if not isinstance(tree, dict):
        return ''
    rest = {key: value for key, value in tree.items()
            if key != '_prism_view'}
    try:
        return json.dumps(rest, ensure_ascii=False, sort_keys=True)
    except (TypeError, ValueError):             # pragma: no cover - defensive
        return repr(rest)


def parse_tree_payload(payload):
    """Read what the page sent: ``{tree, images}`` or a bare tree."""
    if not payload:
        return None, {}
    try:
        parsed = json.loads(payload)
    except ValueError:
        logger.info('Ignoring malformed mind map data from the page')
        return None, {}
    if isinstance(parsed, dict) and 'tree' in parsed:
        images = parsed.get('images')
        return parsed.get('tree'), images if isinstance(images, dict) else {}
    return parsed, {}


def mindmap_available():
    """Whether the mind map page and its host can both run."""
    if not WEBENGINE_AVAILABLE:
        return False
    required = (
        os.path.join(MINDMAP_DIR, 'index.html'),
        os.path.join(MINDMAP_DIR, 'vendor', 'simpleMindMap.esm.min.js'),
        os.path.join(MINDMAP_DIR, 'vendor', 'simpleMindMap.esm.min.css'),
    )
    missing = [path for path in required if not os.path.exists(path)]
    if missing:
        logger.debug('Mind map assets missing: %s', ', '.join(missing))
        return False
    return True


if WEBENGINE_AVAILABLE:

    class MindMapBridge(QtCore.QObject):
        """Python end of the QWebChannel connection.

        JavaScript can only call slots; signals travel the other way and are
        connected to from the page. The slots below exist so the page has
        something to call, and immediately re-emit the matching signal for
        the rest of Prism to listen on.
        """

        #: Emitted by Python, connected to by the page.
        loadTree = QtCore.pyqtSignal(str)
        requestTree = QtCore.pyqtSignal()
        resetView = QtCore.pyqtSignal()
        imagePicked = QtCore.pyqtSignal(str)

        #: Internal: mirrors what the page reports.
        ready = QtCore.pyqtSignal()
        treeChanged = QtCore.pyqtSignal(str)
        treeLoaded = QtCore.pyqtSignal(bool, str)
        pickImageRequested = QtCore.pyqtSignal()

        @QtCore.pyqtSlot()
        def notifyReady(self):
            self.ready.emit()

        @QtCore.pyqtSlot(str)
        def pushTree(self, payload):
            self.treeChanged.emit(payload)

        @QtCore.pyqtSlot(bool, str)
        def notifyTreeLoaded(self, ok, error):
            self.treeLoaded.emit(ok, error)

        @QtCore.pyqtSlot()
        def pickImage(self):
            """The page cannot open a file dialog; the panel does it."""
            self.pickImageRequested.emit()


class MindMapPanel(QtWidgets.QWidget):
    """Board mind map: a tree the user edits, saved with the board."""

    def __init__(self, view, parent=None, page_id=None):
        super().__init__(parent)
        self.view = view
        self.page_id = page_id
        self._ready = False
        self._loaded = False
        self._pending_tree = None
        self._tree = None
        #: What the page was last given, so an unchanged load is skipped.
        self._sent_tree = None
        #: The page echoes a tree right after loading it (its own bookkeeping
        #: fields get added); that echo must not mark the board as edited.
        self._echo_deadline = 0.0
        self._channel = None
        self._page = None
        self._bridge = None
        self.web_view = None
        self._placeholder = None

        layout = QtWidgets.QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        if not mindmap_available():
            layout.addWidget(self._build_unavailable_notice())
            self._status = components.RoleLabel(role='secondary')
            return

        self._bridge = MindMapBridge()
        self._bridge.ready.connect(self._on_bridge_ready)
        self._bridge.treeChanged.connect(self._on_tree_changed)
        self._bridge.treeLoaded.connect(self._on_tree_loaded)
        self._bridge.pickImageRequested.connect(self._choose_image)

        # Chromium is started only when this tab is first opened. Creating
        # the engine is slow, and in a headless process without command line
        # arguments it aborts outright, so nothing below happens until then.
        self._placeholder = components.RoleLabel(
            _('The mind map loads when you open this tab.'),
            role='secondary')
        self._placeholder.setAlignment(QtCore.Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(self._placeholder, 1)

        self._status = components.RoleLabel(role='secondary')
        self._status.setContentsMargins(8, 3, 8, 3)
        layout.addWidget(self._status, 0)
        self._update_status()

    def _build_unavailable_notice(self):
        notice = components.RoleLabel(
            _('The mind map needs the optional QtWebEngine dependency.\n'
              'Install it with:  pip install PyQt6-WebEngine'),
            role='secondary')
        notice.setAlignment(QtCore.Qt.AlignmentFlag.AlignCenter)
        notice.setWordWrap(True)
        notice.setContentsMargins(24, 24, 24, 24)
        return notice

    # ── Lazy loading ─────────────────────────────────────────────

    def _ensure_loaded(self):
        """Start Chromium the first time the panel is actually used."""
        if self._loaded or not mindmap_available():
            return
        self._loaded = True

        self._channel = QtWebChannel.QWebChannel(self)
        self._channel.registerObject('bridge', self._bridge)
        self._page = QtWebEngineCore.QWebEnginePage(self)
        self._page.setWebChannel(self._channel)
        self._page.loadFinished.connect(self._on_page_loaded)

        self.web_view = QtWebEngineWidgets.QWebEngineView(self)
        self.web_view.setPage(self._page)
        self._status.hide()  # The editor itself already renders its node/shortcut status.

        layout = self.layout()
        layout.replaceWidget(self._placeholder, self.web_view)
        self._placeholder.deleteLater()
        self._placeholder = None

        index = os.path.join(MINDMAP_DIR, 'index.html')
        logger.debug('Loading mind map page from %s', index)
        self.web_view.load(QtCore.QUrl.fromLocalFile(index))

    def showEvent(self, event):
        super().showEvent(event)
        self._ensure_loaded()

    # ── Board <-> panel ──────────────────────────────────────────

    def load_from_scene(self):
        """Push the board's stored tree into the editing surface."""
        if not mindmap_available():
            return
        self._ensure_loaded()
        tree = self._scene_tree()
        self._tree = tree
        if self._ready:
            # Compare against what the page was actually given, not against
            # ``self._tree``: an import replaces the tree behind this panel's
            # back (view.py sets ``_tree`` then stores it), and comparing to
            # our own copy would silently skip the push and leave the page
            # showing the old map.
            if tree == self._sent_tree:
                return
            self._sent_tree = tree
            self._push_tree_to_page(tree)
        else:
            # The page is still starting up; hand it over once it is ready.
            self._pending_tree = tree
        self._update_status()

    def store_to_scene(self):
        """Copy the current tree back into the scene for saving."""
        if self._tree is None:
            return
        self._adopt_images(self._tree)
        self._set_scene_tree(self._tree)

    def request_tree(self):
        if self._ready:
            self._bridge.requestTree.emit()

    def reset_view(self):
        if self._ready:
            self._bridge.resetView.emit()

    def export_snapshot(self, path):
        """Save the mind map as currently displayed to ``path`` as PNG.

        The bundled build registers no export plugin (``map.export`` only
        forwards to it and throws without it), so the picture is taken from
        the widget instead. The view is fitted first so the whole map is in
        frame rather than whatever happened to be scrolled into sight.
        """
        if self.web_view is None:
            raise MindMapExportError(_(
                'PNG export needs the mind map window; open that tab first.'))
        self.reset_view()
        application = QtWidgets.QApplication.instance()
        if application is not None:
            application.processEvents()
        pixmap = self.web_view.grab()
        if pixmap.isNull():
            raise MindMapExportError(_('The mind map could not be rendered.'))
        try:
            saved = pixmap.save(path, 'PNG')
        except OSError as exc:
            raise MindMapExportError(
                _('Could not write {path}: {reason}')
                .format(path=path, reason=exc)) from exc
        if not saved:
            raise MindMapExportError(
                _('Could not write {path}.').format(path=path))
        return path

    # ── Pictures ─────────────────────────────────────────────────

    def _attachments(self):
        scene = self.view.scene
        if not hasattr(scene, 'attachments') or scene.attachments is None:
            scene.attachments = {}
        return scene.attachments

    def _store_image(self, name, mime, blob):
        """Add one picture to the board's attachment table."""
        attachments = self._attachments()
        attachment_id = max(attachments.keys(), default=0) + 1
        attachments[attachment_id] = (name, mime, blob)
        return attachment_url(attachment_id)

    def _adopt_images(self, tree):
        """Move inline images out of the tree and into the board file.

        Called before anything is stored: a tree pulled in from XMind
        carries its pictures as base64, which would otherwise be written
        into the board as one enormous string.
        """
        adopted = 0
        for data in node_data(tree):
            image = data.get('image')
            if not isinstance(image, str) or not image.startswith('data:'):
                continue
            decoded = decode_data_url(image)
            if decoded is None:
                continue
            mime, blob = decoded
            blob, mime = self._shrink(blob, mime)
            data['image'] = self._store_image(
                f'mindmap-{adopted + 1}', mime, blob)
            adopted += 1
        if adopted:
            logger.debug('Moved %s mind map picture(s) into the board', adopted)
        return adopted

    def _image_payload(self, tree):
        """Every stored picture the tree uses, as data URLs for the page."""
        attachments = self._attachments()
        payload = {}
        for data in node_data(tree):
            image = data.get('image')
            if not isinstance(image, str) or image in payload:
                continue
            if not image.startswith(ATTACHMENT_PREFIX):
                continue
            entry = attachment_for(image, attachments)
            if entry is None:
                logger.debug('Mind map picture %s is missing', image)
                continue
            _name, mime, blob = entry
            payload[image] = encode_data_url(mime, blob)
        return payload

    def _store_incoming_images(self, tree, images):
        """Save the pictures the page just pasted into the nodes."""
        stored = 0
        for data in node_data(tree):
            image = data.get('image')
            if not isinstance(image, str) or image not in images:
                continue
            decoded = decode_data_url(images[image])
            if decoded is None:
                continue
            mime, blob = decoded
            blob, mime = self._shrink(blob, mime)
            data['image'] = self._store_image(
                f'mindmap-pasted-{stored + 1}', mime, blob)
            stored += 1
        if stored:
            logger.debug('Stored %s pasted mind map picture(s)', stored)
        return stored

    @staticmethod
    def _shrink(blob, mime):
        """Shrink a picture that is bigger than a board should carry."""
        image = QtGui.QImage.fromData(blob)
        if image.isNull():
            return blob, mime
        if max(image.width(), image.height()) <= MAX_ATTACHMENT_EDGE:
            return blob, mime
        image = image.scaled(
            MAX_ATTACHMENT_EDGE, MAX_ATTACHMENT_EDGE,
            QtCore.Qt.AspectRatioMode.KeepAspectRatio,
            QtCore.Qt.TransformationMode.SmoothTransformation)
        buffer = QtCore.QBuffer()
        buffer.open(QtCore.QIODevice.OpenModeFlag.WriteOnly)
        image.save(buffer, 'PNG')
        return bytes(buffer.data()), 'image/png'

    def _choose_image(self):
        """Pick a picture for the selected nodes, on behalf of the page."""
        if self._bridge is None:
            return
        extensions = sorted(set(PASSTHROUGH_EXTENSIONS) | {'.tif', '.tiff'})
        formats = ' '.join(f'*{ext}' for ext in extensions)
        filenames, _selected = QtWidgets.QFileDialog.getOpenFileNames(
            self, _('Insert image'), '', f'{_("Images")} ({formats})')
        if not filenames:
            return
        for path in filenames[:1]:
            payload = self._read_image_file(path)
            if payload is None:
                QtWidgets.QMessageBox.information(
                    self, _('Insert image'),
                    _('{name} is not a readable image.')
                    .format(name=os.path.basename(path)))
                continue
            self._bridge.imagePicked.emit(payload)

    def _read_image_file(self, path):
        """Read a file as ``{url, width, height}`` for the page."""
        extension = os.path.splitext(path)[1].lower()
        mime = PASSTHROUGH_EXTENSIONS.get(extension)
        try:
            with open(path, 'rb') as handle:
                blob = handle.read()
        except OSError as exc:
            logger.info('Could not read image %s: %s', path, exc)
            return None
        name = os.path.basename(path)
        if mime and len(blob) <= 8 * 1024 * 1024:
            image = QtGui.QImage.fromData(blob)
            if image.isNull():
                return None
            if max(image.width(), image.height()) <= MAX_ATTACHMENT_EDGE:
                return json.dumps({
                    'url': encode_data_url(mime, blob),
                    'title': name,
                    'width': image.width(),
                    'height': image.height(),
                }, ensure_ascii=False)
        # Anything else (huge, exotic or unreadable by Chromium) is
        # re-encoded, which also brings it under the size limit.
        image = QtGui.QImage(path)
        if image.isNull():
            return None
        image, mime = self._as_shrunk_image(image)
        buffer = QtCore.QBuffer()
        buffer.open(QtCore.QIODevice.OpenModeFlag.WriteOnly)
        image.save(buffer, 'PNG' if mime == 'image/png' else 'JPEG')
        blob = bytes(buffer.data())
        return json.dumps({
            'url': encode_data_url(mime, blob),
            'title': name,
            'width': image.width(),
            'height': image.height(),
        }, ensure_ascii=False)

    @staticmethod
    def _as_shrunk_image(image):
        if max(image.width(), image.height()) > MAX_ATTACHMENT_EDGE:
            image = image.scaled(
                MAX_ATTACHMENT_EDGE, MAX_ATTACHMENT_EDGE,
                QtCore.Qt.AspectRatioMode.KeepAspectRatio,
                QtCore.Qt.TransformationMode.SmoothTransformation)
        return image, 'image/png'

    # ── Bridge callbacks ─────────────────────────────────────────

    def _on_page_loaded(self, ok):
        if not ok:
            logger.warning('The mind map page failed to load')
            self._status.setText(_('Failed to load the mind map page'))

    def _on_bridge_ready(self):
        logger.debug('Mind map page is ready')
        self._ready = True
        tree = self._pending_tree
        self._pending_tree = None
        if tree is None:
            tree = self._scene_tree()
            self._tree = tree
        self._sent_tree = tree
        self._push_tree_to_page(tree)
        self._update_status()

    def _push_tree_to_page(self, tree):
        """Send a tree (and its pictures) to the page."""
        self._bridge.loadTree.emit(self._payload_for(tree))
        self._echo_deadline = time.monotonic() + 3.0

    def _payload_for(self, tree):
        """What the page needs to show ``tree``: the tree plus its pictures."""
        if not tree:
            return ''
        return json.dumps({
            'tree': tree,
            'images': self._image_payload(tree),
        }, ensure_ascii=False)

    def _on_tree_loaded(self, ok, error):
        if ok and getattr(self, '_pending_view_restore', None) is not None:
            self.restore_search_view(self._pending_view_restore)
        if ok and getattr(self, '_pending_search', None) is not None:
            self.locate_search_result(self._pending_search)
        if not ok:
            logger.info('The mind map could not load the stored tree: %s',
                        error)
            self._status.setText(_('Could not load the stored mind map'))
            return
        self._update_status()

    def _on_tree_changed(self, payload):
        tree, images = parse_tree_payload(payload)
        if tree is None:
            return
        if images:
            self._store_incoming_images(tree, images)
        previous = self._scene_tree()
        self._tree = tree
        self._set_scene_tree(tree)
        # Scrolling or zooming also reports a change, and the page re-sends
        # the tree once right after loading it; neither must make the board
        # look edited, so only a real difference counts.
        if time.monotonic() >= self._echo_deadline \
                and content_signature(tree) != content_signature(previous) \
                and hasattr(self.view, 'mark_content_dirty'):
            self.view.mark_content_dirty()
        self._update_status()
        library = getattr(self.view, 'category_panel', None)
        if library is not None:
            library.search_content_changed()

    def locate_search_result(self, node_path):
        self._pending_search = node_path
        if not self._ready:
            return
        self._pending_search = None
        self.web_view.page().runJavaScript(
            'window.__locateMindMap && window.__locateMindMap(' + json.dumps(node_path) + ')')

    def capture_search_view(self, callback):
        if self._ready:
            self.web_view.page().runJavaScript(
                'window.__mindMap && window.__mindMap.view.getTransformData()', callback)

    def restore_search_view(self, state):
        self._pending_search = None
        self._pending_view_restore = state
        if state is None or not self._ready:
            return
        self._pending_view_restore = None
        self.web_view.page().runJavaScript('window.__restoreSearchMindMap(' + json.dumps(state) + ')')

    def _workspace_page(self):
        for page in getattr(self.view.scene, 'workspace_pages', []):
            if page.get('id') == self.page_id:
                return page
        return None

    def _scene_tree(self):
        if self.page_id is None:
            return getattr(self.view.scene, 'mindmap_tree', None)
        page = self._workspace_page()
        return (page or {}).get('content')

    def _set_scene_tree(self, tree):
        if self.page_id is None:
            self.view.scene.mindmap_tree = tree
            return
        page = self._workspace_page()
        if page is not None:
            page['content'] = tree

    # ── Status ───────────────────────────────────────────────────

    def _update_status(self):
        if not hasattr(self, '_status'):
            return
        count = self._count_nodes(self._tree)
        if count:
            self._status.setText(_('{count} nodes').format(count=count))
        else:
            self._status.setText(
                _('Double-click a node to rename it, Tab adds a child.'))

    @staticmethod
    def _count_nodes(tree):
        if not isinstance(tree, dict):
            return 0
        total = 0
        stack = [tree]
        while stack:
            node = stack.pop()
            if not isinstance(node, dict):
                continue
            total += 1
            children = node.get('children')
            if isinstance(children, list):
                stack.extend(children)
        return total
