"""Stable tab/inspector hosts; heavy library widgets are built on first use."""
from PyQt6 import QtCore, QtWidgets
from types import GeneratorType


class InspectorHost(QtWidgets.QWidget):
    texture_requested = QtCore.pyqtSignal(str, str)
    material_apply_requested = QtCore.pyqtSignal()

    def __init__(self, library):
        super().__init__()
        self.library = library
        self.body = QtWidgets.QVBoxLayout(self)
        self.body.setContentsMargins(0, 0, 0, 0)

    def __getattr__(self, name):
        if name.startswith('__') or 'library' not in self.__dict__:
            raise AttributeError(name)
        return getattr(self.library.ensure_created().details, name)

    def __setattr__(self, name, value):
        library = self.__dict__.get('library')
        page = library.__dict__.get('_page') if library is not None else None
        if page is not None and name not in self.__dict__ and hasattr(page.details, name):
            setattr(page.details, name, value)
        else:
            super().__setattr__(name, value)


class DeferredLibrary(QtWidgets.QWidget):
    folder_requested = QtCore.pyqtSignal(object)
    create_folder_requested = QtCore.pyqtSignal(object)
    construction_finished = QtCore.pyqtSignal()

    def __init__(self, factory, parent=None, *, prepare=None):
        super().__init__(parent)
        self._factory = factory
        self._page = None
        self._construction = None
        self._prepare = prepare
        self._preparation = None
        self._prepared = False
        self._prepared_value = None
        self.details = InspectorHost(self)
        self._body = QtWidgets.QVBoxLayout(self)
        self._body.setContentsMargins(0, 0, 0, 0)

    @property
    def initialized(self):
        return self._page is not None

    @property
    def waiting_for_content(self):
        """Whether a readiness wait is in flight for this host."""
        return bool(self.__dict__.get('_waiting_for_content'))

    def is_content_ready(self):
        """Whether the hosted page can already show content.

        The library panels are built on first use, and a card surface needs
        its web document before it has anything to show. Switching to a panel
        that is not ready yet is what the user sees as a flash: the middle of
        the window is blank for a moment and the content arrives afterwards.
        """
        page = self._page
        if page is None:
            return False
        ready = getattr(page, 'is_content_ready', None)
        return True if ready is None else bool(ready())

    def wait_for_content(self, timeout_ms=2000):
        """Wait (bounded) until the hosted page can show content.

        The wait runs a nested event loop with a deadline, the same shape as
        the editor snapshot read: the page reports readiness through the event
        loop, and the deadline ends the wait instead of blocking the app.
        Returns whether the page is ready when the wait ends.
        """
        if self.is_content_ready():
            return True
        if self.__dict__.get('_waiting_for_content'):
            # A nested wait is already running: the outer one owns the switch.
            return self.is_content_ready()
        page = self._page
        if page is None:
            return False
        signal = getattr(page, 'content_ready', None)
        if signal is None:
            # The page says it is not ready and has no way to announce it.
            return False
        loop = QtCore.QEventLoop()
        timer = QtCore.QTimer()
        timer.setSingleShot(True)
        timer.timeout.connect(loop.quit)

        def finished():
            loop.quit()

        try:
            signal.connect(finished)
        except (TypeError, RuntimeError):
            # The page is gone: there is nothing left to wait for.
            return False
        timer.start(timeout_ms)
        self._waiting_for_content = True
        try:
            loop.exec()
        finally:
            self._waiting_for_content = False
            timer.stop()
            try:
                signal.disconnect(finished)
            except (TypeError, RuntimeError):
                pass
        return self.is_content_ready()

    def set_service(self, service):
        """Give the hosted page its service, now or as soon as it exists.

        This used to travel through ``__getattr__``, which built the panel
        at the caller's convenience. The service arrives from the model
        composition's worker callback, so building the panel there froze
        the window for a second and a half at a moment the user never
        asked for. The service is kept instead, and handed over the moment
        the panel exists.
        """
        if self._page is None:
            self.__dict__['_pending_service'] = service
            return
        self._page.set_service(service)

    def prewarm(self):
        """Start bounded GUI slices; no widget work runs in this call."""
        if self._page is not None:
            return None
        if self._construction is not None and not self._construction.done:
            return self._construction
        from .widget_construction import WidgetConstruction
        job = WidgetConstruction(self._construction_steps(), self)
        self._construction = job
        job.finished.connect(self._construction_ready)
        job.failed.connect(self._construction_failed)
        if self._prepare is not None and not self._prepared:
            from .widget_construction import LibraryPreparation
            self._preparation = LibraryPreparation(self._prepare, self._preparation_ready, self)
            QtCore.QThreadPool.globalInstance().start(self._preparation)
        else:
            job.start()
        return job

    def _preparation_ready(self, outcome):
        worker, value, error = outcome
        if worker is not self._preparation:
            return
        self._preparation = None
        if self._construction.done:
            return
        if error is not None:
            self._construction.reject(error)
            return
        self._prepared_value = value
        self._prepared = True
        self._construction.start()

    def _construction_steps(self):
        page = None
        complete = False
        try:
            build = self._factory(self._prepared_value) if self._prepare is not None else self._factory()
            page = (yield from build) if isinstance(build, GeneratorType) else build
            prepare = getattr(page, 'prepare_surface_steps', None)
            if prepare is not None:
                yield from prepare()
            yield
            page._tab_host = self
            page._preserve_editor_on_show = self.__dict__.get('_preserve_editor_on_show', False)
            # Factories normally supply these parents. Keep the fallback in
            # the same final ownership before polishing too, rather than
            # reparenting a complete inspector on its first visible show.
            if page.parentWidget() is not self:
                page.setParent(self)
            if page.details.parentWidget() is not self.details:
                page.details.setParent(self.details)
            page.hide()
            # The page is **not** attached to the tab's layout here.  Attaching
            # invalidates the layout of the host, and the host sits in the
            # window's tab bar: every attachment during the pre-warm made the
            # **window** repaint itself -- canvas and side panels included --
            # in the middle of the open the user just asked for.  That is the
            # flash.  The page is attached when the tab is shown, where one
            # repaint is the expected price of switching pages.
            yield
            # The construction batches already polished their new widgets
            # before yielding. Resolve the remaining layout constraints in
            # slices too, in final parents, without showing either panel.
            for root in (page, page.details):
                for widget in reversed([root] + root.findChildren(QtWidgets.QWidget)):
                    layout = widget.layout()
                    if layout is not None:
                        layout.activate()
                    yield
            for name in ('texture_requested', 'material_apply_requested'):
                signal = getattr(page.details, name, None)
                if signal is not None:
                    signal.connect(getattr(self.details, name).emit)
            for name in ('folder_requested', 'create_folder_requested'):
                signal = getattr(page, name, None)
                if signal is not None:
                    signal.connect(getattr(self, name).emit)
            yield
            # The latest service wins if projects changed during assembly.
            pending = self.__dict__.get('_pending_service')
            if pending is not None:
                setter = getattr(page, 'set_service', None)
                if setter is not None:
                    setter(pending)
                self.__dict__.pop('_pending_service', None)
            complete = True
            return page
        finally:
            if page is not None and not complete:
                from .widget_construction import discard_widget
                discard_widget(page)

    def _attach_page(self, page):
        """Put the built page into the tab and its inspector into the dock.

        This is where the page joins the visible layout, so it happens when
        the tab is shown -- not while the library is still being built in the
        background.  Attaching invalidates the layout of the host, and the
        host sits in the window's tab bar: during the pre-warm that made the
        **window** repaint itself, canvas and side panels included, in the
        middle of the open the user just asked for.
        """
        if self.__dict__.get('_page_attached'):
            return
        self.__dict__['_page_attached'] = True
        from prism.performance_trace import operation
        with operation('library.attach_page', library=page.objectName()):
            self._body.addWidget(page)
        with operation('library.attach_inspector', library=page.objectName()):
            self.details.body.addWidget(page.details)

    def _construction_ready(self, page):
        self._page = page
        if self.isVisible():
            self._attach_page(page)
            page.show()
        from prism.performance_trace import record
        record('library_panel_prewarmed', library=page.objectName(),
               max_slice_ms=round(self._construction.max_slice_ms, 3),
               slices=self._construction.slices)
        self.construction_finished.emit()

    def _construction_failed(self, error):
        import logging
        logging.getLogger(__name__).error('Library construction failed',
            exc_info=(type(error), error, error.__traceback__))

    def ensure_created(self):
        if self._page is None:
            job = self.prewarm()
            if job.advancing:
                raise RuntimeError('A library cannot access its unfinished construction')
            if not job.done:
                # Somebody is waiting for this panel right now: the paced
                # pre-warm yields to the user, but a click must not wait for
                # the whole paced schedule to run out.
                job.expedite()
                loop = QtCore.QEventLoop()
                signals = (job.finished, job.failed, self.destroyed)
                for signal in signals:
                    signal.connect(loop.quit)
                try:
                    loop.exec()
                finally:
                    for signal in signals:
                        try:
                            signal.disconnect(loop.quit)
                        except (RuntimeError, TypeError):
                            pass
            if job.cancel_requested:
                raise RuntimeError('Library construction was cancelled with its owner')
            if job.error is not None:
                raise job.error
        return self._page

    def showEvent(self, event):
        page = self.ensure_created()
        self._attach_page(page)
        from prism.performance_trace import operation
        with operation('library.first_show' if not self.__dict__.get('_shown_once') else 'library.show',
                       library=page.objectName()):
            page.show()
        self.__dict__['_shown_once'] = True
        super().showEvent(event)

    def hideEvent(self, event):
        if self._page is not None:
            self._page.hideEvent(event)
        super().hideEvent(event)

    def store_to_scene(self):
        return True if self._page is None else self._page.store_to_scene()

    def _has_draft(self):
        return False if self._page is None else self._page._has_draft()

    def has_pending_changes(self):
        return False if self._page is None else self._page.has_pending_changes()

    def __setattr__(self, name, value):
        page = self.__dict__.get('_page')
        if page is not None and name == '_preserve_editor_on_show':
            setattr(page, name, value)
            return
        if page is not None and name not in self.__dict__ and hasattr(page, name):
            setattr(page, name, value)
        else:
            super().__setattr__(name, value)

    def __getattr__(self, name):
        if name.startswith('__') or '_factory' not in self.__dict__:
            raise AttributeError(name)
        return getattr(self.ensure_created(), name)
