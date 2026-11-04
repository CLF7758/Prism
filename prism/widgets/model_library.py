"""Model-library page. Reuses the shared cards; owns paging, selection and jobs.

The model library is a *bounded* view over a much larger store. Three rules
shape this module:

* **A state is named, never guessed.** The panel distinguishes "not
  connected", "loading", "empty library", "no match", "failed" and "ready".
  "Not connected" never reads as "import succeeded", and an unknown value is
  never shown as zero.
* **The page is a window, not the library.** Only a bounded number of
  summaries is installed at a time; the DOM and the image cache stay bounded
  no matter how many models the project holds.
* **The selection belongs to the user, not to the page.** Picks survive a page
  change, a filter change and a list refresh. Only a project switch clears
  them, because only then do the ids point at a different library.

The page never reads a model original and never creates an engine. Covers
come from the repository as cached images; the native runtime lives in its own
process (see :mod:`prism.widgets.model_preview`).
"""
import os
from collections import OrderedDict
from functools import lru_cache

from PyQt6 import QtCore, QtGui, QtWidgets
from PyQt6.QtCore import Qt

from prism.model_library.contracts import (
    MODEL_EXTENSIONS, ModelFeatureUnavailable, ModelQuery,
)
from prism.model_library.events import EVENT_UI_IMAGE_ACCEPTED, record_model_event
from prism.model_library.selection import ModelSelection, QueryIdentity
from prism.model_library.service import ModelLibraryService
from prism.widgets import components
from prism.widgets.material_cards import MaterialCards
from prism.widgets.model_details import ModelDetails

#: How many summaries one page holds. Four pages in flight stay under the
#: 300 visible/prefetched card budget.
PAGE_SIZE = 72
MAX_WINDOW_PAGES = 4

#: The search field waits this long before it issues a new query.
QUERY_DEBOUNCE_MS = 180

#: A run of appearance edits is submitted once, this long after the last one.
#: Long enough to swallow a slider drag, short enough to feel saved.
APPEARANCE_COMMIT_MS = 320

#: Scroll / layout / sort changes are written to the settings after this pause.
VIEW_STATE_DELAY_MS = 400


class ModelLibraryState:
    """The named states of the model page."""

    UNAVAILABLE = 'unavailable'
    LOADING = 'loading'
    EMPTY = 'empty'
    NO_RESULTS = 'no_results'
    ERROR = 'error'
    READY = 'ready'


class _QuerySignals(QtCore.QObject):
    result = QtCore.pyqtSignal(object)


class _ModelQuery(QtCore.QRunnable):
    """Run one summary query off the GUI thread.

    The worker only produces a bounded page of summaries. It never touches a
    widget and never reads an original file.
    """

    def __init__(self, service, query, identity, token, callback):
        super().__init__()
        self.service = service
        self.query = query
        self.identity = identity
        self.token = token
        # The carrier has no owner on purpose: it must outlive the page
        # that queued the work.  The page keeps the job alive until it
        # finished, and that is where the carrier is released -- on the GUI
        # thread, never on a worker.
        self.signals = _QuerySignals()
        self.signals.result.connect(callback)

    def run(self):
        try:
            page = self.service.query(self.query)
            outcome = (self, self.token, self.identity, page, None)
        except ModelFeatureUnavailable as error:
            outcome = (self, self.token, self.identity, None, str(error))
        except Exception as error:  # a broken repository must not kill the UI
            outcome = (self, self.token, self.identity, None, str(error))
        try:
            self.signals.result.emit(outcome)
        except RuntimeError:
            pass


class _ModelRecord(QtCore.QRunnable):
    """Read one metadata record using the page's existing pool and signal."""
    def __init__(self, service, getter, model_id, identity, token, callback):
        super().__init__()
        self.service, self.getter, self.model_id = service, getter, model_id
        self.identity, self.token = identity, token
        # The carrier has no owner on purpose; see ``_ModelQuery``.
        self.signals = _QuerySignals()
        self.signals.result.connect(callback)

    def run(self):
        try:
            record, error = self.getter(self.model_id), None
        except Exception as caught:
            record, error = None, str(caught)
        try:
            self.signals.result.emit((self, self.token, self.identity, record, error))
        except RuntimeError:
            pass


