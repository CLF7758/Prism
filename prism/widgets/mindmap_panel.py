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
import threading
import time
from collections import OrderedDict

# Keep WebGL available for the shared material/GLB renderer. Headless tests
# may still explicitly override Chromium flags in their own environment.
from prism.webengine_runtime import configure_webengine
configure_webengine()

from PyQt6 import QtCore, QtGui, QtWidgets  # noqa: E402

from prism.fileio.attachments import (  # noqa: E402
    ATTACHMENT_PREFIX, MAX_ATTACHMENT_EDGE, attachment_id_of, attachment_url,
    decode_data_url, encode_data_url, node_data)
from prism.fileio.attachments_store import (  # noqa: E402
    attachment_content_version, attachment_mime, attachment_source_key,
    default_cache_bytes, read_attachment_bytes)
from prism.fileio.mindmap_export import MindMapExportError  # noqa: E402
from prism.i18n import _  # noqa: E402
from prism.performance_trace import operation  # noqa: E402
from prism.qt_thread_safety import JobRegistry, gui_signal_carrier  # noqa: E402
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


# ── 后台准备载荷 ────────────────────────────────────────────────────
#
# 页面要的是「树 + 图片的 data URL」。图片的字节住在工程文件里（还没存过盘
# 的工程就在内存里），读出来再 base64 编码，在 406 张附件的板上要 1.2 秒，
# 而且全在 GUI 线程上：实测 mindmap.build_payload 的 p95 就是 1249 ms。
#
# 现在 GUI 线程只做三件事：拍下树的快照、记下用到的附件号、记下内容版本。
# 读字节和编码都搬到工作线程上。装回去之前要核对工程、页面、内容版本和
# 代次 —— 任何一项对不上就丢掉，这样用户后来的编辑不会被旧载荷盖掉。

#: 编码好的图片（data URL）留一份，按 (来源, 内容版本, 规格) 认。
#: 同一张图片在两张脑图页之间换来换去时，不必再读一遍、再编码一遍。
_ENCODED_PICTURES = OrderedDict()
_ENCODED_PICTURES_BYTES = 0
_ENCODED_PICTURES_LIMIT = default_cache_bytes()
_ENCODED_PICTURES_LOCK = threading.Lock()

#: 准备载荷的线程池。它自己一个，不挤全局池：全局池还要铺瓦片、解码，
#: 脑图排在瓦片后面依然是脑图在等。
_PAYLOAD_POOL = None


def payload_pool():
    """The pool that prepares mind map payloads."""
    global _PAYLOAD_POOL
    pool = _PAYLOAD_POOL
    if pool is None:
        pool = _PAYLOAD_POOL = QtCore.QThreadPool(QtWidgets.QApplication.instance())
        pool.setMaxThreadCount(2)
        pool.setExpiryTimeout(30000)
    return pool


class _PayloadRequest:
    """What the GUI thread hands to the worker: a snapshot, not live state.

    Only plain data crosses the thread boundary.  The tree travels as its
    JSON text (the payload needs it anyway, and a text is immune to the
    tree being edited while the worker runs), the pictures travel as the
    references the tree uses, and the tokens say whether a finished
    result may still be installed.
    """

    __slots__ = ('tree_json', 'pictures', 'attachments', 'generation',
                 'project_id', 'page_id', 'content_version', 'cancel')

    def __init__(self, tree_json, pictures, attachments, generation,
                 project_id, page_id, content_version, cancel):
        self.tree_json = tree_json
        self.pictures = pictures
        self.attachments = attachments
        self.generation = generation
        self.project_id = project_id
        self.page_id = page_id
        self.content_version = content_version
        self.cancel = cancel


class _PayloadSignals(QtCore.QObject):
    """The worker's way back to the GUI thread."""

    finished = QtCore.pyqtSignal(object)


