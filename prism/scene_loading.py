"""Install queued assets in short UI turns; finish only after the last batch."""
from PyQt6 import QtCore


class SceneLoadScheduler(QtCore.QObject):
    def __init__(self, view):
        super().__init__(view)
        self.scene = view.scene
        self.finished = None
        self.operation = None
        self.valid = None
        self.on_installed = None
        self.on_error = None
        self.timer = QtCore.QTimer(self)
        self.timer.setObjectName('scene.install_items')
        self.timer.setSingleShot(True)
        self.timer.timeout.connect(self.drain)

    def bind(self, operation, valid, on_installed, on_error):
        self.cancel()
        self.operation = operation
        self.valid = valid
        self.on_installed = on_installed
        self.on_error = on_error

    def schedule(self, *_):
        if self.valid is not None and not self.valid(self.operation):
            return
        if not self.timer.isActive():
            self.timer.start(16)

    def finish(self, callback):
        self.timer.stop()
        self.finished = callback
        self.drain()

    def cancel(self):
        self.timer.stop()
        self.finished = None
        self.operation = self.valid = self.on_installed = self.on_error = None

    def drain(self):
        if self.valid is not None and not self.valid(self.operation):
            self.cancel()
            return
        operation = self.operation
        try:
            self._install_batch()
        except Exception as error:
            handler = self.on_error
            self.cancel()
            if handler is None:
                raise
            handler(error)
            return
        # Public scene signals can cancel/reenter the operation.
        if self.operation is not operation:
            return
        if self.valid is not None and not self.valid(self.operation):
            self.cancel()
            return
        if not self.scene.items_to_add.empty():
            self.timer.start(16)
        elif self.finished is not None:
            callback, self.finished = self.finished, None
            self._complete(callback)

    def _install_batch(self):
        # Cooperative budget: a native item operation cannot be interrupted.
        self.scene.add_queued_items(max_items=128, time_budget_ms=6,
                                   on_installed=self.on_installed)

    def _complete(self, callback):
        callback()
