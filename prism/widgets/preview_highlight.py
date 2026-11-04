"""Transient selection feedback, independent of the material being edited."""
import time
from PyQt6 import QtCore
from prism.material_preview_profile import preview_profile


class PreviewHighlight(QtCore.QObject):
    def __init__(self, apply, parent=None):
        super().__init__(parent)
        self._apply = apply
        self._active = False
        self.timer = QtCore.QTimer(self)
        self.timer.setSingleShot(True)
        self.timer.timeout.connect(self.clear)

    def show(self, binding_ids, deadline=None):
        maximum = preview_profile()['selectionDurationMs']
        remaining = maximum if deadline is None else min(maximum, int((deadline - time.monotonic()) * 1000))
        self.timer.stop()
        if not binding_ids or remaining <= 0:
            self.clear()
            return
        self._apply(tuple(binding_ids))
        self._active = True
        self.timer.start(remaining)

    def clear(self):
        self.timer.stop()
        if self._active:
            self._active = False
            self._apply(())