class _PayloadWorker(QtCore.QRunnable):
    """Prepares one payload off the GUI thread.

    The result is delivered through a signal, so it arrives on the GUI
    thread and is installed where it belongs.
    """

    def __init__(self, request, callback):
        super().__init__()
        self.request = request
        # The carrier has no owner on purpose: it must outlive the panel
        # that queued the work.  The panel keeps the job alive until it
        # finished, and that is where the carrier is released -- on the GUI
        # thread, never on a worker.
        self.signals = gui_signal_carrier(_PayloadSignals(),
                                          QtCore.QCoreApplication.instance())
        self.signals.finished.connect(callback)

    def run(self):
        request = self.request
        try:
            with operation('mindmap.build_payload', pictures=len(request.pictures)):
                payload, error = _payload_for(request)
        except Exception as failure:
            payload, error = None, ('failed', str(failure))
        try:
            self.signals.finished.emit((request, payload, error))
        except RuntimeError:
            pass


def _payload_for(request):
    """What the page needs to show the prepared tree: it and its pictures.

    Returns ``(payload, None)`` or ``(None, error)``, where the error is a
    ``(kind, detail)`` pair: ``('unreadable', url)`` for a picture whose
    bytes could not be read, ``('cancelled', '')`` for a request that was
    superseded, ``('failed', text)`` for anything else.
    """
    if not request.tree_json:
        return '', None
    images, error = _image_payload(request)
    if error is not None:
        return None, error
    return ('{"tree": ' + request.tree_json + ', "images": '
            + json.dumps(images, ensure_ascii=False) + '}'), None


def _image_payload(request):
    """The pictures a tree uses, as data URLs for the page.

    Every picture is read on demand and encoded here, never on the GUI
    thread.  A picture that was already encoded for this project is
    reused instead of being read and encoded again.

    A picture that is simply not in the table is skipped, as it always
    was.  A picture that is in the table but whose bytes cannot be read
    is *not* papered over with an empty image: the whole payload fails
    and the caller says so.
    """
    images = {}
    for url in request.pictures:
        if request.cancel.is_set():
            return None, ('cancelled', '')
        key = attachment_id_of(url)
        if key is None:
            continue
        data_url, failure = _encoded_picture(request.attachments, key)
        if data_url is None:
            if failure == 'missing':
                logger.warning('Mind map picture %s is missing', url)
                continue
            return None, failure
        images[url] = data_url
    return images, None


def _encoded_picture(attachments, key):
    """One picture as a data URL, reusing an earlier encoding.

    Returns ``(data_url, None)``, ``(None, 'missing')`` when the table
    holds no such picture, or ``(None, ('unreadable', url))`` when it
    holds one whose bytes cannot be read.
    """
    mime = attachment_mime(attachments, key)
    if mime is None:
        return None, 'missing'
    spec = ('data-url', mime)
    cache_key = (attachment_source_key(attachments),
                 attachment_content_version(attachments, key), spec)
    cached = _cached_picture(cache_key)
    if cached is not None:
        return cached, None
    read = read_attachment_bytes(attachments, key)
    if read is None:
        return None, 'missing'
    mime, blob = read
    if blob is None:
        return None, ('unreadable', attachment_url(key))
    data_url = encode_data_url(mime, blob)
    _remember_picture(cache_key, data_url)
    return data_url, None


def _cached_picture(cache_key):
    """The data URL of an already encoded picture, or None."""
    with _ENCODED_PICTURES_LOCK:
        data_url = _ENCODED_PICTURES.get(cache_key)
        if data_url is not None:
            _ENCODED_PICTURES.move_to_end(cache_key)
        return data_url


