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

"""Persistent preview cache for the images stored inside a project.

Opening a project used to decode every stored image at full resolution.
That costs a couple of milliseconds per image - several seconds on a
reference library of a few thousand files - although the canvas only
shows them as small pictures until the user zooms in.

This module keeps a downscaled preview *and* the original pixel size
(the canvas needs it for layout) in a database next to the settings
file.  Entries are keyed by a hash of the stored image data, so they
survive renaming or moving a project and are shared between projects
that contain the same image.  A project opens from these previews
without decoding anything, and asks for full resolution only when an
image is actually shown large enough to need it.

A single database file rather than one file per preview is deliberate:
Windows scans freshly written files, and reading back a few thousand of
them costs seconds the first time, while one file is looked at once.
"""

import hashlib
import logging
import os
import sqlite3
import threading
import time

from PyQt6 import QtCore, QtGui

from prism.config import PrismSettings


logger = logging.getLogger(__name__)

#: Longest side of a stored preview, in pixels.  Big enough that the
#: overview a canvas opens with looks sharp, small enough that keeping
#: one per image for a few thousand images does not cost much memory
#: (a preview is held as a full 32 bit pixmap whatever it was stored
#: as).  Beyond this size the original is decoded instead.
#: Preview size, longest side in pixels.  This is the single biggest
#: lever on memory: 1562 previews of an average 604x397 artwork cost
#: about 232 MB at 256, 131 MB at 192 and 58 MB at 128.  192 is the
#: compromise -- small enough to matter, large enough that ordinary
#: viewing does not keep falling back to decoding the original (which
#: happens once what is on screen exceeds the preview's own size).
PREVIEW_MAX_SIDE = 192

#: Bump to invalidate old caches when the preview format changes.
CACHE_VERSION = 'v2'

#: Default cap for the whole cache.
DEFAULT_MAX_BYTES = 512 * 1024 * 1024

#: JPEG quality used for previews of images without transparency.
JPEG_QUALITY = 88

#: How often the cache may rebuild itself before giving up for a session.
MAX_FAILURES = 3

_SCHEMA = (
    'CREATE TABLE IF NOT EXISTS previews ('
    ' key TEXT PRIMARY KEY,'
    ' width INTEGER NOT NULL,'
    ' height INTEGER NOT NULL,'
    ' data BLOB NOT NULL,'
    ' created_at REAL NOT NULL)')


def cache_directory():
    """Directory holding the preview database."""
    settings = PrismSettings()
    return os.path.join(os.path.dirname(settings.fileName()), 'previews')


def digest(data, max_side=PREVIEW_MAX_SIDE):
    """Return a short, stable key for a blob of image data.

    Deliberately a content hash rather than the file name or the item id:
    the same picture is then cached once no matter where it is used, and
    an image that was replaced in the project can never be served from a
    stale entry.

    The preview size is part of the key on purpose.  Without it, lowering
    PREVIEW_MAX_SIDE would keep serving previews that were built at the
    old size, and the memory would not actually come down.
    """
    hasher = hashlib.blake2b(data, digest_size=16)
    hasher.update(f':{max_side}'.encode())
    return hasher.hexdigest()


def make_preview(image, max_side=PREVIEW_MAX_SIDE):
    """Downscale an image so that its longest side is at most max_side."""
    width, height = image.width(), image.height()
    longest = max(width, height)
    if longest <= max_side or longest <= 0:
        return image
    scale = max_side / longest
    return image.scaled(
        max(1, round(width * scale)), max(1, round(height * scale)),
        QtCore.Qt.AspectRatioMode.KeepAspectRatio,
        QtCore.Qt.TransformationMode.SmoothTransformation)


def encode_preview(image):
    """Encode a preview; returns None if the image cannot be written."""
    buffer = QtCore.QBuffer()
    buffer.open(QtCore.QIODevice.OpenModeFlag.WriteOnly)
    # Transparency would turn black in a JPEG, and those are exactly the
    # cut-out references where it matters most.
    if image.hasAlphaChannel():
        ok = image.save(buffer, 'PNG')
    else:
        ok = image.save(buffer, 'JPG', JPEG_QUALITY)
    if not ok:
        return None
    return bytes(buffer.data())


def decode_preview_image(blob, max_side=PREVIEW_MAX_SIDE):
    """Decode an image straight down to preview size.

    Returns ``(image, original_size)``, or ``(None, None)`` if the data
    cannot be read.

    The whole point is never to hold the full size image.  Opening a
    project with a thousand photographs used to decode every one of them
    at full resolution before the canvas had drawn a single frame; here
    the reader reports the original size from the header (cheap, no
    pixels) and then scales *while decoding*, so a 6000x4000 photo costs
    a 256 pixel image instead of ninety megapixels.
    """
    buffer = QtCore.QBuffer()
    buffer.setData(QtCore.QByteArray(blob))
    if not buffer.open(QtCore.QIODevice.OpenModeFlag.ReadOnly):
        return None, None

    reader = QtGui.QImageReader(buffer)
    size = reader.size()
    if size.isValid() and not size.isEmpty():
        # Qt 的 QSize.scaled() 在有框时会放大，而这里只该缩小：小图
        # 保持原样，别为了一张 40x30 的小图铺 256x192 的内存。
        if max(size.width(), size.height()) > max_side:
            reader.setScaledSize(size.scaled(
                max_side, max_side,
                QtCore.Qt.AspectRatioMode.KeepAspectRatio))
        image = reader.read()
        if image.isNull():
            return None, None
        if max(image.width(), image.height()) > max_side:
            # The plugin ignored setScaledSize (uncommon or animated
            # formats); shrink what came back rather than keep it.
            image = make_preview(image, max_side)
        return image, size

    # This format cannot be sized from the header alone, so the only way
    # to learn the size is to decode.  Do it, then shrink immediately.
    image = reader.read()
    if image.isNull():
        return None, None
    size = image.size()
    return make_preview(image, max_side), size


