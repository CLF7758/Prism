"""Bounded Qt process/staging adapter for local OpenCLIP + FAISS search."""
import json
import os
from pathlib import Path
import tempfile
import uuid
import shutil
from concurrent.futures import ThreadPoolExecutor

from PyQt6 import QtCore, QtGui
from prism import image_decode
from prism.item_lifecycle import native_pixmap
from prism.performance_trace import record
from prism.component_runtime import invocation


def component_directory():
    return Path(os.environ.get('LOCALAPPDATA', Path.home()))/'Prism/components/visual-search'


def source_snapshot(item):
    snapshot = image_decode.source_snapshot(item)
    if any(snapshot):
        return snapshot
    pixmap = native_pixmap(item)
    return pixmap.toImage() if pixmap is not None and not pixmap.isNull() else None


def stage_source(value, directory):
    if isinstance(value, (str, Path)):
        return dict(path=str(value))
    if isinstance(value, QtGui.QImage):
        path = directory/(uuid.uuid4().hex+'.png')
        if value.isNull() or not value.save(str(path), 'PNG'):
            raise ValueError('查询图片不可用')
        return dict(path=str(path))
    if value is None:
        raise ValueError('原图不可用')
    blob, path, archive = value
    if blob is not None:
        target = directory/(uuid.uuid4().hex+'.image')
        target.write_bytes(blob)
        return dict(path=str(target))
    return dict(path=path, archive=archive)


def stage_request(directory, query, assets):
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    query_source = stage_source(query, directory)
    rows = [(stage_source(source, directory) if source is not None else dict(unavailable=True)) | dict(key=key)
            for key, source in assets]
    manifest = directory/'request.json'
    manifest.write_text(json.dumps(dict(query=query_source, assets=rows)), encoding='utf-8')
    try:
        result = image_decode.decode(path=query_source.get('path'), archive=query_source.get('archive'), max_side=64)
        preview = result[0] if result else None
    except (OSError, ValueError):
        preview = None
    return dict(manifest=str(manifest), preview=preview)


