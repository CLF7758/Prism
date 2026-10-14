"""Read images through native Qt devices and deliver display pixels off-thread.

PyQt6 6.7 releases the GIL in QImageReader.read(), but not size(). A Python
QBuffer can reacquire it inside Qt's image-handler mutex. Passing a filename
lets Qt own its QFile and avoids that lock inversion.
"""
from concurrent.futures import ThreadPoolExecutor
from collections import OrderedDict, deque
from contextlib import contextmanager
from pathlib import Path
import logging
import tempfile
import time
import weakref

from prism.performance_trace import measured

from PyQt6 import QtCore, QtGui, QtWidgets, sip
from prism.item_lifecycle import logical_item, is_record
from prism.item_lifecycle import is_deleted

logger = logging.getLogger(__name__)


@contextmanager
def native_reader(blob=None, path=None):
    """Own the backing file until Qt closes it, including on Windows."""
    temporary = None
    reader = None
    try:
        if blob is not None:
            temporary = tempfile.TemporaryDirectory(prefix='prism-image-decode-')
            path = str(Path(temporary.name) / 'image')
            Path(path).write_bytes(blob)
        reader = QtGui.QImageReader(str(path or ''))
        reader.setAutoTransform(True)
        yield reader
    finally:
        if reader is not None:
            # setDevice deletes the internally owned QFile before unlinking.
            reader.setDevice(None)
        if temporary is not None:
            temporary.cleanup()


@measured('image.decode', 32)
def decode(blob=None, path=None, archive=None, max_side=None):
    """Return QImage and original dimensions; never construct a QPixmap."""
    if blob is None and not path and archive:
        from prism.source_store import read_archive
        blob = read_archive(archive[:2])
        if not blob and len(archive) > 2:
            blob = None
            path = archive[2]
    if blob is None and not path:
        return None
    with native_reader(blob=blob, path=path) as reader:
        size = reader.size()
        rotated = bool(reader.transformation().value & 4)
        if max_side and size.isValid() and max(size.width(), size.height()) > max_side:
            scaled = size.scaled(max_side, max_side, QtCore.Qt.AspectRatioMode.KeepAspectRatio)
            reader.setScaledSize(QtCore.QSize(max(1, scaled.width()), max(1, scaled.height())))
        image = reader.read()
        if image.isNull():
            return None
        if not size.isValid() or size.isEmpty():
            size = image.size()
        elif rotated:
            size.transpose()
        if max_side and max(image.width(), image.height()) > max_side:
            scaled = image.size().scaled(max_side, max_side, QtCore.Qt.AspectRatioMode.KeepAspectRatio)
            image = image.scaled(QtCore.QSize(max(1, scaled.width()), max(1, scaled.height())),
                QtCore.Qt.AspectRatioMode.IgnoreAspectRatio,
                QtCore.Qt.TransformationMode.SmoothTransformation)
        return image, size


def decode_thumbnail(value, original_size, snapshot, max_side):
    """Worker-only native memory decoding of bounded JPEG/PNG previews."""
    import base64
    import io
    from PIL import Image
    from prism.image_pyramid import decode_display
    try:
        blob = base64.b64decode(value[2], validate=True)
        # Inspect dimensions before allocating Qt pixels. fromData uses Qt's
        # own memory device, avoiding both Python QBuffer callbacks and temp
        # file creation for every tiny preview.
        with Image.open(io.BytesIO(blob)) as header:
            valid = header.format in ('JPEG', 'PNG') and header.size == (value[0], value[1])
        if valid:
            image = QtGui.QImage.fromData(blob)
            if not image.isNull() and image.size() == QtCore.QSize(value[0], value[1]):
                from prism.thumbnail_cache import make_preview
                image = make_preview(image, max_side)
                return image, QtCore.QSize(*original_size)
    except (ValueError, TypeError, OSError):
        pass
    return decode_display(*snapshot, max_side=max_side)


def source_snapshot(item):
    archive = item._prism_source
    if archive and item.filename:
        archive = (*archive[:2], item.filename)
    return item._source_blob, item._source_path, archive


