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

"""Document panel: a Quill editor hosted in a QWebEngineView.

The editing surface is Quill (BSD-3), shipped unmodified under
``prism/assets/editor``.  Python and the page talk over QWebChannel: Python
pushes the note in and receives every change back as HTML, which is stored in
the board file.  The page owns the ribbon, the editing behaviour and the
dialogs Quill can answer itself; Python owns everything that needs the rest of
Prism, so it hosts the file picker, the colour chooser, the table size prompt
and the image size prompt.

Images are the fiddly part.  A note keeps its pictures in the board's
``attachments`` table and refers to them as ``prism://attach/<id>``.  Chromium
cannot load that URL: a custom scheme has to be registered before QApplication
exists (measured, not assumed - registering later silently stops the scheme
handler from being called at all), which is far too early for a widget module.
So the attachment bytes are mirrored into a scratch directory and the URLs are
rewritten to ``file://`` on the way in and back to ``prism://`` on the way out.
The page therefore only ever deals with URLs this module produced.

Text is kept as HTML, using the same ``prism://`` references, so a .prism file
stays self contained.
"""

import atexit
import base64
import binascii
import html.parser
import json
import logging
import os
import re
import shutil
import tempfile
import time

# Chromium needs these before QtWebEngine initialises; without them a
# headless or remote session fails to create a context at all.
os.environ.setdefault(
    'QTWEBENGINE_CHROMIUM_FLAGS',
    '--disable-gpu --disable-software-rasterizer --no-sandbox '
    '--disable-features=RendererCodeIntegrity')

from PyQt6 import QtCore, QtGui, QtWidgets  # noqa: E402

from prism.fileio.attachments import extension_for  # noqa: E402
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


#: Pseudo scheme used for images that live in the board file. The page never
#: sees it; it is rewritten to a scratch file:// URL before it gets there.
ATTACHMENT_SCHEME = 'prism'
ATTACHMENT_HOST = 'attach'

IMAGE_EXTENSIONS = {
    '.png', '.jpg', '.jpeg', '.gif', '.bmp', '.webp', '.tiff', '.tif',
    '.svg', '.ico', '.jfif', '.jpe', '.ppm', '.pgm', '.pbm', '.xbm', '.xpm',
}
#: Longest edge at which a picture is still drawn at full detail.  A larger
#: picture is *shown* from a smaller copy so the renderer does not have to
#: hold a wall poster in memory; what gets stored, saved and exported stays
#: the bytes the user brought in.
MAX_ATTACHMENT_EDGE = 2000

EDITOR_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    'assets', 'editor')

EDITOR_ASSETS = (
    'index.html',
    'editor.js',
    'editor.css',
    os.path.join('vendor', 'quill.js'),
    os.path.join('vendor', 'quill.snow.css'),
)

#: Font sizes offered in the ribbon, in points.
FONT_SIZES = ('9', '10.5', '11', '12', '14', '16', '18', '20', '22', '24',
              '28', '36', '48', '72')

#: Fonts put in front of the alphabetical list; the rest follow.
PREFERRED_FONTS = (
    'Microsoft YaHei', '微软雅黑', 'SimSun', '宋体', 'SimHei', '黑体',
    'KaiTi', '楷体', 'FangSong', '仿宋', 'Consolas', 'Courier New',
    'Arial', 'Times New Roman', 'Calibri', 'Cambria',
)

_FONT_LIMIT = 160

_ATTACHMENT_URL = re.compile(
    f'{ATTACHMENT_SCHEME}://{ATTACHMENT_HOST}/(\\d+)')
_DATA_URL = re.compile(
    r'src="data:(image/[a-zA-Z0-9.+-]+);base64,([^"]+)"')
_IMG_TAG = re.compile(r'<img\b[^>]*>', re.IGNORECASE)
_IMG_SIZE_ATTR = re.compile(r'\b(width|height)\s*=\s*"?(\d+)"?', re.IGNORECASE)
_STYLE_ATTR = re.compile(r'style\s*=\s*"([^"]*)"', re.IGNORECASE)
_TAG = re.compile(r'<[^>]+>')

_MIME_EXTENSIONS = {'image/jpeg': '.jpg', 'image/png': '.png',
                    'image/gif': '.gif', 'image/webp': '.webp',
                    'image/bmp': '.bmp', 'image/svg+xml': '.svg'}

#: Scratch directories this process created, removed when it exits.
_SCRATCH_DIRS = []
_SCRATCH_PRUNED = False


def attachment_url(attachment_id):
    """The in-board URL of an embedded picture."""
    return f'{ATTACHMENT_SCHEME}://{ATTACHMENT_HOST}/{attachment_id}'


def _file_url(path):
    return 'file:///' + os.path.abspath(path).replace(os.sep, '/').lstrip('/')


def _is_image_path(path):
    return os.path.splitext(path)[1].lower() in IMAGE_EXTENSIONS


def _larger_than_editor_draws(blob):
    """Whether a picture has more pixels than the editor ever shows.

    Only the header is read, so asking costs nothing even for a 40 MB PNG.
    A blob Qt cannot read at all is reported as small: it will fail on its
    own terms rather than here.
    """
    if not blob:
        return False
    # The buffer gets its own copy of the bytes.  Constructing a QBuffer from
    # a QByteArray only borrows it, and a temporary that is collected before
    # the reader runs takes the whole process down with it - no traceback.
    buffer = QtCore.QBuffer()
    buffer.setData(blob)
    if not buffer.open(QtCore.QIODevice.OpenModeFlag.ReadOnly):
        return False
    try:
        size = QtGui.QImageReader(buffer).size()
    finally:
        buffer.close()
    if not size.isValid():
        return False
    return max(size.width(), size.height()) > MAX_ATTACHMENT_EDGE


