"""Read images through native Qt devices and deliver display pixels off-thread.

PyQt6 6.7 releases the GIL in QImageReader.read(), but not size(). A Python
QBuffer can reacquire it inside Qt's image-handler mutex. Passing a filename
lets Qt own its QFile and avoids that lock inversion.
"""
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from pathlib import Path
import logging
import sqlite3
import tempfile
import weakref

from PyQt6 import QtCore, QtGui, QtWidgets, sip

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


def decode(blob=None, path=None, archive=None, max_side=None):
    """Return QImage and original dimensions; never construct a QPixmap."""
    if blob is None and not path and archive:
        filename, item_id = archive
        connection = sqlite3.connect(Path(filename).resolve().as_uri() + '?mode=ro', uri=True)
        try:
            row = connection.execute('SELECT data FROM sqlar WHERE item_id = ?', (item_id,)).fetchone()
            blob = row[0] if row else None
        finally:
            connection.close()
    if blob is None and not path:
        return None
    with native_reader(blob=blob, path=path) as reader:
        size = reader.size()
        rotated = bool(reader.transformation().value & 4)
        if max_side and size.isValid() and max(size.width(), size.height()) > max_side:
            reader.setScaledSize(size.scaled(max_side, max_side,
                QtCore.Qt.AspectRatioMode.KeepAspectRatio))
        image = reader.read()
        if image.isNull():
            return None
        if not size.isValid() or size.isEmpty():
            size = image.size()
        elif rotated:
            size.transpose()
        if max_side and max(image.width(), image.height()) > max_side:
            image = image.scaled(max_side, max_side,
                QtCore.Qt.AspectRatioMode.KeepAspectRatio,
                QtCore.Qt.TransformationMode.SmoothTransformation)
        return image, size


def source_snapshot(item):
    return item._source_blob, item._source_path, item._prism_source


class DisplayDecoder(QtCore.QObject):
    completed = QtCore.pyqtSignal(object, object)

    def __init__(self, app):
        super().__init__(app)
        self.pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix='PrismImageDecode')
        self.closed = False
        self.completed.connect(self._complete, QtCore.Qt.ConnectionType.QueuedConnection)
        app.aboutToQuit.connect(self.shutdown)

    def shutdown(self):
        self.closed = True
        self.pool.shutdown(wait=False, cancel_futures=True)

    def request(self, item, max_side=None):
        if self.closed or getattr(item, '_display_decode_request', None) is not None:
            return
        token = (weakref.ref(item), weakref.ref(item.scene()), source_snapshot(item), max_side)
        item._display_decode_request = token
        future = self.pool.submit(decode, *token[2], max_side=max_side)
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
    def _complete(self, token, result):
        item, scene = token[0](), token[1]()
        if item is None or sip.isdeleted(item):
            return
        if getattr(item, '_display_decode_request', None) is not token:
            return
        item._display_decode_request = None
        if scene is None or sip.isdeleted(scene) or item.scene() is not scene:
            return
        if source_snapshot(item) != token[2]:
            item.update()
            return
        if result is None:
            item._display_decode_failed = True
            return
        image, size = result
        item._grayscale_cache = None
        if item._image_size != size:
            item.prepareGeometryChange()
            item._image_size = size
            if item.crop.isEmpty():
                item.reset_crop()
        if token[3] is not None:
            item._preview_pixmap = QtGui.QPixmap.fromImage(image)
        else:
            QtWidgets.QGraphicsPixmapItem.setPixmap(item, QtGui.QPixmap.fromImage(image))
            from prism.items import _schedule_budget_check
            _schedule_budget_check(scene)
        item.update()


def request_display(item, max_side=None):
    app = QtWidgets.QApplication.instance()
    if app is None or item.scene() is None or getattr(item, '_display_decode_failed', False):
        return
    dispatcher = getattr(app, '_prism_display_decoder', None)
    if dispatcher is None:
        dispatcher = app._prism_display_decoder = DisplayDecoder(app)
    dispatcher.request(item, max_side)
