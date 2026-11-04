"""Local Babylon PBR viewer, with an explicit update and cached thumbnail."""
import base64
import copy
import json
import time
import uuid
from collections import deque
from dataclasses import dataclass, field
from functools import wraps
from pathlib import Path

from PyQt6 import QtCore, QtGui, QtWidgets
from prism.webengine_runtime import configure_webengine
configure_webengine()
from prism.performance_trace import QueueAccounting, record, capture_context, restore_context
from prism.widgets.material_task import MaterialBatchQueue
from prism.library_observability import event as library_event


@dataclass(eq=False)
class _RenderRequest:
    payload: dict
    token: str
    # Private bridge identity, never the caller's reusable UI/cache token.
    wire_token: str = ''
    phase: str = 'waiting'
    frame_submitted: bool = False
    trace_context: object = field(default_factory=capture_context)

    @property
    def signature(self):
        return self.payload.get('signature', '')


def _request_context(function):
    @wraps(function)
    def bound(self,*args,**kwargs):
        active=self.viewer._active
        context=active.trace_context if active is not None else capture_context()
        with restore_context(context):return function(self,*args,**kwargs)
    return bound


class _RenderBridge(QtCore.QObject):
    def __init__(self, viewer):
        super().__init__(viewer)
        self.viewer = viewer

    @QtCore.pyqtSlot()
    def ready(self):
        self.viewer.ready = True
        self.viewer.web.page().runJavaScript(
            'prismRender.pause(' + str(not self.viewer.isVisible()).lower() + ')')
        self.viewer._push()
        self.viewer.browser_ready.emit()

    @QtCore.pyqtSlot(str, str)
    def model_result(self, request_id, payload):
        try:
            result = json.loads(payload)
            if not isinstance(result, dict):raise ValueError('Invalid scene result')
        except (TypeError, ValueError):
            result = dict(ok=False, error='模型预览返回无效结果')
        self.viewer.scene_result.emit(request_id, result)

    @QtCore.pyqtSlot(str, str)
    def model_event(self, name, payload):
        try:
            fields = json.loads(payload)
            if isinstance(fields, dict):self.viewer.scene_event.emit(name, fields)
        except (TypeError, ValueError):
            return

    @QtCore.pyqtSlot(str)
    def metrics(self, payload):
        self.viewer._last_metrics = time.perf_counter()
        if len(payload) <= 8192:
            try:
                self.viewer._record('material_browser', surface='render',
                                    token=self.viewer.token, metrics=json.loads(payload))
            except (ValueError, TypeError):
                pass

    @QtCore.pyqtSlot(str, str)
    @_request_context
    def loaded(self, token, signature):
        viewer = self.viewer
        request = viewer._match(token, signature, phases=('rendering',))
        if request is None or not request.payload.get('model'):
            return
        viewer._render_deadline.stop()
        request.phase = 'completed'
        viewer._record('material_model_loaded', token=request.token,
                       duration_ms=viewer._elapsed())
        if not viewer._advance_pending(request) and viewer._can_publish(request):
            viewer.model_loaded.emit()

    @QtCore.pyqtSlot(str, str)
    @_request_context
    def frame_submitted(self, token, signature):
        """A scene render was submitted; this is not display presentation."""
        viewer = self.viewer
        request = viewer._match(token, signature, phases=('rendering',))
        if request is None or request.frame_submitted:
            return
        request.frame_submitted = True
        viewer._record('material_frame_submitted', token=request.token,
                       signature=signature, duration_ms=viewer._elapsed())

    @QtCore.pyqtSlot(str, str, str)
    @_request_context
    def rendered(self, token, signature, image):
        viewer = self.viewer
        request = viewer._match(token, signature, phases=('capturing',))
        if request is None or viewer._capture_request is not request:
            return
        try:
            blob = base64.b64decode(image.split(',', 1)[1], validate=True)
            if QtGui.QImage.fromData(blob).isNull():
                raise ValueError('渲染结果不是图片')
        except (ValueError, IndexError):
            viewer._fail(request, '无法读取渲染结果')
            return
        viewer._render_deadline.stop()
        viewer._capture_request = None
        request.phase = 'completed'
        viewer._record('material_capture_done', token=request.token, bytes=len(blob))
        if viewer._can_publish(request):
            viewer.request_completed.emit(request.token, signature, blob)
        if viewer._can_publish(request):
            viewer.completed.emit(signature, blob)

    @QtCore.pyqtSlot(str, str)
    @_request_context
    def updated(self, token, signature):
        viewer = self.viewer
        request = viewer._match(token, signature, phases=('rendering',))
        if request is None or request.payload.get('model'):
            return
        viewer._render_deadline.stop()
        request.phase = 'updated'
        viewer._record('material_render_done', token=request.token,
                       signature=signature, duration_ms=viewer._elapsed())
        if viewer._advance_pending(request):
            return
        if viewer._can_publish(request):
            viewer.request_updated.emit(request.token, signature)
        if viewer._can_publish(request):
            viewer.updated.emit(signature)
        # Both emissions can synchronously load/release/hide this viewer.
        if (viewer._can_publish(request) and request.phase == 'updated'
                and viewer.capture_enabled):
            viewer._capture_request = request
            viewer._capture_timer.start()
            viewer._render_deadline.start()

    @QtCore.pyqtSlot(str, str)
    @_request_context
    def error(self, token, message):
        viewer = self.viewer
        request = viewer._match(token, phases=('rendering', 'updated', 'capturing'))
        if request is not None:
            viewer._fail(request, message)


