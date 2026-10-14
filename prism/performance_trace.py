"""Low-overhead interaction trace and GUI stall diagnostics (local JSONL)."""
from collections import Counter, deque
from functools import wraps
from logging.handlers import RotatingFileHandler
import json
import itertools
import os
from pathlib import Path
import queue
import sys
import threading
import time
import traceback
import uuid
from PyQt6 import QtCore, QtGui, QtWidgets

_active = None


def record(kind, **fields):
    trace = _active
    if trace is not None:
        trace.record(kind, **fields)


def measured(name, minimum_ms=0):
    def decorate(function):
        @wraps(function)
        def run(*args, **kwargs):
            if _active is None:
                return function(*args, **kwargs)
            origin_action = _active.last_action
            started = time.perf_counter()
            failed = False
            try:
                return function(*args, **kwargs)
            except BaseException:
                failed = True
                raise
            finally:
                elapsed = (time.perf_counter()-started)*1000
                if failed or elapsed >= minimum_ms:
                    record('operation', name=name, duration_ms=round(elapsed, 3), failed=failed,
                           thread=threading.current_thread().name, origin_action=origin_action)
        return run
    return decorate


class PerformanceTrace(QtCore.QObject):
    INTERVAL = .1
    MAX_EVENT_BYTES = 32*1024

    def __init__(self, app, window, directory):
        super().__init__(app)
        self.window = window
        self.session = uuid.uuid4().hex[:12]
        self.path = Path(directory)/f'Prism-operations-{os.getpid()}.jsonl'
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.events = queue.Queue(maxsize=512)
        self.dropped = 0
        self.sequence = itertools.count(1)
        self.last_action = None
        self.recent = deque(maxlen=128)
        self.last_heartbeat = time.perf_counter()
        self.gui_thread = threading.get_ident()
        self.stopped = threading.Event()
        self.actions = set()
        self.presses = {}
        self.counts = Counter()
        self.max_dispatch = 0
        self.writer = threading.Thread(target=self._write, daemon=True, name='PrismTraceWriter')
        self.writer.start()
        self.watchdog = threading.Thread(target=self._watch, daemon=True, name='PrismGuiWatchdog')
        self.watchdog.start()
        self.timer = QtCore.QTimer(self)
        self.timer.setInterval(100)
        self.timer.timeout.connect(self._heartbeat)
        self.timer.start()
        self.summary_time = time.perf_counter()
        self.hook_actions(window)
        app.installEventFilter(self)
        app.aboutToQuit.connect(self.close)
        self.record('session_start', log=str(self.path), pid=os.getpid())

    def record(self, kind, **fields):
        if self.stopped.is_set():
            return
        value = dict(event=kind, sequence=next(self.sequence), session=self.session,
                     time=time.time(), monotonic=time.perf_counter(), **fields)
        if kind in ('input', 'action'):
            self.last_action = {k:v for k,v in value.items() if k not in ('session', 'monotonic')}
        payload = json.dumps(value, ensure_ascii=False, default=str).encode('utf-8')
        if len(payload) > self.MAX_EVENT_BYTES:
            limited = {key:value[key] for key in ('event', 'sequence', 'session', 'time', 'monotonic')}
            limited.update(truncated=True, details=payload[:8000].decode('utf-8', errors='replace'))
            payload = json.dumps(limited, ensure_ascii=False).encode('utf-8')
        self.recent.append(payload)
        try:
            self.events.put_nowait(payload)
        except queue.Full:
            self.dropped += 1

    def crash_report(self, exc_type, exc, tb):
        path = self.path.parent/f'Prism-crash-{os.getpid()}.json'
        report = dict(session=self.session, time=time.time(), error=str(exc),
                      traceback=traceback.format_exception(exc_type, exc, tb),
                      last_action=self.last_action, recent=[json.loads(entry) for entry in list(self.recent)],
                      operation_log=str(self.path))
        try:
            path.write_text(json.dumps(report, ensure_ascii=False, default=str, indent=2), encoding='utf-8')
        except OSError:
            pass
        return path

    def _write(self):
        handler = None
        try:
            # Keep three process logs (plus their rotated parts). Old open
            # files on Windows are left alone rather than interrupting a run.
            bases = sorted(self.path.parent.glob('Prism-operations-*.jsonl'),
                           key=lambda p: p.stat().st_mtime, reverse=True)
            for old in bases[3:]:
                if old != self.path:
                    for part in old.parent.glob(old.name+'*'):
                        try:
                            part.unlink()
                        except OSError:
                            pass
            for pattern in ('Prism-crash-*.json', 'Prism-native-crash-*.log'):
                old_reports = sorted(self.path.parent.glob(pattern), key=lambda p: p.stat().st_mtime, reverse=True)
                for old in old_reports[3:]:
                    try:
                        old.unlink()
                    except OSError:
                        pass
            handler = RotatingFileHandler(self.path, maxBytes=4*1024*1024, backupCount=2, encoding='utf-8')
            while not self.stopped.is_set() or not self.events.empty():
                try:
                    value = self.events.get(timeout=.1)
                except queue.Empty:
                    continue
                handler.stream.write(value.decode('utf-8')+'\n')
                handler.flush()
                if handler.stream.tell() >= handler.maxBytes:
                    handler.doRollover()
        except OSError:
            # A diagnostic failure must not prevent editing or saving.
            self.stopped.set()
        finally:
            if handler is not None:
                handler.close()

    def _watch(self):
        reported = False
        last_report = 0
        while not self.stopped.wait(.25):
            now = time.perf_counter()
            stalled = now-self.last_heartbeat
            if stalled < .5:
                reported = False
            elif not reported or now-last_report >= 5:
                frame = sys._current_frames().get(self.gui_thread)
                self.record('gui_stall', duration_ms=round(stalled*1000, 1),
                            last_action=self.last_action,
                            stack=traceback.format_stack(frame, limit=24) if frame else [])
                reported, last_report = True, now

    def _heartbeat(self):
        now = time.perf_counter()
        gap = now-self.last_heartbeat
        self.last_heartbeat = now
        if gap > .25:
            self.record('gui_recovered', blocked_ms=round((gap-self.INTERVAL)*1000, 1), last_action=self.last_action)
        if now-self.summary_time >= 1:
            view = self.window.view
            scene = view.scene
            decoder = getattr(QtWidgets.QApplication.instance(), '_prism_display_decoder', None)
            manager = scene._viewport_items
            tiles = view._overview_renderer.tiles
            browser = getattr(self.window, 'asset_browser', None)
            canvas = getattr(browser, 'list', None)
            asset_tiles = getattr(canvas, '_tiles', None)
            from prism import lod
            self.record('performance', dispatch_counts=dict(self.counts), max_dispatch_ms=round(self.max_dispatch, 3),
                        assets=scene._item_count, edit_objects=len(manager.materialized),
                        scale=round(view.transform().m11(), 6), tile_bytes=tiles.bytes,
                        tile_coverage_missing=tiles.coverage_missing,
                        tile_pending=len(tiles.pending), decode_pending=len(decoder.requests) if decoder else 0,
                        dropped_events=self.dropped, record_layers=len(manager.layers), image_cache_bytes=lod.cache().bytes,
                        active_surface='assets' if canvas is not None and canvas.isVisible() else 'canvas',
                        asset_tile_bytes=asset_tiles.bytes if asset_tiles else 0,
                        asset_tile_coverage_missing=asset_tiles.coverage_missing if asset_tiles else 0,
                        asset_tile_pending=len(asset_tiles.pending) if asset_tiles else 0,
                        asset_cards_painted=getattr(canvas, '_last_painted_count', 0))
            self.counts.clear()
            self.max_dispatch = 0
            self.summary_time = now

    def dispatch_finished(self, receiver, event_type, milliseconds):
        self.counts[str(event_type)] += 1
        self.max_dispatch = max(self.max_dispatch, milliseconds)
        if milliseconds >= 32:
            self.record('slow_dispatch', target=type(receiver).__name__, event_type=event_type,
                        duration_ms=round(milliseconds, 3), last_action=self.last_action)

    def hook_actions(self, widget):
        for action in widget.findChildren(QtGui.QAction):
            if action not in self.actions:
                self.actions.add(action)
                name = action.objectName() or action.text()
                action.triggered.connect(lambda checked=False, name=name: self.record('action', name=name, checked=checked))

    def eventFilter(self, watched, event):
        kind = event.type()
        types = QtCore.QEvent.Type
        fields = None
        if kind in (types.MouseButtonPress, types.MouseButtonDblClick, types.MouseButtonRelease):
            point = event.position()
            fields = dict(operation=kind.name, x=round(point.x(), 1), y=round(point.y(), 1), button=event.button().value,
                          modifiers=event.modifiers().value)
            key = event.button().value
            if kind == types.MouseButtonPress:
                self.presses[key] = (time.perf_counter(), point.x(), point.y())
            elif kind == types.MouseButtonRelease and key in self.presses:
                started, x, y = self.presses.pop(key)
                fields.update(gesture_ms=round((time.perf_counter()-started)*1000, 2), dx=round(point.x()-x, 1), dy=round(point.y()-y, 1))
        elif kind == types.Wheel:
            fields = dict(operation='wheel', dx=event.angleDelta().x(), dy=event.angleDelta().y(),
                          pixel_dy=event.pixelDelta().y(), modifiers=event.modifiers().value)
        elif kind == types.KeyPress:
            fields = dict(operation='key', key=event.key(), modifiers=event.modifiers().value, repeat=event.isAutoRepeat())
        elif kind == types.Drop:
            fields = dict(operation='drop')
        elif kind == types.ActionAdded:
            self.hook_actions(self.window)
        if fields is not None:
            self.record('input', target=type(watched).__name__, object=watched.objectName(), **fields)
        return False

    def close(self):
        if self.stopped.is_set():
            return
        app = QtWidgets.QApplication.instance()
        if getattr(app, '_performance_trace', None) is self:
            app._performance_trace = None
        global _active
        if _active is self:
            _active = None
        self.timer.stop()
        QtWidgets.QApplication.instance().removeEventFilter(self)
        self.record('session_end', dropped_events=self.dropped)
        self.stopped.set()
        self.writer.join(timeout=1)


def install(app, window, directory):
    global _active
    if _active is None:
        _active = PerformanceTrace(app, window, directory)
        app._performance_trace = _active
    return _active
