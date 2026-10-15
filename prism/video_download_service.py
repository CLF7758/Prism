"""Bounded yt-dlp jobs; no media bytes, cookies or parser work in the UI."""
from collections import deque
import json
import os
from pathlib import Path
import shutil
import uuid
from PyQt6 import QtCore
from prism.component_runtime import invocation
from prism.performance_trace import record


def component_directory():
    return Path(os.environ.get('LOCALAPPDATA', Path.home()))/'Prism/components/video-download'


class VideoDownloadService(QtCore.QObject):
    progress = QtCore.pyqtSignal(str, object)
    finished = QtCore.pyqtSignal(str, object)
    failed = QtCore.pyqtSignal(str, str)

    def __init__(self, parent):
        super().__init__(parent)
        self.queue = deque()
        self.jobs = {}
        self.running = set()
        self.completed = deque()
        self.closed = False

    def submit(self, url, directory, command='download', format=None, cookie_file=None, context=None, cookie_browser=None):
        from prism.video_urls import normalize_share_url
        url = normalize_share_url(url)
        if cookie_browser not in (None, 'chrome', 'edge', 'firefox'):
            raise ValueError('不支持的浏览器登录来源')
        if len(self.queue)+len(self.running) >= 20:
            raise ValueError('下载等待队列已满（最多 20 项）')
        root = component_directory()
        if not (root/'runtime/Scripts/python.exe').exists():
            raise OSError('视频下载组件未安装，请运行 tools/setup_video_download_component.py')
        identifier = uuid.uuid4().hex
        work = root/'jobs'/identifier
        work.mkdir(parents=True)
        data = dict(command=command, url=url, directory=str(Path(directory).resolve()/identifier),
            job_directory=str(work), format=format, cookie_file=cookie_file,
            cookie_browser=cookie_browser,
            ffmpeg=str(root/'bin'), node=shutil.which('node'))
        manifest = work/'request.json'
        manifest.write_text(json.dumps(data, ensure_ascii=False), encoding='utf-8')
        self.jobs[identifier] = dict(data=data, context=context, manifest=manifest,
                                     buffer=bytearray(), result=None, cancelled=False)
        self.queue.append(identifier)
        self._pump()
        return identifier

    def _pump(self):
        while self.queue and len(self.running) < 2 and not self.closed:
            identifier = self.queue.popleft()
            job = self.jobs[identifier]
            process = job['process'] = QtCore.QProcess(self)
            program, arguments = invocation(component_directory()/'runtime',
                Path(__file__).parent/'components/video_download_worker.py', [job['manifest']])
            process.setProgram(program)
            process.setArguments(arguments)
            environment = QtCore.QProcessEnvironment.systemEnvironment()
            environment.insert('PYTHONIOENCODING', 'utf-8')
            environment.remove('PYTHONPATH')
            process.setProcessEnvironment(environment)
            process.readyReadStandardOutput.connect(lambda key=identifier: self._read(key))
            process.readyReadStandardError.connect(lambda p=process: p.readAllStandardError())
            process.finished.connect(lambda code, status, key=identifier: self._ended(key, code))
            process.errorOccurred.connect(lambda error, key=identifier: self._error(key, error))
            self.running.add(identifier)
            process.start()
            self.progress.emit(identifier, dict(phase='starting'))
            record('download.start', request_id=identifier, command=job['data']['command'])

    def _read(self, identifier):
        job = self.jobs[identifier]
        job['buffer'].extend(bytes(job['process'].readAllStandardOutput()))
        if len(job['buffer']) > 1024*1024:
            job['result'] = dict(event='failed', error='下载组件返回数据异常')
            job['process'].kill()
            return
        while b'\n' in job['buffer']:
            line, _, job['buffer'] = job['buffer'].partition(b'\n')
            try:
                value = json.loads(line)
                event = value['event']
                if event in ('completed', 'inspected', 'failed'):
                    if event == 'completed':
                        path = Path(value['path']).resolve()
                        if not path.is_relative_to(Path(job['data']['directory'])) or not path.is_file():
                            raise ValueError('无效的视频结果路径')
                    job['result'] = value
                else:
                    self.progress.emit(identifier, value)
            except (ValueError, KeyError, TypeError):
                job['result'] = dict(event='failed', error='下载组件返回无效结果')
                job['process'].kill()

    def _ended(self, identifier, code):
        if identifier not in self.running:
            return
        job = self.jobs[identifier]
        self._read(identifier)
        result = job['result'] or dict(event='failed', error='下载任务意外中断')
        if job['cancelled']:
            result = dict(event='cancelled')
        (job['manifest'].parent/'private-cookies.txt').unlink(missing_ok=True)
        job['manifest'].unlink(missing_ok=True)
        try:
            job['manifest'].parent.rmdir()
        except OSError:
            pass
        self.running.discard(identifier)
        job['process'].deleteLater()
        job.pop('process', None)
        job.pop('buffer', None)
        self.completed.append(identifier)
        while len(self.completed) > 20:
            self.jobs.pop(self.completed.popleft(), None)
        if not self.closed:
            if result['event'] == 'failed':
                self.failed.emit(identifier, result.get('error', '下载失败'))
            else:
                self.finished.emit(identifier, result | dict(context=job['context']))
        record('download.'+result['event'], request_id=identifier,
               reason=result.get('error', '')[:500], code=result.get('code'))
        self._pump()

    def _error(self, identifier, error):
        if error == QtCore.QProcess.ProcessError.FailedToStart:
            self.jobs[identifier]['result'] = dict(event='failed', error='无法启动下载组件')
            self._ended(identifier, -1)

    def cancel(self, identifier):
        job = self.jobs.get(identifier)
        if job is None:
            return
        job['cancelled'] = True
        if identifier in self.running:
            job['process'].kill()
        elif identifier in self.queue:
            self.queue.remove(identifier)
            job['manifest'].unlink(missing_ok=True)
            try:
                job['manifest'].parent.rmdir()
            except OSError:
                pass
            self.completed.append(identifier)
            while len(self.completed) > 20:
                self.jobs.pop(self.completed.popleft(), None)
            self.finished.emit(identifier, dict(event='cancelled', context=job['context']))

    def close(self):
        self.closed = True
        for identifier in list(self.queue)+list(self.running):
            self.cancel(identifier)
        for job in list(self.jobs.values()):
            process = job.get('process')
            if process is not None:
                process.waitForFinished(500)