class MaterialRender(QtWidgets.QWidget):
    browser_ready = QtCore.pyqtSignal()
    scene_result = QtCore.pyqtSignal(str, object)
    scene_event = QtCore.pyqtSignal(str, object)
    completed = QtCore.pyqtSignal(str, bytes)
    failed = QtCore.pyqtSignal(str)
    model_loaded = QtCore.pyqtSignal()
    updated = QtCore.pyqtSignal(str)
    request_updated = QtCore.pyqtSignal(str, str)
    request_completed = QtCore.pyqtSignal(str, str, bytes)
    request_failed = QtCore.pyqtSignal(str, str)

    def __init__(self, parent=None):
        from .widget_construction import finish_construction
        finish_construction(self._construct(parent))

    def _construct(self, parent=None):
        super().__init__(parent)
        self.ready = False
        self.payload = None
        self.token = ''
        self.web = None
        self.viewer_id = uuid.uuid4().hex
        self._active = None
        self._pending = None
        self._capture_request = None
        self._view_slot = 'full'
        self._shape = 'plane'
        self.capture_enabled = True
        self._push_pending = False
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
        yield
        # The Chromium surface is **not** created here.  Creating it costs
        # 236 ms of GUI thread (measured, cold browser process), and this
        # panel is built during the library pre-warm -- right after a project
        # opened, while the user is looking at the canvas.  The inline
        # preview is hidden by default (``MaterialDetails.set_live_preview``
        # puts the static preview on top), so the surface was created and
        # never shown.  The card surface already works this way
        # (``material_cards._ensure_web`` runs from ``showEvent``); the
        # preview does the same now.

    def showEvent(self, event):
        super().showEvent(event)
        self._ensure_web()
        if self.ready:
            self.web.page().runJavaScript('prismRender.pause(false)')
            self._push()

    def hideEvent(self, event):
        # Resume only the latest legal request, with a fresh bridge identity.
        # Release invalidates JS continuations instead of publishing while hidden.
        if self._pending is None and self._active is not None:
            request = self._active
            if request.phase != 'failed':
                self._pending = _RenderRequest(request.payload, request.token)
        self._drop_active()
        self._push_pending = self._pending is not None
        if self.ready:
            self.web.page().runJavaScript('prismRender.pause(true); prismRender.release()')
        super().hideEvent(event)

    def _ensure_web(self):
        if self.web is not None:
            return
        from PyQt6 import QtWebChannel, QtWebEngineCore, QtWebEngineWidgets
        self.web = QtWebEngineWidgets.QWebEngineView(self)
        self.web.renderProcessTerminated.connect(lambda status, code:
            self._record('material_browser_process_exit', surface='render', status=status.name, code=code))
        self.web.settings().setAttribute(QtWebEngineCore.QWebEngineSettings.WebAttribute.WebGLEnabled, True)
        self.web.page().setBackgroundColor(QtGui.QColor('#202023'))
        self.channel = QtWebChannel.QWebChannel(self.web.page())
        self.bridge = _RenderBridge(self)
        self.channel.registerObject('materialRender', self.bridge)
        self.web.page().setWebChannel(self.channel)
        self.layout_box.addWidget(self.web)
        path = Path(__file__).resolve().parents[1] / 'assets/materials/render.html'
        self.web.load(QtCore.QUrl.fromLocalFile(str(path)))

    def _record(self, event, **fields):
        request=self._active
        if request:
            fields.setdefault('request_id',request.wire_token)
            fields.setdefault('material_id',request.payload.get('recordId'))
            fields.setdefault('signature',request.signature)
        with restore_context(request.trace_context if request else capture_context()):
            record(event, viewer_id=self.viewer_id, **fields)

    def _elapsed(self):
        return round((time.perf_counter() - self._load_started) * 1000, 3)

    def load_material(self, payload, token):
        """Keep one renderer execution and one replaceable latest request.

        ``token``/``payload`` describe the active request, not a waiting one.
        Private requestId values protect even identical public tokens/signatures.
        """
        from prism.material_preview_profile import preview_profile
        detached = copy.deepcopy(payload)
        detached['previewProfile'] = preview_profile()
        from prism.material_parameters import resolved_controls, linear_color
        properties = detached.setdefault('properties', {})
        if not properties.get('controls'):
            properties['controls'] = resolved_controls(properties, detached.get('maps', {}))
        else:
            defaults = resolved_controls(properties, detached.get('maps', {}))
            for slot, options in defaults.items():
                control = properties['controls'].setdefault(slot, options)
                if 'linearColor' not in control:
                    space = properties.get('basecolor_color_space', 'srgb') if slot == 'basecolor' else 'srgb'
                    control['linearColor'] = linear_color(control.get('color', options['color']), space)
        self._pending = _RenderRequest(detached, token)
        self._push_pending = True
        self._record('material_render_request', token=token, material=payload.get('recordId'),
                     maps=len(payload.get('maps', {})), ready=self.ready)
        self._capture_timer.stop()
        self._capture_request = None
        if self._active is not None and self._active.phase == 'rendering':
            return  # Do not reset the active deadline during a slider burst.
        self._drop_active()
        self._push()

    def _drop_active(self):
        self._render_deadline.stop()
        self._capture_timer.stop()
        self._capture_request = None
        self._active = None
        self.token = ''
        self.payload = None

    def _push(self):
        if (not self.ready or not self.isVisible() or self._pending is None
                or self._active is not None):
            return
        request, self._pending = self._pending, None
        request.wire_token = uuid.uuid4().hex
        request.phase = 'rendering'
        self._active = request
        self.token, self.payload = request.token, request.payload
        self._push_pending = False
        self._load_started = time.perf_counter()
        self._render_deadline.start()
        self._record('material_render_start', token=request.token,
                     request_id=request.wire_token, signature=request.signature)
        self.web.page().runJavaScript('prismRender.load(' + json.dumps(
            dict(request.payload, token=request.token, requestId=request.wire_token,
                 signature=request.signature, shape=self._shape,
                 view=self._view_slot, capture=False)) + ')')

    def _match(self, wire_token, signature=None, *, phases):
        request = self._active
        if (request is None or not self.isVisible() or not wire_token
                or wire_token != request.wire_token or request.phase not in phases):
            library_event('material','browser_result','stale',viewer_id=self.viewer_id,
                received_request_id=wire_token,expected_request_id=request.wire_token if request else None,
                reason='no_active_request' if request is None else 'hidden' if not self.isVisible()
                    else 'request_mismatch' if wire_token!=request.wire_token else 'phase_mismatch')
            return None
        if signature is not None and signature != request.signature:
            library_event('material','browser_result','stale',viewer_id=self.viewer_id,
                request_id=wire_token,signature=signature,reason='signature_mismatch')
            return None
        return request

    def _can_publish(self, request):
        return self._active is request and self._pending is None and self.isVisible()

    def _advance_pending(self, request):
        if self._active is request and self._pending is not None:
            self._drop_active()
            self._push()
            return True
        return False

    def _capture(self):
        request = self._capture_request
        if (request is not None and self._can_publish(request) and self.ready
                and self.capture_enabled and request.phase == 'updated'):
            request.phase = 'capturing'
            self.web.page().runJavaScript('prismRender.capture(' + ','.join(
                json.dumps(value) for value in
                (request.token, request.wire_token, request.signature)) + ')')

    def _fail(self, request, message):
        if self._active is not request:
            return
        self._render_deadline.stop()
        self._capture_timer.stop()
        self._capture_request = None
        request.phase = 'failed'
        self._record('material_render_error', token=request.token, message=message)
        library_event('material','browser_render','failed',viewer_id=self.viewer_id,
            request_id=request.wire_token,material_id=request.payload.get('recordId'),
            signature=request.signature,reason=message,error_type='BrowserRenderError')
        # Invalidate JS await continuations and queued screenshots before retry.
        if self.ready:
            self.web.page().runJavaScript('prismRender.release()')
        if self._advance_pending(request):
            return
        if self._can_publish(request):
            self.request_failed.emit(request.token, message)
        if self._can_publish(request):
            self.failed.emit(message)

    def release_material(self):
        """Invalidate active/pending/capture without discarding saved cards."""
        if self.payload or self.token or self._pending is not None:
            self._record('material_render_release', token=self.token,
                         material=(self.payload or {}).get('recordId'),
                         maps=len((self.payload or {}).get('maps', {})))
        self._pending = None
        self._push_pending = False
        self._drop_active()
        if self.ready:
            self.web.page().runJavaScript('prismRender.release()')

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
        request = self._active
        if (request is None or not self.isVisible()
                or request.phase not in ('rendering', 'updated', 'capturing')):
            return
        self._record('material_render_timeout', token=request.token,
                     material=request.payload.get('recordId'), duration_ms=self._elapsed(),
                     browser_silent_ms=round((time.perf_counter()-self._last_metrics)*1000))
        self._fail(request, '材质预览超过 35 秒未完成，性能日志已记录渲染请求和浏览器状态。')


