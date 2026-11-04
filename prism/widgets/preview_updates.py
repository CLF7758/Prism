"""Shared latest-intent preview throttle; durable commits never enter this queue."""
from PyQt6 import QtCore


class PreviewUpdates(QtCore.QObject):
    timeout = QtCore.pyqtSignal()
    ready = QtCore.pyqtSignal(object)

    def __init__(self, parent=None, interval=33):
        super().__init__(parent)
        self._timer = QtCore.QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.setInterval(interval)
        self._timer.timeout.connect(self._dispatch)
        self._latest = None

    def setSingleShot(self, enabled):
        if not enabled:
            raise ValueError('Preview updates must be single-shot')

    def setInterval(self, interval):
        self._timer.setInterval(interval)

    def isActive(self):
        return self._timer.isActive()

    def start(self, interval=None):
        # More input replaces intent, not the scheduled deadline. A continuous
        # drag therefore keeps rendering, rather than starving until release.
        if not self._timer.isActive():
            self._timer.start(self._timer.interval() if interval is None else interval)

    def submit(self, intent):
        self._latest = intent
        self.start()

    def stop(self):
        self._timer.stop()
        self._latest = None

    def flush(self):
        if self._timer.isActive():
            self._timer.stop()
            self._dispatch()

    def _dispatch(self):
        intent, self._latest = self._latest, None
        if intent is not None:
            self.ready.emit(intent)
        self.timeout.emit()