class DisplayDecoder(QtCore.QObject):
    completed = QtCore.pyqtSignal(object, object)

    def __init__(self, app):
        super().__init__(app)
        self.pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix='PrismImageDecode')
        self.closed = False
        self.requests = OrderedDict()
        self.max_pending = 8
        self.waiting_owners = weakref.WeakSet()
        self.ready = deque()
        self.install_timer = QtCore.QTimer(self)
        self.install_timer.setSingleShot(True)
        self.install_timer.setInterval(16)
        self.install_timer.timeout.connect(self._install_ready)
        self.repaint_timer = QtCore.QTimer(self)
        self.repaint_timer.setSingleShot(True)
        self.repaint_timer.setInterval(16)
        self.repaint_timer.timeout.connect(self._repaint_waiting)
        self.completed.connect(self._queue_complete, QtCore.Qt.ConnectionType.QueuedConnection)
        app.aboutToQuit.connect(self.shutdown)

    def _repaint_waiting(self):
        owners = list(self.waiting_owners)
        self.waiting_owners.clear()
        for owner in owners:
            if not is_deleted(owner) and owner.isVisible():
                refresh = getattr(owner, 'request_display_refresh', None)
                if refresh is not None:
                    owner.queue_visible_details()
                    refresh()
                    continue
                renderer = getattr(owner, '_overview_renderer', None)
                if renderer is not None:
                    renderer.queue_visible_details()
                target = owner.viewport() if hasattr(owner, 'viewport') else owner
                target.update()

    def shutdown(self):
        self.closed = True
        self.repaint_timer.stop()
        self.install_timer.stop()
        self.ready.clear()
        self.waiting_owners.clear()
        for token, future in self.requests.values():
            future.cancel()
            item = token[0]()
            if (item is not None and not is_deleted(item)
                    and item._display_decode_request is token):
                item._display_decode_request = None
        self.requests.clear()
        self.pool.shutdown(wait=False, cancel_futures=True)

    @staticmethod
    def _needed(token):
        owner = token[4]() if token[4] else None
        item = token[0]()
        if token[4] is None:
            return True
        return (owner is not None and not is_deleted(owner) and item is not None
                and not is_deleted(item) and owner.isVisible() and owner.needs_display(item))

    def _cancel(self, key):
        token, future = self.requests[key]
        if future.cancel():
            self.requests.pop(key, None)
            item = token[0]()
            if item is not None and not is_deleted(item) and item._display_decode_request is token:
                item._display_decode_request = None
            return True
        return False

    def request(self, item, max_side=None, owner=None):
        item = logical_item(item)
        if self.closed:
            return
        if getattr(item, '_display_decode_request', None) is not None:
            if owner is not None:
                self.waiting_owners.add(owner)
            return
        token = (weakref.ref(item), weakref.ref(item.scene()), source_snapshot(item), max_side,
                 weakref.ref(owner) if owner is not None else None)
        if not self._needed(token):
            return
        for key, (pending, _) in list(self.requests.items()):
            if not self._needed(pending):
                self._cancel(key)
        if len(self.requests) >= self.max_pending:
            if owner is not None:
                self.waiting_owners.add(owner)
            priority = getattr(owner, 'display_priority', None)
            queued = [(key, pending[0]()) for key, (pending, future) in self.requests.items()
                      if pending[4] is not None and pending[4]() is owner
                      and not future.running() and not future.done()]
            # Running decodes finish normally; replace only work not started yet.
            if priority is None or not queued:
                return
            key, farthest = max(queued, key=lambda pair: priority(pair[1]))
            if priority(item) >= priority(farthest) or not self._cancel(key):
                return
        item._display_decode_request = token
        from prism.image_pyramid import decode_display
        thumbnail = getattr(item, '_display_thumbnail', None)
        size = item._image_size
        if (max_side is not None and max_side <= 192 and isinstance(thumbnail, list)
                and len(thumbnail) == 3 and type(thumbnail[0]) is int and type(thumbnail[1]) is int
                and 0 < thumbnail[0] <= 192 and 0 < thumbnail[1] <= 192
                and max(thumbnail[:2]) >= min(max_side, max(size.width(), size.height()))
                and isinstance(thumbnail[2], str) and len(thumbnail[2]) <= 220000):
            future = self.pool.submit(decode_thumbnail, tuple(thumbnail), (size.width(), size.height()), token[2], max_side)
        else:
            future = self.pool.submit(decode_display, *token[2], max_side=max_side)
        self.requests[id(token)] = (token, future)
        if owner is not None and len(self.requests) >= self.max_pending:
            self.waiting_owners.add(owner)
        future.add_done_callback(lambda done: self._deliver(token, done))

    def _deliver(self, token, future):
        if self.closed or future.cancelled():
            return
        try:
            result = future.result()
        except Exception:
            logger.debug('Could not decode display image', exc_info=True)
            result = None
        try:
            self.completed.emit(token, result)
        except RuntimeError:
            pass  # QApplication has already destroyed this dispatcher.

    @QtCore.pyqtSlot(object, object)
    def _queue_complete(self, token, result):
        if self.closed:
            return
        self.ready.append((token, result))
        if not self.install_timer.isActive():
            self.install_timer.start(0 if token[3] is not None and token[3] <= 192 else 16)

    def _install_ready(self):
        started = time.perf_counter()
        while self.ready:
            self._complete(*self.ready.popleft())
            if (time.perf_counter()-started)*1000 >= 4:
                break
        if self.ready and not self.closed:
            self.install_timer.start(0)
        if self.waiting_owners and not self.closed:
            self.repaint_timer.stop()
            self._repaint_waiting()

    @QtCore.pyqtSlot(object, object)
    def _complete(self, token, result):
        self.requests.pop(id(token), None)
        if self.waiting_owners and not self.closed and not self.repaint_timer.isActive():
            self.repaint_timer.start()
        for key, (pending, _) in list(self.requests.items()):
            if not self._needed(pending):
                self._cancel(key)
        item, scene = token[0](), token[1]()
        if item is None or is_deleted(item):
            return
        if getattr(item, '_display_decode_request', None) is not token:
            return
        item._display_decode_request = None
        if not self._needed(token):
            return
        if scene is None or is_deleted(scene) or item.scene() is not scene:
            return
        if source_snapshot(item) != token[2]:
            item.update()
            return
        if result is None:
            item._display_decode_failed = True
            item._display_decode_failed_source = token[2]
            return
        image, size = result
        item._grayscale_cache = None
        if item._image_size != size:
            item.prepareGeometryChange()
            item._image_size = size
            if item.crop.isEmpty():
                item.reset_crop()
        if token[3] is not None and token[3] > 192:
            from prism.lod import cache
            display = QtGui.QPixmap.fromImage(image)
            if item._preview_pixmap is None:
                item._preview_pixmap = display.scaled(192, 192, QtCore.Qt.AspectRatioMode.KeepAspectRatio,
                                                      QtCore.Qt.TransformationMode.SmoothTransformation)
            cache().store(item, token[3], display, notify=False)
        elif token[3] is not None:
            item._preview_pixmap = QtGui.QPixmap.fromImage(image)
        else:
            target = item.graphics_item() if is_record(item) else item
            QtWidgets.QGraphicsPixmapItem.setPixmap(target, QtGui.QPixmap.fromImage(image))
            from prism.items import _schedule_budget_check
            _schedule_budget_check(scene)
        item.update()
        from prism.lod import cache
        cache().track_preview(item, notify=False)
        cache().changed.emit(item)


def request_display(item, max_side=None, owner=None):
    item = logical_item(item)
    app = QtWidgets.QApplication.instance()
    if getattr(item, '_display_decode_failed', False):
        if getattr(item, '_display_decode_failed_source', None) == source_snapshot(item):
            return
        item._display_decode_failed = False
    if app is None or item.scene() is None:
        return
    dispatcher = getattr(app, '_prism_display_decoder', None)
    if dispatcher is None:
        dispatcher = app._prism_display_decoder = DisplayDecoder(app)
    dispatcher.request(item, max_side, owner)
