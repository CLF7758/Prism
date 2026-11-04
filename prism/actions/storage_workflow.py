"""Visible, cancellable storage operations in the existing Qt host."""
from pathlib import Path
import logging
import time
import uuid
from PyQt6 import QtCore, QtWidgets
from prism.storage.managed import root_for

logger=logging.getLogger(__name__)


class _Operation(QtCore.QThread):
    completed=QtCore.pyqtSignal(object,object)
    progress=QtCore.pyqtSignal(object)
    def __init__(self,owner,job,title,with_progress=False):
        super().__init__(owner);self.job=job;self.title=title;self.operation_id=uuid.uuid4().hex
        self.with_progress=with_progress
        self.project_id = getattr(getattr(owner, 'scene', None), 'project_id', None)
        self.open_epoch = getattr(owner, '_trace_open_epoch', None)
    def run(self):
        started=time.perf_counter()
        self._progress_started = started
        self._progress_emitted = started
        self._progress_phase_started = started
        self._progress_phase = None
        self._progress_latest = {}
        self._progress_previous = {}
        self._progress_plain_emitted = started
        logger.info('Storage operation started: %s [%s]',self.title,self.operation_id)
        self._trace('start', priority='terminal')
        try:
            result=(self.job(self.isInterruptionRequested,self._report_progress) if self.with_progress
                    else self.job(self.isInterruptionRequested))
        except Exception as error:
            from prism.storage.contracts import StorageError, StorageErrorKind
            cancelled = isinstance(error, StorageError) and error.kind is StorageErrorKind.CANCELLED
            if self._progress_latest:
                self._report_progress({}, force=True)
            if cancelled:
                logger.info('Storage operation cancelled: %s [%s]', self.title, self.operation_id)
            else:
                logger.exception('Storage operation failed: %s [%s]',self.title,self.operation_id)
            self._trace('end',duration_ms=(time.perf_counter()-started)*1000,
                        status='cancelled' if cancelled else 'failed',
                        priority='terminal' if cancelled else 'error',
                        progress=dict(self._progress_latest),
                        error_kind=getattr(getattr(error, 'kind', None), 'value', None),
                        error_type=type(error).__name__,reason=str(error))
            self.completed.emit(None,str(error))
        else:
            if self._progress_latest:
                self._report_progress({}, force=True)
            logger.info('Storage operation completed: %s [%s] %.1f ms',self.title,self.operation_id,
                        (time.perf_counter()-started)*1000)
            self._trace('end',duration_ms=(time.perf_counter()-started)*1000,status='completed',
                        priority='terminal', progress=dict(self._progress_latest))
            self.completed.emit(result,None)

    def _report_progress(self, value, *, force=False):
        now = time.perf_counter()
        phase = value.get('phase', self._progress_phase)
        changed = phase != self._progress_phase
        if changed:
            if self._progress_phase is not None:
                self._trace('stage_end', priority='terminal', stage=self._progress_phase,
                            duration_ms=(now-self._progress_phase_started)*1000)
            self._progress_phase_started = now
            self._progress_phase = phase
        # The migration counters are cumulative logical payload work. Missing
        # later-phase fields retain the last observed value, never become zero.
        self._progress_latest.update(value)
        if not force and not changed and now-self._progress_emitted < 1.0:
            return
        latest = self._progress_latest
        interval = now-self._progress_emitted
        counters = {}
        rates = {}
        for key in ('source_read_bytes', 'target_payload_bytes', 'verify_read_bytes'):
            current = latest.get(key)
            previous = self._progress_previous.get(key)
            counters[key] = current
            rates[key+'_per_s'] = ((current-previous)/interval
                if isinstance(current, (int, float)) and isinstance(previous, (int, float))
                and current >= previous and interval > 0 else None)
        self._trace('progress', priority='state', stage=phase,
            elapsed_ms=(now-self._progress_started)*1000,
            stage_elapsed_ms=(now-self._progress_phase_started)*1000,
            rate_window_ms=interval*1000, cumulative=True,
            processed=latest.get('processed'), copied_this_run=latest.get('copied_this_run'),
            new_objects=latest.get('new_objects'), **counters, **rates,
            pack_sync_count=latest.get('pack_sync_count'), pack_sync_ms=latest.get('pack_sync_ms'),
            index_commit_ms=latest.get('index_commit_ms'),
            metadata_commit_ms=latest.get('metadata_commit_ms'), batch_ms=latest.get('batch_ms'),
            byte_basis='logical original BLOB reads / repository payload submitted / verified payload reads; not physical disk or process I/O',
            gui_blocking_ms=None, progress_interval_s=1.0,
            counter_scope='current attempt; processed references may include resumed work; new_objects counts writes')
        self._progress_previous = counters
        self._progress_emitted = now
        if changed or force or now-self._progress_plain_emitted >= 5.0:
            logger.info('Storage progress [%s] stage=%s elapsed_ms=%.1f processed=%s '
                        'logical_read_B=%s object_B=%s verify_B=%s '
                        'logical_read_Bps=%s object_Bps=%s verify_Bps=%s',
                        self.operation_id, phase, (now-self._progress_started)*1000,
                        latest.get('processed'), counters['source_read_bytes'],
                        counters['target_payload_bytes'], counters['verify_read_bytes'],
                        rates['source_read_bytes_per_s'], rates['target_payload_bytes_per_s'],
                        rates['verify_read_bytes_per_s'])
            self._progress_plain_emitted = now
        if force and phase is not None:
            self._trace('stage_end', priority='terminal', stage=phase,
                        duration_ms=(now-self._progress_phase_started)*1000)
        self.progress.emit(dict(latest))
    def _trace(self,phase,**fields):
        try:
            from prism.performance_trace import record
            record('storage.workflow',phase=phase,operation_id=self.operation_id,
                   project_id=self.project_id, open_epoch=self.open_epoch,
                   operation=self.title,io_bytes=None,io_bytes_basis='not_measured',**fields)
        except Exception:
            pass  # Diagnostics must never skip the storage operation or its UI result.


