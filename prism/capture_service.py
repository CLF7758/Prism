"""Qt6 process adapter for the optional ScreenPinKit Qt5 component."""
import json
import os
from pathlib import Path
import tempfile
import uuid
import shutil
import time
import threading
from concurrent.futures import ThreadPoolExecutor

from PyQt6 import QtCore, QtGui
from prism import image_decode
from prism.component_runtime import invocation
from prism.performance_trace import record


def capture_directory():
    return Path(os.environ.get('LOCALAPPDATA', Path.home()))/'Prism'/'components'/'capture'


def inbox_directory():
    return capture_directory().parent.parent/'capture-inbox'


INBOX_MAX_ITEMS = 100
INBOX_MAX_BYTES = 512*1024*1024
INBOX_MAX_AGE_S = 7*24*3600
INBOX_CLEANUP_INTERVAL_MS = 15*60*1000
_inbox_lock = threading.RLock()
# Source files in use by this process must survive until its owners close.
# Keeping them past a quota is safer than deleting an unsaved original.
_active_capture_paths = set()


def protect_capture_paths(paths):
    directory = inbox_directory().resolve()
    with _inbox_lock:
        for path in paths:
            candidate = Path(path).resolve()
            if candidate.parent == directory:
                _active_capture_paths.add(str(candidate))


def release_capture_paths(paths):
    with _inbox_lock:
        for path in paths:
            _active_capture_paths.discard(str(Path(path).resolve()))


def cleanup_capture_inbox(*, reserve_items=0, reserve_bytes=0, now=None):
    """Expire unused history; preserve active sources, serialize copy/cleanup."""
    directory = inbox_directory()
    if directory.is_symlink():
        raise OSError('Capture inbox must not be a symbolic link')
    now = time.time() if now is None else now
    removed = released = failed = protected = 0
    with _inbox_lock:
        files = []
        for path in directory.glob('*.png'):
            if path.is_symlink() or not path.is_file():
                continue
            try:
                files.append((path, path.stat()))
            except OSError:
                failed += 1
        total = sum(stat.st_size for _, stat in files)
        count = len(files)
        for path, stat in sorted(files, key=lambda entry: entry[1].st_mtime):
            if str(path.resolve()) in _active_capture_paths:
                protected += 1
                continue
            if (now-stat.st_mtime < INBOX_MAX_AGE_S and count+reserve_items <= INBOX_MAX_ITEMS
                    and total+reserve_bytes <= INBOX_MAX_BYTES):
                continue
            try:
                current = path.stat()
                if (current.st_mtime_ns, current.st_size, current.st_ino) != (
                        stat.st_mtime_ns, stat.st_size, stat.st_ino):
                    continue
                path.unlink()
                removed += 1
                released += stat.st_size
                total -= stat.st_size
                count -= 1
            except OSError:
                failed += 1
        result = dict(removed_files=removed, released_bytes=released, failed_files=failed,
                      protected_files=protected,
                      remaining_files=count, remaining_bytes=total,
                      over_budget=(count+reserve_items > INBOX_MAX_ITEMS
                                   or total+reserve_bytes > INBOX_MAX_BYTES))
    record('capture.inbox.cleanup', **result)
    return result


def retain_capture(path):
    """Keep the newest capture without deleting an active original at quota."""
    directory = inbox_directory()
    directory.mkdir(parents=True, exist_ok=True)
    with _inbox_lock:
        cleanup_capture_inbox(reserve_items=1, reserve_bytes=path.stat().st_size)
        destination = directory/(uuid.uuid4().hex+'.png')
        shutil.copyfile(path, destination)
        _active_capture_paths.add(str(destination.resolve()))
        return str(destination)


def component_command():
    directory = capture_directory()
    try:
        manifest = json.loads((directory/'upstream.json').read_text(encoding='utf-8'))
    except (OSError, ValueError) as error:
        raise OSError('截图组件尚未安装，请先运行 tools/setup_capture_component.py') from error
    python = directory/'runtime'/'Scripts'/'python.exe'
    source = directory/'upstream-source'/'src'
    if not source.is_dir():
        source = Path(manifest['source'])
    if not python.is_file() or not (source/'view'/'screen_shot_window.py').is_file():
        raise OSError('截图组件尚未安装，请先运行 tools/setup_capture_component.py')
    return str(python), str(source)


