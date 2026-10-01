"""Clipboard capture: adapter, inbox items and the capture service.

Layering follows the task book: the adapter knows Qt and nothing about
Prism; the service owns the inbox and the rules; the page and asset
models stay untouched until the user files something.

    ClipboardCaptureAdapter -> ClipboardCaptureService -> AssetImport
"""
import hashlib
import logging
import os
import sys
import time
from urllib import parse, request
from urllib.error import HTTPError, URLError

from PyQt6 import QtCore, QtGui

logger = logging.getLogger(__name__)

#: Collapse the burst of dataChanged signals a single copy can produce.
DEBOUNCE_MS = 120

#: Our own marker, so a paste we perform is never captured back.
OWN_MARKER = 'application/x-prism-clipboard'

#: Formats worth keeping, in the order we prefer them.  Original MIME
#: bytes beat re-encoding: a PNG stays a PNG, an animated GIF keeps its
#: frames.
IMAGE_FORMATS = (
    'image/png',
    'image/jpeg',
    'image/webp',
    'image/gif',
    'image/bmp',
    'image/tiff',
    'image/x-exr',
)

#: Anything larger than this is not worth putting in the inbox.
MAX_BYTES = 64 * 1024 * 1024

#: Defaults for the inbox limits; the settings dialog can override them.
DEFAULT_MAX_ITEMS = 50
DEFAULT_MAX_AGE_HOURS = 24

#: Applications whose clipboard contents are never collected unless the
#: user takes them off this list.  These are password managers and
#: credential prompts: whatever they put on the clipboard is by definition
#: something the user does not want sitting in a picture inbox.
#:
#: Matched against the lower-case process name without the ``.exe`` suffix,
#: which is exactly what foreground_process_name() returns.
SENSITIVE_APPLICATIONS = (
    '1password',
    '1passwordbeta',
    'bitwarden',
    'dashlane',
    'enpass',
    'keepass',
    'keepass2',
    'keepassxc',
    'keeper',
    'lastpass',
    'nordpass',
    'roboform',
    'credentialuibroker',
    'logonui',
    'lsass',
)

#: Setting key for extra process names the user wants ignored, on top of
#: SENSITIVE_APPLICATIONS.
EXTRA_EXCLUDED = 'Clipboard/excluded_apps'


def parse_excluded(text):
    """Split the exclusion setting into process names.

    Accepts commas, semicolons and newlines so any way of typing a list
    works.  Entries are lower-cased and stripped of a trailing ``.exe``,
    which is what foreground_process_name() returns.

    Stored as one string rather than a QSettings list on purpose: the INI
    backend writes lists in a format that is easy to break by hand, and
    this is a setting people will edit by hand.
    """
    names = set()
    for chunk in (text or '').replace(';', ',').replace('\n', ',').split(','):
        name = chunk.strip().lower()
        if name.endswith('.exe'):
            name = name[:-4]
        if name:
            names.add(name)
    return names


def excluded_applications(settings=None):
    """The effective exclusion list.

    Built-in password managers plus whatever the user added.  Reading the
    setting must never be able to break capture, so a malformed value just
    falls back to the built-in list.
    """
    names = set(SENSITIVE_APPLICATIONS)
    if settings is not None:
        try:
            names |= parse_excluded(settings.valueOrDefault(EXTRA_EXCLUDED))
        except Exception:
            logger.debug('Could not read the excluded-app list',
                         exc_info=True)
    return names