def run(view,title,job,done=None,*,with_progress=False,nonmodal=False,failed=None,
        progress_changed=None):
    if getattr(view,'_storage_operation',None) is not None or getattr(view,'_save_operation',None) is not None:
        view.parent.statusBar().showMessage('请等待当前工程操作完成',6000);return False
    operation=_Operation(view,job,title,with_progress);view._storage_operation=operation
    progress = None
    if progress_changed is None:
        progress=QtWidgets.QProgressDialog(title,'取消',0,0,view)
        progress.setWindowModality(QtCore.Qt.WindowModality.NonModal if nonmodal
                                  else QtCore.Qt.WindowModality.WindowModal)
        progress.setMinimumDuration(0);progress.show()
        progress.canceled.connect(operation.requestInterruption)
    def update_progress(value):
        phase=value.get('phase')
        text={'metadata':'正在复制工程数据…','objects':'正在复制并校验原件…',
              'verification':'正在核对全部数据及引用…',
              'eagle_scan':'正在读取 Eagle 资料库并校验原件…',
              'eagle_copy':'正在转换 Eagle 图片和视频…'}.get(phase,title)
        if phase in ('eagle_scan', 'eagle_copy'):
            text += f" {value.get('processed',0)}"
            if value.get('total') is not None:
                text += f" / {value['total']}"
        if phase=='objects':text+=f" 已处理 {value.get('copied_this_run',0)} 项"
        if progress is not None:
            progress.setLabelText(text)
        else:
            progress_changed(value)
    operation.progress.connect(update_progress)
    result_box=[]
    operation.completed.connect(lambda result,error:result_box.append((result,error)))
    def complete():
        result,error=result_box[0] if result_box else (None,'操作结束但未返回结果')
        if progress is not None:
            progress.close()
        view._storage_operation=None
        if error:
            if failed:failed(error)
            else:QtWidgets.QMessageBox.warning(view,title,error)
        else:
            view.parent.statusBar().showMessage(title+'完成',8000)
            if done:done(result)
    operation.finished.connect(complete)
    operation.finished.connect(operation.deleteLater)
    operation.start()
    return True