class ModelLibrary(QtWidgets.QWidget):
    """The model page: list, paging, selection and job feedback."""

    #: The user asked to import files. Payload is a tuple of paths.
    import_requested = QtCore.pyqtSignal(object)
    #: The user asked to export originals. Payload is a SelectionSnapshot.
    export_requested = QtCore.pyqtSignal(object)
    #: The user asked to open the large preview. Payload is a model id.
    preview_requested = QtCore.pyqtSignal(str)
    #: A cover or the details of one model changed.
    record_changed = QtCore.pyqtSignal(str)
    #: The selection changed. Payload is the selection controller.
    selection_changed = QtCore.pyqtSignal(object)

    def __init__(self, window, service=None, *, details_parent=None, parent=None):
        from .widget_construction import finish_construction
        finish_construction(self._construct(window, service, details_parent=details_parent,
                                            parent=parent))

    def _construct(self, window, service=None, *, details_parent=None, parent=None):
        from .widget_construction import widget_steps
        # The page belongs to the tab that hosts it, not to the window: see
        # the same note in ``MaterialLibrary._construct``.
        super().__init__(window if parent is None else parent)
        self.window_host = window
        self.service = service or ModelLibraryService()
        self.selected_id = None
        self._summaries = {}
        self._project_id = None
        self._scope_token = None
        self._project_session = None
        self._open_epoch = 0
        self._identity = QueryIdentity()
        self._revision = 0
        self._pages = OrderedDict()
        self._first_page = 0
        self._total = 0
        self._pending_pages = set()
        self._pool = None
        self._workers = {}
        self._record_worker = None
        self._state = ModelLibraryState.UNAVAILABLE
        self._jobs = {}
        self.selection = ModelSelection()
        self.setObjectName('modelLibrary')
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setAcceptDrops(True)
        self.details = yield from widget_steps(ModelDetails, details_parent, legacy_slots=False)
        self.details.rename_requested.connect(self.rename_model)
        self.details.open_requested.connect(self._open_preview)
        self.details.export_requested.connect(self._export_selection)
        self.details.appearance_changed.connect(self._commit_appearance)
        self.details.metadata_changed.connect(self._save_metadata)
        layout = QtWidgets.QVBoxLayout(self)
        layout.setContentsMargins(24, 20, 24, 20)
        layout.setSpacing(16)
        self.search = QtWidgets.QLineEdit()
        self.search.setPlaceholderText('按名称、标签或备注查找模型')
        self.search.setClearButtonEnabled(True)
        self._search_timer = QtCore.QTimer(self)
        self._search_timer.setSingleShot(True)
        self._search_timer.setInterval(QUERY_DEBOUNCE_MS)
        self._search_timer.timeout.connect(self._apply_search)
        self.search.textChanged.connect(self._search_timer.start)
        self._pending_appearance = None
        self._appearance_timer = QtCore.QTimer(self)
        self._appearance_timer.setSingleShot(True)
        self._appearance_timer.setInterval(APPEARANCE_COMMIT_MS)
        self._appearance_timer.timeout.connect(self._submit_appearance)
        # Scroll, layout and card size are remembered per project+folder. The
        # timer keeps a fast scroll from writing the settings every pixel.
        self._restore_scroll = None
        self._restore_selection = None
        self._view_state_timer = QtCore.QTimer(self)
        self._view_state_timer.setSingleShot(True)
        self._view_state_timer.setInterval(VIEW_STATE_DELAY_MS)
        self._view_state_timer.timeout.connect(self._save_view_state)
        layout.addWidget(self.search)
        tools = QtWidgets.QHBoxLayout()
        view_tools = tools
        self.scope_label = QtWidgets.QLabel('全部模型 · 数据服务未接入')
        self.scope_label.setProperty('uiRole', 'secondary')
        tools.addWidget(self.scope_label, 1)
        self.sort = QtWidgets.QComboBox()
        for label, value in (('名称 ↑', 'name'), ('名称 ↓', '-name'),
                             ('格式 ↑', 'format'), ('格式 ↓', '-format'),
                             ('最近更新 ↑', '-updated_at'), ('最近更新 ↓', 'updated_at')):
            self.sort.addItem(label, value)
        self.sort.currentIndexChanged.connect(self._apply_sort)
        self.sort.currentIndexChanged.connect(self._schedule_view_state)
        self.sort.setParent(self)
        self.sort.hide()
        self.import_button = components.Button('导入模型')
        self.folder_button = components.Button('新建文件夹')
        self.export_button = components.Button('导出模型与贴图')
        self.cancel_button = components.Button('取消任务')
        self.cancel_button.setEnabled(False)
        self.cancel_button.clicked.connect(self._cancel_jobs)
        self.cancel_button.setParent(self)
        self.cancel_button.hide()
        for button in (self.import_button, self.folder_button, self.export_button):
            button.setEnabled(False)
            button.setToolTip('功能尚未接入；不会创建或修改项目数据')
            if button is self.export_button:
                button.setParent(self)
                button.hide()
            else:
                tools.addWidget(button)
        self.import_button.clicked.connect(self._choose_import)
        self.folder_button.clicked.connect(self.create_folder)
        self.export_button.clicked.connect(self._export_selection)
        self.grid_button = QtWidgets.QToolButton()
        self.grid_button.setText('网格')
        self.list_button = QtWidgets.QToolButton()
        self.list_button.setText('列表')
        self._display_modes = QtWidgets.QButtonGroup(self)
        for button in (self.grid_button, self.list_button):
            button.setCheckable(True)
            self._display_modes.addButton(button)
            button.clicked.connect(self._set_layout)
            button.clicked.connect(self._schedule_view_state)
            view_tools.addWidget(button)
        self.grid_button.setChecked(True)
        view_tools.addWidget(QtWidgets.QLabel('卡片大小'))
        self.card_size = QtWidgets.QSlider(Qt.Orientation.Horizontal)
        self.card_size.setRange(160, 320)
        self.card_size.setValue(220)
        self.card_size.setMaximumWidth(90)
        self.card_size.valueChanged.connect(self._set_layout)
        self.card_size.valueChanged.connect(self._schedule_view_state)
        view_tools.addWidget(self.card_size)
        logs = components.Button('性能日志')
        logs.clicked.connect(self.open_logs)
        view_tools.addWidget(logs)
        layout.addLayout(tools)
        # Match the material library's single toolbar. Model-only actions
        # remain available from the scope label's context menu and cards.
        self.scope_label.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.scope_label.setToolTip('右键：排序、导出原件、取消任务')
        self.scope_label.customContextMenuRequested.connect(self._scope_menu)
        self.scope_label.setWordWrap(True)
        self.scope_label.setMinimumWidth(0)
        self.status = QtWidgets.QLabel('模型库框架 · 导入、预览与原件导出尚未接入')
        self.status.setProperty('uiRole', 'secondary')
        layout.addWidget(self.status)
        # The selection bar is separate from the status line: it must always
        # show how many models are selected, even while a job runs.
        self.selection_bar = QtWidgets.QWidget()
        selection_layout = QtWidgets.QHBoxLayout(self.selection_bar)
        selection_layout.setContentsMargins(0, 0, 0, 0)
        self.selection_label = QtWidgets.QLabel('未选择模型')
        self.selection_label.setProperty('uiRole', 'secondary')
        selection_layout.addWidget(self.selection_label, 1)
        self.clear_selection_button = components.Button('清除选择')
        self.clear_selection_button.setEnabled(False)
        self.clear_selection_button.clicked.connect(self._clear_selection)
        self.clear_selection_button.hide()
        # Selection can be cleared through the existing blank-area interaction.
        self.selection_bar.hide()
        layout.addWidget(self.selection_bar)
        # Lazy activation avoids starting a second Chromium surface at app startup.
        self._cards_host = QtWidgets.QWidget()
        self._cards_layout = QtWidgets.QVBoxLayout(self._cards_host)
        self._cards_layout.setContentsMargins(0, 0, 0, 0)
        self.cards = None
        layout.addWidget(self._cards_host, 1)
        self.empty_state = QtWidgets.QWidget()
        empty = QtWidgets.QVBoxLayout(self.empty_state)
        empty.addStretch()
        self.empty_title = QtWidgets.QLabel('模型库')
        self.empty_title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        empty.addWidget(self.empty_title)
        self.empty_hint = QtWidgets.QLabel(
            '导入模型后，这里显示模型当前材质效果的封面\n'
            'FBX · OBJ · GLB · glTF')
        self.empty_hint.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.empty_hint.setWordWrap(True)
        self.empty_hint.setProperty('uiRole', 'secondary')
        empty.addWidget(self.empty_hint)
        empty.addStretch(2)
        layout.addWidget(self.empty_state, 1)
        self._cards_host.hide()
        self.search.setEnabled(self.service.capabilities.browse)
        self._connect_host_scope()
        self._update_buttons()

    def is_content_ready(self):
        """Whether the page can already show something.

        The model panel always has content to show: until the first query
        answers it shows its own empty state, so there is no blank frame to
        wait for.
        """
        return True

    def _scope_menu(self, position):
        menu = QtWidgets.QMenu(self)
        sorting = menu.addMenu('排序')
        for index in range(self.sort.count()):
            action = sorting.addAction(self.sort.itemText(index))
            action.setCheckable(True)
            action.setChecked(index == self.sort.currentIndex())
            action.triggered.connect(
                lambda checked=False, index=index: self.sort.setCurrentIndex(index))
        for button in (self.export_button, self.cancel_button):
            action = menu.addAction(button.text())
            action.setEnabled(button.isEnabled())
            action.triggered.connect(button.click)
        menu.exec(self.scope_label.mapToGlobal(position))

    def _connect_host_scope(self):
        """Listen to the host's project-open completion.

        A project switch must invalidate the old page even when the model tab
        stays mounted: the covers, the record and the selection of the old
        project may not survive into the new one.
        """
        view = getattr(self.window_host, 'view', None)
        signal = getattr(view, 'project_open_finished', None)
        if signal is None:
            return
        try:
            signal.connect(self._on_project_opened)
        except (AttributeError, RuntimeError, TypeError):
            record_model_event('scope_hook_missing')

    # ------------------------------------------------------------------
    # Scope
    # ------------------------------------------------------------------
    def bind_scope(self, project_session, open_epoch, refresh=True):
        """Bind the project session and open epoch this page belongs to.

        The session/epoch pair is the real identity of "the project that is
        open right now". A different pair means a different library, so every
        pending result is dropped and the selection is cleared. A repeated
        bind with the same pair changes nothing.

        ``refresh=False`` invalidates the page without starting a query. The
        project-open hook uses it: a project switch must throw the old covers
        and the old record away immediately, but the new list is only read
        when the user actually opens the model page.
        """
        project_session = str(project_session or '')
        open_epoch = int(open_epoch or 0)
        token = f'{project_session}#{open_epoch}'
        if token == self._scope_token:
            return False
        self._scope_token = token
        self._transfer_worker = None
        self._reveal_model_id = None
        self._project_session = project_session
        self._open_epoch = open_epoch
        self._revision += 1
        # The query identity belongs to the project that just left. Bumping
        # only the revision counter is not enough: a late page carries the
        # identity of the old query, and it must not match the new one.
        self._identity = QueryIdentity(revision=self._revision)
        self._pages.clear()
        self._pending_pages.clear()
        self._first_page = 0
        self._total = 0
        self._summaries = {}
        self.selected_id = None
        # An edit that has not been submitted yet belongs to the old project;
        # it may not travel into the new one.
        self._appearance_timer.stop()
        self._pending_appearance = None
        self.selection.bind_scope(token)
        self._refresh_selection_bar()
        self.details.show_summary()
        self._publish_model_count(None)
        # Remembered layout/scroll of this project (and folder) come back
        # before the first query, so the very first page is already sorted
        # the way the user left it.
        self._restore_view_state()
        record_model_event('scope_bound', project_session=project_session,
                           open_epoch=open_epoch)
        if refresh:
            self.refresh()
        else:
            self._set_state(ModelLibraryState.LOADING)
        return True

    def scope_token(self):
        return self._scope_token

    def _host_scope(self):
        """The project identity the host window currently has open.

        Returns ``(project_id, open_epoch)``. The epoch is what separates two
        openings of the *same* project; a project id alone cannot.
        """
        view = getattr(self.window_host, 'view', None)
        scene = getattr(view, 'scene', None)
        project_id = getattr(scene, 'project_id', '') or ''
        try:
            epoch = int(getattr(view, '_trace_open_epoch', 0) or 0)
        except (TypeError, ValueError):
            epoch = 0
        return str(project_id), epoch

    def _on_project_opened(self, *_args):
        """Drop the previous project's page, selection and record.

        The window keeps a late ``project_open_finished`` from the previous
        project off the new one. Here we only care that a switch invalidates
        what this page holds: an old cover or an old record must never be
        shown - let alone saved - under the new project.
        """
        project_id, epoch = self._host_scope()
        token = f'{project_id}#{epoch}'
        if token == self._scope_token:
            return False
        return self.bind_scope(project_id, epoch, refresh=False)

    def load_from_scene(self):
        """Called when the model page is opened.

        The scene's project id alone is not a session identity: reopening the
        same project must produce a new epoch, or a late result from the
        previous session could still land. The window owns the epoch and
        binds it through :meth:`bind_scope`; this method only re-binds when
        the host scope moved on without the window telling us (for example a
        project opened straight from the recent list).
        """
        project_id = getattr(getattr(self.window_host, 'view', None), 'scene', None)
        project_id = getattr(project_id, 'project_id', None)
        changed = project_id != self._project_id
        if changed:
            self._project_id = project_id
            self.selected_id = None
            self._summaries.clear()
            self.details.show_summary()
            with QtCore.QSignalBlocker(self.search):
                self.search.clear()
        self._on_project_opened()
        # A page that already holds a window (or a query in flight) does not
        # need a second identical read just because the tab was re-entered.
        if (changed or self._scope_token is None
                or (not self._pages and not self._pending_pages)):
            self.refresh()

    # ------------------------------------------------------------------
    # Service
    # ------------------------------------------------------------------
    def set_service(self, service):
        """Composition hook. Supply a project-bound service, never global state."""
        self.service = service
        material_port = getattr(service, 'multimaterial', None)
        self.details.material_panel.set_port(material_port)
        self.details.material_panel.set_base_source(material_port if hasattr(material_port, 'bases') else None)
        self.details.set_multimaterial_owned(callable(getattr(material_port, 'target_for', None)))
        self.selected_id = None
        self._summaries.clear()
        self._pages.clear()
        self._pending_pages.clear()
        self._first_page = 0
        self._total = 0
        self._revision += 1
        # A different service is a different library: results queued for the
        # previous one must not be installed.
        self._identity = QueryIdentity(revision=self._revision)
        self._appearance_timer.stop()
        self._pending_appearance = None
        self.selection.clear()
        self._refresh_selection_bar()
        self.details.show_summary()
        self.details.set_capabilities(
            self.service.capabilities,
            getattr(self.service, 'appearance_slots', None))
        self.search.setEnabled(self.service.capabilities.browse)
        self._update_buttons()
        self.refresh()

    # ------------------------------------------------------------------
    # Query and paging
    # ------------------------------------------------------------------
    def _identity_for(self, text, folder_id, sort):
        return QueryIdentity(text=text or '', folder_id=folder_id,
                             sort=sort or 'name', revision=self._revision)

    def _current_query(self, offset, limit):
        identity = self._identity
        return ModelQuery(text=identity.text, folder_id=identity.folder_id,
                          sort=identity.sort, offset=offset, limit=limit,
                          focus_id=getattr(self, '_reveal_model_id', None) if offset == 0 else None)

    def refresh(self, *_):
        """Start a new query revision.

        Any result of an older revision becomes invalid the moment this runs.
        """
        self._revision += 1
        self._pages.clear()
        self._pending_pages.clear()
        self._first_page = 0
        self._total = 0
        self._summaries = {}
        identity = self._identity_for(self.search.text(), self._folder_id(),
                                      self.sort.currentData())
        self._identity = identity
        self.selection.set_query(identity)
        if not self.service.capabilities.browse:
            self._set_state(ModelLibraryState.UNAVAILABLE)
            return
        self._set_state(ModelLibraryState.LOADING)
        self._request_page(0, identity)

    def _folder_id(self):
        return getattr(self, '_current_folder_id', None)

    def reveal_model(self, model_id, *, from_search=False):
        """Use the normal navigation gate, bounded query and selection pipeline."""
        from prism.project_search import visible_models
        scene = self.window_host.view.scene
        project_id = scene.project_id
        epoch = getattr(self.window_host.view, '_trace_open_epoch', 0)

        def current_record():
            if (self.window_host.view.scene is not scene or scene.project_id != project_id
                    or getattr(self.window_host.view, '_trace_open_epoch', 0) != epoch):
                return None
            return next((m for m in visible_models(scene)
                         if m.get('id', m.get('model_id')) == model_id), None)

        if current_record() is None:
            return False

        def reveal():
            record = current_record()
            if record is None:
                return
            self.window_host.open_model_library(record.get('folder_id'), from_search=from_search)
            if (current_record() is None or self.window_host.tabs.currentWidget()
                    is not getattr(self, '_tab_host', self)):
                return
            self.search.clear()
            self._restore_selection = None
            self._reveal_model_id = model_id
            self.refresh()
            # 「最近使用」记一笔：账本在用户目录里，不写工程文件。
            self.window_host.view.category_panel.record_resource_usage('model', model_id,
                                        record.get('name', ''))

        if self.window_host._navigation_is_committed():
            reveal()
        else:
            self.window_host.request_navigation(reveal)
        return True

    def set_folder(self, folder_id):
        """Narrow the page to one model folder.

        Each folder remembers its own scroll position, layout and focused
        model, so walking between folders does not lose the user's place.
        """
        self._view_state_timer.stop()
        self._save_view_state()
        if folder_id == self._folder_id():
            return
        self._current_folder_id = folder_id
        self._restore_view_state()
        self.refresh()

    def _request_page(self, index, identity=None):
        identity = identity or self._identity
        if index in self._pending_pages:
            return
        offset = index * PAGE_SIZE
        if self._total and offset >= self._total:
            return
        self._pending_pages.add(index)
        self._ensure_pool()
        worker = _ModelQuery(self.service, self._current_query(offset, PAGE_SIZE),
                             identity, self._scope_token, self._query_finished)
        # Keep the worker alive until its result was delivered: the queued
        # signal must not lose its sender while the result is in flight.
        self._workers[id(worker)] = worker
        self._pool.start(worker)

    def _ensure_pool(self):
        if self._pool is None:
            # A thread pool must not be built before the application exists.
            self._pool = QtCore.QThreadPool(self)
            self._pool.setMaxThreadCount(2)

    def _query_finished(self, outcome):
        worker, token, identity, page, error = outcome
        self._workers.pop(id(worker), None)
        if token != self._scope_token:
            # A result of a previous session. It must never be painted.
            record_model_event('query_discarded', reason='scope')
            return
        if not identity.matches(self._identity):
            record_model_event('query_discarded', reason='revision')
            return
        index = 0
        if page is not None:
            index = int(getattr(page, 'offset', 0) or 0) // PAGE_SIZE
        self._pending_pages.discard(worker.query.offset // PAGE_SIZE)
        if error is not None:
            record_model_event('query_failed', error=error[:200])
            self._set_state(ModelLibraryState.ERROR, error)
            return
        self.publish_page(page, identity)

    def publish_page(self, page, query_identity=None, page_index=None):
        """Install one page of summaries.

        Public entry point: the query worker calls it, and so may a caller
        that already has the page. A page of an older query identity is
        refused, so a late result cannot overwrite a newer list.
        """
        identity = query_identity if query_identity is not None else self._identity
        if not identity.matches(self._identity):
            record_model_event('page_discarded', reason='revision')
            return False
        if page is None:
            return False
        index = page_index
        if index is None:
            index = int(getattr(page, 'offset', 0) or 0) // PAGE_SIZE
        self._pages[index] = tuple(page.items)
        self._total = int(getattr(page, 'total', 0) or 0)
        self._trim_window()
        self._install_window()
        return True

    def _trim_window(self):
        """Keep the installed window bounded.

        The window is a run of consecutive pages. Once it is longer than the
        budget, the oldest page leaves the window: the user is at the far end
        when that happens, and the DOM must not grow with the library.
        """
        while len(self._pages) > MAX_WINDOW_PAGES:
            oldest = min(self._pages)
            self._pages.pop(oldest, None)
            self._first_page = max(self._first_page, oldest + 1)

    def _window_summaries(self):
        items = []
        for index in sorted(self._pages):
            items.extend(self._pages[index])
        return items

    def prepare_surface_steps(self):
        """Warm the shared card surface without querying a project."""
        from .widget_construction import widget_steps
        if self.cards is not None or not self.service.capabilities.browse:
            return
        self.cards = yield from widget_steps(MaterialCards, self)
        self.cards.set_external_drop_handler(self._accept_model_drop, self._import_model_drop)
        self._cards_layout.addWidget(self.cards)
        self.cards.selection_changed.connect(self._selection_changed)
        self.cards.selection_toggled.connect(self._selection_toggled)
        self.cards.selection_replaced.connect(self._selection_replaced)
        self.cards.textures_drag_requested.connect(self.drag_models)
        self.cards.delete_requested.connect(self.delete_selection)
        self.cards.rename_requested.connect(self.rename_model)
        self.cards.rename_committed.connect(self.rename_model)
        self.cards.card_size_requested.connect(self.card_size.setValue)
        self.cards.open_requested.connect(self._open_preview)
        self.cards.export_requested.connect(self._export_one)
        self.cards.batch_export_requested.connect(self._export_batch)
        self.cards.thumbnail_requested.connect(self._deliver_cover)
        self.cards.more_requested.connect(self.load_more)
        self.cards.scroll_changed.connect(self._schedule_view_state)
        self._set_layout()
        yield

    def _install_window(self):
        summaries = self._window_summaries()
        ensure_covers = getattr(self.service, 'ensure_covers', None)
        if callable(ensure_covers):
            ensure_covers(summaries)
        self._summaries = {item.model_id: item for item in summaries}
        if self.selected_id not in self._summaries:
            self.selected_id = None
        capabilities = self.service.capabilities
        if summaries:
            if self.cards is None:
                from .widget_construction import finish_construction
                finish_construction(self.prepare_surface_steps())
            assets = []
            from .material_cards import model_card_fields
            for item in summaries:
                assets.append(dict(
                    id=item.model_id, name=item.name, tags=list(item.tags),
                    **model_card_fields(item.format), dragEnabled=bool(capabilities.edit_metadata or capabilities.export_originals),
                    renameEnabled=bool(capabilities.edit_metadata),
                    exportEnabled=bool(capabilities.export_originals),
                    previewEnabled=bool(capabilities.preview)))
            visible = self.selection.visible_subset(self._summaries)
            self.cards.set_display_assets(
                assets, selected_id=self.selected_id,
                selected_ids=visible, bounded=True)
            self._apply_restored_view()
        self._cards_host.setVisible(bool(summaries))
        self.empty_state.setVisible(not summaries)
        target = getattr(self, '_reveal_model_id', None)
        if target in self._summaries:
            self._reveal_model_id = None
            self.selection.set_single(target)
            self._selection_changed(target)
            self.cards.reveal_record(target)
        elif target and self.service.capabilities.browse:
            self._reveal_model_id = None
        self._update_scope_label()
        # The tree row shows the library total, not the size of this page.
        # A filtered answer carries the *hit* count, which is not the total,
        # so the last known total stays instead of being replaced by it.
        if not self._is_filtered():
            self._publish_model_count(self._total)
        self._update_buttons()
        self._refresh_selection_bar()
        if not summaries:
            if not self._has_draft():
                self.details.show_summary(self._summaries.get(self.selected_id))
            if self._state != ModelLibraryState.ERROR:
                self._set_state(
                    ModelLibraryState.NO_RESULTS if self._is_filtered()
                    else ModelLibraryState.EMPTY)
        else:
            self._set_state(ModelLibraryState.READY)
            # A page arrived, but the focused model may live on another page.
            # Re-read its full record so the inspector keeps the appearance
            # and the dependency files instead of degrading to a summary.
            current = self.selection.current
            if current and current in self._summaries:
                self._load_record(current)
            else:
                if not self._has_draft():
                    self.details.show_summary(self._summaries.get(self.selected_id))

    def _has_draft(self):
        return (self._pending_appearance is not None or self.details._pending_tags is not None
                or self.details._pending_notes is not None)

    def _is_filtered(self):
        """Whether the current query narrows the library.

        An empty answer to a *filtered* query means "no hits", not "the
        library holds nothing": reporting the second would tell the user
        their models are gone.
        """
        identity = self._identity
        return bool(identity.text or identity.folder_id)

    def _publish_model_count(self, total):
        """Publish the number of model *entries* to the resource tree.

        Only entries count: textures, BIN sidecars and covers are files of a
        model, not models. ``None`` means "not known" and is shown as empty,
        never as 0. The hit count and the shown count belong to this page and
        are displayed here, not in the tree.
        """
        tree = getattr(getattr(self.window_host, 'view', None),
                       'category_panel', None)
        tree = getattr(tree, 'resource_tree', None)
        model = tree.model() if tree is not None else None
        setter = getattr(model, 'set_model_count', None)
        if not callable(setter):
            return False
        setter(None if total is None else int(total))
        return True

    def load_more(self):
        """Fetch the next page. Called when the surface reaches the window end."""
        if not self.service.capabilities.browse:
            return
        index = (max(self._pages) + 1) if self._pages else 0
        if self._total and index * PAGE_SIZE >= self._total:
            return
        self._request_page(index)

    def _update_scope_label(self):
        if not self.service.capabilities.browse:
            self.scope_label.setText('全部模型 · 数据服务未接入')
            return
        total = self._total
        shown = len(self._summaries)
        if total:
            self.scope_label.setText(
                f'全部模型 · 共 {total:,} 项 · 已显示 {shown:,} 项')
        else:
            self.scope_label.setText('全部模型 · 共 0 项')

    # ------------------------------------------------------------------
    # State
    # ------------------------------------------------------------------
    def _set_state(self, state, error=None):
        self._state = state
        if state == ModelLibraryState.UNAVAILABLE:
            self.status.setText('模型数据服务未接入；导入、预览与导出保持关闭')
            self.empty_title.setText('模型库')
            self.empty_hint.setText('数据服务未接入时不会显示任何模型\n'
                                    '也不会写入项目数据')
        elif state == ModelLibraryState.LOADING:
            self.status.setText('正在读取模型摘要…')
            self.empty_title.setText('模型库')
            self.empty_hint.setText('正在读取摘要…')
        elif state == ModelLibraryState.EMPTY:
            self.status.setText('模型库为空 · 还没有导入任何模型')
            self.empty_title.setText('模型库是空的')
            self.empty_hint.setText('导入模型后，这里显示模型当前材质效果的封面\n'
                                    'FBX · OBJ · GLB · glTF')
        elif state == ModelLibraryState.NO_RESULTS:
            self.status.setText('没有符合条件的模型')
            self.empty_title.setText('没有匹配的模型')
            self.empty_hint.setText('换一个关键词，或清空搜索条件')
        elif state == ModelLibraryState.ERROR:
            self.status.setText('读取模型摘要失败：' + (error or '未知错误'))
            self.empty_title.setText('读取失败')
            self.empty_hint.setText('没有读到模型数据\n'
                                    '项目数据未被修改')
        elif state == ModelLibraryState.READY:
            self.status.clear()
            self.empty_title.setText('模型库')
            self.empty_hint.setText('导入模型后，这里显示模型当前材质效果的封面')
        self._update_buttons()

    @property
    def state(self):
        """The named state of the page. Never inferred from a widget."""
        return self._state

    # ------------------------------------------------------------------
    # Buttons
    # ------------------------------------------------------------------
    def _capability(self, name):
        """A capability counts only when the service really implements it.

        The contract says a capability may only be enabled once the
        operation, the UI handler and the acceptance checks exist. Checking
        the method here keeps a half-wired service from unlocking a button
        that would fail on click.
        """
        return bool(getattr(self._effective_capabilities(), name, False))

    def _effective_capabilities(self):
        from prism.model_library.contracts import ModelCapabilities
        caps = self.service.capabilities
        return ModelCapabilities(
            browse=bool(getattr(caps, 'browse', False)),
            import_models=bool(getattr(caps, 'import_models', False))
            and self._service_has('import_paths'),
            export_originals=bool(getattr(caps, 'export_originals', False))
            and self._service_has('export_originals'),
            edit_metadata=bool(getattr(caps, 'edit_metadata', False))
            and self._service_has('update_metadata'),
            edit_appearance=bool(getattr(caps, 'edit_appearance', False))
            and self._service_has('update_appearance'),
            preview=bool(getattr(caps, 'preview', False))
            and self._service_has('open_preview'),
            wireframe=bool(getattr(caps, 'wireframe', False)),
            create_folders=bool(getattr(caps, 'create_folders', False))
            and self._service_has('create_folder'),
            backend_version=getattr(caps, 'backend_version', ''),
        )

    def _service_has(self, method_name):
        """A capability only counts when the service really has the operation."""
        return callable(getattr(self.service, method_name, None))

    def _has_session(self):
        """A real session identity, not just a project id.

        ``project_id`` alone cannot tell two openings of the same project
        apart, so a late result from the previous opening could still land.
        """
        return bool(self._scope_token and self._project_session)

    def _blocking_reason(self, capability, handler, *, needs_selection=False):
        """Why an operation cannot run right now, or ``''`` when it can.

        Every button is unlocked by the same three facts: the capability is
        real (that is, the service actually has the handler), the project
        session is valid, and - where it applies - the user selected
        something. Naming the *first* missing precondition keeps the tooltip
        honest: a half-wired service must not read as "select a model first".
        """
        if not self._capability('browse'):
            return '模型数据服务未接入'
        if not self._capability(capability) or not self._service_has(handler):
            return '功能尚未接入；不会创建或修改项目数据'
        if not self._has_session():
            return '没有有效的项目会话'
        if needs_selection and not self.selection.count:
            return '请先选择要操作的模型'
        return ''

    def _update_buttons(self):
        browse = self._capability('browse')
        session = self._has_session()
        self.search.setEnabled(browse)
        self.sort.setEnabled(browse and session)
        for button, capability, handler, selected in (
                (self.import_button, 'import_models', 'import_paths', False),
                (self.folder_button, 'create_folders', 'create_folder', False),
                (self.export_button, 'export_originals', 'export_originals', True),
                (self.details.open_button, 'preview', 'open_preview', True),
                (self.details.export_button, 'export_originals',
                 'export_originals', True),
                (self.details.apply_button, 'edit_appearance',
                 'update_appearance', True)):
            reason = self._blocking_reason(capability, handler,
                                           needs_selection=selected)
            button.setEnabled(not reason)
            button.setToolTip(reason)

    # ------------------------------------------------------------------
    # Selection
    # ------------------------------------------------------------------
    def _selection_replaced(self, ids, current, additive):
        if not self.store_to_scene():
            self._mirror_selection()
            return
        if not additive:
            self.selection.clear()
        else:
            for key in tuple(self.selection.ids()):
                if key in self._summaries and key not in ids:
                    self.selection.toggle(key)
        for key in ids:
            if not self.selection.contains(key):
                self.selection.toggle(key)
        self.selection.set_current(current)
        self._mirror_selection()
        # The shared bridge emits selection_changed once after replacing the
        # range; that existing handler installs only the final inspector.

    def _selection_toggled(self, model_id, additive):
        # A pending appearance edit belongs to the model that is leaving the
        # focus; submit it before the selection moves on.
        if not self.store_to_scene():
            return
        if additive:
            self.selection.toggle(model_id)
        else:
            self.selection.set_single(model_id)
        self.selection.set_current(model_id)
        self._mirror_selection()
        self._load_record(model_id)
        # The bar is the only place that states the size of the batch. A
        # click must update it: waiting for the next page installation would
        # leave "未选择模型" next to a real selection.
        self._refresh_selection_bar()
        self._update_buttons()
        self._schedule_view_state()
        self.selection_changed.emit(self.selection)

    def _selection_changed(self, model_id):
        if not self.store_to_scene():
            return
        previous = self.selected_id
        self.selected_id = model_id
        if model_id and model_id != previous:
            summary = self._summaries.get(model_id)
            self.window_host.view.category_panel.record_resource_usage('model', model_id, getattr(summary, 'name', ''))
        self.selection.set_current(model_id)
        self._mirror_selection()
        self._load_record(model_id)
        self._refresh_selection_bar()
        self._update_buttons()
        self._schedule_view_state()
        self.selection_changed.emit(self.selection)

    def _flush_appearance(self):
        """Submit a pending appearance edit now (selection, project, close)."""
        if not self._appearance_timer.isActive() and self._pending_appearance is None:
            return None
        self._appearance_timer.stop()
        return self._submit_appearance()

    def _record_getter(self):
        """Find the full-record reader.

        The contract's service exposes ``query``. Reading one complete
        record needs ``get``; the composition root may publish it on the
        service or leave it on the repository.
        """
        getter = getattr(self.service, 'get', None)
        if callable(getter):
            return getter
        repository = (getattr(self.service, 'repository', None)
                      or getattr(self.service, '_repository', None))
        getter = getattr(repository, 'get', None)
        return getter if callable(getter) else None

    def _load_record(self, model_id):
        """Load the complete record of the selected model.

        The detail panel is fed from the record, never from the card
        summary: the summary is a small projection and carries neither the
        appearance nor the dependency files.
        """
        summary = self._summaries.get(model_id)
        current = getattr(self.details, '_record', None)
        if (current is not None and current.model_id == model_id and
                (self.details._pending_tags is not None or self.details._pending_notes is not None
                 or self._pending_appearance is not None)):
            # A cover refresh isn't permission to replace an editor draft.
            if summary is not None:
                self.details._show_cover(summary.thumbnail_data_url)
            return current
        if not model_id or summary is None:
            self.details.show_summary(summary)
            return None
        getter = self._record_getter()
        if getter is None:
            self.details.show_summary(summary)
            return None
        pending = self._record_worker
        if (pending is not None and pending.model_id == model_id
                and pending.service is self.service and pending.token == self._scope_token
                and pending.identity.matches(self._identity)):
            return None
        # Show the summary immediately, so an old editable record cannot stay
        # under the new selection while its metadata is still being read.
        self.details.show_summary(summary)
        self._ensure_pool()
        worker = _ModelRecord(self.service, getter, model_id, self._identity,
                              self._scope_token, self._record_read_finished)
        self._record_worker = worker
        self._workers[id(worker)] = worker
        self._pool.start(worker)
        return None

    @QtCore.pyqtSlot(object)
    def _record_read_finished(self, outcome):
        worker, token, identity, record, error = outcome
        self._workers.pop(id(worker), None)
        if worker is not self._record_worker:
            return
        self._record_worker = None
        if (token != self._scope_token or worker.service is not self.service
                or not identity.matches(self._identity)
                or self.selection.current != worker.model_id):
            return
        summary = self._summaries.get(worker.model_id)
        current = getattr(self.details, '_record', None)
        if (current is not None and current.model_id == worker.model_id
                and self._has_draft()):
            return
        if error is not None:
            record_model_event('record_read_failed', model=worker.model_id, error=error[:200])
        if summary is None or record is None or error is not None:
            self.details.show_summary(summary)
            return
        self._show_loaded_record(record, summary)

    def _show_loaded_record(self, record, summary):
        # One snapshot of the capabilities feeds both the detail pane and the
        # capability banner: asking the service twice rebuilt the whole
        # capability object (and re-ran the writable check) for nothing.
        capabilities = self._effective_capabilities()
        self.details.show_record(record, summary.thumbnail_data_url,
                                 capabilities)
        self.details.set_capabilities(
            capabilities,
            getattr(self.service, 'appearance_slots', None))
        material_port = getattr(self.service, 'multimaterial', None)
        if callable(getattr(material_port, 'target_for', None)):
            target = material_port.target_for(record)
            if self.details.material_panel.target() != target:
                self.details.material_panel.reset(target)
        return record

    def _mirror_selection(self):
        if self.cards is None:
            return
        visible = self.selection.visible_subset(self._summaries)
        self.cards.set_selection(visible, self.selection.current)

    def _refresh_selection_bar(self):
        count = self.selection.count
        self.selection_bar.setVisible(count > 0)
        self.clear_selection_button.setEnabled(count > 0)
        if count:
            self.selection_label.setText(f'已选择 {count} 个模型')
        else:
            self.selection_label.setText('未选择模型')

    def _clear_selection(self):
        self.selection.clear()
        self._mirror_selection()
        self._refresh_selection_bar()
        self._update_buttons()
        self.selection_changed.emit(self.selection)

    def selected_model_ids(self):
        """The frozen export scope of the current selection."""
        return self.selection.freeze()

    # ------------------------------------------------------------------
    # Covers
    # ------------------------------------------------------------------
    def _deliver_cover(self, model_id):
        """Answer a cover request with the cached image, never the original."""
        summary = self._summaries.get(model_id)
        if summary is None or self.cards is None:
            return
        data_url = summary.thumbnail_data_url
        if not data_url:
            self.cards.update_thumbnail({'id': model_id}, self._placeholder())
            return
        self.cards.update_thumbnail({'id': model_id}, data_url)

    @staticmethod
    @lru_cache(maxsize=1)
    def _placeholder():
        # A neutral missing-cover marker, never a sample model or material sphere.
        from .material_cards import placeholder_image
        return placeholder_image({'assetKind':'model'})

    # ------------------------------------------------------------------
    # Import / export / preview
    # ------------------------------------------------------------------
    def _choose_import(self):
        patterns = ' '.join('*' + extension for extension in sorted(MODEL_EXTENSIONS))
        paths, _ = QtWidgets.QFileDialog.getOpenFileNames(
            self, '导入模型', '', f'模型 ({patterns});;所有文件 (*)')
        if paths:
            self.import_paths(tuple(paths))

    def import_paths(self, paths):
        """Submit an import job. The UI never copies or unpacks a file."""
        if not self._can_operate('import_models'):
            return None
        try:
            job_id = self.service.import_paths(
                tuple(paths), folder_id=self._folder_id())
        except ModelFeatureUnavailable as error:
            self._report_unavailable('import_models', error)
            return None
        except Exception as error:
            self._report_failure('import', error)
            return None
        # A job id is not a finished import. Say so.
        self.status.setText(f'导入任务已提交（{job_id}）· 等待后台完成')
        record_model_event('import_submitted', job_id=str(job_id),
                           count=len(tuple(paths)))
        self.import_requested.emit(tuple(paths))
        return job_id

    def export_originals(self, snapshot):
        """Submit an export job for the frozen selection."""
        if not self._can_operate('export_originals'):
            return None
        if not snapshot:
            return None
        existing = getattr(self, '_export_dialog', None)
        if existing is not None:
            existing.raise_()
            return existing
        dialog = QtWidgets.QFileDialog(self, '选择导出目录')
        dialog.setFileMode(QtWidgets.QFileDialog.FileMode.Directory)
        dialog.setOption(QtWidgets.QFileDialog.Option.ShowDirsOnly, True)
        dialog.setOption(QtWidgets.QFileDialog.Option.DontUseNativeDialog, True)
        service, scope = self.service, self._scope_token
        self._export_dialog = dialog
        def selected(destination):
            if self.service is service and self._scope_token == scope:
                self._submit_export(snapshot, destination)
        def finished(_result):
            if getattr(self, '_export_dialog', None) is dialog:
                self._export_dialog = None
            dialog.deleteLater()
        dialog.fileSelected.connect(selected)
        dialog.finished.connect(finished)
        dialog.open()
        return dialog

    def _submit_export(self, snapshot, destination):
        if not destination:
            return None
        try:
            job_id = self.service.export_originals(
                tuple(snapshot.model_ids), destination)
        except ModelFeatureUnavailable as error:
            self._report_unavailable('export_originals', error)
            return None
        except Exception as error:
            self._report_failure('export', error)
            return None
        self.status.setText(
            f'导出任务已提交（{job_id}）· {len(snapshot.model_ids)} 个模型原件')
        record_model_event('export_submitted', job_id=str(job_id),
                           count=len(snapshot.model_ids))
        self.export_requested.emit(snapshot)
        return job_id

    def drag_models(self, _visible_ids):
        caps = self.service.capabilities
        if not (caps.edit_metadata or caps.export_originals) or not self.store_to_scene():
            return
        if not caps.export_originals:
            from .library_transfer import start_library_drag
            folders = self.window_host.view.category_panel.resource_service
            start_library_drag(self.cards, folders, 'model', self.selection.freeze().model_ids)
            return
        if getattr(self, '_transfer_worker', None) is not None or getattr(self, '_transfer_active', False):
            return
        ids = self.selection.freeze().model_ids
        if not ids:
            return
        self._ensure_pool()
        worker = _ModelRecord(self.service, self.service.prepare_transfer, ids,
                              self.cards.mouse_sequence, self._scope_token, self._transfer_ready)
        self._transfer_worker = worker
        self._workers[id(worker)] = worker
        self.status.setText('正在准备模型及全部贴图；请保持按住鼠标，拖到 UE 内容浏览器。')
        record_model_event('external_drag_requested', count=len(ids))
        self._pool.start(worker)

    def _transfer_ready(self, outcome):
        worker, token, gesture, result, error = outcome
        self._workers.pop(id(worker), None)
        if getattr(self, '_transfer_worker', None) is worker:
            self._transfer_worker = None
        if token != self._scope_token or worker.service is not self.service:
            return
        if error:
            self._report_failure('准备模型拖出', error)
            return
        if set(worker.model_id) != set(self.selection.freeze().model_ids):
            record_model_event('external_drag_discarded', reason='selection_changed')
            return
        if (self.cards.mouse_sequence != gesture or
                not QtWidgets.QApplication.mouseButtons() & Qt.MouseButton.LeftButton):
            self.status.setText('模型及贴图已准备好，再次按住选中的卡片拖动即可。')
            return
        from .library_transfer import start_library_drag
        folders = self.window_host.view.category_panel.resource_service
        self._transfer_active = True
        try:
            action = start_library_drag(self.cards, folders, 'model', worker.model_id, result['paths'])
        finally:
            self._transfer_active = False
        text = (f"已交给目标应用：{len(worker.model_id)} 个模型、{result['textures']} 张贴图。"
                if action == Qt.DropAction.CopyAction else '拖放已结束。')
        if result['missing']:
            text += f" 有 {len(result['missing'])} 个依赖缺失，未包含在拖出文件中。"
        self.status.setText(text)
        record_model_event('external_drag_finished', action=getattr(action, 'name', 'ignored'),
                           count=len(worker.model_id), textures=result['textures'], missing=len(result['missing']))

    def rename_model(self, model_id, name=None):
        if not self.service.capabilities.edit_metadata or not self.store_to_scene():
            return
        record = self.service.get(model_id)
        if record is None:
            return
        if name is None:
            name, ok = QtWidgets.QInputDialog.getText(self, '重命名模型', '名称：', text=record.name)
            if not ok:
                return
        try:
            self.service.rename(model_id, name)
            record_model_event('model_renamed', model_id=model_id)
            self.refresh()
            if self.selection.current == model_id:
                self._load_record(model_id)
        except Exception as error:
            self._report_failure('重命名', error)

    def delete_selection(self):
        ids = self.selection.freeze().model_ids
        if not ids or not self.service.capabilities.edit_metadata or not self.store_to_scene():
            return
        service = self.service
        try:
            import datetime
            service.patch_metadata_batch(ids, {'deleted_at': datetime.datetime.now(datetime.timezone.utc).isoformat()})
            record_model_event('models_deleted', count=len(ids))
            self._clear_selection()
            self.refresh()
            from .toast import UndoToast
            def restore():
                if service is not self.service or not service.capabilities.edit_metadata:
                    return
                try:
                    service.patch_metadata_batch(ids, {'deleted_at': None})
                    record_model_event('models_restored', count=len(ids))
                    self.refresh()
                except Exception as error:
                    self._report_failure('撤销删除', error)
            self._delete_toast = UndoToast(self.window_host, f'已删除 {len(ids)} 个模型', restore)
            self._delete_toast.show_at_bottom()
        except Exception as error:
            self._report_failure('删除', error)

    def event(self, event):
        if (event.type() == QtCore.QEvent.Type.ShortcutOverride and self.hasFocus()
                and event.key() in (Qt.Key.Key_Delete, Qt.Key.Key_F2)):
            event.accept()
            return True
        return super().event(event)

    def keyPressEvent(self, event):
        if event.key() == Qt.Key.Key_Delete:
            self.delete_selection()
            event.accept()
            return
        if event.key() == Qt.Key.Key_F2 and self.selection.current:
            self.rename_model(self.selection.current)
            event.accept()
            return
        super().keyPressEvent(event)

    def create_folder(self):
        """Create a model folder. Disabled until the model domain can save it."""
        if not self._can_operate('create_folders'):
            return None
        try:
            result = self.service.create_folder(self._folder_id())
        except ModelFeatureUnavailable as error:
            self._report_unavailable('create_folders', error)
            return None
        except Exception as error:
            self._report_failure('create_folder', error)
            return None
        self.refresh()
        return result

    def open_preview(self, model_id):
        """Ask the host to open the large preview for one model."""
        if not self._can_operate('preview'):
            return None
        try:
            result = self.service.open_preview(model_id)
        except ModelFeatureUnavailable as error:
            self._report_unavailable('preview', error)
            return None
        except Exception as error:
            self._report_failure('preview', error)
            return None
        self.preview_requested.emit(model_id)
        return result

    def apply_material(self, material_record):
        """Apply a material-library configuration to the selected model.

        The snapshot is taken here; the controlled save happens through the
        appearance signal the details panel emits.
        """
        if not self._can_operate('edit_appearance'):
            return False
        return self.details.apply_material_record(material_record)

    def _save_metadata(self, tags, notes, expected_revision):
        """Submit a metadata edit through the service."""
        if not self._can_operate('edit_metadata'):
            self.details._pending_tags, self.details._pending_notes = tags, notes
            return None
        record = getattr(self.details, '_record', None)
        model_id = record.model_id if record is not None else self.selection.current
        if not model_id:
            return None
        try:
            result = self.service.update_metadata(
                model_id, tags=tags, notes=notes,
                expected_revision=expected_revision)
        except ModelFeatureUnavailable as error:
            self._report_unavailable('edit_metadata', error)
            self.details._pending_tags, self.details._pending_notes = tags, notes
            self.details.set_metadata_feedback('元数据保存尚未接入', ok=False)
            return None
        except Exception as error:
            self._report_failure('metadata', error)
            self.details._pending_tags, self.details._pending_notes = tags, notes
            self.details.set_metadata_feedback('元数据保存失败：' + str(error), ok=False)
            return None
        self.details.set_metadata_feedback('已保存')
        if self.selection.current == model_id:
            self._load_record(model_id)
        self.record_changed.emit(model_id)
        return result

    def _can_operate(self, capability):
        if not self._capability(capability):
            self._report_unavailable(capability, '功能尚未接入')
            return False
        if not self._has_session():
            self._report_unavailable(capability, '没有有效的项目会话')
            return False
        return True

    def _report_unavailable(self, capability, reason):
        self._store_error = True
        self.status.setText(f'{capability} 尚未接入：{reason}')
        record_model_event('feature_unavailable', feature=str(capability),
                           reason=str(reason)[:200])

    def _report_failure(self, kind, error):
        self._store_error = True
        self.status.setText(f'{kind} 失败：{error}')
        record_model_event('operation_failed', operation=str(kind),
                           error=str(error)[:200])

    def _commit_appearance(self, appearance=None):
        """Accept an appearance edit and schedule one controlled save.

        A slider drag emits a patch per step. Saving each one would write the
        project dozens of times for a single user action and fill the undo
        history with pixels. The latest edit is kept here and submitted once
        the user stops moving (C4.6).
        """
        if appearance is None:
            appearance = self.details.collect_appearance()
        model_id = self.selection.current
        if not model_id:
            return None
        self._pending_appearance = appearance
        # The old cover stays visible, clearly marked as being replaced.
        self.details.set_cover_pending(True, '封面更新中')
        self._appearance_timer.start()
        return None

    def _submit_appearance(self):
        """Submit the coalesced appearance revision to the service."""
        self._appearance_timer.stop()
        appearance = self._pending_appearance
        self._pending_appearance = None
        model_id = self.selection.current
        if appearance is None or not model_id:
            return None
        # One user action, one revision: the session closes the group here.
        appearance = self.details.commit_appearance()
        try:
            result = self.service.update_appearance(model_id, appearance)
        except ModelFeatureUnavailable as error:
            self._report_unavailable('edit_appearance', error)
            self._pending_appearance = appearance
            return None
        except Exception as error:
            self._report_failure('appearance', error)
            self._pending_appearance = appearance
            return None
        self.details.set_cover_pending(True, '封面更新中')
        record_model_event('appearance_submitted', model=model_id,
                           revision=appearance.revision)
        self.record_changed.emit(model_id)
        return result

    def store_to_scene(self):
        """Flush debounced model edits through the existing navigation gate."""
        self._store_error = False
        self._flush_appearance()
        self.details._flush_metadata()
        return not self._store_error

    # -- signal handlers ------------------------------------------------
    def _open_preview(self, model_id=None):
        model_id = model_id if isinstance(model_id, str) else (self.selection.current or self.selected_id)
        if not model_id:
            return
        if model_id != self.selection.current:
            self._selection_changed(model_id)
            if self.selection.current != model_id:
                return
        self.open_preview(model_id)

    def _export_selection(self, *_):
        snapshot = self.selection.freeze()
        if not snapshot:
            self.status.setText('请先选择要导出的模型')
            return
        self.export_originals(snapshot)

    def _export_one(self, model_id):
        if not self.selection.contains(model_id):
            self.selection.set_single(model_id)
        self._export_selection()

    def _export_batch(self, *_):
        self._export_selection()

    # ------------------------------------------------------------------
    # Jobs
    # ------------------------------------------------------------------
    def publish_job_event(self, event):
        """Install the state of one background job.

        A job id is not a result: the UI only reports what the job actually
        reported. A partial failure is reported as a partial failure.
        """
        data = self._event_data(event)
        if not data:
            return
        job_id = str(data.get('job_id') or data.get('id') or '')
        state = str(data.get('state') or '')
        # "The renderer produced an image" and "a card shows it" are different
        # facts; this is the second one, recorded where it becomes true.
        if state == 'completed' and str(data.get('kind') or '') in (
                'thumbnail', 'texture', 'preview'):
            record_model_event(EVENT_UI_IMAGE_ACCEPTED, job_id=job_id,
                               model_id=data.get('model_id'),
                               subject=data.get('subject'),
                               kind=data.get('kind'), state='completed')
        message = data.get('message') or data.get('error_message') or ''
        error_code = data.get('error_code') or ''
        if job_id:
            self._jobs[job_id] = dict(data)
            if len(self._jobs) > 512:
                for old_id in list(self._jobs):
                    if self._jobs[old_id].get('state') not in ('queued', 'running'):
                        self._jobs.pop(old_id)
                        if len(self._jobs) <= 512:
                            break
        self.cancel_button.setEnabled(any(
            job.get('state') in ('queued', 'running') for job in self._jobs.values()))
        kind = str(data.get('kind') or '任务')
        if state in ('queued', 'running'):
            self.status.setText(f'{kind}进行中' + (f'（{job_id}）' if job_id else ''))
        elif state == 'completed':
            missing = data.get('missing') or []
            # Refresh first: the refresh installs the new page and sets the
            # ordinary status. The job's own message is written afterwards so
            # the user still sees what happened to the job.
            self._after_job(data)
            if missing:
                self.status.setText(
                    f'{kind}部分完成：{len(missing)} 项缺失（{job_id}）')
            else:
                self.status.setText(f'{kind}完成' + (f'（{job_id}）' if job_id else ''))
        elif state == 'failed':
            self.status.setText(
                f'{kind}失败' + (f'：{message}' if message else '')
                + (f'（{error_code}）' if error_code else '')
                + (f'（{job_id}）' if job_id else ''))
            record_model_event('job_failed', kind=kind, job_id=job_id,
                               error_code=str(error_code))
        elif state == 'cancelled':
            self.status.setText(f'{kind}已取消' + (f'（{job_id}）' if job_id else ''))
        elif state == 'stale':
            self.status.setText(f'{kind}结果已过期，已忽略')
        else:
            self.status.setText(f'{kind}状态：{state or "未知"}')

    def _after_job(self, data):
        kind = str(data.get('kind') or '')
        if kind in ('import', 'export_originals', 'texture'):
            self.refresh()

        elif kind == 'thumbnail':
            # The new cover of the edited revision is here, so the old one is
            # no longer "更新中". The refresh below reinstalls the summary and
            # re-reads the full record, so the panel ends up showing the cover
            # that belongs to the stored appearance.
            self.details.set_cover_pending(False)
            self.refresh()

    def _cancel_jobs(self):
        cancel = getattr(self.service, 'cancel', None)
        if not callable(cancel):
            return
        for job_id, data in list(self._jobs.items()):
            if data.get('state') in ('queued', 'running'):
                cancel(job_id)

    @staticmethod
    def _event_data(event):
        if event is None:
            return {}
        if isinstance(event, dict):
            return dict(event)
        data = {}
        for name in ('job_id', 'kind', 'state', 'message', 'error_code',
                     'error_message', 'output_ref', 'missing', 'model_id'):
            value = getattr(event, name, None)
            if value is not None:
                data[name] = value
        identity = getattr(event, 'identity', None)
        if identity is not None:
            for name in ('model_id', 'request_id', 'project_session', 'open_epoch'):
                value = getattr(identity, name, None)
                if value is not None:
                    data.setdefault(name, value)
        state = data.get('state')
        if state is not None and not isinstance(state, str):
            data['state'] = getattr(state, 'value', str(state))
        return data

    # ------------------------------------------------------------------
    # Filter plumbing
    # ------------------------------------------------------------------
    def _apply_search(self, *_):
        self.refresh()

    def _apply_sort(self, *_):
        self.refresh()

    def _set_layout(self, *_):
        if self.cards is not None:
            self.cards.set_layout(
                'list' if self.list_button.isChecked() else 'grid',
                self.card_size.value())

    # ------------------------------------------------------------------
    # View state (per project and folder)
    # ------------------------------------------------------------------
    def _settings(self):
        """The host's local settings store, or ``None`` for a bare page.

        The view state is a *local* convenience (scroll position, layout,
        card size). It is not project data, so it goes to the settings the
        window already owns and never into the project store.
        """
        return getattr(getattr(self.window_host, 'view', None), 'settings', None)

    def _view_state_key(self):
        project_id, _epoch = self._host_scope()
        return 'ModelLibrary/%s/%s' % (project_id, self._folder_id() or '__root__')

    def _schedule_view_state(self, *_):
        timer = getattr(self, '_view_state_timer', None)
        if timer is not None:
            timer.start()

    def _save_view_state(self):
        settings = self._settings()
        if settings is None:
            return False
        key = self._view_state_key()
        for name, value in (
                ('mode', 'list' if self.list_button.isChecked() else 'grid'),
                ('cardSize', self.card_size.value()),
                ('sort', self.sort.currentData() or 'name'),
                ('scroll', self._scroll_position()),
                ('selected', self.selection.current or '')):
            settings.setValue('%s/%s' % (key, name), value)
        return True

    def _scroll_position(self):
        if self.cards is None:
            return 0
        return int(self.cards.scroll_position or 0)

    def _restore_view_state(self):
        """Apply the remembered layout, then schedule the scroll/selection.

        The scroll position and the focused model only make sense once a page
        is installed, so they wait in ``_restore_scroll`` / ``_restore_selection``
        until :meth:`_install_window` has something to show.
        """
        settings = self._settings()
        if settings is None:
            return False
        key = self._view_state_key()
        mode = settings.value('%s/mode' % key, 'grid')
        try:
            size = int(settings.value('%s/cardSize' % key, 220))
        except (TypeError, ValueError):
            size = 220
        sort = settings.value('%s/sort' % key, 'name')
        try:
            scroll = int(settings.value('%s/scroll' % key, 0))
        except (TypeError, ValueError):
            scroll = 0
        selected = settings.value('%s/selected' % key, '')
        with QtCore.QSignalBlocker(self.grid_button), \
                QtCore.QSignalBlocker(self.list_button), \
                QtCore.QSignalBlocker(self.card_size), \
                QtCore.QSignalBlocker(self.sort):
            (self.list_button if mode == 'list' else self.grid_button).setChecked(True)
            self.card_size.setValue(max(160, min(320, size)))
            index = self.sort.findData(sort)
            if index >= 0:
                self.sort.setCurrentIndex(index)
        self._set_layout()
        self._restore_scroll = max(0, scroll)
        self._restore_selection = selected if isinstance(selected, str) else None
        return True

    def _apply_restored_view(self):
        """Install the restored scroll and focus onto the freshly built cards."""
        scroll, self._restore_scroll = self._restore_scroll, None
        if self.cards is not None and scroll:
            self.cards.set_scroll_position(scroll)
        selected, self._restore_selection = self._restore_selection, None
        if selected and selected in self._summaries:
            self.selection.set_single(selected)
            self._mirror_selection()
            self._load_record(selected)
            self._refresh_selection_bar()
            self._update_buttons()

    # ------------------------------------------------------------------
    # Drag and drop
    # ------------------------------------------------------------------
    def _model_paths(self, mime):
        paths = []
        for url in mime.urls():
            if not url.isLocalFile():
                continue
            path = url.toLocalFile()
            if os.path.isdir(path):
                try:
                    paths.extend(os.path.join(path, name)
                                 for name in sorted(os.listdir(path))
                                 if os.path.splitext(name)[1].lower() in MODEL_EXTENSIONS
                                 and os.path.isfile(os.path.join(path, name)))
                except OSError:
                    continue
            elif os.path.splitext(path)[1].lower() in MODEL_EXTENSIONS:
                paths.append(path)
        return tuple(dict.fromkeys(paths))

    def _accept_model_drop(self, mime):
        if not self.import_button.isEnabled() or mime.hasFormat('application/x-prism-library-assets'):
            return False
        # Folder enumeration happens once, on drop, never on every mouse move.
        return any(url.isLocalFile() and (os.path.splitext(url.toLocalFile())[1].lower()
                   in MODEL_EXTENSIONS or os.path.isdir(url.toLocalFile())) for url in mime.urls())

    def _import_model_drop(self, mime):
        if not self._accept_model_drop(mime):
            return False
        paths = self._model_paths(mime)
        if not paths:
            return False
        self.import_paths(paths)
        return True

    def dragEnterEvent(self, event):
        if self._accept_model_drop(event.mimeData()):
            event.setDropAction(QtCore.Qt.DropAction.CopyAction)
            event.accept()

    def dragMoveEvent(self, event):
        if self._accept_model_drop(event.mimeData()):
            event.setDropAction(QtCore.Qt.DropAction.CopyAction)
            event.accept()

    def dropEvent(self, event):
        if self._import_model_drop(event.mimeData()):
            event.setDropAction(QtCore.Qt.DropAction.CopyAction)
            event.accept()

    # ------------------------------------------------------------------
    # Diagnostics
    # ------------------------------------------------------------------
    def open_logs(self):
        from pathlib import Path
        from prism.config import logfile_name
        directory = Path(logfile_name()).parent
        record_model_event('diagnostic_folder_opened', directory=str(directory))
        QtGui.QDesktopServices.openUrl(QtCore.QUrl.fromLocalFile(str(directory)))
