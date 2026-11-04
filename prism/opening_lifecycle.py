"""Opening-only lifecycle; the public save/import ThreadedIO protocol is unchanged."""
from dataclasses import dataclass, field
import weakref

from PyQt6 import QtCore

from prism import fileio
from prism.performance_trace import restore_context


@dataclass(frozen=True)
class OpenCompletion:
    """GUI installation terminal, not a reader-finished or first-paint claim."""
    filename: str
    epoch: int
    project_id: object
    outcome: str


@dataclass(eq=False)
class OpenOperation:
    filename: str
    scene: object
    epoch: int
    context: object
    started: float
    content_revision: int
    undo_revision: int
    phase: str = 'scope'
    terminal: bool = False
    cancel_requested: bool = False
    clear_requested: bool = False
    preparation_pending: bool = False
    gui_pending: bool = False
    reader_stopped: bool = False
    reader_result: object = None
    reader: object = None
    bridge: object = None
    installed_ids: set = field(default_factory=set)
    source_stamp: object = None
    project_id: object = None
    error: object = None


class OpenReader(fileio.ThreadedIO):
    """Unexpected reader exceptions must also have a GUI result and a native end."""
    def run(self):
        try:
            super().run()
        except Exception as error:
            self.finished.emit(str(self.args[0]), [str(error)])


class OpenReaderBinding(QtCore.QObject):
    """All reader slots run on the GUI; dialog completion is GUI-install completion."""
    progress = QtCore.pyqtSignal(int)
    begin_processing = QtCore.pyqtSignal(int)
    finished = QtCore.pyqtSignal(str, list)
    user_input_required = QtCore.pyqtSignal(str)

    def __init__(self, view, operation, reader):
        super().__init__(view)
        self.owner = weakref.ref(view)
        self.operation = operation
        reader.progress.connect(self.on_progress)
        reader.begin_processing.connect(self.on_begin)
        reader.user_input_required.connect(self.on_user_input)
        reader.finished.connect(self.on_result)
        # ThreadedIO.finished(str,list) shadows the native no-argument signal.
        QtCore.QThread.finished.__get__(reader, type(reader)).connect(self.on_stopped)

    def current(self):
        view = self.owner()
        return view if view is not None and view._is_current_open(self.operation) else None

    @QtCore.pyqtSlot(int)
    def on_progress(self, value):
        view = self.current()
        if view is None or self.operation.cancel_requested:
            return
        with restore_context(self.operation.context):
            view._load_scheduler.schedule()
            self.progress.emit(value)

    @QtCore.pyqtSlot(int)
    def on_begin(self, value):
        if self.current() is not None and not self.operation.cancel_requested:
            self.begin_processing.emit(value)

    @QtCore.pyqtSlot(str)
    def on_user_input(self, message):
        if self.current() is not None and not self.operation.cancel_requested:
            self.user_input_required.emit(message)

    @QtCore.pyqtSlot(str, list)
    def on_result(self, filename, errors):
        if self.current() is not None:
            self.operation.reader_result = (filename, errors)

    @QtCore.pyqtSlot()
    def on_stopped(self):
        # Resource termination is recorded even for an obsolete operation;
        # the view validates ownership before changing any shared state.
        self.operation.reader_stopped = True
        view = self.current()
        if view is not None:
            with restore_context(self.operation.context):
                view._open_reader_stopped(self.operation)

    @QtCore.pyqtSlot()
    def on_canceled(self):
        view = self.current()
        if view is not None:
            view._cancel_open(self.operation)

    def complete_dialog(self):
        self.finished.emit(self.operation.filename, [])