def _remember_picture(cache_key, data_url):
    """Keep an encoded picture, dropping the oldest ones past the limit."""
    global _ENCODED_PICTURES_BYTES
    with _ENCODED_PICTURES_LOCK:
        previous = _ENCODED_PICTURES.pop(cache_key, None)
        if previous is not None:
            _ENCODED_PICTURES_BYTES -= len(previous)
        _ENCODED_PICTURES[cache_key] = data_url
        _ENCODED_PICTURES_BYTES += len(data_url)
        while (_ENCODED_PICTURES_BYTES > _ENCODED_PICTURES_LIMIT
               and len(_ENCODED_PICTURES) > 1):
            _oldest, oldest_url = _ENCODED_PICTURES.popitem(last=False)
            _ENCODED_PICTURES_BYTES -= len(oldest_url)


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
        #: Bumped for every push, so a result prepared for an older tree
        #: is dropped instead of installed.
        self._payload_generation = 0
        #: Set when a push is superseded, so the worker stops early.
        self._payload_cancel = threading.Event()
        self._payload_worker = None
        # 任务在结果送达前一直由 GUI 侧持有：线程池在工作线程里删除
        # runnable，那次删除不能成为某个 Qt 对象的最后一个引用。
        self._background_jobs = JobRegistry()

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
        self._page.urlChanged.connect(self._configure_clipboard)

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
        self._snapshot_epoch = getattr(self, '_snapshot_epoch', 0) + 1
        self._search_epoch = getattr(self, '_search_epoch', 0) + 1
        """Push the board's stored tree into the editing surface."""
        if not mindmap_available():
            return
        self._ensure_loaded()
        tree = self._scene_tree()
        self._tree = tree
        if self._ready:
            # Compare against what the page was actually given. The scene
            # can be replaced by an import or project load independently
            # of the last tree sent to the renderer.
            if tree == self._sent_tree:
                return
            self._sent_tree = tree
            self._push_tree_to_page(tree)
        else:
            # The page is still starting up; hand it over once it is ready.
            self._pending_tree = tree
        self._update_status()

    def set_tree(self, tree):
        """Replace the map from an import without reading the old renderer."""
        self._adopt_images(tree)
        self._set_scene_tree(tree)
        self.load_from_scene()

    def store_to_scene(self):
        if getattr(self.view, '_upgrade_preview_readonly', False):
            return True
        """Copy the current tree back into the scene for saving."""
        with operation('mindmap.store_to_scene'):
            project_id = self.view.scene.project_id
            epoch = getattr(self, '_snapshot_epoch', 0)
            if (self._ready and self.web_view is not None and self._pending_tree is None
                    and not getattr(self, '_tree_loading', False)):
                from prism.widgets.editor_snapshot import read_editor_snapshot
                from prism.item_lifecycle import is_deleted
                with operation('mindmap.read_editor_snapshot'):
                    payload = read_editor_snapshot(self.web_view.page(),
                        'window.__searchSnapshot ? window.__searchSnapshot() : null',
                        valid=lambda: not is_deleted(self) and self.view.scene.project_id == project_id
                            and getattr(self, '_snapshot_epoch', 0) == epoch)
                if (is_deleted(self) or self.view.scene.project_id != project_id
                        or getattr(self, '_snapshot_epoch', 0) != epoch):
                    return False
                if payload is None or parse_tree_payload(payload)[0] is None:
                    logger.warning('Mind map snapshot unavailable; refusing stale content')
                    return False
                with operation('mindmap.store_to_scene.on_tree_changed'):
                    self._on_tree_changed(payload)
            if self._tree is None:
                return True
            self._adopt_images(self._tree)
            self._set_scene_tree(self._tree)
            return True

    def store_to_scene_async(self, callback):
        """Read back edits asynchronously; read-only preview never writes back."""
        if getattr(self.view, '_upgrade_preview_readonly', False):
            callback(True)
            return
        if (not self._ready or self.web_view is None or self._pending_tree is not None
                or getattr(self, '_tree_loading', False)):
            if self._tree is not None:
                self._adopt_images(self._tree)
                self._set_scene_tree(self._tree)
            callback(True)
            return
        from prism.widgets.editor_snapshot import request_editor_snapshot
        from prism.item_lifecycle import is_deleted
        project_id, page_id = self.view.scene.project_id, self.page_id
        epoch = getattr(self, '_snapshot_epoch', 0)

        def valid():
            return (not is_deleted(self) and self.view.scene.project_id == project_id
                    and self.page_id == page_id
                    and getattr(self, '_snapshot_epoch', 0) == epoch)

        def finished(payload):
            if payload is None or not valid() or parse_tree_payload(payload)[0] is None:
                callback(False)
                return
            try:
                self._on_tree_changed(payload)
                self._adopt_images(self._tree)
                self._set_scene_tree(self._tree)
            except Exception:
                logger.exception('Could not apply mind map navigation snapshot')
                callback(False)
                return
            callback(True)

        request_editor_snapshot(self.web_view.page(),
            'window.__searchSnapshot ? window.__searchSnapshot() : null', finished, valid=valid)

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

    def _picture_references(self, tree):
        """The stored pictures a tree uses, in the order the page saw them.

        Only the references are collected here: the bytes themselves are
        read and encoded on the worker thread.
        """
        references = []
        seen = set()
        for data in node_data(tree):
            image = data.get('image')
            if not isinstance(image, str) or image in seen:
                continue
            if not image.startswith(ATTACHMENT_PREFIX):
                continue
            seen.add(image)
            references.append(image)
        return references

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
        if getattr(self.view, '_upgrade_preview_readonly', False):
            return
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

    def _configure_clipboard(self, location):
        """Only our bundled editor may use the existing clipboard workflow."""
        expected = os.path.realpath(os.path.join(MINDMAP_DIR, 'index.html'))
        trusted = (location.isLocalFile()
                   and os.path.normcase(os.path.realpath(location.toLocalFile()))
                   == os.path.normcase(expected))
        settings = self._page.settings()
        for name in ('JavascriptCanAccessClipboard', 'JavascriptCanPaste'):
            settings.setAttribute(getattr(QtWebEngineCore.QWebEngineSettings.WebAttribute, name), trusted)

    def _on_bridge_ready(self):
        logger.debug('Mind map page is ready')
        self._ready = True
        tree = self._pending_tree
        self._pending_tree = None
        if tree is None:
            tree = self._scene_tree()
            self._tree = tree
        self._sent_tree = tree
        with operation('mindmap.push_tree'):
            self._push_tree_to_page(tree)
        self._update_status()

    def _push_tree_to_page(self, tree):
        """Send a tree (and its pictures) to the page.

        The pictures are read and encoded on a worker thread: a board can
        carry hundreds of them, and reading them from the project file
        plus base64-encoding them took 1.2 s of GUI time (the p95 of
        ``mindmap.build_payload`` was 1249 ms).

        The GUI thread only takes the snapshot: the tree as its JSON
        text, the picture references it uses, and the tokens that say
        whether a finished result may still be installed.
        """
        self._tree_loading = True
        self._payload_generation += 1
        superseded = self._payload_cancel
        self._payload_cancel = threading.Event()
        if superseded is not None:
            # The request this replaces stops at its next picture.
            superseded.set()
        request = _PayloadRequest(
            tree_json=json.dumps(tree, ensure_ascii=False) if tree else '',
            pictures=self._picture_references(tree),
            attachments=self._attachments(),
            generation=self._payload_generation,
            project_id=self._scene_project_id(),
            page_id=self.page_id,
            content_version=self._content_version(),
            cancel=self._payload_cancel,
        )
        self._payload_worker = _PayloadWorker(request, self._on_payload_ready)
        # The job stays alive until its result was delivered: the pool
        # deletes the runnable in the worker thread, and that deletion must
        # not release the last reference to a Qt object.
        self._background_jobs.start(payload_pool(), self._payload_worker)

    @QtCore.pyqtSlot(object)
    def _on_payload_ready(self, result):
        """Install a prepared payload, if it still describes this tree."""
        request, payload, error = result
        self._background_jobs.release(self._payload_worker)
        if request is None:
            return
        if not self._payload_is_current(request):
            logger.debug('Dropping a superseded mind map payload')
            return
        self._payload_worker = None
        if error is not None:
            kind, detail = error
            if kind == 'cancelled':
                return
            message = self._payload_failure(error)
            logger.warning('%s', message)
            self._tree_loading = False
            # The page never got this tree, so it may be pushed again.
            self._sent_tree = None
            self._status.setText(message)
            return
        envelope = json.loads(payload) if payload else {'tree': None}
        envelope['readonly'] = bool(getattr(self.view, '_upgrade_preview_readonly', False))
        self._bridge.loadTree.emit(json.dumps(envelope, ensure_ascii=False))
        self._echo_deadline = time.monotonic() + 3.0

    def _payload_is_current(self, request):
        """Whether a prepared payload still describes what the page should show.

        All four have to agree: the project, the page, the content version
        and the generation counter.  The generation counter drops a request
        that a newer push superseded; the content version drops a payload
        that was prepared from a tree the user has edited since.
        """
        from prism.item_lifecycle import is_deleted
        if is_deleted(self):
            return False
        if request.generation != self._payload_generation:
            return False
        if request.page_id != self.page_id:
            return False
        if request.content_version != self._content_version():
            return False
        return request.project_id == self._scene_project_id()

    def _payload_failure(self, error):
        """The message a failed preparation is reported with."""
        kind, detail = error
        if kind == 'unreadable':
            return _('The mind map picture {url} could not be read.') \
                .format(url=detail)
        return _('The mind map could not be prepared: {reason}') \
            .format(reason=detail)

    def _content_version(self):
        """A token that changes whenever the tree behind the panel changes."""
        return getattr(self, '_content_revision', 0)

    def _scene_project_id(self):
        """The project the panel is showing, or None once it is gone."""
        try:
            return self.view.scene.project_id
        except (AttributeError, RuntimeError):
            return None

    def _on_tree_loaded(self, ok, error):
        self._tree_loading = not ok
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
        if getattr(self.view, '_upgrade_preview_readonly', False):
            return
        self._content_revision = getattr(self, '_content_revision', 0) + 1
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

    def search_snapshot(self, callback):
        if not self._ready or self.web_view is None:
            callback(True)
            return
        if self._pending_tree is not None or getattr(self, '_tree_loading', False):
            callback(False)
            return
        project_id = self.view.scene.project_id
        page_id, web_view = self.page_id, self.web_view
        page = web_view.page()
        epoch = getattr(self, '_search_epoch', 0)
        revision = getattr(self, '_content_revision', 0)
        from prism.widgets.editor_snapshot import request_editor_snapshot, owner_alive
        def valid():
            return (owner_alive(self) and owner_alive(self.view)
                    and self.view.scene.project_id == project_id and self.page_id == page_id
                    and self.web_view is web_view and epoch == getattr(self, '_search_epoch', 0))
        def finished(payload):
            if not isinstance(payload, str) or not valid():
                callback(False)
                return
            if revision == getattr(self, '_content_revision', 0):
                self._on_tree_changed(payload)
            callback(True)
        request_editor_snapshot(page, 'window.__searchSnapshot ? window.__searchSnapshot() : null', finished, valid=valid)

    def editor_history(self, redo=False):
        if self._ready:
            self.web_view.page().runJavaScript('window.__mindMap && window.__mindMap.execCommand(' +
                                              json.dumps('FORWARD' if redo else 'BACK') + ')')

    def end_search_location(self):
        self._pending_search = None
        if self._ready and self.web_view is not None:
            self.web_view.page().runJavaScript('window.__endSearchLocation && window.__endSearchLocation()')

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
        from prism.fileio import load_deferred_board_extras
        if self.page_id is None:
            load_deferred_board_extras(self.view.scene, names={'mindmap_tree'})
        else:
            load_deferred_board_extras(self.view.scene, names={'workspace_pages'},
                                       page_ids={self.page_id})
        if self.page_id is None:
            return getattr(self.view.scene, 'mindmap_tree', None)
        page = self._workspace_page()
        return (page or {}).get('content')

    def _set_scene_tree(self, tree):
        if getattr(self.view, '_upgrade_preview_readonly', False):
            return
        if self.page_id is None:
            self.view.scene.mindmap_tree = tree
            deferred = getattr(self.view.scene, '_deferred_board_extras', None)
            if deferred is not None:
                deferred.discard('mindmap_tree')
            return
        page = self._workspace_page()
        if page is not None:
            page['content'] = tree
            self.view.mark_page_modified(page)

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
