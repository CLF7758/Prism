"""Use Qt's owned thread pool for file IO and large texture processing."""
from PyQt6 import QtCore
import time
from prism.performance_trace import record


class TaskSignals(QtCore.QObject):
    result = QtCore.pyqtSignal(object)
    error = QtCore.pyqtSignal(str)
    finished = QtCore.pyqtSignal()


class MaterialTask(QtCore.QRunnable):
    def __init__(self, function, obsolete=None):
        super().__init__()
        self.function = function
        self.obsolete = obsolete
        self.signals = TaskSignals()
        self.task_id = None
        self.queued_at = time.perf_counter()

    def run(self):
        started = time.perf_counter()
        record('material_task_start', task=self.task_id,
               queue_ms=round((started-self.queued_at)*1000, 3))
        try:
            if self.obsolete is not None and self.obsolete():
                record('material_task_cancelled', task=self.task_id)
                return
            self.signals.result.emit(self.function())
        except Exception as error:
            self.signals.error.emit(str(error))
        finally:
            record('material_task_work', task=self.task_id,
                   duration_ms=round((time.perf_counter()-started)*1000, 3))
            self.signals.finished.emit()