class Preview:
    """A cached preview together with the size of the original image."""

    __slots__ = ('image', 'width', 'height')

    def __init__(self, image, width, height):
        self.image = image
        self.width = width
        self.height = height

    @property
    def size(self):
        return QtCore.QSize(self.width, self.height)


class ThumbnailCache:
    """Database-backed store for previews, keyed by image data hash.

    Safe to use from several threads: each thread gets its own
    connection, and the database runs in WAL mode so that previews can
    be written in the background while the canvas reads them.
    """

    def __init__(self, directory=None, max_bytes=DEFAULT_MAX_BYTES,
                 max_side=PREVIEW_MAX_SIDE):
        self.directory = directory
        self.max_bytes = max_bytes
        self.max_side = max_side
        self._local = threading.local()
        self._stores_since_prune = 0
        self._prune_after = 250
        self._failures = 0
        self._broken = False

    # ── database plumbing ────────────────────────────────────────────

    @property
    def path(self):
        return os.path.join(self.directory, f'previews-{CACHE_VERSION}.db')

    def _connect(self):
        os.makedirs(self.directory, exist_ok=True)
        self._discard_old_files()
        connection = sqlite3.connect(self.path, timeout=30)
        connection.execute('PRAGMA journal_mode=WAL')
        connection.execute('PRAGMA synchronous=NORMAL')
        connection.execute(_SCHEMA)
        connection.commit()
        return connection

    def _discard_old_files(self):
        """Remove leftovers from earlier preview formats, if any."""
        try:
            names = os.listdir(self.directory)
        except OSError:
            return
        current = os.path.basename(self.path)
        for name in names:
            if name == current or name.startswith(current):
                continue
            if name.endswith('.ptc') or name.endswith('.db'):
                try:
                    os.remove(os.path.join(self.directory, name))
                except OSError:
                    pass

    def connection(self):
        """The calling thread's connection, created on first use."""
        connection = getattr(self._local, 'connection', None)
        if connection is None:
            connection = self._connect()
            self._local.connection = connection
        return connection

    def close(self):
        """Close this thread's connection."""
        connection = getattr(self._local, 'connection', None)
        if connection is not None:
            connection.close()
            self._local.connection = None

    def _forget(self):
        """Deal with a database that cannot be used at all.

        The cache is an optimisation; a broken one must not keep a
        project from opening.  It is dropped so that the next attempt
        can build it again, but after repeated failures the cache gives
        up for this session rather than trying again on every image.
        """
        logger.warning('Preview cache unusable; rebuilding it', exc_info=True)
        self.close()
        for suffix in ('', '-wal', '-shm'):
            try:
                os.remove(self.path + suffix)
            except OSError:
                pass
        self._failures += 1
        if self._failures >= MAX_FAILURES:
            logger.warning('Giving up on the preview cache for this session')
            self._broken = True

    # ── reading and writing ──────────────────────────────────────────

    def load(self, key):
        """Return the cached Preview for key, or None if there is none."""
        if self._broken:
            return None
        try:
            row = self.connection().execute(
                'SELECT width, height, data FROM previews WHERE key=?',
                (key,)).fetchone()
        except sqlite3.Error:
            self._forget()
            return None
        if row is None:
            return None
        width, height, payload = row
        if not payload or width <= 0 or height <= 0:
            return None
        image = QtGui.QImage.fromData(payload)
        if image.isNull():
            return None
        return Preview(image, width, height)

    def store(self, key, image, width, height):
        """Cache a preview of the given image; failures are not fatal."""
        if self._broken:
            return False
        if image is None or image.isNull() or width <= 0 or height <= 0:
            return False
        payload = encode_preview(make_preview(image, self.max_side))
        if not payload:
            return False
        try:
            connection = self.connection()
            connection.execute(
                'INSERT OR REPLACE INTO previews '
                '(key, width, height, data, created_at) VALUES (?, ?, ?, ?, ?)',
                (key, int(width), int(height), payload, time.time()))
            connection.commit()
        except sqlite3.Error:
            self._forget()
            return False
        self._stores_since_prune += 1
        if self._stores_since_prune >= self._prune_after:
            self._stores_since_prune = 0
            self.prune()
        return True

    # ── housekeeping ─────────────────────────────────────────────────

    def entry_count(self):
        if self._broken:
            return 0
        try:
            row = self.connection().execute(
                'SELECT COUNT(*) FROM previews').fetchone()
        except sqlite3.Error:
            self._forget()
            return 0
        return row[0] if row else 0

    def size_on_disk(self):
        if self._broken:
            return 0
        try:
            row = self.connection().execute(
                'SELECT COALESCE(SUM(LENGTH(data)), 0) FROM previews').fetchone()
        except sqlite3.Error:
            self._forget()
            return 0
        return row[0] if row else 0

    def prune(self, max_bytes=None):
        """Drop the least recently written previews until under the cap."""
        limit = self.max_bytes if max_bytes is None else max_bytes
        if self._broken or limit is None or limit <= 0:
            return 0
        total = self.size_on_disk()
        if total <= limit:
            return 0
        removed = 0
        try:
            connection = self.connection()
            for key, size in connection.execute(
                    'SELECT key, LENGTH(data) FROM previews '
                    'ORDER BY created_at').fetchall():
                if total <= limit:
                    break
                connection.execute('DELETE FROM previews WHERE key=?', (key,))
                total -= size
                removed += 1
            connection.commit()
        except sqlite3.Error:
            self._forget()
            return removed
        if removed:
            logger.debug('Pruned %d cached previews (%d bytes left)',
                         removed, total)
        return removed

    def clear(self):
        """Remove every cached preview."""
        if self._broken:
            return 0
        try:
            connection = self.connection()
            removed = self.entry_count()
            connection.execute('DELETE FROM previews')
            connection.commit()
            connection.execute('VACUUM')
        except sqlite3.Error:
            self._forget()
            return 0
        return removed


