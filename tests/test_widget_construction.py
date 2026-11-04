import threading
import time

from PyQt6 import QtCore, QtWidgets, sip

from prism.widgets.deferred_library import DeferredLibrary
from prism.widgets.widget_construction import WidgetConstruction, finish_construction, widget_steps


class _Page(QtWidgets.QWidget):
    def __init__(self, seen, fail=False):
        finish_construction(self._construct(seen, fail))

    def _construct(self, seen, fail=False):
        super().__init__()
        self.details = QtWidgets.QWidget()
        seen.append(self)
        for index in range(12):
            QtWidgets.QLabel(str(index), self)
            yield
        if fail:
            raise ValueError('injected construction failure')
        self.complete = True
        self.services = []

    def set_service(self, service):
        self.services.append(service)


def test_prewarm_is_sliced_and_first_use_joins_the_same_job(qtbot, monkeypatch):
    monkeypatch.setattr(WidgetConstruction, 'SLICE_SECONDS', 0)
    seen, beats = [], []
    host = DeferredLibrary(lambda: widget_steps(_Page, seen))
    qtbot.addWidget(host)
    timer = QtCore.QTimer(host)
    timer.setInterval(0)
    timer.timeout.connect(lambda: beats.append(True))
    timer.start()
    job = host.prewarm()
    assert not seen and not host.initialized
    assert host.prewarm() is job
    host.set_service('old-project')
    QtCore.QTimer.singleShot(0, lambda: host.set_service('current-project'))
    page = host.ensure_created()
    timer.stop()
    assert len(seen) == 1 and page is seen[0] and page.complete
    assert len(beats) >= 12 and job.slices >= 12
    assert page.services == ['current-project']
    assert all(widget.thread() is host.thread() for widget in page.findChildren(QtWidgets.QWidget))


def test_only_preparation_runs_in_the_worker(qtbot):
    threads, seen = [], []
    gui = threading.get_ident()
    def prepare():
        threads.append(threading.get_ident())
        return _Page
    def factory(widget_type):
        threads.append(threading.get_ident())
        return widget_steps(widget_type, seen)
    host = DeferredLibrary(factory, prepare=prepare)
    qtbot.addWidget(host)
    assert host.ensure_created().complete
    assert threads[0] != gui and threads[1] == gui
    assert seen[0].thread() is host.thread()


def test_failed_construction_releases_partial_inspector_and_can_retry(qtbot):
    seen = []
    host = DeferredLibrary(lambda: widget_steps(_Page, seen, fail=not seen))
    qtbot.addWidget(host)
    job = host.prewarm()
    qtbot.waitUntil(lambda: job.done)
    assert isinstance(job.error, ValueError) and not host.initialized
    partial, inspector = seen[0], seen[0].details
    qtbot.waitUntil(lambda: sip.isdeleted(partial) and sip.isdeleted(inspector))
    assert host.ensure_created().complete
    assert len(seen) == 2


def test_destroying_host_cancels_partial_widgets(qtbot, monkeypatch):
    monkeypatch.setattr(WidgetConstruction, 'SLICE_SECONDS', 0)
    seen = []
    host = DeferredLibrary(lambda: widget_steps(_Page, seen))
    job = host.prewarm()
    qtbot.waitUntil(lambda: bool(seen))
    partial, inspector = seen[0], seen[0].details
    host.deleteLater()
    qtbot.waitUntil(lambda: sip.isdeleted(host))
    assert job.done
    qtbot.waitUntil(lambda: sip.isdeleted(partial) and sip.isdeleted(inspector))


def test_first_show_polish_is_completed_in_slices_without_showing_inspector(qtbot):
    """Expensive leaf polish must not be deferred to one recursive show call."""
    polished, beats = [], []

    class Leaf(QtWidgets.QLabel):
        def event(self, event):
            if event.type() == QtCore.QEvent.Type.Polish:
                # Stand in for a real font/style size-hint calculation. Each
                # individual call is cheap; the whole hidden tree is not.
                time.sleep(.002)
                polished.append(self)
            return super().event(event)

    class Page(QtWidgets.QWidget):
        def __init__(self):
            finish_construction(self._construct())

        def _construct(self):
            super().__init__()
            self.details = QtWidgets.QWidget()
            layout = QtWidgets.QVBoxLayout(self.details)
            for index in range(24):
                layout.addWidget(Leaf(str(index)))
                yield

    host = DeferredLibrary(lambda: widget_steps(Page))
    qtbot.addWidget(host)
    qtbot.addWidget(host.details)
    timer = QtCore.QTimer(host)
    timer.setInterval(1)
    timer.timeout.connect(lambda: beats.append(len(polished)))
    timer.start()
    page = host.ensure_created()
    timer.stop()
    assert len(polished) == 24
    assert not host.details.isVisible() and not page.details.isVisible()
    assert any(0 < count < 24 for count in beats)
    assert page.details.parentWidget() is host.details
    for widget in page.details.findChildren(QtWidgets.QWidget):
        assert widget.testAttribute(QtCore.Qt.WidgetAttribute.WA_WState_Polished)
    before = len(polished)
    host.details.show()
    qtbot.waitUntil(host.details.isVisible)
    assert len(polished) == before, 'first show must not repeat the prepared leaf polish'


def test_cancelling_widget_steps_closes_the_inner_construction(qtbot):
    cleanup = []

    class Page(QtWidgets.QWidget):
        def _construct(self):
            super().__init__()
            try:
                yield
                yield
            finally:
                cleanup.append('closed')

    steps = widget_steps(Page)
    next(steps)
    steps.close()
    assert cleanup == ['closed']