def _open_result(view,path,readonly=False):
    if view.is_dirty() and not view.get_confirmation_unsaved_changes('当前工程有未保存的修改，仍要打开新工程吗？'):
        return
    view.open_from_file(str(path),readonly=readonly)


def upgrade_on_open(view,source,*,readonly=False,_check_mapping=True):
    """Mandatory upgrade creates a verified copy; Cancel cancels opening."""
    from prism import upgrade_mapping
    settings = getattr(view, 'settings', None)
    saved = upgrade_mapping.hint(settings, source) if _check_mapping else None
    if saved is not None:
        epoch = getattr(view, '_trace_open_epoch', None)
        project = getattr(getattr(view, 'scene', None), 'project_id', None)
        # The settings record is only a hint. Resolve its published entry and
        # check source/WAL and storage-instance binding off the GUI first.
        def checked(target):
            if (getattr(view, '_trace_open_epoch', None) != epoch
                    or getattr(getattr(view, 'scene', None), 'project_id', None) != project):
                return  # The user replaced the project while validation ran.
            if target is None:
                upgrade_on_open(view, source, readonly=readonly, _check_mapping=False)
                return
            answer = QtWidgets.QMessageBox.question(view, '已转换的工程',
                '此旧文件已有转换完成的新版工程。打开新版可保留之后在新版中的修改。\n'
                '是否打开已有新版？选择“否”可另选文件夹重新转换；取消则不打开。',
                QtWidgets.QMessageBox.StandardButton.Yes | QtWidgets.QMessageBox.StandardButton.No
                | QtWidgets.QMessageBox.StandardButton.Cancel,
                QtWidgets.QMessageBox.StandardButton.Yes)
            if answer == QtWidgets.QMessageBox.StandardButton.Yes:
                # A modal confirmation can run other events. Revalidate after
                # it closes, rather than trusting the pre-dialog identity.
                def confirmed(current_target):
                    if (getattr(view, '_trace_open_epoch', None) != epoch
                            or getattr(getattr(view, 'scene', None), 'project_id', None) != project):
                        return
                    if current_target == target:
                        _open_result(view, target, readonly)
                    else:
                        QtWidgets.QMessageBox.warning(view, '工程已变化',
                            '旧文件或新版工程在确认期间发生变化，未打开。请重新选择工程。')
                run(view, '确认已转换工程', lambda cancel: upgrade_mapping.validate(source, saved), confirmed)
            elif answer == QtWidgets.QMessageBox.StandardButton.No:
                upgrade_on_open(view, source, readonly=readonly, _check_mapping=False)
        return run(view, '核对已转换工程', lambda cancel: upgrade_mapping.validate(source, saved), checked)
    answer=QtWidgets.QMessageBox.question(view,'升级旧版工程',
        '此工程使用旧版单文件存储，需要升级。\n'
        '请选择新工程的保存位置。原文件保持不变，所有原件按原始字节复制，'
        '先只读浏览工程资源，后台转换并验证后再切换新版。\n'
        '转换期间可切换画布、文档、脑图和素材进行只读浏览；暂不可编辑或保存。'
        '失败或取消不会打开不完整工程。',
        QtWidgets.QMessageBox.StandardButton.Ok|QtWidgets.QMessageBox.StandardButton.Cancel,
        QtWidgets.QMessageBox.StandardButton.Cancel)
    if answer!=QtWidgets.QMessageBox.StandardButton.Ok:return False
    source=Path(source).resolve()
    folder=QtWidgets.QFileDialog.getExistingDirectory(view,'选择新工程保存文件夹（原文件保持不变）',
        str(source.parent))
    if not folder:return False
    target=str(Path(folder)/(source.stem+'-新版.prism'))
    if Path(target).resolve()==source or Path(target).exists():
        QtWidgets.QMessageBox.warning(view,'不能覆盖现有工程','请选择不存在的新工程名称，原工程不会被覆盖。')
        return False
    from prism.upgrade_preview import UpgradePreview
    UpgradePreview(view,source,target,readonly=readonly).start()
    return True