def _mime_for_name(name):
    """The mime type a file name implies, for bytes with no mime of their own."""
    extension = os.path.splitext(name or '')[1].lower()
    for mime, known in _MIME_EXTENSIONS.items():
        if known == extension:
            return mime
    return 'image/png'


def _read_file(path):
    """Read a file whole, or return None when it cannot be read."""
    try:
        with open(path, 'rb') as handle:
            return handle.read()
    except OSError:
        logger.info('Could not read %s', path, exc_info=True)
        return None


def editor_available():
    """Whether the editor page and its host can both run."""
    if not WEBENGINE_AVAILABLE:
        return False
    missing = [name for name in EDITOR_ASSETS
               if not os.path.exists(os.path.join(EDITOR_DIR, name))]
    if missing:
        logger.debug('Editor assets missing: %s', ', '.join(missing))
        return False
    return True


def _prune_scratch_dirs(max_age_days=7):
    """Drop scratch directories left behind by crashed sessions.

    Only directories older than a week are touched: another Prism may be
    running right now with its images staged in one of them, and a window that
    displays the wrong pixels is far worse than a few stale megabytes.
    """
    global _SCRATCH_PRUNED
    if _SCRATCH_PRUNED:
        return
    _SCRATCH_PRUNED = True
    root = tempfile.gettempdir()
    cutoff = time.time() - max_age_days * 86400
    try:
        entries = os.listdir(root)
    except OSError:                                 # pragma: no cover
        return
    for name in entries:
        if not name.startswith('prism-document-'):
            continue
        path = os.path.join(root, name)
        try:
            if os.path.isdir(path) and os.path.getmtime(path) < cutoff:
                shutil.rmtree(path, ignore_errors=True)
        except OSError:                             # pragma: no cover
            continue


@atexit.register
def _remove_scratch_dirs():                         # pragma: no cover - exit
    for path in list(_SCRATCH_DIRS):
        shutil.rmtree(path, ignore_errors=True)


class _FragmentExtractor(html.parser.HTMLParser):
    """Reduce a stored document to the fragment the editor can import.

    Notes written by earlier Prism builds were serialised by QTextEdit, which
    emits a complete document: doctype, a head full of CSS and Qt-specific
    attributes.  Handing that to Quill's clipboard converter pastes the style
    sheet in as text, so only the body's children are kept.
    """

    _SKIP = {'style', 'script', 'link', 'meta', 'title', 'base', 'head'}
    #: Wrappers whose tags are dropped but whose contents are kept.
    _UNWRAP = {'html', 'body'}

    def __init__(self):
        super().__init__(convert_charrefs=False)
        self.parts = []
        self._skipping = 0
        self._in_body = False
        self._body_seen = False

    def _collecting(self):
        if self._skipping:
            return False
        if self._body_seen and not self._in_body:
            return False
        return True

    def handle_starttag(self, tag, attrs):
        if tag == 'body':
            self._in_body = True
            self._body_seen = True
            return
        if tag in self._UNWRAP:
            return
        if tag in self._SKIP:
            self._skipping += 1
            return
        if self._collecting():
            self.parts.append(self.get_starttag_text() or f'<{tag}>')

    def handle_startendtag(self, tag, attrs):
        if tag in self._SKIP or tag in self._UNWRAP:
            return
        if self._collecting():
            self.parts.append(self.get_starttag_text() or f'<{tag} />')

    def handle_endtag(self, tag):
        if tag in self._SKIP:
            self._skipping = max(0, self._skipping - 1)
            return
        if tag == 'body':
            self._in_body = False
            return
        if tag in self._UNWRAP:
            return
        if self._collecting():
            self.parts.append(f'</{tag}>')

    def handle_data(self, data):
        if self._collecting():
            self.parts.append(data)

    def handle_entityref(self, name):
        if self._collecting():
            self.parts.append(f'&{name};')

    def handle_charref(self, name):
        if self._collecting():
            self.parts.append(f'&#{name};')


def _body_fragment(html_text):
    """Return the importable part of a stored document."""
    if not html_text:
        return ''
    if '<' not in html_text:
        return html_text
    parser = _FragmentExtractor()
    try:
        parser.feed(html_text)
        parser.close()
    except Exception:                               # pragma: no cover - bad HTML
        logger.debug('Could not parse the stored document, using it verbatim')
        return html_text
    return ''.join(parser.parts)


def _inline_image_sizes(fragment):
    """Move image size attributes into inline styles.

    Qt writes an image's size as ``width="600" height="400"``; Quill only
    reads sizes from the style attribute, so the numbers would be dropped and
    every picture would jump back to its natural size.
    """
    def fix(match):
        tag = match.group(0)
        sizes = {}
        for name, value in _IMG_SIZE_ATTR.findall(tag):
            sizes[name.lower()] = value
        if not sizes:
            return tag
        style = '; '.join(f'{name}: {value}px'
                          for name, value in sorted(sizes.items()))
        stripped = _IMG_SIZE_ATTR.sub('', tag)
        stripped = re.sub(r'\s+>', '>', re.sub(r'\s{2,}', ' ', stripped))
        existing = _STYLE_ATTR.search(stripped)
        if existing:
            merged = existing.group(1).strip().rstrip(';')
            return _STYLE_ATTR.sub(f'style="{merged}; {style}"', stripped, 1)
        return f'{stripped[:-1].rstrip()} style="{style}">'

    return _IMG_TAG.sub(fix, fragment)