def foreground_process_name():
    """Best-effort name of the process owning the foreground window.

    The clipboard does not say who put data on it.  Sampling the foreground
    window at the moment ``dataChanged`` fires is the usual approximation:
    whatever the user just acted in is almost always the application that
    just copied something.  It is only an approximation -- a background
    tool can rewrite the clipboard without ever being in front -- so an
    unknown result must be treated as "unknown", never as "safe".

    Returns the lower-case executable name without its suffix, or ``''``
    when it cannot be determined (non-Windows, no foreground window, or an
    API call that fails).
    """
    if sys.platform != 'win32':
        return ''
    try:
        import ctypes
        import ctypes.wintypes

        user32 = ctypes.WinDLL('user32', use_last_error=True)
        kernel32 = ctypes.WinDLL('kernel32', use_last_error=True)

        user32.GetForegroundWindow.restype = ctypes.wintypes.HWND
        window = user32.GetForegroundWindow()
        if not window:
            return ''

        user32.GetWindowThreadProcessId.argtypes = [
            ctypes.wintypes.HWND, ctypes.POINTER(ctypes.wintypes.DWORD)]
        user32.GetWindowThreadProcessId.restype = ctypes.wintypes.DWORD
        pid = ctypes.wintypes.DWORD()
        user32.GetWindowThreadProcessId(window, ctypes.byref(pid))
        if not pid.value:
            return ''

        # PROCESS_QUERY_LIMITED_INFORMATION also works across integrity
        # levels, unlike PROCESS_QUERY_INFORMATION.
        kernel32.OpenProcess.argtypes = [ctypes.wintypes.DWORD,
                                         ctypes.wintypes.BOOL,
                                         ctypes.wintypes.DWORD]
        kernel32.OpenProcess.restype = ctypes.wintypes.HANDLE
        handle = kernel32.OpenProcess(0x1000, False, pid.value)
        if not handle:
            return ''
        try:
            buffer = ctypes.create_unicode_buffer(1024)
            size = ctypes.wintypes.DWORD(len(buffer))
            kernel32.QueryFullProcessImageNameW.argtypes = [
                ctypes.wintypes.HANDLE, ctypes.wintypes.DWORD,
                ctypes.wintypes.LPWSTR,
                ctypes.POINTER(ctypes.wintypes.DWORD)]
            kernel32.QueryFullProcessImageNameW.restype = ctypes.wintypes.BOOL
            if not kernel32.QueryFullProcessImageNameW(
                    handle, 0, buffer, ctypes.byref(size)):
                return ''
        finally:
            kernel32.CloseHandle(handle)

        name = os.path.basename(buffer.value)
        if name.lower().endswith('.exe'):
            name = name[:-4]
        return name.lower()
    except Exception:
        # Knowing the source is a nicety; never let it break capture.
        logger.debug('Could not determine the clipboard owner',
                     exc_info=True)
        return ''


class ClipboardSnapshot:
    """One clipboard reading, detached from the live QMimeData."""

    __slots__ = ('data', 'suffix', 'source_format', 'has_text', 'digest',
                 'source_url')

    def __init__(self, data, suffix, source_format, has_text,
                 source_url=None):
        self.data = data
        self.suffix = suffix
        self.source_format = source_format
        self.has_text = has_text
        self.digest = hashlib.sha256(data).hexdigest()
        self.source_url = source_url

    def __repr__(self):
        return (f'<ClipboardSnapshot {self.source_format} '
                f'{len(self.data)} bytes {self.digest[:12]}>')


def snapshot_from_mime(mime):
    """Copy the image bytes out of ``QMimeData``, or return None.

    Everything is copied here and now: the caller must not keep ``mime``
    around, because Qt is free to invalidate it once the signal handler
    returns.
    """
    if mime is None:
        return None

    has_text = mime.hasText()
    formats = set(mime.formats())

    # Prefer the original encoded bytes over anything Qt would re-encode.
    for name in IMAGE_FORMATS:
        if name not in formats:
            continue
        payload = bytes(mime.data(name))
        if not payload or len(payload) > MAX_BYTES:
            continue
        suffix = name.split('/')[-1]
        if suffix == 'jpeg':
            suffix = 'jpg'
        return ClipboardSnapshot(payload, suffix, name, has_text)

    # An image pasted from an application that only offers QImage.  Losing
    # the original encoding is unavoidable here; PNG keeps it lossless.
    if mime.hasImage():
        image = mime.imageData()
        if isinstance(image, QtGui.QImage) and not image.isNull():
            buffer = QtCore.QBuffer()
            buffer.open(QtCore.QIODevice.OpenModeFlag.WriteOnly)
            if image.save(buffer, 'PNG'):
                payload = bytes(buffer.data())
                if 0 < len(payload) <= MAX_BYTES:
                    return ClipboardSnapshot(payload, 'png',
                                             'image/png (re-encoded)',
                                             has_text)
    return None


