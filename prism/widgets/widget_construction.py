"""Cooperative GUI construction; widgets never leave their owning thread."""
import time

from PyQt6 import QtCore


def finish_construction(steps):
    """Retain the ordinary synchronous constructor for direct callers."""
    for _ in steps:
        pass


def widget_steps(widget_type, *args, **kwargs):
    """Build through the same initializer, publishing only a complete widget."""
    widget = widget_type.__new__(widget_type)
    try:
        steps = widget._construct(*args, **kwargs)
        delegated = None
        try:
            while True:
                try:
                    next(steps)
                except StopIteration:
                    _polish_constructed(widget)
                    break
                # Qt posts a PolishRequest for every newly created widget. If
                # we return to the event loop first, those requests can polish
                # a whole unfinished inspector outside the slice budget.
                # Charge each small batch before yielding, not on first show.
                nested = steps.gi_yieldfrom
                # A nested widget_steps owns each of its own batches. Until
                # that delegation changes, this constructor has not added
                # more widgets: do not rescan every ancestor's whole tree.
                if nested is None or nested is not delegated:
                    _polish_constructed(widget)
                delegated = nested
                yield
        finally:
            steps.close()
        return widget
    except BaseException:
        discard_widget(widget)
        raise


def _polish_constructed(widget):
    from PyQt6 import QtWidgets
    roots = [widget]
    details = widget.__dict__.get('details')
    if isinstance(details, QtWidgets.QWidget) and not widget.isAncestorOf(details):
        roots.append(details)
    for root in roots:
        # Leaves first prevents ensurePolished(parent) from recursively doing
        # all of its new descendants in an indivisible native call.
        for child in reversed([root] + root.findChildren(QtWidgets.QWidget)):
            if not child.testAttribute(QtCore.Qt.WidgetAttribute.WA_WState_Polished):
                child.ensurePolished()


def discard_widget(widget):
    """A library's sibling inspector is logically owned by its page too."""
    for owned in (widget.__dict__.get('details'), widget):
        if isinstance(owned, QtCore.QObject):
            try:
                owned.deleteLater()
            except RuntimeError:  # Not initialized yet, or already destroyed.
                pass


class _PreparationSignals(QtCore.QObject):
    finished = QtCore.pyqtSignal(object)


class LibraryPreparation(QtCore.QRunnable):
    """Load Python definitions only; this callback must not create QObjects."""
    def __init__(self, prepare, callback, parent):
        super().__init__()
        self.prepare = prepare
        self.signals = _PreparationSignals(parent)
        self.signals.finished.connect(callback)

    def run(self):
        try:
            result, error = self.prepare(), None
        except Exception as failure:
            result, error = None, failure
        try:
            self.signals.finished.emit((self, result, error))
        except RuntimeError:  # Owning window was closed during the import.
            pass