class MaterialRenderQueue(QtCore.QObject):
    """Render a queue of materials through one Babylon viewer.

    The viewer owns a single token, so exactly one material is in flight and
    the rest wait in a bounded deque. The queue owns a generation token:
    after ``cancel()`` the pending materials are dropped and the result of
    the material that was in flight is discarded instead of overwriting a
    newer selection.

    The viewer is driven through ``load_material(payload, token)``; results
    come back through the viewer's ``updated`` / ``completed`` / ``failed``
    signals. The queue never rewrites those signals, so a host that
    disconnects them with a bare ``disconnect()`` also severs the queue.
    """

    started = QtCore.pyqtSignal(str, int)
    item_done = QtCore.pyqtSignal(str, str, bytes)
    item_failed = QtCore.pyqtSignal(str, str)
    item_cancelled = QtCore.pyqtSignal(str)
    progress = QtCore.pyqtSignal(int, int)
    finished = QtCore.pyqtSignal(str, bool)

    def __init__(self, viewer, *, parent=None, item_timeout_ms=35000):
        super().__init__(parent)
        self.viewer = viewer
        self.item_timeout_ms = max(0, int(item_timeout_ms))
        self._generation = 0
        self._batch_id = ''
        self._total = 0
        self._pending = deque()
        self._current = None
        self._active = False
        self._held = False
        self._cancelled = False
        self._capture = False
        self._counts = dict(completed=0, failed=0, cancelled=0)
        self._status = {}
        self._results = {}
        self._started_at = 0.0
        # Queue accounting for the batch that is running. One object per batch
        # so a rollup describes exactly that batch; it only counts.
        self._acct = None
        self._advance_timer = QtCore.QTimer(self)
        self._advance_timer.setSingleShot(True)
        self._advance_timer.timeout.connect(self._advance)
        self._deadline = QtCore.QTimer(self)
        self._deadline.setSingleShot(True)
        self._deadline.timeout.connect(self._on_timeout)
        viewer.updated.connect(self._on_updated)
        viewer.completed.connect(self._on_completed)
        viewer.failed.connect(self._on_failed)

    # -- public API ---------------------------------------------------------

    def start(self, items=(), *, batch_id=None, capture=False, total=None, hold=False):
        """Render ``items`` = ``(key, payload)`` pairs, one material at a time.

        ``capture`` asks the viewer for a PNG of every material, so
        ``item_done`` carries the blob; without it only the signature is
        reported. ``hold`` keeps the batch open for later ``enqueue()``
        calls; ``close()`` then ends it.
        """
        self.cancel()
        self._trace_context=capture_context()
        self._generation += 1
        self._batch_id = batch_id or uuid.uuid4().hex[:12]
        self._capture = bool(capture)
        self._held = bool(hold)
        self._pending = deque()
        self._current = None
        self._active = True
        self._cancelled = False
        self._counts = dict(completed=0, failed=0, cancelled=0)
        self._status = {}
        self._results = {}
        self._started_at = time.perf_counter()
        pairs = list(items)
        self._total = len(pairs) if total is None else int(total)
        for key, payload in pairs:
            self._pending.append((key, payload, time.monotonic()))
            self._status[key] = 'pending'
        self._acct = QueueAccounting('material_texture')
        if pairs:
            self._acct.submitted(len(pairs))
        record('material_render_batch_submit', batch=self._batch_id,
               total=self._total, capture=self._capture)
        self.started.emit(self._batch_id, self._total)
        self._schedule_advance()
        return self._batch_id

    def enqueue(self, key, payload):
        """Add one prepared payload to the running batch."""
        if not self._active:
            return False
        self._pending.append((key, payload, time.monotonic()))
        self._status[key] = 'pending'
        if self._acct is not None:
            self._acct.submitted(1)
        self._schedule_advance()
        return True

    def close(self):
        """End a held batch once no more material will be enqueued."""
        if not self._active:
            return False
        self._held = False
        self._schedule_advance()
        return True

    def cancel(self):
        """Cancel the batch; the in-flight render is released and dropped."""
        if not self._active:
            return False
        self._cancelled = True
        self._generation += 1
        self._held = False
        self._advance_timer.stop()
        self._deadline.stop()
        acct = self._acct
        # Items still outstanding when the cancel landed: the in-flight render
        # cannot be interrupted, the waiting ones are dropped.
        outstanding = len(self._pending) + (1 if self._current is not None else 0)
        if self._current is not None:
            key, _token, _began = self._current
            self._current = None
            self._counts['cancelled'] += 1
            self._status[key] = 'cancelled'
            if acct is not None:
                acct.cancelled(state='running', remaining=outstanding,
                               reason='batch_cancelled')
            self.item_cancelled.emit(key)
        while self._pending:
            key, _payload, _queued = self._pending.popleft()
            self._counts['cancelled'] += 1
            self._status[key] = 'cancelled'
            if acct is not None:
                acct.cancelled(state='pending', reason='batch_cancelled')
            self.item_cancelled.emit(key)
        if self.viewer is not None:
            self.viewer.release_material()
        self._report()
        self._finish()
        return True

    def cancel_item(self, key):
        """Cancel one material; the in-flight one is released and dropped."""
        if not self._active:
            return False
        acct = self._acct
        if self._current is not None and self._current[0] == key:
            self._deadline.stop()
            self._current = None
            self._counts['cancelled'] += 1
            self._status[key] = 'cancelled'
            if acct is not None:
                acct.cancelled(state='running',
                               remaining=len(self._pending) + 1,
                               reason='item_cancelled')
            self.item_cancelled.emit(key)
            if self.viewer is not None:
                self.viewer.release_material()
            self._report()
            self._schedule_advance()
            return True
        for index, (pending_key, _payload, _queued) in enumerate(self._pending):
            if pending_key != key:
                continue
            del self._pending[index]
            self._counts['cancelled'] += 1
            self._status[key] = 'cancelled'
            if acct is not None:
                acct.cancelled(state='pending', reason='item_cancelled')
            self.item_cancelled.emit(key)
            self._report()
            return True
        return False

    def snapshot(self):
        """Read-only view of the render queue state; baseline scripts poll this."""
        settled = self._counts['completed'] + self._counts['failed'] + self._counts['cancelled']
        return {'batch': self._batch_id, 'active': self._active, 'cancelled': self._cancelled,
                'generation': self._generation, 'total': self._total,
                'queued': len(self._pending), 'running': 1 if self._current is not None else 0,
                'completed': self._counts['completed'], 'failed': self._counts['failed'],
                'cancelled': self._counts['cancelled'], 'settled': settled,
                'elapsed_ms': round((time.perf_counter()-self._started_at)*1000, 3)
                if self._started_at else 0.0,
                'capture': self._capture, 'held': self._held,
                'items': dict(self._status)}

    def results(self):
        """``{key: (signature, blob)}`` of the materials that rendered."""
        return dict(self._results)

    def pending_keys(self):
        """Keys that have not settled yet."""
        return [key for key, status in self._status.items() if status in ('pending', 'running')]

    def generation(self):
        """The current generation token; callers compare it to detect staleness."""
        return self._generation

    # -- internals ----------------------------------------------------------

    def _schedule_advance(self):
        if not self._active:
            return
        self._advance_timer.start(0)

    def _advance(self):
        if not self._active or self._current is not None:
            return
        if self._cancelled:
            self._finish()
            return
        if not self._pending:
            if self._held:
                return  # more material may still be enqueued
            self._finish()
            return
        key, payload, queued_at = self._pending.popleft()
        token = uuid.uuid4().hex
        began = time.monotonic()
        self._current = (key, token, began)
        self._status[key] = 'running'
        if self._acct is not None:
            self._acct.started(wait_ms=(began - queued_at) * 1000.0)
        if self.item_timeout_ms:
            self._deadline.start(self.item_timeout_ms)
        try:
            with restore_context(self._trace_context):self.viewer.load_material(payload, token)
        except Exception as error:
            self._deadline.stop()
            self._current = None
            self._counts['failed'] += 1
            self._status[key] = 'failed'
            if self._acct is not None:
                self._acct.service_done(service_ms=(time.monotonic() - began) * 1000.0)
                self._acct.failed(reason='render_start_failed', state='ready')
            self.item_failed.emit(key, str(error))
            self._report()
            self._schedule_advance()

    def _on_updated(self, signature):
        if not self._active or self._current is None:
            return
        key, token, _began = self._current
        if getattr(self.viewer, 'token', '') != token:
            return
        if not self._capture:
            self._deadline.stop()
            self._complete(key, signature, b'')
            return
        # The blob arrives through completed() after the viewer's own capture
        # delay. Keep the deadline running so a lost capture fails the item
        # instead of hanging the batch forever.
        if self.item_timeout_ms:
            self._deadline.start(self.item_timeout_ms)

    def _on_completed(self, signature, blob):
        if not self._active or self._current is None:
            return
        key, token, _began = self._current
        if getattr(self.viewer, 'token', '') != token:
            return
        self._deadline.stop()
        self._complete(key, signature, blob)

    def _on_failed(self, message):
        if not self._active or self._current is None:
            return
        key, _token, began = self._current
        self._deadline.stop()
        self._current = None
        self._counts['failed'] += 1
        self._status[key] = 'failed'
        if self._acct is not None:
            self._acct.service_done(service_ms=(time.monotonic() - began) * 1000.0)
            self._acct.failed(reason='render_failed', state='ready')
        self.item_failed.emit(key, message)
        self._report()
        self._schedule_advance()

    def _on_timeout(self):
        if not self._active or self._current is None:
            return
        key, _token, _began = self._current
        self._current = None
        self._counts['failed'] += 1
        self._status[key] = 'failed'
        if self._acct is not None:
            # The render never came back: the slot is abandoned, so no service
            # duration is claimed.
            self._acct.service_done()
            self._acct.failed(reason='timeout', state='ready')
        self.item_failed.emit(key, '材质预览超时（未收到渲染完成回调）')
        self._report()
        self._schedule_advance()

    def _complete(self, key, signature, blob):
        current = self._current
        began = current[2] if current is not None else None
        self._current = None
        self._counts['completed'] += 1
        self._status[key] = 'completed'
        self._results[key] = (signature, blob)
        acct = self._acct
        if acct is not None:
            if began is not None:
                acct.service_done(service_ms=(time.monotonic() - began) * 1000.0)
            else:
                acct.service_done()
        self.item_done.emit(key, signature, blob)
        if acct is not None:
            acct.installed()
        self._report()
        self._schedule_advance()

    def _report(self):
        settled = self._counts['completed'] + self._counts['failed'] + self._counts['cancelled']
        record('material_render_batch_progress', batch=self._batch_id, settled=settled,
               total=self._total, pending=len(self._pending),
               running=1 if self._current is not None else 0,
               completed=self._counts['completed'], failed=self._counts['failed'],
               cancelled=self._counts['cancelled'],
               elapsed_ms=round((time.perf_counter()-self._started_at)*1000, 3))
        self.progress.emit(settled, self._total)
        if self._acct is not None:
            self._acct.rollup(batch=self._batch_id, phase='progress')

    def _finish(self):
        if not self._active:
            return
        self._active = False
        record('material_render_batch_finished', batch=self._batch_id,
               cancelled=self._cancelled,
               elapsed_ms=round((time.perf_counter()-self._started_at)*1000, 3),
               completed=self._counts['completed'], failed=self._counts['failed'],
               cancelled_count=self._counts['cancelled'])
        if self._acct is not None:
            self._acct.rollup(force=True, reason='batch_finished', batch=self._batch_id)
        self.finished.emit(self._batch_id, self._cancelled)