def http_url_from_mime(mime):
    """Return one explicit HTTP(S) URL from clipboard MIME data."""
    if mime is None:
        return None
    candidates = []
    if mime.hasUrls():
        candidates.extend(url.toString() for url in mime.urls())
    if mime.hasText():
        candidates.append(mime.text().strip())
    for candidate in candidates:
        parsed = parse.urlparse(candidate)
        if parsed.scheme in ('http', 'https') and parsed.netloc:
            return candidate
    return None


def download_url_snapshot(url, max_bytes=MAX_BYTES, opener=None):
    """Download and validate one clipboard URL, preserving response bytes."""
    parsed = parse.urlparse(url)
    if parsed.scheme not in ('http', 'https') or not parsed.netloc:
        raise ValueError('Only HTTP(S) image URLs are supported')
    open_url = opener or request.urlopen
    try:
        with open_url(url, timeout=20) as response:
            content_type = (response.headers.get_content_type()
                            if hasattr(response.headers, 'get_content_type')
                            else response.headers.get('Content-Type', ''))
            payload = response.read(int(max_bytes) + 1)
    except (HTTPError, URLError, OSError) as exc:
        raise ValueError(f'Download failed: {exc}') from exc
    if not payload:
        raise ValueError('The URL returned an empty response')
    if len(payload) > max_bytes:
        raise ValueError('The downloaded image is larger than the inbox limit')
    image = QtGui.QImage.fromData(payload)
    if image.isNull():
        raise ValueError('The URL did not return a supported image')
    suffix = {
        'image/jpeg': 'jpg', 'image/png': 'png', 'image/gif': 'gif',
        'image/webp': 'webp', 'image/bmp': 'bmp', 'image/tiff': 'tiff',
    }.get(str(content_type).split(';', 1)[0].lower())
    if suffix is None:
        suffix = os.path.splitext(parsed.path)[1].lower().lstrip('.') or 'png'
    return ClipboardSnapshot(payload, suffix, content_type or 'image', False,
                             source_url=url)


class UrlDownloadWorker(QtCore.QThread):
    """Background HTTP download for a clipboard URL."""
    succeeded = QtCore.pyqtSignal(str, object)
    failed = QtCore.pyqtSignal(str, str)

    def __init__(self, url, parent=None):
        super().__init__(parent)
        self.url = url

    def run(self):
        try:
            snapshot = download_url_snapshot(self.url)
        except ValueError as exc:
            self.failed.emit(self.url, str(exc))
        else:
            self.succeeded.emit(self.url, snapshot)


