"""Asynchronous log tail and explicit, local diagnostics export."""
from pathlib import Path
import uuid
from PyQt6 import QtCore, QtWidgets
from prism.config import logfile_name
from prism.diagnostics import DiagnosticCancelled, collect_bundle, runtime_snapshot, utc_now


def _model_library_service(window):
    """The model service, if this window happens to have one.

    Never imported eagerly and never required: the dialog must work in windows
    that have no model library at all (a fresh project, a test harness).
    """
    library = getattr(window, 'model_library', None)
    return getattr(library, 'service', None) if library is not None else None


def _service_queue(service):
    """In-flight counts read from the service's own bookkeeping.

    These are private attributes today; the snapshot marks the basis so a
    reader knows the number came from `getattr`, not from a public API.
    """
    if service is None:
        return None
    try:
        futures = getattr(service, '_futures', {}) or {}
        running = getattr(service, '_running_native', {}) or {}
        leases = getattr(service, '_leases', {}) or {}
        imports = getattr(service, '_imports', set()) or set()
        prepared = getattr(service, '_preparing', set()) or set()
        material=getattr(service,'multimaterial',None)
        material_jobs=getattr(material,'_jobs',{}) or {}
        material_running=getattr(material,'_native_running',{}) or {}
        return {
            'basis': 'service-private-attributes',
            'futures': len(futures),
            'native_running': len(running),
            'leases': len(leases),
            'imports_pending': len(imports),
            'preparing': len(prepared),
            'material_jobs_retained':len(material_jobs),
            'material_native_running':len(material_running),
            'material_pending_cover_models':len(getattr(material,'_cover_pending',()) or ()),
            'state': 'measured',
        }
    except Exception as error:                     # noqa: BLE001 - recorded
        return {'state': 'unknown', 'reason': type(error).__name__}


def _model_snapshot(service, problem):
    """Build the model diagnostic snapshot, or explain why there is none.

    Runs on the exporter's background thread.  A snapshot that cannot be built
    is recorded in ``problem['model_snapshot']`` so the bundle shows
    "not collected, and here is why" instead of an empty section.
    """
    try:
        from prism.model_library.diagnostic_snapshot import model_diagnostic_snapshot
    except Exception as error:                       # noqa: BLE001 - recorded
        problem['model_snapshot'] = {'state': 'unavailable',
                                     'reason': type(error).__name__}
        return None
    try:
        snapshot = model_diagnostic_snapshot(service=service,
                                             queue=_service_queue(service))
    except Exception as error:                       # noqa: BLE001 - recorded
        problem['model_snapshot'] = {'state': 'failed',
                                     'reason': type(error).__name__}
        return None
    problem['model_snapshot'] = {
        'state': 'collected',
        'service_supplied': service is not None,
        'reads_model_bytes': False,
    }
    return snapshot


class _Job(QtCore.QThread):
    def __init__(self, parent, function):
        super().__init__(parent)
        self.function = function
        self.value = self.error = None

    def run(self):
        try:
            self.value = self.function(self.isInterruptionRequested)
        except Exception as error:
            self.error = error


