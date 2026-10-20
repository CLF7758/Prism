"""Local Babylon PBR viewer, with an explicit update and cached thumbnail."""
import base64
import json
import time
from pathlib import Path

from PyQt6 import QtCore, QtGui, QtWidgets
from prism.webengine_runtime import configure_webengine
configure_webengine()
from prism.performance_trace import record


class _RenderBridge(QtCore.QObject):
    def __init__(self, viewer):
        super().__init__(viewer)
        self.viewer = viewer

    @QtCore.pyqtSlot()
    def ready(self):
        self.viewer.ready = True
        self.viewer._push()

    @QtCore.pyqtSlot(str)
    def metrics(self, payload):
        self.viewer._last_metrics = time.perf_counter()
        if len(payload) <= 8192:
            try:
                record('material_browser', surface='render', token=self.viewer.token, metrics=json.loads(payload))
            except (ValueError, TypeError):
                pass

    @QtCore.pyqtSlot(str)
    def loaded(self, token):
        if token == self.viewer.token:
            self.viewer._render_deadline.stop()
            record('material_model_loaded', token=token,
                   duration_ms=round((time.perf_counter()-self.viewer._load_started)*1000, 3))
            self.viewer.model_loaded.emit()

    @QtCore.pyqtSlot(str, str, str)
    def rendered(self, token, signature, image):
        if token != self.viewer.token:
            return
        try:
            blob = base64.b64decode(image.split(',', 1)[1], validate=True)
            if QtGui.QImage.fromData(blob).isNull():
                raise ValueError('渲染结果不是图片')
        except (ValueError, IndexError):
            self.viewer.failed.emit('无法读取渲染结果')
            return
        self.viewer.completed.emit(signature, blob)
        record('material_capture_done', token=token, bytes=len(blob))

    @QtCore.pyqtSlot(str, str)
    def updated(self, token, signature):
        if token == self.viewer.token:
            self.viewer._render_deadline.stop()
            record('material_render_done', token=token,
                   duration_ms=round((time.perf_counter()-self.viewer._load_started)*1000, 3))
            self.viewer.updated.emit(signature)
            if self.viewer.capture_enabled:
                self.viewer._capture_timer.start()

    @QtCore.pyqtSlot(str, str)
    def error(self, token, message):
        if token == self.viewer.token:
            self.viewer._render_deadline.stop()
            record('material_render_error', token=token, message=message)
            self.viewer.failed.emit(message)


class MaterialRender(QtWidgets.QWidget):
    completed = QtCore.pyqtSignal(str, bytes)
    failed = QtCore.pyqtSignal(str)
    model_loaded = QtCore.pyqtSignal()
    updated = QtCore.pyqtSignal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.ready = False
        self.payload = None
        self.token = ''
        self.web = None
        self._view_slot = 'full'
        self._shape = 'plane'
        self.capture_enabled = True
        self._load_started = time.perf_counter()
        self._last_metrics = time.perf_counter()
        self._render_deadline = QtCore.QTimer(self)
        self._render_deadline.setSingleShot(True)
        self._render_deadline.setInterval(35000)
        self._render_deadline.timeout.connect(self._render_timeout)
        self._capture_timer = QtCore.QTimer(self)
        self._capture_timer.setSingleShot(True)
        self._capture_timer.setInterval(350)
        self._capture_timer.timeout.connect(self._capture)
        self.layout_box = QtWidgets.QVBoxLayout(self)
        self.layout_box.setContentsMargins(0, 0, 0, 0)
        self._ensure_web()

    def showEvent(self, event):
        super().showEvent(event)
        self._ensure_web()
        if self.ready:
            self.web.page().runJavaScript('prismRender.pause(false)')

    def hideEvent(self, event):
        self._render_deadline.stop()
        if self.ready:
            self.web.page().runJavaScript('prismRender.pause(true)')
        super().hideEvent(event)

    def _ensure_web(self):
        if self.web is not None:
            return
        from PyQt6 import QtWebChannel, QtWebEngineCore, QtWebEngineWidgets
        self.web = QtWebEngineWidgets.QWebEngineView(self)
        self.web.renderProcessTerminated.connect(lambda status, code:
            record('material_browser_process_exit', surface='render', status=status.name, code=code))
        self.web.settings().setAttribute(QtWebEngineCore.QWebEngineSettings.WebAttribute.WebGLEnabled, True)
        self.web.page().setBackgroundColor(QtGui.QColor('#202023'))
        self.channel = QtWebChannel.QWebChannel(self.web.page())
        self.bridge = _RenderBridge(self)
        self.channel.registerObject('materialRender', self.bridge)
        self.web.page().setWebChannel(self.channel)
        self.layout_box.addWidget(self.web)
        path = Path(__file__).resolve().parents[1] / 'assets/materials/render.html'
        self.web.load(QtCore.QUrl.fromLocalFile(str(path)))

    def load_material(self, payload, token):
        self._load_started = time.perf_counter()
        self._render_deadline.start()
        record('material_render_request', token=token, material=payload.get('recordId'),
               maps=len(payload.get('maps', {})), ready=self.ready)
        self._capture_timer.stop()
        self.payload = payload
        self.token = token
        self._push()

    def _push(self):
        if self.ready and self.payload:
            self.web.page().runJavaScript('prismRender.load(' + json.dumps(
                dict(self.payload, token=self.token, shape=self._shape, view=self._view_slot, capture=False)) + ')')

    def _capture(self):
        if self.ready:
            self.web.page().runJavaScript('prismRender.capture(' + json.dumps(self.token) + ')')

    def set_shape(self, shape):
        if shape not in ('sphere', 'cylinder', 'box', 'plane'):
            return
        self._shape = shape
        if self.ready:
            self.web.page().runJavaScript('prismRender.shape(' + json.dumps(shape) + ')')

    def view_channel(self, slot):
        self._view_slot = slot
        if self.ready:
            self.web.page().runJavaScript('prismRender.view(' + json.dumps(slot) + ')')

    def reset_camera(self):
        if self.ready:
            self.web.page().runJavaScript('prismRender.front()')

    def _render_timeout(self):
        record('material_render_timeout', token=self.token,
               material=(self.payload or {}).get('recordId'),
               duration_ms=round((time.perf_counter()-self._load_started)*1000),
               browser_silent_ms=round((time.perf_counter()-self._last_metrics)*1000))
        self.failed.emit('材质预览超过 35 秒未完成，性能日志已记录渲染请求和浏览器状态。')