class ClipboardCaptureAdapter(QtCore.QObject):
    """Watch the clipboard and report new images, debounced and deduped."""

    snapshot_ready = QtCore.pyqtSignal(object)
    url_ready = QtCore.pyqtSignal(str)

    def __init__(self, parent=None, enabled=True, excluded=None):
        super().__init__(parent)
        self._enabled = bool(enabled)
        #: Process names whose clipboard writes are ignored outright.
        #: Defaults to the password-manager list, so the guard is on even
        #: for callers that know nothing about it.
        self._excluded = {
            name.lower() for name in
            (SENSITIVE_APPLICATIONS if excluded is None else excluded)}
        self._timer = QtCore.QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.setInterval(DEBOUNCE_MS)
        self._timer.timeout.connect(self._on_debounce)
        self._last_digest = None
        self._last_url = None
        self._clipboard = None
        #: Which application owned the foreground window when the clipboard
        #: changed.  Sampled in _on_data_changed rather than _on_debounce:
        #: by the time the debounce fires, focus has often moved on and the
        #: answer would be about the wrong application.
        self._source_app = ''

    def is_excluded(self, process_name):
        """Whether clipboard writes from this application are ignored.

        Compared on the bare process name without ``.exe`` -- exactly what
        foreground_process_name() returns.  An empty name means "could not
        tell", which is *not* the same as "safe", but it is let through:
        refusing everything would disable the feature on any platform where
        the source cannot be determined.
        """
        if not process_name:
            return False
        name = process_name.lower()
        if name.endswith('.exe'):
            name = name[:-4]
        return name in self._excluded

    def set_excluded(self, names):
        """Replace the exclusion list (used when settings change)."""
        self._excluded = {str(name).lower() for name in (names or ())}

    def start(self):
        clipboard = QtGui.QGuiApplication.clipboard()
        if clipboard is None:
            logger.warning('No clipboard available; capture disabled')
            return
        if clipboard is self._clipboard:
            return
        self.stop()
        self._clipboard = clipboard
        clipboard.dataChanged.connect(self._on_data_changed)
        logger.info('Clipboard capture watching')

    def stop(self):
        if self._clipboard is not None:
            try:
                self._clipboard.dataChanged.disconnect(self._on_data_changed)
            except (TypeError, RuntimeError):
                pass
            self._clipboard = None

    @property
    def enabled(self):
        return self._enabled

    def set_enabled(self, enabled):
        self._enabled = bool(enabled)
        if not enabled:
            self._timer.stop()

    def _on_data_changed(self):
        if not self._enabled:
            return
        clipboard = self._clipboard
        if clipboard is None:
            return
        # Pasting an image into Prism must not feed it back into the inbox.
        # QClipboard.ownsClipboard() also returns true in tests and for other
        # in-process producers, so ownership alone cannot distinguish Prism's
        # Copy action.  That action writes this explicit marker instead.
        mime = clipboard.mimeData()
        if mime is not None and mime.hasFormat(OWN_MARKER):
            logger.trace('Ignoring clipboard data carrying our own marker')
            return
        # Sample the source now, not in _on_debounce: the debounce waits
        # 120 ms and by then focus may already have moved, which would
        # attribute the copy to the wrong application.
        self._source_app = foreground_process_name()
        self._timer.start()

    def _on_debounce(self):
        if not self._enabled:
            return
        clipboard = self._clipboard
        if clipboard is None:
            return
        # Sensitivity guard goes first, so the bytes are never even read
        # out of a password manager's clipboard payload.
        if self.is_excluded(self._source_app):
            logger.info('Clipboard change came from an excluded application '
                        '(%s); not collected', self._source_app)
            return
        snapshot = snapshot_from_mime(clipboard.mimeData())
        if snapshot is None:
            url = http_url_from_mime(clipboard.mimeData())
            if url and url != self._last_url:
                self._last_url = url
                self.url_ready.emit(url)
            return
        # Change-level dedupe: the same bytes arriving again (a double
        # dataChanged, or a second copy of the same picture) is not new.
        if snapshot.digest == self._last_digest:
            logger.trace('Clipboard content unchanged')
            return
        self._last_digest = snapshot.digest
        self.snapshot_ready.emit(snapshot)

    @staticmethod
    def mark_own_mime(mime):
        """Tag MIME data we are about to put on the clipboard."""
        if mime is not None:
            mime.setData(OWN_MARKER, b'1')
        return mime


class InboxItem:
    """One clipboard capture, waiting for the user to file it."""

    __slots__ = ('snapshot', 'added_at', 'preview', 'tags')

    def __init__(self, snapshot, preview=None, tags=None):
        self.snapshot = snapshot
        self.added_at = time.time()
        self.preview = preview
        # Tags the user attached while the capture was still in the inbox.
        # They travel with it when it is filed onto a page.
        self.tags = list(tags) if tags else []

    @property
    def digest(self):
        return self.snapshot.digest

    @property
    def data(self):
        return self.snapshot.data

    @property
    def suffix(self):
        return self.snapshot.suffix

    @property
    def label(self):
        kib = len(self.data) / 1024
        stamp = time.strftime('%H:%M:%S', time.localtime(self.added_at))
        source = '  URL' if self.snapshot.source_url else ''
        return f'{stamp}  {self.suffix.upper()}  {kib:.0f} KB{source}'