class DebugLogDialog(QtWidgets.QDialog):
    def __init__(self, parent):
        super().__init__(parent)
        self.setWindowTitle('日志与诊断')
        self.setProperty('prism_diagnostics_surface', True)
        self._preview_job = self._export_job = None
        self._close_pending = False
        self.marker = {}
        self.log_txt = ''
        self.log = QtWidgets.QPlainTextEdit('正在读取最近日志…')
        self.log.setReadOnly(True)
        self.log.document().setMaximumBlockCount(3000)
        self.description = QtWidgets.QPlainTextEdit()
        self.description.setPlaceholderText('问题描述（可选）：做了什么 / 预期结果 / 实际结果。此描述会包含在诊断包中。')
        self.description.setMaximumHeight(90)
        self.status = QtWidgets.QLabel('仅显示最近 128 KiB；诊断包包含保留的轮转日志和崩溃记录。')
        self.mark_button = QtWidgets.QPushButton('标记刚才的问题')
        self.mark_button.clicked.connect(self.mark_problem)
        self.export_button = QtWidgets.QPushButton('导出诊断包…')
        self.export_button.clicked.connect(self.export_bundle)
        self.copy_button = QtWidgets.QPushButton('复制当前日志')
        self.copy_button.clicked.connect(self.copy_to_clipboard)
        buttons = QtWidgets.QDialogButtonBox(QtWidgets.QDialogButtonBox.StandardButton.Close)
        buttons.rejected.connect(self.reject)
        for button in (self.mark_button, self.export_button, self.copy_button):
            buttons.addButton(button, QtWidgets.QDialogButtonBox.ButtonRole.ActionRole)
        layout = QtWidgets.QVBoxLayout(self)
        layout.addWidget(self.log)
        layout.addWidget(self.description)
        layout.addWidget(self.status)
        layout.addWidget(buttons)
        self.resize(850, 550)
        self.show()
        self._start_tail()

    def _start_tail(self):
        path = Path(logfile_name())
        def read(cancel):
            if cancel():
                return ''
            try:
                with path.open('rb') as stream:
                    stream.seek(0, 2)
                    stream.seek(max(0, stream.tell()-128*1024))
                    return stream.read(128*1024).decode('utf-8', errors='replace')
            except FileNotFoundError:
                return '当前尚无普通日志。仍可导出其它已保留的诊断记录。'
        job = self._preview_job = _Job(self, read)
        def finished():
            self._preview_job = None
            self.log_txt = job.value if job.error is None else '日志读取失败：' + str(job.error)
            self.log.setPlainText(self.log_txt)
            job.deleteLater()
            if self._close_pending:
                self.reject()
        job.finished.connect(finished)
        job.start()

    def mark_problem(self):
        self.marker = dict(marker_id=uuid.uuid4().hex, time=utc_now())
        self.status.setText('已标记问题时间；可补充描述后导出。')
        try:
            from prism.performance_trace import record
            record('user_problem_marker', marker_id=self.marker['marker_id'], marked_at=self.marker['time'])
        except Exception:
            pass  # Marking a problem must not break the diagnostics UI.

    def export_bundle(self):
        if self._export_job is not None:
            return
        name, _ = QtWidgets.QFileDialog.getSaveFileName(self, '导出诊断包',
            'Prism-diagnostics.zip', '诊断包 (*.zip)')
        if not name:
            return
        if not name.lower().endswith('.zip'):
            name += '.zip'
        problem = dict(self.marker, description=self.description.toPlainText()[:4000])
        parent = self.parentWidget()
        window = parent.window() if parent is not None else None
        runtime = runtime_snapshot(window)
        # Only the handle is taken here; building the snapshot happens on the
        # exporter thread so the dialog never blocks on it.
        service = _model_library_service(window)
        directory = Path(logfile_name()).parent
        source_root = Path(__file__).resolve().parents[1]

        def collect(cancel):
            snapshot = _model_snapshot(service, problem)
            return collect_bundle(
                directory, name, runtime=runtime, problem=problem,
                source_root=source_root, cancel=cancel, model_snapshot=snapshot)

        job = self._export_job = _Job(self, collect)
        self.export_button.setEnabled(False)
        self.status.setText('正在后台收集诊断…不会附带工程原件、配置原文、剪贴板或截图，不会自动上传。')
        def finished():
            self._export_job = None
            self.export_button.setEnabled(True)
            if isinstance(job.error, DiagnosticCancelled):
                self.status.setText('诊断包导出已取消。')
            elif job.error is not None:
                self.status.setText('诊断包未完成：' + str(job.error))
            else:
                self.status.setText('诊断包已保存：' + job.value)
            job.deleteLater()
            if self._close_pending:
                self.reject()
        job.finished.connect(finished)
        job.start()

    def copy_to_clipboard(self):
        QtWidgets.QApplication.clipboard().setText(self.log_txt)

    def reject(self):
        pending = [job for job in (self._preview_job, self._export_job)
                   if job is not None and job.isRunning()]
        if pending:
            self._close_pending = True
            for job in pending:
                job.requestInterruption()
            self.status.setText('正在停止诊断收集…')
            return
        super().reject()