def prepare_pin(snapshot, crop, flip, gray, opacity, destination):
    decoded = image_decode.decode(*snapshot, max_side=4096)
    if decoded is None:
        raise OSError('原图不可用，无法创建清晰贴图')
    image, original = decoded
    scale_x, scale_y = image.width()/original.width(), image.height()/original.height()
    x, y, width, height = crop
    image = image.copy(QtCore.QRect(round(x*scale_x), round(y*scale_y),
                                   max(1, round(width*scale_x)), max(1, round(height*scale_y))))
    if flip < 0:
        image = image.mirrored(True, False)
    if gray:
        from prism.fileio.image import qt_grayscale_image
        image = qt_grayscale_image(image)
    if opacity < 1:
        result = QtGui.QImage(image.size(), QtGui.QImage.Format.Format_ARGB32_Premultiplied)
        result.fill(QtCore.Qt.GlobalColor.transparent)
        painter = QtGui.QPainter(result)
        painter.setOpacity(opacity)
        painter.drawImage(0, 0, image)
        painter.end()
        image = result
    if not image.save(str(destination), 'PNG'):
        raise OSError('无法写入贴图文件')
    return str(destination)


class CaptureService(QtCore.QObject):
    imported = QtCore.pyqtSignal(object, str)
    failed = QtCore.pyqtSignal(str)
    prepared = QtCore.pyqtSignal(str, object, str)
    retained = QtCore.pyqtSignal(object, str, str, str)

    #: How long the helper may take to leave after the shutdown request.
    #: The old code waited for it on the GUI thread; it is a timer now.
    SHUTDOWN_GRACE_MS = 500

    def __init__(self, parent):
        super().__init__(parent)
        self.directory = tempfile.TemporaryDirectory(prefix='prism-capture-')
        self.process = QtCore.QProcess(self)
        self.process.readyReadStandardOutput.connect(self._read)
        self.process.readyReadStandardError.connect(self._stderr)
        self.process.errorOccurred.connect(self._error)
        self.process.finished.connect(self._finished)
        self.ready = False
        self.buffer = bytearray()
        self.diagnostics = bytearray()
        self.pending = []
        self.jobs = {}
        self.pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix='PrismPinPrepare')
        self._cleanup_future = None
        self._inbox_cleanup_timer = QtCore.QTimer(self)
        self._inbox_cleanup_timer.setInterval(INBOX_CLEANUP_INTERVAL_MS)
        self._inbox_cleanup_timer.timeout.connect(self._schedule_inbox_cleanup)
        self._inbox_cleanup_timer.start()
        self.prepared.connect(self._prepared)
        self.retained.connect(self._retained)
        self.startup = QtCore.QTimer(self)
        self.startup.setSingleShot(True)
        self.startup.timeout.connect(self._timeout)
        self.closed = False
        self._failure_reported = False
        self._startup_started = None
        self.references = []

    def start(self):
        if self.process.state() != QtCore.QProcess.ProcessState.NotRunning:
            return
        python, source = component_command()
        self._schedule_inbox_cleanup()
        self.ready = False
        self.buffer.clear()
        self.diagnostics.clear()
        self._failure_reported = False
        self._startup_started = time.monotonic()
        helper = Path(__file__).parent/'components'/'capture_helper.py'
        program, arguments = invocation(Path(python).parent.parent, helper,
            ['--source', source, '--session-dir', self.directory.name, '--persistent'])
        self.process.setProgram(program)
        self.process.setArguments(arguments)
        environment = QtCore.QProcessEnvironment.systemEnvironment()
        # Never inherit Qt6 plugins or Python paths into the Qt5 helper.
        for key in ('PYTHONPATH', 'QT_PLUGIN_PATH', 'QT_QPA_PLATFORM_PLUGIN_PATH'):
            environment.remove(key)
        environment.insert('PYTHONIOENCODING', 'utf-8')
        settings = capture_directory()/'settings'
        settings.mkdir(exist_ok=True)
        environment.insert('PRISM_CAPTURE_CONFIG_HOME', str(settings))
        self.process.setProcessEnvironment(environment)
        self.process.start()
        self.startup.start(20000)
        record('capture.helper.start')

    def _schedule_inbox_cleanup(self):
        if self.closed or (self._cleanup_future is not None and not self._cleanup_future.done()):
            return
        self._cleanup_future = self.pool.submit(cleanup_capture_inbox)
        def completed(future):
            if future.cancelled():
                return
            try:
                future.result()
            except Exception as error:
                record('capture.inbox.cleanup_failed', error_type=type(error).__name__)
        self._cleanup_future.add_done_callback(completed)

    def send(self, command, **fields):
        if self.closed:
            raise RuntimeError('截图组件已关闭')
        if len(self.pending) >= 32:
            raise ValueError('截图任务队列已满')
        self.start()
        if self._failure_reported:
            raise RuntimeError('截图组件正在退出，请稍后重试')
        message = dict(command=command, **fields)
        if self.ready:
            self._write(message)
        else:
            self.pending.append(message)

    def _write(self, message):
        self.process.write((json.dumps(message, ensure_ascii=False)+'\n').encode('utf-8'))

    def capture(self, target):
        if any(value.get('kind') == 'capture' for value in self.jobs.values()):
            raise ValueError('已有截图正在进行')
        request = uuid.uuid4().hex
        self.jobs[request] = dict(target, kind='capture', started=time.monotonic())
        try:
            self.send('capture', request_id=request)
        except Exception:
            self.jobs.pop(request, None)
            raise
        record('capture.start', request_id=request)

    def pin(self, item):
        if len(self.jobs) >= 8:
            raise ValueError('贴图准备队列已满，请稍后再试')
        request = uuid.uuid4().hex
        self.jobs[request] = dict(kind='pin')
        crop = item.crop
        future = self.pool.submit(prepare_pin, image_decode.source_snapshot(item),
            (crop.x(), crop.y(), crop.width(), crop.height()), item.flip(),
            item.grayscale, item.opacity(), Path(self.directory.name)/(request+'.png'))
        def done(future):
            if self.closed:
                return
            try:
                path, error = future.result(), ''
            except Exception as exc:
                path, error = None, str(exc)
            self.prepared.emit(request, path, error)
        future.add_done_callback(done)

    def _prepared(self, request, path, error):
        if request not in self.jobs:
            return
        if error:
            self.jobs.pop(request, None)
            self.failed.emit(error)
        else:
            self.jobs.pop(request, None)
            self.show_reference(path)
            Path(path).unlink(missing_ok=True)

    def show_reference(self, path):
        from prism.widgets.desktop_reference import DesktopReference
        from PyQt6 import sip
        self.references = [window for window in self.references if not sip.isdeleted(window)]
        image = QtGui.QImage(str(path))
        if image.isNull():
            self.failed.emit('参考图无法打开')
            return
        pixels = sum(window.canvas.reference.pixmap().width()*window.canvas.reference.pixmap().height() for window in self.references)
        if len(self.references) >= 20 or (pixels+image.width()*image.height())*4 > 128*1024*1024:
            self.failed.emit('桌面参考图已达到 20 张或 128 MiB 上限，请关闭部分窗口')
            return
        window = DesktopReference(image, self.parent().window())
        window.send_to_canvas.connect(self.import_reference)
        self.references.append(window)
        window.show()

    def import_reference(self, image):
        path = Path(self.directory.name)/(uuid.uuid4().hex+'.png')
        if image.save(str(path), 'PNG'):
            self._message(dict(event='import_pin', path=str(path)))

    def _read(self):
        self.buffer.extend(bytes(self.process.readAllStandardOutput()))
        if len(self.buffer) > 1024*1024:
            self._fail_helper('protocol_overflow', '截图组件返回的数据过大')
            return
        while b'\n' in self.buffer:
            line, _, self.buffer = self.buffer.partition(b'\n')
            try:
                self._message(json.loads(line))
            except (ValueError, KeyError, TypeError, OSError) as error:
                self.failed.emit('截图组件返回无效结果：'+str(error))

    def _message(self, value):
        if self.closed or self._failure_reported:
            return
        event = value['event']
        if event == 'startup':
            # Only record known stages, never upstream output or user content.
            phase = value.get('phase')
            if phase in ('imports', 'application', 'bridge'):
                record('capture.helper.startup', phase=phase,
                       duration_ms=self._startup_duration())
            return
        if event == 'overlay_ready':
            target = self.jobs.get(value.get('request_id'))
            if target and target.get('started'):
                record('capture.overlay_ready', request_id=value.get('request_id'),
                       duration_ms=round((time.monotonic()-target['started'])*1000, 2))
            return
        if event == 'ready':
            if value.get('protocol_version') != 1:
                raise ValueError('截图组件协议不兼容')
            self.ready = True
            self.startup.stop()
            record('capture.helper.ready', duration_ms=self._startup_duration())
            pending, self.pending = self.pending, []
            for message in pending:
                self._write(message)
            return
        request = value.get('request_id')
        target = self.jobs.pop(request, None)
        if event == 'captured' and target is not None:
            target['destination'] = value.get('destination', 'canvas')
        if event == 'pinned' and target and target.get('path'):
            Path(target['path']).unlink(missing_ok=True)
        if event == 'failed':
            self.failed.emit(value.get('error', '截图失败'))
        elif event in ('captured', 'import_pin') and value.get('path'):
            if event == 'captured' and target is None:
                return  # Late or duplicate response must not insert again.
            path = Path(value['path']).resolve()
            if not path.is_relative_to(Path(self.directory.name).resolve()) or path.suffix != '.png':
                raise ValueError('无效截图文件路径')
            future = self.pool.submit(retain_capture, path)
            def done(future):
                if self.closed:
                    return
                try:
                    result, error = future.result(), ''
                except Exception as exc:
                    result, error = '', str(exc)
                self.retained.emit(target, result, str(path), error)
            future.add_done_callback(done)
        record('capture.'+event, request_id=request)

    def _retained(self, target, result, temporary, error):
        if error:
            self.failed.emit(error)
            return
        destination = target.get('destination', 'canvas') if target else 'canvas'
        if destination in ('pin', 'both'):
            self.show_reference(result)
            if destination == 'pin':
                # The reference now owns decoded pixels, not the inbox file.
                release_capture_paths([result])
        if destination in ('canvas', 'both'):
            self.imported.emit(target, result)
        if self.process.state() == QtCore.QProcess.ProcessState.Running:
            self._write(dict(command='ack', path=temporary))

    def _stderr(self):
        self.diagnostics.extend(bytes(self.process.readAllStandardError()))
        del self.diagnostics[:-8192]

    def _timeout(self):
        # A busy GUI can deliver the timer before an already queued readyRead.
        if self.ready or self.closed or self._failure_reported:
            return
        if self.process.state() == QtCore.QProcess.ProcessState.Running:
            # Poll the OS pipe too: readAllStandardOutput only sees Qt's buffer
            # until its notifier runs. Zero timeout does not wait on the GUI.
            self.process.waitForReadyRead(0)
        self._read()
        if self.ready or self.closed or self._failure_reported:
            return
        self._fail_helper('startup_timeout', '截图组件启动超时，请重试')

    def _startup_duration(self):
        return (round((time.monotonic()-self._startup_started)*1000, 2)
                if self._startup_started is not None else None)

    def _fail_helper(self, reason, message):
        if self.closed or self._failure_reported:
            return
        self._failure_reported = True
        self.startup.stop()
        self.ready = False
        record('capture.helper.failed', reason=reason,
               duration_ms=self._startup_duration(), jobs=len(self.jobs),
               pending=len(self.pending))
        self.jobs.clear()
        self.pending.clear()
        # Tear down BEFORE notifying UI: failed handlers may run a modal event
        # loop. A late ready/crash must not launch work or show more dialogs.
        if self.process.state() != QtCore.QProcess.ProcessState.NotRunning:
            self.process.kill()
        self.failed.emit(message)

    def _error(self, error):
        self._fail_helper('process_error', '截图组件启动失败：'+self.process.errorString())

    def _finished(self, code, status):
        self._stderr()
        self.startup.stop()
        record('capture.helper.exit', exit_code=code, exit_status=status.value,
               ready=self.ready, expected=self.closed or self._failure_reported,
               duration_ms=self._startup_duration())
        self.ready = False
        had_jobs = bool(self.jobs)
        self.jobs.clear()
        self.pending.clear()
        if not self.closed and (code or had_jobs):
            self._fail_helper('unexpected_exit', '截图组件已退出，请重试')

    def close(self):
        self.closed = True
        self._inbox_cleanup_timer.stop()
        for window in self.references:
            from PyQt6 import sip
            if not sip.isdeleted(window):
                window.close()
        self.references.clear()
        self.startup.stop()
        self.pool.shutdown(wait=False, cancel_futures=True)
        if self.process.state() != QtCore.QProcess.ProcessState.NotRunning:
            self._write(dict(command='shutdown'))
            # Waiting for the helper to exit froze the GUI thread for up to
            # half a second on every close (measured: 535 ms of stalls in
            # one session).  The helper still gets the same grace period to
            # shut down cleanly, but the wait is a timer now, so the GUI
            # thread returns to the event loop immediately.  An overrunning
            # helper is killed by the timer; a helper that outlives this
            # service is killed by QProcess's own destructor, so it is never
            # left running without an owner.
            self._shutdown_timer = QtCore.QTimer(self)
            self._shutdown_timer.setSingleShot(True)
            self._shutdown_timer.timeout.connect(self._kill_helper)
            self._shutdown_timer.start(self.SHUTDOWN_GRACE_MS)

    def _kill_helper(self):
        if self.process.state() != QtCore.QProcess.ProcessState.NotRunning:
            self.process.kill()