class ClipboardCaptureService(QtCore.QObject):
    """The clipboard inbox.

    Captures land here rather than straight on the canvas: a screenshot
    taken for something else must not end up inside the page the user is
    working on.  Filing them is an explicit action.
    """

    item_added = QtCore.pyqtSignal(object)
    item_removed = QtCore.pyqtSignal(object)
    changed = QtCore.pyqtSignal()
    url_status_changed = QtCore.pyqtSignal(str, str, str)

    def __init__(self, parent=None, max_items=DEFAULT_MAX_ITEMS,
                 max_age_hours=DEFAULT_MAX_AGE_HOURS):
        super().__init__(parent)
        self.max_items = int(max_items)
        self.max_age_hours = float(max_age_hours)
        self._items = []
        self._by_digest = {}
        self._ignored = 0
        self._downloads = set()
        self._url_status = {}

    # ── Contents ──────────────────────────────────────────────

    @property
    def items(self):
        return list(self._items)

    def __len__(self):
        return len(self._items)

    @property
    def ignored(self):
        """How many captures were dropped as duplicates."""
        return self._ignored

    def total_bytes(self):
        return sum(len(item.data) for item in self._items)

    @property
    def url_status(self):
        return dict(self._url_status)

    def enqueue_url(self, url):
        """Download a copied image URL without blocking the GUI thread."""
        if any(worker.url == url for worker in self._downloads):
            return
        self._set_url_status(url, 'downloading', '')
        worker = UrlDownloadWorker(url, self)
        self._downloads.add(worker)
        worker.succeeded.connect(self._on_url_downloaded)
        worker.failed.connect(self._on_url_failed)
        worker.finished.connect(lambda: self._finish_download(worker))
        worker.start()

    def _set_url_status(self, url, status, detail):
        self._url_status[url] = (status, detail)
        self.url_status_changed.emit(url, status, detail)

    def _on_url_downloaded(self, url, snapshot):
        self.accept(snapshot)
        self._set_url_status(url, 'complete', '')

    def _on_url_failed(self, url, detail):
        self._set_url_status(url, 'failed', detail)

    def _finish_download(self, worker):
        self._downloads.discard(worker)
        worker.deleteLater()

    # ── Receiving ─────────────────────────────────────────────

    def accept(self, snapshot):
        """Take a snapshot; duplicates are promoted instead of added."""
        self._expire()

        existing = self._by_digest.get(snapshot.digest)
        if existing is not None:
            # History-level dedupe: same content copied again moves the
            # record to the top instead of creating a second entry.
            self._items.remove(existing)
            self._items.insert(0, existing)
            existing.added_at = time.time()
            self._ignored += 1
            self.changed.emit()
            return existing

        item = InboxItem(snapshot, self._make_preview(snapshot))
        self._items.insert(0, item)
        self._by_digest[snapshot.digest] = item
        self._trim()
        self.item_added.emit(item)
        self.changed.emit()
        return item

    def remove(self, item):
        if item in self._items:
            self._items.remove(item)
            self._by_digest.pop(item.digest, None)
            self.item_removed.emit(item)
            self.changed.emit()

    def clear(self):
        removed = list(self._items)
        self._items.clear()
        self._by_digest.clear()
        for item in removed:
            self.item_removed.emit(item)
        self.changed.emit()

    def take(self, items):
        """Remove and return the given items (used when filing them)."""
        taken = []
        for item in items:
            if item in self._items:
                self._items.remove(item)
                self._by_digest.pop(item.digest, None)
                taken.append(item)
        if taken:
            self.changed.emit()
        return taken

    # ── Housekeeping ──────────────────────────────────────────

    def _trim(self):
        while len(self._items) > self.max_items:
            oldest = self._items.pop()
            self._by_digest.pop(oldest.digest, None)
            self.item_removed.emit(oldest)

    def _expire(self):
        if self.max_age_hours <= 0:
            return
        cutoff = time.time() - self.max_age_hours * 3600
        stale = [i for i in self._items if i.added_at < cutoff]
        for item in stale:
            self._items.remove(item)
            self._by_digest.pop(item.digest, None)
            self.item_removed.emit(item)

    @staticmethod
    def _make_preview(snapshot):
        """Decode a small preview; failure just means no thumbnail."""
        image = QtGui.QImage.fromData(snapshot.data)
        if image.isNull():
            return None
        if max(image.width(), image.height()) > 256:
            image = image.scaled(256, 256,
                                 QtCore.Qt.AspectRatioMode.KeepAspectRatio,
                                 QtCore.Qt.TransformationMode.SmoothTransformation)
        return image