class VisualSearchService(QtCore.QObject):
    progress = QtCore.pyqtSignal(object)
    results = QtCore.pyqtSignal(str, object)
    failed = QtCore.pyqtSignal(str)
    staged = QtCore.pyqtSignal(str, object, str)
    preview = QtCore.pyqtSignal(object)

    def __init__(self, parent):
        super().__init__(parent)
        self.process = QtCore.QProcess(self)
        self.process.readyReadStandardOutput.connect(self._read)
        self.process.readyReadStandardError.connect(lambda: self.process.readAllStandardError())
        self.process.finished.connect(self._finished)
        self.process.errorOccurred.connect(self._error)
        self.pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix='PrismVisualStage')
        self.staged.connect(self._staged)
        self.root = tempfile.TemporaryDirectory(prefix='prism-visual-query-')
        self.jobs = {}
        self.active = None
        self.buffer = bytearray()
        self.ready = False
        self.closed = False
        self.pending = None
        self.terminating = False
        self.staging = set()
        self.retired = set()
        self.idle = QtCore.QTimer(self)
        self.idle.setSingleShot(True)
        self.idle.setInterval(60000)
        self.idle.timeout.connect(self.stop)

    def search(self, query, assets):
        if self.active:
            raise ValueError('本地检索仍在处理上一项任务')
        if len(self.jobs) >= 2:
            raise ValueError('检索准备队列已满，请稍后重试')
        python = component_directory()/'runtime/Scripts/python.exe'
        if not python.is_file():
            raise OSError('本地检索组件未安装，请运行 tools/setup_visual_search_component.py')
        model = component_directory()/'index/models/ViT-B-32.pt'
        if not model.is_file() or model.stat().st_size != 353976522:
            raise OSError('本地模型尚未准备完成，请运行 tools/setup_visual_search_component.py --download-model；查询不会上传图片')
        self.idle.stop()
        identifier = uuid.uuid4().hex
        self.active = identifier
        self.jobs[identifier] = Path(self.root.name)/identifier
        self.staging.add(identifier)
        future = self.pool.submit(stage_request, self.jobs[identifier], query, assets)
        def done(future):
            if self.closed:
                return
            try:
                result, error = future.result(), ''
            except Exception as exc:
                result, error = None, str(exc)
            self.staged.emit(identifier, result, error)
        future.add_done_callback(done)
        record('visual_search.start', request_id=identifier, assets=len(assets))
        return identifier

    def _staged(self, identifier, manifest, error):
        self.staging.discard(identifier)
        if identifier != self.active:
            self._cleanup(identifier)
            return
        if error:
            self.active = None
            self._cleanup(identifier)
            self.failed.emit(error)
            return
        if manifest.get('preview') is not None:
            self.preview.emit(manifest['preview'])
        self.pending = dict(command='search', request_id=identifier, manifest=manifest['manifest'])
        if self.process.state() == QtCore.QProcess.ProcessState.NotRunning:
            self._launch()
        elif self.ready:
            self._write()

    def _launch(self):
        self.ready = False
        self.buffer.clear()
        program, arguments = invocation(component_directory()/'runtime',
            Path(__file__).parent/'components/visual_search_worker.py', [component_directory()/'index'])
        self.process.setProgram(program)
        self.process.setArguments(arguments)
        environment = QtCore.QProcessEnvironment.systemEnvironment()
        environment.insert('PYTHONIOENCODING', 'utf-8')
        environment.remove('PYTHONPATH')
        self.process.setProcessEnvironment(environment)
        self.process.start()

    def _error(self, error):
        if not self.closed and not self.terminating and error == QtCore.QProcess.ProcessError.FailedToStart:
            identifier, self.active = self.active, None
            if identifier:
                self._cleanup(identifier)
            self.failed.emit('本地检索组件启动失败：'+self.process.errorString())

    def _write(self):
        value, self.pending = self.pending, None
        if value:
            self.process.write((json.dumps(value)+'\n').encode('utf-8'))

    def _read(self):
        self.buffer.extend(bytes(self.process.readAllStandardOutput()))
        if len(self.buffer) > 1024*1024:
            self.failed.emit('检索组件返回异常数据')
            self.stop()
            return
        while b'\n' in self.buffer:
            line, _, self.buffer = self.buffer.partition(b'\n')
            try:
                value = json.loads(line)
                event = value['event']
                if event == 'ready':
                    self.ready = True
                    self._write()
                elif event == 'progress':
                    self.progress.emit(value)
                elif value.get('request_id') == self.active:
                    identifier, self.active = self.active, None
                    if event == 'results':
                        self.results.emit(identifier, value)
                        record('visual_search.results', request_id=identifier, indexed=value.get('indexed'), cached=value.get('cached'))
                    elif event == 'failed':
                        record('visual_search.failed', request_id=identifier, reason=value.get('error', '')[:500])
                        self.failed.emit(value.get('error', '本地检索失败'))
                    self._cleanup(identifier)
                    self.idle.start()
            except (ValueError, TypeError, KeyError) as error:
                record('visual_search.protocol_error', error_type=type(error).__name__)
                self.failed.emit('本地检索组件返回无效结果')
                self.stop()

    def stop(self):
        previous = self.active
        self.active = None
        self.ready = False
        self.pending = None
        if self.process.state() != QtCore.QProcess.ProcessState.NotRunning:
            self.terminating = True
            if previous:
                self.retired.add(previous)
            self.process.kill()
        elif previous and previous not in self.staging:
            self._cleanup(previous)

    def _finished(self, code, status):
        for identifier in list(self.retired):
            if identifier not in self.staging:
                self._cleanup(identifier)
                self.retired.discard(identifier)
        if self.terminating:
            self.terminating = False
            if self.active and self.pending and not self.closed:
                self._launch()
            return
        active, self.active = self.active, None
        self.ready = False
        if active and not self.closed:
            self._cleanup(active)
            self.failed.emit('本地检索任务已中断，可重试；已提交索引会保留')

    def _cleanup(self, identifier):
        directory = self.jobs.pop(identifier, None)
        if directory is not None and directory.resolve().is_relative_to(Path(self.root.name).resolve()):
            shutil.rmtree(directory, ignore_errors=True)

    def close(self):
        self.closed = True
        self.idle.stop()
        self.stop()
        self.pool.shutdown(wait=False, cancel_futures=True)