class WidgetConstruction(QtCore.QObject):
    # This is a cooperative budget, not a deadline for an indivisible Qt call.
    # Record actual maxima so a slow native initialization is never hidden.
    finished = QtCore.pyqtSignal(object)
    failed = QtCore.pyqtSignal(object)
    SLICE_SECONDS = .004
    #: A step slower than this is reported with its source line: a single
    #: step cannot be cut by the slice budget, so a slow one is exactly what
    #: holds the window.  Real sessions showed steps of 45-118 ms.
    SLOW_STEP_MS = 16
    #: How many slow steps are remembered for the report.
    SLOW_STEP_COUNT = 8
    #: Gap between two slices.  The slices used to be 1 ms apart, so the
    #: window stayed busy for the whole run: on the real project the library
    #: pre-warm kept the GUI thread occupied for 1.4 s inside a 1.8 s window,
    #: which is what the user reported as a freeze.  The gap gives the event
    #: loop room to deliver input and repaint between the slices.
    SLICE_GAP_MS = 4
    EXPEDITED_GAP_MS = 1
    #: The person at the keyboard has the window: the panel waits for them.
    YIELD_SECONDS = .35

    def __init__(self, steps, parent):
        super().__init__(parent)
        self.steps = steps
        self.result = None
        self.error = None
        self.done = False
        self.advancing = False
        self.cancel_requested = False
        self.expedited = False
        self.slices = 0
        self.max_slice_ms = 0.
        self.max_step_ms = 0.
        self.slow_steps = []
        self.timer = QtCore.QTimer(self)
        self.timer.setSingleShot(True)
        self.timer.timeout.connect(self._advance)
        parent.destroyed.connect(self.cancel)

    def start(self):
        self.timer.start(0)

    def expedite(self):
        """Remove idle pacing for a waiting caller, while retaining slices."""
        self.expedited = True
        if not self.advancing and self.timer.isActive():
            self.timer.start(0)

    def reject(self, error):
        self.timer.stop()
        self.steps.close()
        self.error = error
        self.done = True
        self.failed.emit(error)

    def cancel(self):
        self.timer.stop()
        self.cancel_requested = True
        if not self.done and not self.advancing:
            self.steps.close()
            self.done = True

    def _note_step(self, elapsed_ms):
        """Remember the slow steps, and where they were.

        The slice budget can only be enforced *between* steps, so a single
        slow step is exactly what holds the window.  The source line says
        which part of the panel construction has to be split.  Real sessions
        showed steps of 45-118 ms, and the window must never be held longer
        than the limit the contract sets.
        """
        if elapsed_ms < self.SLOW_STEP_MS:
            return
        self.slow_steps.append((round(elapsed_ms, 3), self._step_location()))
        if len(self.slow_steps) > self.SLOW_STEP_COUNT:
            self.slow_steps.sort(key=lambda item: item[0], reverse=True)
            del self.slow_steps[self.SLOW_STEP_COUNT:]

    def _step_location(self):
        """The source line of the generator that ran the step.

        ``widget_steps`` drives the panel's own generator, and that
        generator is a local of its frame.  The chain of ``yield from``
        delegations is walked so the innermost generator -- the one that
        actually did the work -- is named.
        """
        frame = getattr(self.steps, 'gi_frame', None)
        generator = frame.f_locals.get('steps') if frame is not None else None
        location = ''
        while generator is not None:
            inner = getattr(generator, 'gi_frame', None)
            if inner is not None:
                location = '%s:%d' % (inner.f_code.co_filename, inner.f_lineno)
            generator = getattr(generator, 'gi_yieldfrom', None)
        return location

    def _advance(self):
        from prism.performance_trace import operation, record
        from prism.user_activity import USER_ACTIVITY
        quiet_remaining = self.YIELD_SECONDS - USER_ACTIVITY.seconds_since_input()
        if not self.expedited and quiet_remaining > 0:
            # The user is working: the window is theirs, the panel waits.
            self.timer.start(max(self.SLICE_GAP_MS, int(quiet_remaining * 1000) + 1))
            return
        started = time.perf_counter()
        self.advancing = True
        completed = False
        try:
            with operation('page.library.construct_slice'):
                while True:
                    step_started = time.perf_counter()
                    try:
                        next(self.steps)
                    except StopIteration as result:
                        self.result = result.value
                        self.done = completed = True
                        break
                    finally:
                        elapsed_ms = (time.perf_counter() - step_started) * 1000
                        self.max_step_ms = max(self.max_step_ms, elapsed_ms)
                        self._note_step(elapsed_ms)
                    if self.cancel_requested:
                        self.steps.close()
                        self.done = True
                        return
                    if time.perf_counter() - started >= self.SLICE_SECONDS:
                        break
        except Exception as error:
            self.error = error
            self.done = True
        finally:
            self.advancing = False
            self.slices += 1
            self.max_slice_ms = max(self.max_slice_ms, (time.perf_counter() - started) * 1000)
        if self.done:
            record('library_construction_finished', slices=self.slices,
                   max_slice_ms=round(self.max_slice_ms, 3),
                   max_step_ms=round(self.max_step_ms, 3),
                   slow_steps=self.slow_steps, success=completed)
            if completed:
                self.finished.emit(self.result)
            else:
                self.failed.emit(self.error)
        else:
            # A timer turn gives queued input/paint events a chance to run.
            self.timer.start(self.EXPEDITED_GAP_MS if self.expedited else self.SLICE_GAP_MS)