def convert_project(view):
    if not view.filename:
        QtWidgets.QMessageBox.information(view,'转换工程','请先打开需要转换的旧工程。');return
    if root_for(view.filename) is not None:
        QtWidgets.QMessageBox.information(view,'转换工程','当前已经是目录工程，无需再次转换。');return
    target,_=QtWidgets.QFileDialog.getSaveFileName(view,'转换当前工程（原文件保留）',filter='Prism 工程 (*.prism)')
    if not target:return
    from prism.storage.entrypoint import save_as_entry
    target = save_as_entry(target)
    if target.exists() or target.with_name(target.name+'.data').exists():
        QtWidgets.QMessageBox.warning(view, '无法创建工程入口', '该名称已存在，请选择新名称，现有文件不会被覆盖。')
        return
    # Use the same captured-save protocol as Save As. It preserves unloaded
    # rows, includes current edits and confirms/rebinds the live session only
    # after publication. A separate conversion thread skipped that protocol.
    view.do_save(str(target)+'.data',create_new=True)


def _saved_project(view,action):
    folder=root_for(view.filename)
    if folder is None:
        QtWidgets.QMessageBox.information(view,'目录工程','请先保存或转换为目录工程');return
    if view.is_dirty():
        QtWidgets.QMessageBox.information(view,'请先保存','请保存当前修改后再执行此操作');return
    action(folder)


def export_legacy_project(view):
    def action(folder):
        target,_=QtWidgets.QFileDialog.getSaveFileName(view,'导出兼容旧格式工程',filter='Prism (*.prism)')
        if target:
            from prism.storage.legacy_export import export_legacy
            run(view,'导出兼容工程',lambda cancel:export_legacy(folder,target,experimental=True,cancel=cancel))
    _saved_project(view,action)


def export_bundle(view):
    def action(folder):
        target,_=QtWidgets.QFileDialog.getSaveFileName(view,'导出便携工程包',filter='Prism 便携包 (*.prismbundle)')
        if target:
            from prism.storage.portable import export_bundle
            run(view,'导出便携工程包',lambda cancel:export_bundle(folder,target,cancel=cancel))
    _saved_project(view,action)


def open_bundle(view):
    source,_=QtWidgets.QFileDialog.getOpenFileName(view,'打开便携工程包',filter='Prism 便携包 (*.prismbundle)')
    if not source:return
    target,_=QtWidgets.QFileDialog.getSaveFileName(view,'解包为目录工程',filter='Prism 目录工程 (*.prismproj)')
    if target:
        from prism.storage.portable import open_bundle
        run(view,'打开便携工程包',lambda cancel:open_bundle(source,target,cancel=cancel),lambda path:_open_result(view,path))


def compact_project(view):
    def action(folder):
        from prism.storage.portable import compact
        run(view,'优化目录工程存储',lambda cancel:compact(folder,cancel=cancel))
    _saved_project(view,action)


def recover_project(view):
    source=QtWidgets.QFileDialog.getExistingDirectory(view,'恢复已提交数据：选择 .incomplete 或缺少入口的 .prism.data 文件夹')
    if source:
        from prism.storage.checkpoint import recover
        run(view,'验证并恢复目录工程',lambda cancel:recover(source,cancel=cancel),
            lambda path:_open_result(view,path))