class MaterialPipeline(QtCore.QObject):
    """Prepare and render materials as one cancelable, sharded job.

    Stage one prepares render inputs on worker threads in slices of
    ``chunk_size`` items; stage two renders the prepared payloads one at a
    time through the Babylon viewer. ``cancel()`` stops both stages and no
    result of a cancelled batch is published.
    """

    started = QtCore.pyqtSignal(str, int)
    item_done = QtCore.pyqtSignal(str, str, bytes)
    item_failed = QtCore.pyqtSignal(str, str)
    item_cancelled = QtCore.pyqtSignal(str)
    progress = QtCore.pyqtSignal(int, int)
    finished = QtCore.pyqtSignal(str, bool)

    def __init__(self, viewer, *, prepare=None, chunk_size=4, slice_budget_ms=4000,
                 max_workers=2, capture=False, item_timeout_ms=35000, parent=None):
        super().__init__(parent)
        self._prepare = prepare
        self._capture = bool(capture)
        self._batch_id = ''
        self._total = 0
        self._counts = dict(completed=0, failed=0, cancelled=0)
        self._started_at = 0.0
        self._active = False
        self._finished = False
        self._prepare_done = True
        self._render_done = True
        self._cancelled = False
        self.prepare_queue = MaterialBatchQueue(self, chunk_size=chunk_size,
                                                slice_budget_ms=slice_budget_ms,
                                                max_workers=max_workers)
        self.render_queue = MaterialRenderQueue(viewer, parent=self,
                                                item_timeout_ms=item_timeout_ms)
        self.prepare_queue.item_done.connect(self._on_prepared)
        self.prepare_queue.item_failed.connect(self._on_prepare_failed)
        self.prepare_queue.item_cancelled.connect(self._on_cancelled)
        self.prepare_queue.finished.connect(self._on_prepare_finished)
        self.render_queue.item_done.connect(self._on_render_done)
        self.render_queue.item_failed.connect(self._on_render_failed)
        self.render_queue.item_cancelled.connect(self._on_cancelled)
        self.render_queue.finished.connect(self._on_render_finished)

    # -- public API ---------------------------------------------------------

    def start(self, items, *, batch_id=None):
        """Run ``items`` = ``(key, record)`` pairs through prepare and render.

        With a ``prepare`` callable the second element is the material
        record; without one it is already a render payload.
        """
        self.cancel()
        pairs = list(items)
        self._batch_id = batch_id or uuid.uuid4().hex[:12]
        self._total = len(pairs)
        self._counts = dict(completed=0, failed=0, cancelled=0)
        self._started_at = time.perf_counter()
        self._active = True
        self._finished = False
        self._cancelled = False
        self._prepare_done = self._prepare is None
        self._render_done = False
        self.started.emit(self._batch_id, self._total)
        if self._prepare is None:
            self.render_queue.start(pairs, batch_id=self._batch_id,
                                    capture=self._capture, total=self._total)
        else:
            self.render_queue.start((), batch_id=self._batch_id, capture=self._capture,
                                    total=self._total, hold=True)
            self.prepare_queue.submit(pairs, self._prepare_work,
                                      batch_id=self._batch_id + '-prepare',
                                      key=lambda pair: pair[0])
        return self._batch_id

    def cancel(self):
        """Cancel both stages; nothing of this batch is published afterwards."""
        if not self._active:
            return False
        self._cancelled = True
        self.render_queue.cancel()
        self.prepare_queue.cancel()
        self._active = False
        self._maybe_finish()
        return True

    def cancel_item(self, key):
        """Cancel one material in whichever stage currently holds it."""
        return (self.prepare_queue.cancel_item(key)
                or self.render_queue.cancel_item(key))

    def snapshot(self):
        """Read-only view of the whole pipeline; baseline scripts poll this."""
        settled = self._counts['completed'] + self._counts['failed'] + self._counts['cancelled']
        return {'batch': self._batch_id, 'active': self._active, 'cancelled': self._cancelled,
                'generation': self.prepare_queue.generation(),
                'total': self._total, 'queued': self.render_queue.snapshot()['queued'],
                'running': self.render_queue.snapshot()['running'],
                'completed': self._counts['completed'], 'failed': self._counts['failed'],
                'cancelled': self._counts['cancelled'], 'settled': settled,
                'elapsed_ms': round((time.perf_counter()-self._started_at)*1000, 3)
                if self._started_at else 0.0,
                'prepare': self.prepare_queue.snapshot(),
                'render': self.render_queue.snapshot()}

    def results(self):
        """``{key: (signature, blob)}`` of the materials that rendered."""
        return self.render_queue.results()

    def pending_keys(self):
        """Keys that have not settled yet in either stage."""
        return sorted(set(self.prepare_queue.pending_keys())
                      | set(self.render_queue.pending_keys()))

    # -- internals ----------------------------------------------------------

    def _prepare_work(self, pair):
        return self._prepare(pair[1])

    def _on_prepared(self, key, payload):
        self.render_queue.enqueue(key, payload)

    def _on_render_done(self, key, signature, blob):
        self._counts['completed'] += 1
        self.item_done.emit(key, signature, blob)
        self._report()

    def _on_render_failed(self, key, message):
        self._counts['failed'] += 1
        self.item_failed.emit(key, message)
        self._report()

    def _on_prepare_failed(self, key, message):
        self._counts['failed'] += 1
        self.item_failed.emit(key, message)
        self._report()

    def _on_cancelled(self, key):
        self._counts['cancelled'] += 1
        self.item_cancelled.emit(key)
        self._report()

    def _on_prepare_finished(self, _batch_id, cancelled):
        self._prepare_done = True
        if cancelled:
            self._cancelled = True
        self.render_queue.close()
        self._maybe_finish()

    def _on_render_finished(self, _batch_id, cancelled):
        self._render_done = True
        if cancelled:
            self._cancelled = True
        self._maybe_finish()

    def _maybe_finish(self):
        if self._finished or not (self._prepare_done and self._render_done):
            return
        self._finished = True
        self._active = False
        record('material_pipeline_finished', batch=self._batch_id, cancelled=self._cancelled,
               elapsed_ms=round((time.perf_counter()-self._started_at)*1000, 3),
               completed=self._counts['completed'], failed=self._counts['failed'],
               cancelled_count=self._counts['cancelled'])
        self.finished.emit(self._batch_id, self._cancelled)

    def _report(self):
        settled = self._counts['completed'] + self._counts['failed'] + self._counts['cancelled']
        self.progress.emit(settled, self._total)