#: Shared instance used by the item classes.  Created lazily so that
#: importing Prism does not touch the disk.
_cache = None
_cache_lock = threading.Lock()


def cache():
    global _cache
    with _cache_lock:
        if _cache is None:
            _cache = ThumbnailCache(cache_directory())
        return _cache


def load(data_or_key):
    """Look up a preview by image data (or a precomputed key)."""
    key = data_or_key if isinstance(data_or_key, str) else digest(data_or_key)
    return cache().load(key)


def store(data, image, width, height):
    """Cache a preview for the given image data."""
    return cache().store(digest(data), image, width, height)


# ── filling the cache in the background ──────────────────────────────
#
# Building a preview costs a couple of milliseconds (scaling and
# encoding), which on a project with a thousand images is several
# seconds - too much to add to opening a file.  The first time an image
# is seen it is decoded the ordinary way and only *queued* here; a
# background thread turns the queued images into previews while the
# user gets on with their work, so the next time the project opens it
# is fast.

_pending = []
_pending_lock = threading.Lock()
_build_thread = None


def defer_preview(key, blob):
    """Remember an image whose preview still has to be built."""
    with _pending_lock:
        _pending.append((key, blob))


def pending_count():
    with _pending_lock:
        return len(_pending)


def start_background_build():
    """Build previews for everything queued so far, in the background.

    Deliberately a plain daemon thread using only QImage: images are
    readable and writable from a worker thread, and nothing here may
    keep the application from closing.
    """
    global _build_thread
    with _pending_lock:
        if not _pending:
            return None
        if _build_thread is not None and _build_thread.is_alive():
            # A previous run is still busy; there is nothing to hand it.
            return _build_thread
        entries = list(_pending)
        _pending.clear()
    thread = threading.Thread(
        target=_build_previews, args=(entries,), daemon=True,
        name='prism-preview-cache')
    _build_thread = thread
    thread.start()
    return thread


def wait_for_background_build(timeout=None):
    """Block until the background build finished (used by tests)."""
    thread = _build_thread
    if thread is not None:
        thread.join(timeout)


def _build_previews(entries):
    cache_ = cache()
    built = 0
    start = time.monotonic()
    while True:
        for key, blob in entries:
            try:
                # 这里刻意只用 QImage。QImageReader/QBuffer 在后台线程
                # 还在跑、进程开始退出时会踩到正在销毁的全局状态，让一个
                # 测试全过的进程以 0xC0000005 收场（run_tests_safe.py 就会
                # 把 9 passed 报成 FAIL）。前台路径 load_stored_image 可以
                # 用 decode_preview_image 换速度 —— 它在 GUI 线程里，也不会
                # 活到进程退出之后；后台这条不行。
                image = QtGui.QImage()
                image.loadFromData(blob)
                if image.isNull():
                    continue
                preview = make_preview(image)
                if cache_.store(key, preview, image.width(), image.height()):
                    built += 1
            except Exception:
                # Caching is an optimisation; never let it break a session
                logger.debug('Could not build preview for %s', key,
                             exc_info=True)
            # Give the application back its time slice: this runs while
            # the user is working and should not be noticed.
            time.sleep(0.001)
        # Another project may have been opened while this ran
        with _pending_lock:
            if not _pending:
                break
            entries = list(_pending)
            _pending.clear()
    logger.debug('Built %d previews in %.1f s', built,
                 time.monotonic() - start)