if WEBENGINE_AVAILABLE:

    class DocumentBridge(QtCore.QObject):
        """Python end of the QWebChannel connection to the editor page.

        JavaScript can only call slots, so the slots below exist to give the
        page something to call and immediately re-emit the matching signal for
        the rest of Prism to listen on. The signals travel the other way.
        """

        #: Emitted by Python, connected to by the page.
        setLabels = QtCore.pyqtSignal(str)
        setRibbon = QtCore.pyqtSignal(str)
        loadDocument = QtCore.pyqtSignal(str)
        applyCommand = QtCore.pyqtSignal(str)
        imageStored = QtCore.pyqtSignal(str, str)

        #: Internal: mirrors what the page reports.
        ready = QtCore.pyqtSignal()
        documentLoaded = QtCore.pyqtSignal(bool, str)
        contentChanged = QtCore.pyqtSignal(str)
        commandRequested = QtCore.pyqtSignal(str, str)
        saveRequested = QtCore.pyqtSignal()
        imageReceived = QtCore.pyqtSignal(str, str, str, str)

        @QtCore.pyqtSlot()
        def notifyReady(self):
            self.ready.emit()

        @QtCore.pyqtSlot(bool, str)
        def notifyDocumentLoaded(self, ok, error):
            self.documentLoaded.emit(ok, error)

        @QtCore.pyqtSlot(str)
        def pushContent(self, html):
            self.contentChanged.emit(html)

        @QtCore.pyqtSlot(str, str)
        def requestCommand(self, command, payload):
            self.commandRequested.emit(command, payload)

        @QtCore.pyqtSlot()
        def requestSave(self):
            self.saveRequested.emit()

        @QtCore.pyqtSlot(str, str, str, str)
        def storeImage(self, token, name, mime, payload):
            self.imageReceived.emit(token, name, mime, payload)


class _EditorHandle:
    """``panel.editor`` compatibility shim for the calls view.py makes.

    prism/view.py drives the editor through ``panel.editor.setHtml()``,
    ``panel.editor.toHtml()`` and ``panel.editor.toPlainText()``. There is no
    QTextDocument behind the web editor, so this forwards those three calls to
    the panel rather than keeping a second, divergent API. It can go away once
    the call sites use ``panel.set_html()`` / ``panel.html()`` / ``panel.text()``.
    """

    def __init__(self, panel):
        self._panel = panel

    def setHtml(self, html):                        # noqa: N802 - Qt spelling
        self._panel.set_html(html)

    def toHtml(self):                               # noqa: N802
        return self._panel.html()

    def toPlainText(self):                          # noqa: N802
        return self._panel.text()


