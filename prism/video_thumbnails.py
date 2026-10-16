"""Bounded opening-frame previews, decoded with the existing PyAV backend."""
from collections import OrderedDict
from pathlib import Path
import time
import weakref
from PyQt6 import QtCore, QtGui, sip

MAX_PENDING = 128
CACHE_BYTES = 32*1024*1024
_service = None


def opening_frame(path):
    from prism.fileio.video_pyav import av, OPEN_OPTIONS, frame_to_qimage
    first = None
    best_score = -1
    started = time.monotonic()
    with av.open(path, options=OPEN_OPTIONS, timeout=(5, 5)) as container:
        stream = next(s for s in container.streams if s.type == 'video')
        stream.codec_context.thread_count = 1
        info = dict(width=stream.codec_context.width, height=stream.codec_context.height)
        info['duration'] = container.duration/av.time_base if container.duration else None
        start_time = None
        for index, frame in enumerate(container.decode(stream)):
            if start_time is None:
                start_time = frame.time or 0
            ratio = min(1, 640/max(frame.width, frame.height))
            small = frame.reformat(width=max(1, round(frame.width*ratio)), height=max(1, round(frame.height*ratio)), format='rgb24')
            sample = frame.reformat(width=64, height=36, format='gray').to_ndarray()
            brightness = float(sample.mean())
            visible_fraction = float((sample > 48).mean())
            score = brightness+visible_fraction*30
            if first is None or score > best_score+1:
                first = frame_to_qimage(small)
                best_score = score
            # Reject the almost-black first instant of a fade-in as well as
            # blank frames. If the entire opening is dark, keep its clearest
            # sampled frame rather than returning the initial black frame.
            if brightness > 18 or visible_fraction > .2:
                return frame_to_qimage(small), info
            if index >= 179 or (frame.time is not None and frame.time-start_time >= 5) or time.monotonic()-started > 8:
                break
    if first is None:
        raise ValueError('No decodable video frame')
    return first, info


class Signals(QtCore.QObject):
    done = QtCore.pyqtSignal(object, object, object)


class Decode(QtCore.QRunnable):
    def __init__(self, key):
        super().__init__()
        self.key = key
        self.signals = Signals()

    def run(self):
        try:
            image, info = opening_frame(self.key[0])
        except Exception as error:
            image, info = None, {'error': type(error).__name__}
        self.signals.done.emit(self.key, image, info)


class Thumbnails(QtCore.QObject):
    def __init__(self, app):
        super().__init__(app)
        self.pool = QtCore.QThreadPool(self)
        self.pool.setMaxThreadCount(2)
        self.waiting = OrderedDict()
        self.running = {}
        self.clients = {}
        self.cache = OrderedDict()
        self.bytes = 0

    def request(self, item):
        url = item._video_url
        if not url or not url.isLocalFile():
            return False
        try:
            path = Path(url.toLocalFile()).resolve()
            stat = path.stat()
        except OSError:
            return False
        key = (str(path), stat.st_size, stat.st_mtime_ns)
        if getattr(item, '_thumbnail_request_key', None) == key:
            return True
        item._thumbnail_request_key = key
        if key in self.cache:
            image, info = self.cache[key]
            self.cache.move_to_end(key)
            self.apply(item, image, info)
            return True
        self.clients.setdefault(key, []).append(weakref.ref(item))
        if key not in self.running:
            self.waiting[key] = None
        while len(self.waiting) > MAX_PENDING:
            oldest, _ = self.waiting.popitem(last=False)
            for ref in self.clients.pop(oldest, []):
                if (client := ref()) is not None and not sip.isdeleted(client):
                    client._thumbnail_request_key = None
        self.pump()
        return True

    def pump(self):
        while self.waiting and len(self.running) < 2:
            key, _ = self.waiting.popitem(last=False)
            task = Decode(key)
            self.running[key] = task
            task.signals.done.connect(self.done)
            self.pool.start(task)

    @QtCore.pyqtSlot(object, object, object)
    def done(self, key, image, info):
        self.running.pop(key, None)
        if image is None:
            from prism.performance_trace import record
            record('video.preview.failed', reason=info.get('error', 'unknown'), fallback='qtmultimedia')
        if image is not None:
            self.cache[key] = (image, info)
            self.bytes += image.sizeInBytes()
            while self.bytes > CACHE_BYTES and self.cache:
                _, (old, _) = self.cache.popitem(last=False)
                self.bytes -= old.sizeInBytes()
        for ref in self.clients.pop(key, []):
            item = ref()
            if item is None or sip.isdeleted(item) or getattr(item, '_thumbnail_request_key', None) != key:
                continue
            if image is not None:
                self.apply(item, image, info)
            else:
                item._capture_thumbnail_qt()
        self.pump()

    @staticmethod
    def apply(item, image, info):
        item.prepareGeometryChange()
        item._video_dimensions = QtCore.QSize(info['width'], info['height'])
        item._video_metadata = info
        item.setPixmap(QtGui.QPixmap.fromImage(image))
        item._thumbnail_ready = True
        if item._control_bar:
            item._control_bar.reposition()
        item.update()
        if item.scene():
            item.scene()._invalidate_view_bounds()
            item.scene().video_preview_changed.emit(item)


def request(item):
    from prism.fileio.video_pyav import available
    if not available():
        return False
    global _service
    app = QtCore.QCoreApplication.instance()
    if _service is None or sip.isdeleted(_service):
        _service = Thumbnails(app)
    return _service.request(item)
