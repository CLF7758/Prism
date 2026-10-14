"""Install queued assets in short UI turns; finish only after the last batch."""
from PyQt6 import QtCore


class SceneLoadScheduler(QtCore.QObject):
    def __init__(self, view):
        super().__init__(view)
        self.scene = view.scene
        self.finished = None
        self.timer = QtCore.QTimer(self)
        self.timer.setSingleShot(True)
        self.timer.timeout.connect(self.drain)

    def schedule(self, *_):
        if not self.timer.isActive():
            self.timer.start(16)

    def finish(self, callback):
        self.timer.stop()
        self.finished = callback
        self.drain()

    def cancel(self):
        self.timer.stop()
        self.finished = None

    def drain(self):
        self.scene.add_queued_items(max_items=128, time_budget_ms=6)
        if not self.scene.items_to_add.empty():
            self.timer.start(16)
        elif self.finished is not None:
            callback, self.finished = self.finished, None
            callback()