class DocumentPanel(QtWidgets.QWidget):
    """Board document: a rich text note the user edits, saved with the board."""

    def __init__(self, view, parent=None, page_id=None):
        super().__init__(parent)
        self.view = view
        self.page_id = page_id
        #: Compatibility with the QTextEdit implementation, see _EditorHandle.
        self.editor = _EditorHandle(self)
        self._html = None
        self._content_loaded = False
        #: True while the page shows something older than ``_html``: the Word
        #: importer replaces the document behind the page's back.
        self._page_stale = True
        #: True between pushing a document and the page confirming it. Reading
        #: the page back in that window would return the previous note, and
        #: storing it would undo the import that is still in flight.
        self._pending_push = False
        self._ready = False
        self._channel = None
        self._page = None
        self._bridge = None
        self._web_view = None
        self._placeholder = None
        self._scratch = None
        self._staged = {}
        self._status = None

        # The status line counts characters by walking the whole note, which a
        # long import makes expensive enough to be felt while typing. It is
        # decoration, so it is refreshed at most a few times a second. Built
        # before _build_ui(), which already asks for a refresh.
        self._status_timer = QtCore.QTimer(self)
        self._status_timer.setSingleShot(True)
        self._status_timer.setInterval(400)
        self._status_timer.timeout.connect(self._refresh_status)

        self._build_ui()

        # The page reports changes as the user pauses; this timer writes that
        # into the board, so serialising a long note is not on the typing path.
        self._save_timer = QtCore.QTimer(self)
        self._save_timer.setSingleShot(True)
        self._save_timer.setInterval(400)
        self._save_timer.timeout.connect(self._store_current_html)

    # ── Construction ─────────────────────────────────────────────

    def _build_ui(self):
        layout = QtWidgets.QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        self._status = components.RoleLabel(role='secondary')
        self._status.setContentsMargins(8, 3, 8, 3)
        self._status.setStyleSheet('background: #E9E9EA; color: #AAB0BB; font-size: 12px;')

        if not editor_available():
            layout.addWidget(self._build_unavailable_notice(), 1)
            layout.addWidget(self._status, 0)
            self._update_status()
            return

        self._bridge = DocumentBridge()
        self._bridge.ready.connect(self._on_page_ready)
        self._bridge.documentLoaded.connect(self._on_document_loaded)
        self._bridge.contentChanged.connect(self._on_content_changed)
        self._bridge.commandRequested.connect(self._on_command_requested)
        self._bridge.saveRequested.connect(self._on_save_requested)
        self._bridge.imageReceived.connect(self._on_image_received)

        # Chromium is started only when this tab is first opened: creating the
        # engine is slow, and a session that never opens the document should
        # not pay for it.
        self._placeholder = components.RoleLabel(
            _('The document editor loads when you open this tab.'),
            role='secondary')
        self._placeholder.setAlignment(QtCore.Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(self._placeholder, 1)
        layout.addWidget(self._status, 0)
        self._update_status()

    def _build_unavailable_notice(self):
        notice = components.RoleLabel(_(
            'The document editor needs the optional QtWebEngine dependency.\n'
            'Install it with:  pip install PyQt6-WebEngine'),
            role='secondary')
        notice.setAlignment(QtCore.Qt.AlignmentFlag.AlignCenter)
        notice.setWordWrap(True)
        notice.setContentsMargins(24, 24, 24, 24)
        return notice

    def showEvent(self, event):
        super().showEvent(event)
        self._ensure_loaded()
        self.load_from_scene()

    def _ensure_loaded(self):
        """Start Chromium the first time the panel is actually shown."""
        if self._web_view is not None or not editor_available():
            return
        self._channel = QtWebChannel.QWebChannel(self)
        self._channel.registerObject('bridge', self._bridge)
        self._page = QtWebEngineCore.QWebEnginePage(self)
        self._page.setWebChannel(self._channel)
        self._page.loadFinished.connect(self._on_page_loaded)

        self._web_view = QtWebEngineWidgets.QWebEngineView(self)
        self._web_view.setPage(self._page)

        layout = self.layout()
        if self._placeholder is not None:
            layout.replaceWidget(self._placeholder, self._web_view)
            self._placeholder.deleteLater()
            self._placeholder = None
        else:                                       # pragma: no cover - defensive
            layout.insertWidget(0, self._web_view, 1)

        index = os.path.join(EDITOR_DIR, 'index.html')
        logger.debug('Loading document page from %s', index)
        self._web_view.load(QtCore.QUrl.fromLocalFile(index))

    # ── Board <-> panel ──────────────────────────────────────────

    def load_from_scene(self):
        """Show the stored note, registering its pictures on the way."""
        html = self._scene_html()
        if self._content_loaded and not self._page_stale and html == self._html:
            # Nothing changed behind our back: reloading would throw away the
            # undo stack and the caret for no reason.
            return
        self._html = html
        self._content_loaded = True
        self._page_stale = self._ready
        if self._ready:
            self._push_document()
        self._update_status()

    def store_to_scene(self):
        """Copy the editor content back into the board for saving."""
        if not self._content_loaded:
            return
        raw = self._flush_from_page()
        if raw is not None:
            self._html = self._canonical_html(raw)
            self._page_stale = False
        if self._html is None:
            return
        self._set_scene_html(self._html)
        self._update_status()

    def _store_current_html(self):
        """Timer callback: the page already told us what it holds."""
        if not self._content_loaded or self._html is None:
            return
        self._set_scene_html(self._html)

    def set_html(self, html):
        """Replace the note, e.g. after importing a Word file."""
        self._html = self._canonical_html(html)
        self._content_loaded = True
        self._page_stale = self._ready
        if self._ready:
            self._push_document()
        self._update_status()

    def html(self):
        """The current note as HTML, with ``prism://`` picture references."""
        raw = self._flush_from_page()
        if raw is not None:
            self._html = self._canonical_html(raw)
            self._page_stale = False
            self._content_loaded = True
            self._update_status()
        return self._html or ''

    def text(self):
        """The current note as plain text, for the .txt export."""
        document = QtGui.QTextDocument()
        document.setHtml(self.html())
        return document.toPlainText()

    def _workspace_page(self):
        for page in getattr(self.view.scene, 'workspace_pages', []):
            if page.get('id') == self.page_id:
                return page
        return None

    def _scene_html(self):
        if self.page_id is None:
            return getattr(self.view.scene, 'note_html', '') or ''
        page = self._workspace_page()
        return (page or {}).get('content') or ''

    def _set_scene_html(self, html):
        if self.page_id is None:
            self.view.scene.note_html = html
            return
        page = self._workspace_page()
        if page is not None:
            page['content'] = html

    def _push_document(self):
        if not self._ready:
            return
        payload = {
            'html': self._display_html(_body_fragment(self._html or '')),
        }
        self._page_stale = False
        self._pending_push = True
        self._bridge.loadDocument.emit(json.dumps(payload, ensure_ascii=False))

    def _flush_from_page(self):
        """Ask the page for its current HTML and wait for the reply.

        Saving, exporting and switching pages all have to see the newest text,
        and a round trip to the renderer is the only way to get it: the panel
        keeps the last change the page reported, which may be a keystroke old.
        While a push is still in flight the page holds the *previous* note, so
        the caller is told nothing instead and keeps using what it has.
        """
        if not self._ready or self._page is None or self._pending_push:
            return None
        result = {}
        loop = QtCore.QEventLoop()

        def finished(value):
            result['html'] = value if isinstance(value, str) else None
            loop.quit()

        self._page.runJavaScript(
            'window.__flush ? window.__flush() : null', finished)
        timer = QtCore.QTimer()
        timer.setSingleShot(True)
        timer.timeout.connect(loop.quit)
        timer.start(3000)
        loop.exec()
        timer.stop()
        return result.get('html')

    # ── Page callbacks ───────────────────────────────────────────

    def _on_page_loaded(self, ok):
        if not ok:                                  # pragma: no cover - rare
            logger.warning('The document editor page failed to load')

    def _on_page_ready(self):
        logger.debug('Document editor page is ready')
        self._ready = True
        self._push_labels()
        self._push_ribbon()
        self._push_document()
        self._update_status()

    def _on_document_loaded(self, ok, error):
        self._pending_push = False
        if ok and getattr(self, '_pending_view_restore', None) is not None:
            self.restore_search_view(self._pending_view_restore)
        if ok and getattr(self, '_pending_search', None):
            self.locate_search_result(self._pending_search)
        if not ok:                                  # pragma: no cover - rare
            logger.info('The editor could not load the stored note: %s', error)

    def _on_content_changed(self, raw):
        self._html = self._canonical_html(raw)
        self._content_loaded = True
        self._page_stale = False
        self._pending_push = False
        if hasattr(self.view, 'mark_content_dirty'):
            self.view.mark_content_dirty()
        self._update_status()
        library = getattr(self.view, 'category_panel', None)
        if library is not None:
            library.search_content_changed()

    def locate_search_result(self, text):
        self._pending_search = text
        if not self._ready or self._pending_push:
            return
        self._pending_search = None
        self._page.runJavaScript('window.__locateDocumentText && window.__locateDocumentText(' +
                                 json.dumps(text, ensure_ascii=False) + ')')

    def capture_search_view(self, callback):
        if self._ready:
            self._page.runJavaScript(
                '({top: document.getElementById("workspace").scrollTop, '
                'left: document.getElementById("workspace").scrollLeft, '
                'selection: window.__quill.getSelection()})', callback)

    def restore_search_view(self, state):
        self._pending_search = None
        self._pending_view_restore = state
        if state is None or not self._ready or self._pending_push:
            return
        self._pending_view_restore = None
        self._page.runJavaScript('window.__restoreDocumentView(' + json.dumps(state) + ')')
        self._save_timer.start()

    def _on_save_requested(self):
        handler = getattr(self.view, 'on_action_save', None)
        if callable(handler):
            handler()

    def _on_image_received(self, token, name, mime, payload):
        """An image was dropped or pasted into the page."""
        url = self._store_image_payload(name, mime, payload)
        self._bridge.imageStored.emit(token, url or '')
        if url and hasattr(self.view, 'mark_content_dirty'):
            self.view.mark_content_dirty()

    def _on_command_requested(self, command, payload):
        handlers = {
            'insert-image': self.insert_image_files,
            'insert-canvas': self.insert_canvas_selection,
            'insert-table': self.insert_table,
            'insert-link': self.insert_link,
            'text-color': self.choose_text_color,
            'image-size': self.resize_image,
        }
        handler = handlers.get(command)
        if handler is None:                         # pragma: no cover - defensive
            logger.debug('Ignoring unknown editor command %s', command)
            return
        try:
            arguments = json.loads(payload) if payload else {}
        except ValueError:
            arguments = {}
        handler(arguments)

    def _apply(self, command, **payload):
        """Run a command in the page, e.g. after a dialog was answered."""
        payload['id'] = command
        if self._ready:
            self._bridge.applyCommand.emit(
                json.dumps(payload, ensure_ascii=False))

    # ── Ribbon ───────────────────────────────────────────────────

    def _push_labels(self):
        if not self._ready:
            return
        self._bridge.setLabels.emit(json.dumps(
            self._label_table(), ensure_ascii=False))

    def _push_ribbon(self):
        if not self._ready:
            return
        self._bridge.setRibbon.emit(json.dumps(
            self._ribbon_spec(), ensure_ascii=False))

    @staticmethod
    def _label_table():
        """Short keys used by editor.js, filled from the catalogue here."""
        return {
            'findNext': _('Find next'),
            'replaceOne': _('Replace'),
            'replaceAll': _('Replace all'),
            'findPlaceholder': _('Find what'),
            'replacePlaceholder': _('Replace with'),
            'close': _('Close'),
            'notFound': _('No match found'),
            'replaceDone': _('Replaced {count} occurrence(s)'),
            'tableHint': _('Put the caret inside a table first'),
            'imageHint': _('Select an image first'),
            'selectTextFirst': _('Select the text to clear first'),
            'unsupportedImage': _('{name} is not an image'),
            'imageFailed': _('Could not add {name}'),
            'zoomLabel': _('Zoom {percent}%'),
        }

    def _font_families(self):
        """Fonts for the ribbon, common CJK faces first.

        A Qt build without a font directory (headless CI, a stripped runtime)
        reports nothing at all; the well-known names are offered then, so the
        list is never empty.
        """
        try:
            families = QtGui.QFontDatabase.families()
        except Exception:                           # pragma: no cover - no font db
            families = []
        if not families:
            return list(dict.fromkeys(PREFERRED_FONTS))
        ordered = [name for name in PREFERRED_FONTS if name in families]
        ordered += [name for name in families if name not in ordered]
        return ordered[:_FONT_LIMIT]

    def _ribbon_spec(self):
        """The WPS-style ribbon, described once here so every label is
        translated and the page only has to render it."""
        def button(command, label, tip, mode='action', value=None,
                   style=None, group=None):
            item = {'type': 'button', 'command': command, 'label': label,
                    'tip': tip, 'mode': mode}
            if value is not None:
                item['value'] = value
            if style:
                item['style'] = style
            if group:
                item['group'] = group
            return item

        home = {'id': 'home', 'label': _('Home'), 'groups': [
            {'items': [
                button('undo', _('Undo'), _('Undo (Ctrl+Z)')),
                button('redo', _('Redo'), _('Redo (Ctrl+Y)')),
            ]},
            {'items': [
                {'type': 'select', 'command': 'font', 'tip': _('Font'),
                 'options': [{'value': '', 'label': _('Default')}] + [
                     {'value': name, 'label': name}
                     for name in self._font_families()]},
                {'type': 'input', 'command': 'fontSize', 'unit': 'pt',
                 'value': '11', 'tip': _('Font size, type a value')},
            ]},
            {'items': [
                button('bold', _('Bold'), _('Bold (Ctrl+B)'), 'toggle',
                       style='bold'),
                button('italic', _('Italic'), _('Italic (Ctrl+I)'), 'toggle',
                       style='italic'),
                button('underline', _('Underline'), _('Underline (Ctrl+U)'),
                       'toggle', style='underline'),
            ]},
            {'items': [
                button('header', _('Heading 1'), _('Heading 1'), 'radio',
                       value=1),
                button('header', _('Heading 2'), _('Heading 2'), 'radio',
                       value=2),
                button('header', _('Body text'), _('Body text'), 'radio',
                       value=False),
                button('lineHeight', _('Single spacing'), _('Single spacing'),
                       'radio', value='100%'),
                button('lineHeight', _('1.5 line spacing'),
                       _('1.5 line spacing'), 'radio', value='150%'),
                button('lineHeight', _('Double spacing'), _('Double spacing'),
                       'radio', value='200%'),
            ]},
            {'items': [
                button('align', _('Align left'), _('Align left'), 'radio',
                       value=False),
                button('align', _('Center'), _('Center'), 'radio',
                       value='center'),
                button('align', _('Align right'), _('Align right'), 'radio',
                       value='right'),
                button('text-color', _('Text color'), _('Text color')),
                button('clear-format', _('Clear formatting'),
                       _('Clear formatting')),
            ]},
            {'items': [
                button('list', _('Bullet list'), _('Bullet list'), 'radio',
                       value='bullet'),
                button('list', _('Numbered list'), _('Numbered list'),
                       'radio', value='ordered'),
                button('code-block', _('Code block'), _('Code block'),
                       'toggle'),
                button('find-replace', _('Find and replace (Ctrl+H)'),
                       _('Find and replace (Ctrl+H)')),
            ]},
        ]}

        insert = {'id': 'insert', 'label': _('Insert'), 'groups': [
            {'items': [
                button('insert-image', _('Insert an image file'),
                       _('Insert an image file')),
                button('insert-canvas', _('Insert the selected canvas items'),
                       _('Insert the selected canvas items')),
            ]},
            {'items': [
                button('insert-table', _('Insert a table'),
                       _('Insert a table')),
                button('table-add-row', _('Add row'), _('Add row')),
                button('table-del-row', _('Delete row'), _('Delete row')),
                button('table-add-col', _('Add column'), _('Add column')),
                button('table-del-col', _('Delete column'),
                       _('Delete column')),
            ]},
            {'items': [
                button('insert-link', _('Insert a link'), _('Insert a link')),
            ]},
        ]}

        layout = {'id': 'layout', 'label': _('Page Layout'), 'groups': [
            {'items': [
                button('image-size', _('Image size'), _('Image size')),
                button('image-delete', _('Delete image'),
                       _('Delete the selected image')),
            ]},
            {'items': [
                button('page-view', _('Page view'), _('Page view'), 'radio',
                       value='page', group='view'),
                button('web-view', _('Web view'), _('Web view'), 'radio',
                       value='web', group='view'),
            ]},
            {'items': [
                button('zoom-out', _('Zoom out'), _('Zoom out')),
                button('zoom-reset', _('Reset zoom'), _('Reset zoom')),
                button('zoom-in', _('Zoom in'), _('Zoom in')),
            ]},
        ]}

        return {'tabs': [home, insert, layout], 'initialTab': 'home'}

    # ── Attachments ──────────────────────────────────────────────

    def _attachments(self):
        scene = self.view.scene
        if not hasattr(scene, 'attachments') or scene.attachments is None:
            scene.attachments = {}
        return scene.attachments

    def _scratch_dir(self):
        if self._scratch is None:
            _prune_scratch_dirs()
            base = os.path.join(
                tempfile.gettempdir(), f'prism-document-{os.getpid()}')
            os.makedirs(base, exist_ok=True)
            self._scratch = base
            _SCRATCH_DIRS.append(base)
        return self._scratch

    def _staged_path(self, attachment_id, suffix):
        return os.path.join(self._scratch_dir(), f'{attachment_id}{suffix}')

    def _stage_attachment(self, attachment_id, entry):
        """Mirror one attachment into the scratch directory.

        Returns its file:// URL, or None when the bytes cannot be written.
        What lands there is not always the stored file - see
        :meth:`_display_copy`.
        """
        name, _mime, blob = entry
        if blob is None:
            # The record exists but its bytes do not.  That happens when a
            # save was interrupted between writing the metadata row and the
            # blob (see the KeyError/dictionary-changed crashes on the save
            # path).  `len(blob)` below used to raise TypeError and take the
            # whole panel down when switching from a canvas to a document.
            logger.warning('Attachment %s has no bytes stored; skipping',
                           attachment_id)
            return None
        data, suffix = self._display_copy(name, blob)
        path = self._staged_path(attachment_id, suffix)
        if self._staged.get(attachment_id) == (path, len(blob)):
            return _file_url(path)
        try:
            with open(path, 'wb') as handle:
                handle.write(data)
        except OSError:                             # pragma: no cover - rare
            logger.exception('Could not stage attachment %s', attachment_id)
            return None
        self._staged[attachment_id] = (path, len(blob))
        return _file_url(path)

    def _display_copy(self, name, blob):
        """Return ``(bytes, suffix)`` for the copy the page should show.

        The board stores what the user brought in, at the size it came in.
        Drawing that straight away would make the renderer hold a
        40-megapixel screenshot in memory to show it in a column 700 pixels
        wide, so a picture past :data:`MAX_ATTACHMENT_EDGE` is decoded once,
        here, and written out small.  This copy is only ever shown: it never
        travels back into the note, and the board keeps the original.
        """
        suffix = os.path.splitext(name or '')[1].lower() or '.png'
        if not _larger_than_editor_draws(blob):
            return blob, suffix
        image = QtGui.QImage.fromData(blob)
        if image.isNull():                          # pragma: no cover - rare
            return blob, suffix
        scaled = self._scaled(image)
        buffer = QtCore.QBuffer()
        buffer.open(QtCore.QIODevice.OpenModeFlag.WriteOnly)
        if not scaled.save(buffer, 'PNG'):          # pragma: no cover - rare
            return blob, suffix
        logger.debug('Showing attachment %s from a %dx%d copy', name,
                     scaled.width(), scaled.height())
        return bytes(buffer.data()), '.png'

    def _display_html(self, html):
        """Rewrite prism:// picture references into loadable file:// URLs."""
        attachments = self._attachments()

        def replace(match):
            attachment_id = int(match.group(1))
            entry = attachments.get(attachment_id)
            if entry is None:
                logger.info('Note refers to missing attachment %s',
                            attachment_id)
                return match.group(0)
            return self._stage_attachment(attachment_id, entry) \
                or match.group(0)

        return _ATTACHMENT_URL.sub(replace, html or '')

    def _canonical_html(self, html):
        """Rewrite the page's picture URLs back into prism:// references."""
        if not html:
            return ''
        text = html
        if self._scratch:
            prefix = _file_url(self._scratch).rstrip('/') + '/'
            pattern = re.compile(re.escape(prefix) + r'([^"\'\s<>]+)')

            def restore(match):
                stem = os.path.splitext(match.group(1))[0]
                if stem.isdigit():
                    return attachment_url(stem)
                return match.group(0)

            text = pattern.sub(restore, text)
        return _DATA_URL.sub(self._absorb_data_url, text)

    def _absorb_data_url(self, match):
        """Store an inline image the page sent before Python could see it.

        Dropping a picture inserts it straight away and asks Python for a URL
        in parallel; if the note is written in between, the data URL is still
        in the HTML, and it would end up embedded in the board file forever.
        """
        mime, payload = match.group(1), match.group(2)
        try:
            blob = base64.b64decode(payload)
        except (binascii.Error, ValueError):         # pragma: no cover - rare
            return match.group(0)
        if not blob:                                # pragma: no cover - rare
            return match.group(0)
        suffix = _MIME_EXTENSIONS.get(mime.lower(), '.png')
        attachment_id = self._store_picture(f'pasted{suffix}', blob, mime)
        return f'src="{attachment_url(attachment_id)}"'

    def _store_image_payload(self, name, mime, payload):
        """Store bytes coming from the page, return the URL to show them by."""
        try:
            blob = base64.b64decode(payload)
        except (binascii.Error, ValueError):
            logger.info('Ignoring malformed image data from the editor')
            return None
        if not blob:
            logger.info('Ignoring empty image data from the editor: %s', name)
            return None
        attachment_id = self._store_picture(name, blob, mime)
        entry = self._attachments().get(attachment_id)
        return self._stage_attachment(attachment_id, entry) if entry else None

    def _store_picture(self, name, blob, mime=None):
        """Put a picture into the board exactly as it arrived.

        These bytes are the user's own file, so they are stored as they are.
        Shrinking or re-encoding them here would throw away quality that
        Prism cannot bring back, and the note already decides how big the
        picture is drawn: :meth:`_display_copy` writes a smaller copy for the
        editor to show, and this original is what gets saved and exported.
        """
        attachments = self._attachments()
        attachment_id = max(attachments.keys(), default=0) + 1
        attachments[attachment_id] = (
            name or f'pasted{extension_for(mime)}',
            mime or _mime_for_name(name),
            bytes(blob))
        return attachment_id

    def _store_rendered_picture(self, name, image):
        """Store a picture Prism itself painted, such as a canvas item.

        There is no file behind it, so PNG is the only honest format: nothing
        is lost on the way in, and nothing is guessed at.
        """
        buffer = QtCore.QBuffer()
        buffer.open(QtCore.QIODevice.OpenModeFlag.WriteOnly)
        if not image.save(buffer, 'PNG'):           # pragma: no cover - rare
            logger.info('Could not encode %s for the note', name)
            return None
        return self._store_picture(name or 'canvas.png', bytes(buffer.data()),
                                   'image/png')

    @staticmethod
    def _scaled(image):
        if max(image.width(), image.height()) > MAX_ATTACHMENT_EDGE:
            image = image.scaled(
                MAX_ATTACHMENT_EDGE, MAX_ATTACHMENT_EDGE,
                QtCore.Qt.AspectRatioMode.KeepAspectRatio,
                QtCore.Qt.TransformationMode.SmoothTransformation)
        return image

    # ── Commands that need the rest of Prism ─────────────────────

    def insert_image_files(self, _payload=None):
        """Insert pictures picked from disk, at the quality they are on disk."""
        formats = ' '.join(f'*{ext}' for ext in sorted(IMAGE_EXTENSIONS))
        filenames, _selected = QtWidgets.QFileDialog.getOpenFileNames(
            self, _('Insert image'), '', f'{_("Images")} ({formats})')
        urls = []
        for path in filenames:
            blob = _read_file(path)
            if blob is None:
                logger.info('Skipping unreadable image: %s', path)
                continue
            name = os.path.basename(path)
            attachment_id = self._store_picture(name, blob,
                                                _mime_for_name(name))
            entry = self._attachments().get(attachment_id)
            url = self._stage_attachment(attachment_id, entry) if entry else None
            if url:
                urls.append(url)
        if urls:
            self._apply('insert-images', urls=urls)
            if hasattr(self.view, 'mark_content_dirty'):
                self.view.mark_content_dirty()

    def insert_canvas_selection(self, _payload=None):
        """Insert the pictures selected on the canvas."""
        from prism.items import PrismPixmapItem

        urls = []
        for item in self.view.scene.selectedItems(user_only=True):
            if not isinstance(item, PrismPixmapItem):
                continue
            filename = getattr(item, 'filename', None)
            name = os.path.basename(filename) if filename else _('canvas.png')
            attachment_id = self._canvas_attachment(item, name)
            entry = self._attachments().get(attachment_id) if attachment_id \
                else None
            url = self._stage_attachment(attachment_id, entry) if entry else None
            if url:
                urls.append(url)
            logger.debug('Inserted canvas item %s as %s', item, name)
        if not urls:
            QtWidgets.QMessageBox.information(
                self, _('Insert image'),
                _('Select at least one image on the canvas first.'))
            return
        self._apply('insert-images', urls=urls)
        if hasattr(self.view, 'mark_content_dirty'):
            self.view.mark_content_dirty()

    def _canvas_attachment(self, item, name):
        """Store one canvas item, preferring the file it was imported from.

        An item that still carries its imported bytes is copied over as they
        are; only a picture the canvas produced itself - a pasted screenshot
        with no file behind it - is painted into a fresh PNG.
        """
        original = getattr(item, 'original_bytes', None)
        blob = original() if callable(original) else None
        if blob:
            return self._store_picture(name, blob, _mime_for_name(name))
        pixmap = item.pixmap()
        if pixmap.isNull():
            return None
        return self._store_rendered_picture(name, pixmap.toImage())

    def insert_table(self, _payload=None):
        rows, ok = QtWidgets.QInputDialog.getInt(
            self, _('Insert table'), _('Rows:'), 3, 1, 50)
        if not ok:
            return
        columns, ok = QtWidgets.QInputDialog.getInt(
            self, _('Insert table'), _('Columns:'), 3, 1, 20)
        if not ok:
            return
        self._apply('insert-table', rows=rows, cols=columns)

    def insert_link(self, _payload=None):
        text, ok = QtWidgets.QInputDialog.getText(
            self, _('Insert link'), _('Text:'))
        if not ok:
            return
        url, ok = QtWidgets.QInputDialog.getText(
            self, _('Insert link'), _('URL:'))
        if not ok or not url:
            return
        self._apply('insert-link', text=text or '', url=url)

    def choose_text_color(self, _payload=None):
        color = QtWidgets.QColorDialog.getColor(
            QtGui.QColor('#202124'), self, _('Text color'))
        if not color.isValid():
            return
        self._apply('set-color', color=color.name())

    def resize_image(self, payload=None):
        current = int((payload or {}).get('width') or 0)
        width, ok = QtWidgets.QInputDialog.getInt(
            self, _('Image size'), _('Width (pixels):'),
            max(1, current or 600), 1, 20000)
        if not ok:
            return
        self._apply('image-size', width=width)

    # ── Status ───────────────────────────────────────────────────

    def _update_status(self):
        if self._status is None:                    # pragma: no cover - defensive
            return
        if not self._status_timer.isActive():
            self._status_timer.start()

    def _refresh_status(self):
        html = self._html or ''
        text = _TAG.sub(' ', html)
        words = [word for word in text.split() if word]
        self._status.setText(
            _('{characters} characters · {words} words · {images} images')
            .format(characters=len(text.replace(' ', '')),
                    words=len(words), images=html.count('<img')))
