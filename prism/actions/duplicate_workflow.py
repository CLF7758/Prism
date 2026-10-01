"""Duplicate-scan action adapter."""
from PyQt6 import QtCore, QtWidgets

from prism import fileio, widgets
from prism.duplicate_scan import DuplicateScanService, ScanScope, collect_sources
from prism.i18n import _


def find_duplicates(view, scope=None):
    """Collect an explicit scope and run the duplicate service off the UI."""
    chosen_scope = scope or ScanScope.ALL_CANVASES

    # **先弹窗，再收集。** 收集那一步要在 GUI 线程逐个读素材的字节
    # （`ImageSource.from_item` 要用 QImage，不能挪到 worker 线程），
    # 用户的工程有 1563 个素材。以前是先收完再建对话框，用户看到的就是
    # "点查重 → 卡住几秒 → 窗口才出来"，像没反应。现在先把对话框画出来，
    # 收集过程中刷进度。
    collect_progress = QtWidgets.QProgressDialog(
        _('Reading the photos…'), '', 0, 100, view)
    collect_progress.setWindowTitle(_('Looking for duplicates'))
    collect_progress.setCancelButton(None)
    collect_progress.setMinimumDuration(0)
    collect_progress.setWindowModality(
        QtCore.Qt.WindowModality.WindowModal)
    collect_progress.setValue(0)
    collect_progress.show()
    QtWidgets.QApplication.processEvents()

    def report_progress(done, total):
        if total:
            collect_progress.setValue(int(done * 100 / total))
        QtWidgets.QApplication.processEvents()

    try:
        sources = collect_sources(
            view.scene, chosen_scope,
            current_canvas_id=getattr(view, 'current_canvas_id', None),
            selected=set(view.scene.selectedItems(user_only=True)),
            progress=report_progress)
    finally:
        collect_progress.close()

    if len(sources) < 2:
        QtWidgets.QMessageBox.information(
            view, _('Duplicate Photos'), _('At least two photos are required.'))
        return
    if getattr(view, '_duplicate_service', None) is None:
        view._duplicate_service = DuplicateScanService(view)
        view._duplicate_service.finished.connect(view._on_duplicates_scanned)
        view._duplicate_service.failed.connect(view._on_duplicates_failed)
    service = view._duplicate_service

    def run_scan(worker=None):
        # ThreadedIO owns the progress dialog, while the service owns the
        # actual scan.  Bridge the two explicitly: otherwise the dialog
        # cannot show a useful maximum and its Cancel button only flips the
        # ThreadedIO flag, leaving the scan itself running.
        def relay_progress(done, total):
            if worker is not None:
                if worker.canceled:
                    service.cancel()
                worker.progress.emit(done + 1)

        service.progress.connect(relay_progress)
        try:
            if worker is not None:
                worker.begin_processing.emit(len(sources))
                if worker.canceled:
                    service.cancel()
            service.scan(sources, chosen_scope)
        finally:
            service.progress.disconnect(relay_progress)
            # PrismProgressDialog is attached to ThreadedIO's custom
            # finished signal.  Returning from QThread.run() does not emit
            # that signal, so emit it here to close and dispose the dialog.
            if worker is not None:
                worker.finished.emit('', [])

    view.worker = fileio.ThreadedIO(run_scan)
    view._duplicate_progress = widgets.PrismProgressDialog(
        _('Looking for duplicates'), worker=view.worker, parent=view)
    view._duplicate_progress.canceled.connect(service.cancel)
    view.worker.start()
