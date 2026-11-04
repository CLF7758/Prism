#!/usr/bin/env python3

# This file is part of Prism.
#
# Prism is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# Prism is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with Prism.  If not, see <https://www.gnu.org/licenses/>.

import logging
import json
import gc
import os
import platform
import signal
import time
import sys

# A GUI launch (pythonw.exe) has no console at all: ``sys.stdout`` and
# ``sys.stderr`` are None, and a library that prints to them raises instead of
# writing anything.  Prism keeps its own log files, so the console streams are
# only a sink -- point them at the null device and let the application speak
# through the log.  This runs at import time on purpose: a spawned worker
# re-imports this module and inherits the same guard.
if sys.stdout is None:
    sys.stdout = open(os.devnull, 'w', encoding='utf-8')
if sys.stderr is None:
    sys.stderr = open(os.devnull, 'w', encoding='utf-8')

# Dispatch workers before desktop imports. The shared Babylon host owns its
# Qt/WebEngine application and never receives widgets from the desktop process.
if getattr(sys, 'frozen', False) and len(sys.argv) > 2 and sys.argv[1] == '--prism-native-worker':
    import runpy
    worker = sys.argv[2]
    if worker not in {'prism.components.model_worker', 'prism.components.model_preview',
                      'prism.components.material_preview'}:
        raise SystemExit('Unrecognised bundled worker')
    sys.argv = [worker, *sys.argv[3:]]
    runpy.run_module(worker, run_name='__main__')
    raise SystemExit(0)

from prism.webengine_runtime import configure_webengine
configure_webengine()

from PyQt6 import QtCore, QtGui, QtWidgets
from PyQt6.QtCore import Qt
if QtCore.QCoreApplication.instance() is None:
    QtCore.QCoreApplication.setAttribute(Qt.ApplicationAttribute.AA_ShareOpenGLContexts)

# QtWebEngineWidgets refuses to load once a QApplication exists, and the mind
# map panel imports it lazily, so pull it in here at import time. Importing
# the module is cheap; Chromium itself only starts when the panel is shown.
try:
    from PyQt6 import QtWebEngineCore, QtWebEngineWidgets  # noqa: F401
except ImportError:                       # pragma: no cover - optional
    pass

from prism import constants, ui_tokens
from prism.count_contract import (
    CANVAS_DRAWN, CountPublication, LIBRARY_TOTAL, LIBRARY_VISIBLE,
    RESULT_COUNT, SELECTION_COUNT, BASIS_PAINTED, BASIS_COMPOSITE,
    BASIS_MODEL_ROWS, BASIS_RESULT_ROWS, next_request_token)
from prism.i18n import _
from prism.assets import BeeAssets
from prism.browser_capture import BrowserCaptureService
from prism.config import CommandlineArgs, PrismSettings, logfile_name
from prism.utils import create_palette_from_dict
from prism.view import PrismGraphicsView
from prism.window_workspace import WindowWorkspaceMixin
from prism.widgets.color_filter import ColorFilterBar
from prism.widgets.window_shell import FramelessShellMixin, TitleBarControls
from prism.item_lifecycle import is_deleted
from prism.performance_trace import operation

logger = logging.getLogger(__name__)

#: The collector's thresholds: ``(gen0, gen1, gen2)``.  A *full* pass runs
#: when the generation-1 count passes the third number, so the default
#: ``(700, 10, 10)`` walks the whole live heap every 70,000 allocations.
GC_THRESHOLDS = (700, 10, 100)


def tune_gc_for_large_projects():
    """Make the collector's full pass rare instead of frequent.

    A full pass walks every live object.  With a 100k-item project open the
    trace recorded six full passes of 17-144 ms during one open, and the
    sessions that opened the largest projects paid **1.3-2.3 s** of collector
    time in total - **0.8-1.8 s** of it on the GUI thread, which is what the
    user sees as the window stuttering while a panel is built.

    Measured on the real 100k project (``tests/_audit_library_profile_real.py``,
    collector metering around the open itself):

    ==========================  =========  =========  =========
    thresholds                  (700,10,10) (700,10,50) (700,10,100)
    ==========================  =========  =========  =========
    full passes during the open          5           1            0
    collector time during the open  349.8ms      77.5ms       49.2ms
    longest single pass            100.5ms      51.0ms       12.8ms
    ==========================  =========  =========  =========

    The wall time of the open and the resident size were noise between runs
    (the same threshold differed by 700 ms and 120 MB from run to run), so
    neither of them is used as evidence here.  A full pass still happens
    every 700,000 allocations, so a long session is not left without one.
    """
    gc.set_threshold(*GC_THRESHOLDS)


class PrismApplication(QtWidgets.QApplication):
    #: Events that mean "the user is working".  Background work yields to
    #: them; see ``prism.user_activity``.
    _USER_INPUT_EVENTS = frozenset({
        QtCore.QEvent.Type.MouseButtonPress,
        QtCore.QEvent.Type.MouseButtonRelease,
        QtCore.QEvent.Type.MouseButtonDblClick,
        QtCore.QEvent.Type.KeyPress,
        QtCore.QEvent.Type.KeyRelease,
        QtCore.QEvent.Type.Wheel,
        QtCore.QEvent.Type.TouchBegin,
        QtCore.QEvent.Type.TouchUpdate,
        QtCore.QEvent.Type.TouchEnd,
    })

    def notify(self, receiver, event):
        kind = event.type()
        dragging = (kind == QtCore.QEvent.Type.MouseMove and isinstance(event, QtGui.QMouseEvent)
                    and event.buttons() != QtCore.Qt.MouseButton.NoButton)
        if kind in self._USER_INPUT_EVENTS or dragging:
            from prism.user_activity import USER_ACTIVITY
            USER_ACTIVITY.note_input()
        trace = getattr(self, '_performance_trace', None)
        if trace is None or getattr(trace, 'dispatch_capture_failed', False):
            return super().notify(receiver, event)
        started, event_type = time.perf_counter(), kind.value
        trace.dispatch_started(receiver, event_type, started)
        try:
            return super().notify(receiver, event)
        finally:
            try:
                trace.dispatch_finished(receiver, event_type, (time.perf_counter()-started)*1000,
                                        nested_loop=trace.last_heartbeat > started,
                                        started=started)
            except Exception:
                # Diagnostics must neither break a successful input event
                # nor replace the original exception from an event handler.
                trace.dispatch_capture_failed = True
                logger.exception('GUI dispatch diagnostics disabled after an error')

    def event(self, event):
        if event.type() == QtCore.QEvent.Type.FileOpen:
            for widget in self.topLevelWidgets():
                if isinstance(widget, PrismMainWindow):
                    widget.view.open_from_file(event.file())
                    return True
            return False
        else:
            return super().event(event)


class ShellSplitterHandle(QtWidgets.QSplitterHandle):
    """A six-pixel drag target with a one-pixel visual divider."""

    def __init__(self, orientation, parent):
        super().__init__(orientation, parent)
        self.setAttribute(Qt.WidgetAttribute.WA_Hover, True)

    def paintEvent(self, event):
        colours = ui_tokens.COLOURS_DARK
        colour = (colours['accent'] if self.underMouse() else
                  colours['border-subtle'])
        painter = QtGui.QPainter(self)
        painter.fillRect(self.rect(), QtGui.QColor(colours['app-bg']))
        if self.orientation() == Qt.Orientation.Horizontal:
            x = (self.width() - 1) // 2
            painter.fillRect(x, 0, 1, self.height(), QtGui.QColor(colour))
        else:
            y = (self.height() - 1) // 2
            painter.fillRect(0, y, self.width(), 1, QtGui.QColor(colour))


class ShellSplitter(QtWidgets.QSplitter):
    def createHandle(self):
        return ShellSplitterHandle(self.orientation(), self)


class PrismMainWindow(WindowWorkspaceMixin, FramelessShellMixin, QtWidgets.QMainWindow):
    """A standard desktop window with independently resizable side panels.

    继承 `FramelessShellMixin`（放在最前面，让它的 `nativeEvent` 生效）：
    需求 4 要去掉 Windows 系统那条白色标题栏，同时保留原生缩放/贴靠/阴影。
    平台相关的 Win32 消息处理全部集中在 `window_shell.py`，不散落到这里。
    """

    def event(self, event):
        kind = event.type()
        if kind in (QtCore.QEvent.Type.Hide, QtCore.QEvent.Type.Show,
                    QtCore.QEvent.Type.WinIdChange, QtCore.QEvent.Type.PlatformSurface,
                    QtCore.QEvent.Type.WindowStateChange):
            # Read the existing handle only: winId() would itself create a
            # native window and interfere with the lifecycle being diagnosed.
            import traceback
            from prism.performance_trace import record
            view = getattr(self, 'view', None)
            handle = self.windowHandle()
            fields = dict(event_type=kind.name,
                   surface_type=(event.surfaceEventType().name
                                 if kind == QtCore.QEvent.Type.PlatformSurface else None),
                   visible=self.isVisible(), window_state=self.windowState().value,
                   has_surface=handle is not None,
                   opening=bool(getattr(view, '_project_opening', False)),
                   replacing=bool(getattr(view, '_replacing_project', False)),
                   open_epoch=getattr(view, '_trace_open_epoch', 0),
                   callers=[f'{os.path.basename(frame.filename)}:{frame.lineno}:{frame.name}'
                            for frame in traceback.extract_stack(limit=14)[:-1]])
            record('main_window_lifecycle', priority='state', **fields)
            # Sparse lifecycle events must survive the busy asset trace's
            # rotation, especially the first native surface transition.
            logger.info('Main window lifecycle: %s', json.dumps(fields, ensure_ascii=False))
        return super().event(event)

    def __init__(self, app, *, theme_qss=None):
        super().__init__()
        # The first WebEngine child switches its top-level window to a
        # composited surface. Doing that during library prewarm destroyed and
        # recreated the already-visible HWND (trace seq 658-662, PID 189516).
        # Reuse the existing startup view as a hidden child before any main
        # window handle/show; keep this surface for the window's lifetime.
        surface = getattr(app, '_webengine_warmup', None)
        if surface is not None and not is_deleted(surface):
            surface.setParent(self)
            surface.hide()
            self._webengine_surface_anchor = surface
        app.setOrganizationName(constants.APPNAME)
        app.setApplicationName(constants.APPNAME)
        self.setWindowIcon(BeeAssets().logo)
        self.view = PrismGraphicsView(app, self)
        # File actions must work when the canvas is hidden behind materials,
        # documents or the asset browser. Reuse the same QAction instances.
        from prism.actions.actions import actions
        for action_id in ('new_scene', 'open', 'save', 'save_as'):
            if action_id in actions:
                self.addAction(actions[action_id].qaction)
        # Share the existing actions with the prototype's nine top-level menus.
        for action in self.view.context_menu.actions():
            if action.menu() is not None:
                self.menuBar().addMenu(action.menu())
        self.menuBar().setFixedHeight(36)
        self._menu_file_title = QtWidgets.QLabel()
        self._menu_file_title.setContentsMargins(12, 0, 8, 0)
        self._menu_file_title.setStyleSheet('color: #AAB0BB; font-size: 12px; background: transparent;')
        # 方案 §9.1：无边框后，把「项目标题 / 未保存状态」和右侧窗口控制
        # 按钮（最小化 / 最大化-还原 / 关闭）一起塞进现有深色菜单栏的右上角，
        # 复用这条已有的深色顶栏，不另起一条标题栏。关闭走 closeEvent
        # （含未保存确认），绝不直接退进程。
        self._title_corner = QtWidgets.QWidget()
        self._title_corner.setObjectName('titleBarCorner')
        self._title_corner.setStyleSheet('background: transparent;')
        _corner_layout = QtWidgets.QHBoxLayout(self._title_corner)
        _corner_layout.setContentsMargins(0, 0, 0, 0)
        _corner_layout.setSpacing(0)
        _corner_layout.addWidget(self._menu_file_title)
        self._title_controls = TitleBarControls(self, self._title_corner)
        _corner_layout.addWidget(self._title_controls)
        self.menuBar().setCornerWidget(self._title_corner, Qt.Corner.TopRightCorner)
        self.windowTitleChanged.connect(lambda title: self._menu_file_title.setText(title.removesuffix(' - Prism')))
        # 方案 §9.2：只在菜单栏的空白区（菜单项之间）启动系统拖动，双击空白
        # 区最大化/还原；命中菜单项时照常交给 QMenuBar。拖动逻辑放在
        # eventFilter 里，不新起一套鼠标位移拖动（避免和贴靠/多屏冲突）。
        # 缓存菜单栏引用给 eventFilter 用（见 eventFilter 注释：不能在里面
        # 调 self.menuBar()，会与 application 级过滤器互相触发导致递归）。
        self._title_menubar = self.menuBar()
        self._title_menubar.installEventFilter(self)
        self.workspace_service = self.view.workspace_service
        self.browser_capture = BrowserCaptureService(
            self.view.settings.valueOrDefault('BrowserCapture/port'), self)
        self.browser_capture.capture_received.connect(
            self.view.enqueue_browser_capture)
        # The capture server stops itself; the signal reports the real end
        # so the close path never has to wait for it (see closeEvent).
        self._capture_exit_pending = False
        self._capture_exit_timer = None
        self.browser_capture.stopped.connect(self._on_capture_stopped)
        if self.view.settings.valueOrDefault('BrowserCapture/enabled'):
            self.browser_capture.start()
        shell = ui_tokens.LAYOUT
        self.setMinimumSize(*shell['window-min'])
        screen = app.primaryScreen()
        if screen is not None:
            available = screen.availableGeometry()
            width = min(shell['window-default'][0],
                        max(shell['window-min'][0], int(available.width() * .9)))
            height = min(shell['window-default'][1],
                         max(shell['window-min'][1], int(available.height() * .9)))
            self.resize(width, height)
            self.move(available.center() - self.rect().center())
        else:
            self.resize(*shell['window-default'])
        geom = self.view.settings.value('MainWindow/geometry')
        if geom is not None:
            self.restoreGeometry(geom)

        self.color_filter = ColorFilterBar(self.view)
        # 整个应用**只有一个**重算定时器：侧栏面板的那一个（它负责算并
        # 发布计数）。以前这里还有第二个 300 ms 定时器，两边都能触发同一
        # 件工作 —— 一份通知会被安排两次。没有侧栏面板时才用这个兜底。
        self._fallback_recount_timer = QtCore.QTimer(self)
        self._fallback_recount_timer.setSingleShot(True)
        self._fallback_recount_timer.setInterval(300)
        self._fallback_recount_timer.timeout.connect(self._on_recount)
        self.view.scene.assets_changed.connect(self._schedule_recount)
        self.view.scene.metadata_changed.connect(self._schedule_recount)
        self.view.scene.filter_changed.connect(self._schedule_recount)
        self.view.scene.items_trashed.connect(self._schedule_recount)

        from prism.widgets.detail_panel import DetailPanel
        self.view._detail_panel = DetailPanel(self.view)
        self.view.category_panel.categories_changed.connect(
            self.view._detail_panel._on_selection_changed)

        outer = QtWidgets.QWidget()
        layout = QtWidgets.QVBoxLayout(outer)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        # 常驻的颜色条按 §8.3 收进了「筛选」弹层：这里只留一条 36 px 的
        # 筛选条（范围 / 生效条件 / 结果数 / 清除筛选 / 打开弹层）。
        from prism.widgets.filter_bar import FilterBar
        from prism.widgets.filter_popover import FilterPopover

        self.filter_bar = FilterBar(self.view, outer)

        self.filter_popover = FilterPopover(self.view, outer, color_host=self.color_filter,
                                             defer_ui=True)
        self.filter_popover.hide()
        self.filter_bar.open_requested.connect(self._toggle_filter_popover)
        self.filter_bar.clear_requested.connect(self._clear_filters)
        self.filter_bar.chip_removed.connect(self._remove_filter_chip)
        self.filter_popover.filters_changed.connect(
            self._refresh_filter_bar)
        self.tabs = QtWidgets.QTabWidget()
        self.tabs.setObjectName('workspaceTabs')
        self.tabs.setDocumentMode(True)
        self.tabs.tabBar().hide()
        # Let the splitter squeeze the middle pane. Otherwise the tab widget
        # advertises the widest page's size hint (the document toolbar is
        # wide) and pushes the side panels down to their minimum widths.
        self.tabs.setSizePolicy(QtWidgets.QSizePolicy.Policy.Ignored,
                                QtWidgets.QSizePolicy.Policy.Expanding)
        from prism.widgets.canvas_toolbar import CanvasToolbar
        self.canvas_page = QtWidgets.QWidget()
        canvas_layout = QtWidgets.QVBoxLayout(self.canvas_page)
        canvas_layout.setContentsMargins(0, 0, 0, 0)
        canvas_layout.setSpacing(0)
        self.canvas_toolbar = CanvasToolbar(self.view, self.color_filter)
        canvas_layout.addWidget(self.canvas_toolbar)
        canvas_layout.addWidget(self.view, 1)
        self.view._fixed_canvas_toolbar = self.canvas_toolbar
        self.canvas_toolbar.filter_requested.connect(self._toggle_filter_popover)
        self.filter_bar.filter_button.hide()
        self.tabs.addTab(self.canvas_page, _('Canvas'))

        from prism.widgets.document_panel import DocumentPanel
        self.document_panel = DocumentPanel(self.view, self)
        self.tabs.addTab(self.document_panel, _('Document'))

        # The mind map page is only started when its tab is first opened.
        from prism.widgets.mindmap_panel import MindMapPanel
        self.mindmap_panel = MindMapPanel(self.view, self)
        self.tabs.addTab(self.mindmap_panel, _('Mind Map'))

        from prism.widgets.asset_browser import AssetBrowser
        self.asset_browser = AssetBrowser(self)
        self.tabs.addTab(self.asset_browser, '素材库')
        # A published result set is a count of its own: the counters have to
        # follow it right away, not at the next selection or zoom.
        try:
            self.asset_browser.model.modelReset.connect(
                self._on_hot_result_published)
        except (AttributeError, RuntimeError):       # pragma: no cover
            logger.debug('the result model does not announce its resets')
        from prism.widgets.deferred_library import DeferredLibrary
        def material_type():
            from prism.widgets.material_library import MaterialLibrary
            return MaterialLibrary
        def material_page(widget_type):
            from prism.widgets.widget_construction import widget_steps
            # ``parent`` is the tab that hosts the page, not the window: a
            # page built under the window invalidates the window's layout on
            # every widget it creates, and the window repaints itself in the
            # middle of the open.  ``self`` stays the window host.
            return (yield from widget_steps(widget_type, self,
                details_parent=self.material_library.details,
                parent=self.material_library))
        self.material_library = DeferredLibrary(material_page, self, prepare=material_type)
        self.tabs.addTab(self.material_library, '材质库')
        self.material_library.folder_requested.connect(self.open_material_library)
        self.material_library.create_folder_requested.connect(
            lambda parent_id: self._create_resource_item('material', 'folder', parent_id))
        def model_type():
            from prism.widgets.model_library import ModelLibrary
            # These definitions are otherwise imported lazily by the service
            # and inspector constructors. Keep their disk/module work in the
            # preparation worker too; no widgets or native engines are made.
            from importlib import import_module
            import_module('prism.model_library.multimaterial')
            import_module('prism.widgets.model_material_panel')
            return ModelLibrary
        def model_page(widget_type):
            from prism.widgets.widget_construction import widget_steps
            return (yield from widget_steps(widget_type, self,
                details_parent=self.model_library.details,
                parent=self.model_library))
        self.model_library = DeferredLibrary(model_page, self, prepare=model_type)
        from prism.model_library.composition import ModelLibraryComposition
        self.model_composition = ModelLibraryComposition(self)
        self.tabs.addTab(self.model_library, '模型库')
        self._build_eagle_chrome(layout)

        self._previous_tab = 0
        self.tabs.currentChanged.connect(self._on_tab_changed)

        self.splitter = ShellSplitter(Qt.Orientation.Horizontal)
        self.splitter.setHandleWidth(shell['splitter-hotzone'])
        self.splitter.addWidget(self._left_column)
        self.splitter.addWidget(self._centre_column)
        self.splitter.addWidget(self._right_column)
        self._left_column.setMinimumWidth(shell['left-panel-min'])
        self._left_column.setMaximumWidth(shell['left-panel-max'])
        self._right_column.setMinimumWidth(shell['right-panel-min'])
        self._right_column.setMaximumWidth(shell['right-panel-max'])
        self.splitter.setCollapsible(1, False)
        self.splitter.setStretchFactor(0, 0)
        self.splitter.setStretchFactor(1, 1)
        self.splitter.setStretchFactor(2, 0)
        self.splitter.setSizes([shell['left-panel-width'],
                                shell['window-default'][0] - shell['left-panel-width']
                                - shell['right-panel-width'],
                                shell['right-panel-width']])
        state = self.view.settings.value('MainWindow/splitter')
        if state is not None:
            self.splitter.restoreState(state)
        # 筛选条已经在 outer 顶部（见上面构造处），这里只接三栏分割器。
        layout.addWidget(self.splitter, 1)
        self.setCentralWidget(outer)
        self._sidebar_widths = {side: max(shell[side+'-panel-min'], min(shell[side+'-panel-max'], self.view.settings.value('MainWindow/'+side+'Width', shell[side+'-panel-width'], type=int))) for side in ('left', 'right')}

        self._page_panels = {}
        self._deleted_pages = []
        self._trash_toast = None
        self.current_page_kind = 'canvas'
        self.current_page_id = 'default-canvas'
        self.view.current_canvas_id = self.current_page_id
        self.view.category_panel.workspace_requested.connect(
            self._open_resource_page)
        self.view.category_panel.section_requested.connect(
            self._open_resource_section)
        self.view.category_panel.material_library_requested.connect(
            self._open_material_library_entry)
        self.view.category_panel.model_library_requested.connect(
            self.open_model_library)
        # 工程打开之后先把两个库面板建好：面板是"第一次进入才构造"的，
        # 构造发生在点击处理函数内部时会把窗口堵死一秒多（用户看到的是
        # "程序闪退了"）。预热把这段成本挪到空闲时刻。
        self.view.project_open_finished.connect(self.schedule_library_prewarm)
        self.view.category_panel.resource_create_requested.connect(
            self._create_resource_item)
        self.view.category_panel.resource_tree.create_committed.connect(
            self._on_resource_created)
        self.view.category_panel.resource_tree.trash_requested.connect(
            self._open_trash_panel)
        self.view.category_panel.resource_tree.move_requested.connect(
            self._move_resource_items)
        self.view.category_panel.resource_tree.delete_many_requested.connect(
            self._delete_resource_items)
        self.view.category_panel.resource_delete_requested.connect(
            self._delete_resource_item)
        self.view.category_panel.resource_export_requested.connect(
            self._export_resource_item)
        self.view.category_panel.resource_import_requested.connect(
            self._import_resource_item)
        self.view.category_panel.resource_nodes_changed.connect(
            self._on_resource_tree_change)
        self.workspace_service.changed.connect(self._on_workspace_changed)

        app.installEventFilter(self)
        self._build_layout_menu()
        self._apply_responsive_panels()

        self.view.category_panel.counts_changed.connect(self._update_status)
        self.view.category_panel.counts_changed.connect(lambda _counts: self._on_recount())
        # The immediate, partial publication: the status bar only.  It never
        # queries the directory - it answers from what is already known.
        self.view.category_panel.counts_previewed.connect(
            self._on_counts_previewed)
        self.view.zoom_changed.connect(
            lambda _value: self._update_status(None, interaction=True))
        self._centre_status.setFixedHeight(shell['statusbar-height'])
        self.view.scene.selectionChanged.connect(
            self._on_scene_selection_changed)
        self.view.category_panel.update_counts()
        self.color_filter.recount()
        self._refresh_filter_bar()
        self.view.update_window_title()
        # 需求 4：在窗口 show 之前打开无边框外壳（砍系统白标题栏，保留
        # WS_THICKFRAME 的原生缩放/贴靠/阴影）。离屏测试平台会自己跳过。
        self.enable_frameless_shell()
        from prism.search_controller import SearchController
        self.search_controller = SearchController(self)
        self.view.category_panel.layout().insertWidget(0, self.search_controller.header)
        self._centre_column.layout().insertWidget(2, self.search_controller.match_bar)
        # Polish the completed widget tree once, before its first visible frame.
        # Installing application QSS before construction makes each local
        # stylesheet change cascade through a partially constructed subtree.
        if theme_qss is not None:
            with operation('startup.apply_theme'):
                app.setStyleSheet(theme_qss)
        with operation('startup.first_show'):
            self.show()
        QtCore.QTimer.singleShot(0, self._apply_responsive_panels)
        from prism.recovery import RecoveryStore
        from prism.fileio.save_preparation import AutosaveSchedule
        import uuid
        self._recovery_session = uuid.uuid4().hex
        self._recovery_store = RecoveryStore(
            os.path.join(os.path.dirname(self.view.settings.fileName()), 'recovery'))
        self._recovery_worker = None
        self._has_recovery_records = False
        self._recovery_snapshot_token = None
        self._recovery_candidates = []
        self._recovery_timer = QtCore.QTimer(self)
        self._recovery_timer.setInterval(15000)
        self._recovery_timer.timeout.connect(self._write_recovery_snapshot)
        self._recovery_timer.start()
        self._recovery_cleanup_timer = QtCore.QTimer(self)
        self._recovery_cleanup_timer.setInterval(60 * 60 * 1000)
        self._recovery_cleanup_timer.timeout.connect(self._cleanup_recovery_history)
        self._recovery_cleanup_timer.start()
        # Offer only records from an earlier session, before new edits can
        # supersede them. No recovery ever writes into the original project.
        QtCore.QTimer.singleShot(0, self._scan_recovery)
        self._autosave_schedule = AutosaveSchedule()
        self._autosave_timer = QtCore.QTimer(self)
        self._autosave_timer.setInterval(1000)
        self._autosave_timer.timeout.connect(self._poll_autosave)
        if self.view.settings.valueOrDefault('Autosave/enabled'):
            self._autosave_timer.start()

        from prism.capture_hotkey import CaptureHotkey
        from prism.actions.actions import actions
        self._capture_hotkey = CaptureHotkey(self.view, actions['screen_capture'].qaction)

    def _build_eagle_chrome(self, layout):
        """Each splitter column owns its header and contents, top to bottom."""
        from prism.widgets.components.icons import icon_button
        from prism.widgets.window_shell import TitleBarDragArea
        self.menuBar().hide()

        def column(name):
            widget = QtWidgets.QFrame()
            widget.setObjectName(name)
            box = QtWidgets.QVBoxLayout(widget)
            box.setContentsMargins(0, 0, 0, 0)
            box.setSpacing(0)
            return widget, box

        def header(name):
            widget = TitleBarDragArea(self)
            widget.setObjectName(name)
            widget.setFixedHeight(36)
            box = QtWidgets.QHBoxLayout(widget)
            box.setContentsMargins(6, 0, 4, 0)
            box.setSpacing(0)
            return widget, box

        self._left_column, left = column('eagleLeftColumn')
        self._centre_column, centre = column('eagleCentreColumn')
        self._right_column, right = column('eagleRightColumn')
        self._centre_column.setSizePolicy(QtWidgets.QSizePolicy.Policy.Ignored,
                                          QtWidgets.QSizePolicy.Policy.Expanding)
        self._left_header, tools = header('eagleLeftHeader')
        menu_button = icon_button('menu', '主菜单', size=28)
        menu_button.clicked.connect(self._show_eagle_menu)
        tools.addWidget(menu_button)
        tools.addStretch()
        self._sidebar_toggle = icon_button('sidebars', '折叠 / 展开左右栏 · Tab', size=28)
        self._sidebar_toggle.clicked.connect(self._toggle_sidebars)
        tools.addWidget(self._sidebar_toggle)
        left.addWidget(self._left_header)
        left.addWidget(self.view.category_panel, 1)

        self._centre_header, middle = header('eagleCentreHeader')
        self._restore_sidebars = icon_button('sidebars', '展开左右栏 · Tab', size=28)
        self._restore_sidebars.clicked.connect(self._toggle_sidebars)
        middle.addWidget(self._restore_sidebars)
        self._eagle_header_stack = QtWidgets.QStackedWidget()
        self._eagle_header_stack.setSizePolicy(QtWidgets.QSizePolicy.Policy.Ignored,
                                              QtWidgets.QSizePolicy.Policy.Preferred)
        self._eagle_drag = TitleBarDragArea(self)
        drag_layout = QtWidgets.QHBoxLayout(self._eagle_drag)
        drag_layout.setContentsMargins(6, 0, 0, 0)
        drag_layout.addWidget(self._menu_file_title)
        drag_layout.addStretch()
        self._eagle_header_stack.addWidget(self._eagle_drag)
        self.asset_browser.layout().removeWidget(self.asset_browser.toolbar)
        self.asset_browser.toolbar.setFixedHeight(36)
        self._eagle_header_stack.addWidget(self.asset_browser.toolbar)
        middle.addWidget(self._eagle_header_stack, 1)
        self._collapsed_window_host = QtWidgets.QWidget()
        self._collapsed_window_layout = QtWidgets.QHBoxLayout(self._collapsed_window_host)
        self._collapsed_window_layout.setContentsMargins(0, 0, 0, 0)
        self._collapsed_window_layout.setSpacing(0)
        middle.addWidget(self._collapsed_window_host)
        centre.addWidget(self._centre_header)
        self.filter_bar.hide()
        self.canvas_page.layout().removeWidget(self.canvas_toolbar)
        self._eagle_header_stack.addWidget(self.canvas_toolbar)
        # Aggregate scopes display the shared canvas toolbar, not the hidden
        # page-specific header. Place sizing immediately before its filter.
        sizing=self.asset_browser.zoom_bar
        self.asset_browser.toolbar.layout().removeWidget(sizing)
        tools=self.canvas_toolbar.layout()
        tools.insertWidget(tools.indexOf(self.canvas_toolbar.filter_button),sizing)
        self.canvas_toolbar.thumbnail_zoom_bar=sizing
        sizing.hide()

        self._eagle_header_stack.setCurrentWidget(self.canvas_toolbar)
        self._centre_header.setFixedHeight(self.canvas_toolbar.height())
        middle.setContentsMargins(0, 0, 0, 0)
        centre.addWidget(self.tabs, 1)
        # QMainWindow's full-width status bar would join all three columns.
        self._centre_status = QtWidgets.QStatusBar()
        self._centre_status.setSizeGripEnabled(False)
        self._status_text = QtWidgets.QLabel()
        self._status_text.setObjectName('workspaceStatusText')
        self._status_text.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop)
        self._status_text.setSizePolicy(QtWidgets.QSizePolicy.Policy.Ignored, QtWidgets.QSizePolicy.Policy.Expanding)
        self._status_text.setStyleSheet('padding: 4px 0 8px 0;')
        self._centre_status.addWidget(self._status_text, 1)
        centre.addWidget(self._centre_status)

        self._right_header, controls = header('eagleRightHeader')
        controls.addStretch()
        self._window_controls = QtWidgets.QWidget()
        self._window_controls_layout = QtWidgets.QHBoxLayout(self._window_controls)
        self._window_controls_layout.setContentsMargins(0, 0, 0, 0)
        self._window_controls_layout.setSpacing(0)
        self._eagle_pin = icon_button('pin', '窗口置顶', size=28)
        self._eagle_pin.setCheckable(True)
        self._eagle_pin.toggled.connect(self._pin_eagle_window)
        self._window_controls_layout.addWidget(self._eagle_pin)
        for button in (self._title_controls.min_button, self._title_controls.max_button,
                       self._title_controls.close_button):
            button.setFixedWidth(36)
        self._window_controls_layout.addWidget(self._title_controls)
        self._right_controls_layout = controls
        controls.addWidget(self._window_controls)
        right.addWidget(self._right_header)
        right.addWidget(self.view._detail_panel, 1)
        right.addWidget(self.material_library.details, 1)
        self.material_library.details.hide()
        right.addWidget(self.model_library.details, 1)
        self.model_library.details.hide()
        self._right_empty = QtWidgets.QWidget()
        self._right_empty.hide()
        right.addWidget(self._right_empty, 1)
        self._sidebars_shortcut = QtGui.QShortcut(QtGui.QKeySequence('Tab'), self)
        self._sidebars_shortcut.activated.connect(self._toggle_sidebars)
        for button in (self._sidebar_toggle, self._restore_sidebars):
            button.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
            button.customContextMenuRequested.connect(self._show_sidebar_menu)
        self._sidebar_shortcuts = []
        for key, callback in [('Ctrl+Alt+1', lambda: self._toggle_sidebar('left')),
                              ('Ctrl+Alt+2', lambda: self._toggle_sidebar('right')),
                              ('Ctrl+Alt+3', self._toggle_sidebars)]:
            shortcut = QtGui.QShortcut(QtGui.QKeySequence(key), self)
            shortcut.activated.connect(callback)
            self._sidebar_shortcuts.append(shortcut)


    def statusBar(self):
        status = getattr(self, '_centre_status', None)
        return status if status is not None else super().statusBar()

    def _eagle_menu(self):
        menu = QtWidgets.QMenu(self)
        self.view._build_recent_files()
        menu.addMenu(self.view._recent_files_submenu)
        menu.addSeparator()
        for action in self.view.context_menu.actions():
            if action.menu() is not None:
                menu.addMenu(action.menu())
        menu.addSeparator()
        menu.addAction('项目搜索（图片 / 文档 / 脑图）', self._focus_project_search)
        menu.addAction('返回当前画布', lambda: self.open_workspace_page('canvas', self.view.current_canvas_id))
        return menu

    def _show_eagle_menu(self):
        previous = getattr(self, '_active_eagle_menu', None)
        if previous is not None:
            previous.close()
            previous.deleteLater()
        self._active_eagle_menu = self._eagle_menu()
        self._active_eagle_menu.popup(
            self._left_header.mapToGlobal(QtCore.QPoint(6, 36)))

    def _focus_project_search(self):
        if hasattr(self, 'search_controller'):
            self.search_controller.focus()
            return
        self.view.category_panel.search.show()
        self.view.category_panel.search.setFocus()

    def _pin_eagle_window(self, enabled):
        self.setWindowFlag(Qt.WindowType.WindowStaysOnTopHint, enabled)
        self.show()
        self.enable_frameless_shell()

    def _poll_autosave(self):
        """Cheap revision polling; never enumerate items or fetch originals."""
        if (not self.view.settings.valueOrDefault('Autosave/enabled') or
                self.view.settings.valueOrDefault('Autosave/minutes') <= 0 or
                getattr(self, '_pending_recovery', None)):
            return
        view = self.view
        schedule = self._autosave_schedule
        minutes = self.view.settings.valueOrDefault('Autosave/minutes')
        schedule.max_seconds = min(120, minutes * 60)
        token = (view.scene.project_id, view.filename,
                 getattr(view, '_trace_open_epoch', 0),
                 getattr(view, '_content_revision', 0), getattr(view, '_undo_revision', 0))
        identity = token[:3]
        if identity != getattr(self, '_autosave_identity', None):
            self._autosave_identity = identity
            schedule.reset()
        now = time.monotonic()
        if schedule.observe(token, view.is_dirty(), now):
            result = self._perform_autosave()
            policy = view.autosave_policy
            delay = min(120, policy.next_interval_ms()/1000) if policy.failures else 30
            schedule.attempted(now, 1 if result == 'already_running' else delay)

    def _perform_autosave(self):
        # A single policy/result owner. The old second implementation here
        # treated text snapshots and empty error lists as successful saves.
        return self.view.autosave_tick()

    def _autosave_settled(self):
        policy = self.view.autosave_policy
        if policy.failures:
            self._autosave_schedule.attempted(time.monotonic(),
                                             min(120, policy.next_interval_ms()/1000))
        else:
            self._autosave_schedule.reset()

    @staticmethod
    def _worker_running(worker):
        try:
            return worker is not None and worker.isRunning()
        except RuntimeError:
            return False

    def _recovery_token(self):
        view = self.view
        return (view.scene.project_id, view.filename, getattr(view, '_trace_open_epoch', 0),
                getattr(view, '_content_revision', 0), getattr(view, '_undo_revision', 0))

    def _scan_recovery(self):
        from prism import fileio
        store, session = self._recovery_store, self._recovery_session
        legacy_path = self.view.settings.value('Recovery/snapshot_path')
        legacy_inline = self.view.settings.value('Recovery/snapshot')
        def scan(*, worker):
            try:
                legacy = None
                if legacy_path and os.path.isfile(legacy_path):
                    with open(legacy_path, encoding='utf-8') as handle:
                        legacy = json.load(handle)
                elif legacy_inline:
                    legacy = json.loads(legacy_inline)
                if legacy and legacy.get('filename') and not any(
                        value['payload'].get('legacy_import') for value in store.records()):
                    import sqlite3
                    from prism.storage.managed import database_path
                    from pathlib import Path
                    source = Path(database_path(legacy['filename']))
                    if source.is_file():
                        with sqlite3.connect(source.as_uri()+'?mode=ro', uri=True) as db:
                            identity = db.execute("SELECT value FROM prism_metadata WHERE key='project_id'").fetchone()
                        if identity:
                            legacy.update(project_id=identity[0], legacy_import=True, scope='page_editors')
                            store.write(legacy, session='legacy', owner_pid=0)
                            worker.legacy_imported = True
                store.cleanup()
                worker.records = store.pending(session)
                worker.finished.emit('', [])
            except Exception as error:
                logger.exception('Recovery records could not be inspected')
                # One bad legacy record must not hide unrelated valid ones.
                try:
                    worker.records = store.pending(session)
                except Exception:
                    worker.records = []
                worker.finished.emit('', [str(error)])
        worker = self._recovery_worker = fileio.ThreadedIO(scan)
        def completed(_path, errors):
            self._recovery_candidates = getattr(worker, 'records', [])
            if getattr(worker, 'legacy_imported', False):
                for key in ('Recovery/snapshot', 'Recovery/snapshot_path', 'Recovery/snapshot_mtime'):
                    self.view.settings.remove(key)
            if errors:
                self.statusBar().showMessage('恢复记录检查失败：' + '; '.join(errors), 0)
        worker.finished.connect(completed)
        stopped = QtCore.QThread.finished.__get__(worker, type(worker))
        def release():
            if self._recovery_worker is worker:
                self._recovery_worker = None
            worker.func = None; worker.args = (); worker.kwargs = {}
            worker.deleteLater()
            if not getattr(self, '_recovery_closing', False):
                self._offer_recovery_snapshot()
        stopped.connect(release)
        worker.start()

    def _temporary_project_target(self):
        from prism.recovery import project_key
        key = project_key(self.view.scene.project_id, None)
        folder = self._recovery_store.root / 'drafts' / key
        target = folder / '未命名工程.prism.data'
        self._draft_marker_path = str(target)
        return str(target)

    def _cleanup_recovery_history(self):
        if self._worker_running(self._recovery_worker):
            return
        from prism import fileio
        store = self._recovery_store
        def cleanup(*, worker):
            try:
                store.cleanup()
                worker.finished.emit('', [])
            except Exception as error:
                logger.exception('Recovery history cleanup failed')
                worker.finished.emit('', [str(error)])
        worker = self._recovery_worker = fileio.ThreadedIO(cleanup)
        stopped = QtCore.QThread.finished.__get__(worker, type(worker))
        def release():
            if self._recovery_worker is worker:
                self._recovery_worker = None
            worker.func = None; worker.args = (); worker.kwargs = {}
            worker.deleteLater()
        stopped.connect(release)
        worker.start()

    def _finish_recovery_session(self):
        if getattr(self, '_recovery_session_closed', False) or self._recovery_worker is not None:
            return
        if not self._has_recovery_records:
            self._recovery_session_closed = True
            return
        from prism import fileio
        store, session = self._recovery_store, self._recovery_session
        def finish(*, worker):
            try:
                store.finish_session(session)
            except Exception:
                logger.exception('Recovery session could not be marked closed')
            worker.finished.emit('', [])
        worker = self._recovery_worker = fileio.ThreadedIO(finish)
        worker.finished.connect(lambda *_: setattr(self, '_recovery_session_closed', True))
        stopped = QtCore.QThread.finished.__get__(worker, type(worker))
        def release():
            if self._recovery_worker is worker:
                self._recovery_worker = None
            worker.func = None; worker.args = (); worker.kwargs = {}
            worker.deleteLater()
        stopped.connect(release)
        worker.start()

    def _is_recovery_project(self, filename):
        from pathlib import Path
        return bool(filename) and Path(filename).resolve().is_relative_to(
            self._recovery_store.root / 'drafts')

    def _write_recovery_snapshot(self):
        # A saved project's geometry/originals are covered by the normal save
        # transaction. This extra checkpoint covers uncommitted page editors.
        if (not self.view.is_dirty() or self._worker_running(self._recovery_worker)
                or getattr(self, '_recovery_capture', None) is not None
                or getattr(self, '_pending_recovery', None)):
            return False
        if not self.view.filename:
            return self.view.autosave_tick() in ('started', 'already_running')
        token = self._recovery_token()
        if token == self._recovery_snapshot_token:
            return True
        if self._is_recovery_project(self.view.filename):
            return True  # Its complete project is already discoverable by marker.
        if self._store_current_page() is False:
            self.statusBar().showMessage('恢复记录未更新：编辑器内容尚未取得', 6000)
            return False
        token = self._recovery_token()
        scene = self.view.scene
        attachments = getattr(scene, 'attachments', {}) or {}
        if hasattr(attachments, '_dirty'):
            attachments = {key: attachments._cache[key] for key in attachments._dirty
                           if key in attachments._cache}
        payload = dict(project_id=scene.project_id, filename=self.view.filename,
            revision=list(token[2:]), note_html=scene.note_html, mindmap_tree=scene.mindmap_tree,
            workspace_pages=scene.workspace_pages, workspace_nodes=scene.workspace_nodes,
            attachments=[(key, *entry) for key, entry in attachments.items()],
            scope='page_editors')
        from prism.fileio.sliced_copy import sliced_copy
        self._recovery_capture = sliced_copy(payload)
        timer = self._recovery_capture_timer = QtCore.QTimer(self)
        timer.setInterval(0)
        def capture():
            if token != self._recovery_token() or not self.view.is_dirty():
                timer.stop(); timer.deleteLater(); self._recovery_capture = None
                return
            deadline = time.monotonic() + 0.004
            try:
                while time.monotonic() < deadline:
                    next(self._recovery_capture)
            except StopIteration as complete:
                timer.stop(); timer.deleteLater(); self._recovery_capture = None
                self._persist_recovery(complete.value, token)
            except Exception as error:
                timer.stop(); timer.deleteLater(); self._recovery_capture = None
                logger.exception('Recovery capture failed')
                self.statusBar().showMessage('恢复记录失败：' + str(error), 0)
        timer.timeout.connect(capture)
        timer.start()
        return True

    def _persist_recovery(self, payload, token):
        from prism import fileio
        session = self._recovery_session
        store = self._recovery_store
        def write(*, worker):
            try:
                path = store.write(payload, session=session)
                worker.finished.emit(path, [])
            except Exception as error:
                logger.exception('Recovery record could not be written')
                worker.finished.emit('', [str(error)])
        worker = self._recovery_worker = fileio.ThreadedIO(write)
        def completed(path, errors):
            if errors:
                self.statusBar().showMessage('恢复记录失败：' + '; '.join(errors), 0)
            else:
                self._has_recovery_records = True
                if token == self._recovery_token() and self.view.is_dirty():
                    self._recovery_snapshot_token = token
                # A confirmed save may have happened during this disk write.
                # It may only resolve its own project's older checkpoint.
                if token[:3] == self._recovery_token()[:3] and not self.view.is_dirty():
                    self._recovery_resolve_token = token[:3]
        worker.finished.connect(completed)
        stopped = QtCore.QThread.finished.__get__(worker, type(worker))
        def release():
            if self._recovery_worker is worker:
                self._recovery_worker = None
            worker.func = None; worker.args = (); worker.kwargs = {}
            worker.deleteLater()
            if (getattr(self, '_recovery_resolve_token', None) == self._recovery_token()[:3]
                    and not self.view.is_dirty()):
                self._recovery_resolve_token = None
                self.clear_recovery_snapshot()
        stopped.connect(release)
        worker.start()

    def clear_recovery_snapshot(self):
        from prism.recovery import project_key
        if self._is_recovery_project(self.view.filename):
            return
        if not self._has_recovery_records and not getattr(self, '_draft_marker_path', None):
            return
        self._recovery_snapshot_token = None
        # Resolution uses the current project, never a global settings pointer.
        # The writer may still be publishing a checkpoint; its completion path
        # resolves that record only if this revision is still clean.
        if self._worker_running(self._recovery_worker):
            return
        from prism import fileio
        key = project_key(self.view.scene.project_id, self.view.filename)
        draft = getattr(self, '_draft_marker_path', None)
        draft_key = project_key(self.view.scene.project_id, draft) if draft else None
        def resolve(*, worker):
            try:
                self._recovery_store.resolve(key, session=self._recovery_session)
                if draft_key:
                    self._recovery_store.resolve(draft_key, session=self._recovery_session)
                worker.finished.emit('', [])
            except Exception as error:
                logger.exception('Recovery checkpoint could not be resolved')
                worker.finished.emit('', [str(error)])
        worker = self._recovery_worker = fileio.ThreadedIO(resolve)
        stopped = QtCore.QThread.finished.__get__(worker, type(worker))
        def release():
            if self._recovery_worker is worker:
                self._recovery_worker = None
            worker.func = None; worker.args = (); worker.kwargs = {}
            worker.deleteLater()
        stopped.connect(release)
        worker.start()

    def _offer_recovery_snapshot(self):
        candidates = self._recovery_candidates
        if not candidates:
            return
        labels = [str(value['payload'].get('filename') or '未命名工程') +
                  time.strftime(' · %m-%d %H:%M:%S', time.localtime(value['created']))
                  for value in candidates]
        selected, accepted = QtWidgets.QInputDialog.getItem(self, '发现可恢复记录',
            '选择要恢复的工程；取消会保留记录。页面检查点只恢复文档、脑图及其附件，'
            '其他内容沿用原工程已保存状态。未命名工程记录为完整工程。', labels, 0, False)
        if not accepted:
            return
        record = candidates[labels.index(selected)]
        location = QtWidgets.QFileDialog.getExistingDirectory(self, '选择恢复副本的保存位置')
        if not location:
            return
        from pathlib import Path
        from prism import fileio, widgets
        from prism.recovery import recover_copy
        import uuid
        destination = Path(location) / ('Prism-恢复-' + uuid.uuid4().hex[:8] + '.prismproj')
        self._pending_recovery = record
        def recover(*, worker):
            try:
                output = recover_copy(record, destination, cancel=lambda: worker.canceled)
                self._recovery_store.resolve(record['key'], through=record['created'])
                worker.finished.emit(output, [])
            except Exception as error:
                logger.exception('Recovery copy failed')
                worker.finished.emit('', [str(error)])
        worker = self._recovery_worker = fileio.ThreadedIO(recover)
        def finished(path, errors):
            self._pending_recovery = None
            if errors:
                self.statusBar().showMessage('恢复失败，原工程和恢复记录均已保留：' + '; '.join(errors), 0)
                return
            self.view.open_from_file(path)
        worker.finished.connect(finished)
        stopped = QtCore.QThread.finished.__get__(worker, type(worker))
        def release():
            if self._recovery_worker is worker:
                self._recovery_worker = None
            worker.func = None; worker.args = (); worker.kwargs = {}
            worker.deleteLater()
        stopped.connect(release)
        self._recovery_progress = widgets.PrismProgressDialog('正在创建恢复副本…', worker=worker, parent=self)
        worker.start()

    # ── 批量（C7 / §7.5）──────────────────────────────────────────


    # The canvas/mind map/document entries are generated on demand rather
    # than stored, so the first edit to one has to write it into the
    # project; otherwise a rename would be silently thrown away.
    IMPLICIT_TITLES = {'default-canvas': '默认画布',
                       'default-mindmap': '默认脑图',
                       'default-document': '默认文档'}


    # ── 筛选条与筛选弹层（§8.3）──────────────────────────────────

    def _on_recount(self):
        """颜色计数 + 筛选条上的结果数，一起刷新。"""
        if getattr(self.view, '_project_opening', False):
            return
        self.color_filter.recount()
        self.canvas_toolbar.refresh()
        if hasattr(self, 'filter_bar'):
            self._refresh_filter_bar()

    def _toggle_filter_popover(self):
        """筛选条的「筛选」按钮：在条下面弹出/收起弹层。"""
        if self.filter_popover.isVisible():
            self.filter_popover.hide()
            self.filter_bar.set_open(False)
            self.canvas_toolbar.filter_button.setChecked(False)
            return
        self.filter_popover.refresh()
        self._position_filter_popover()
        self.filter_popover.show()
        self.filter_popover.raise_()
        self.filter_bar.set_open(True)
        self.canvas_toolbar.filter_button.setChecked(True)

    def _position_filter_popover(self):
        """贴在筛选条下面。"""
        bar = self.filter_bar
        popover = self.filter_popover
        anchor = self.canvas_toolbar.filter_button.mapTo(self.centralWidget(), QtCore.QPoint(0, self.canvas_toolbar.height()))
        popover.setMaximumHeight(max(160, self.centralWidget().height() - anchor.y() - 8))
        popover.adjustSize()
        popover.move(max(0, min(anchor.x(), self.centralWidget().width() - popover.width())), anchor.y() + 4)

    def _clear_filters(self):
        """「清除筛选」：只清筛选，**不切页**（§8.3 的陷阱）。"""
        self.filter_popover.clear_all()
        self._refresh_filter_bar()

    def _remove_filter_chip(self, field, label):
        """点 Chip 上的 ×：只去掉这一个条件。"""
        popover = self.filter_popover
        panel = popover.panel
        if field == 'tag':
            panel._active_tags.discard(label)
        elif field == 'rating':
            panel._min_rating = 0
        elif field == 'untagged':
            panel._untagged_only = False
        elif field == 'colour':
            popover.color_host.clear_filter()
        elif field == 'grayscale' and popover.color_host._grayscale_active:
            popover.color_host.apply_filter('grayscale')
        popover.refresh()
        panel._apply_filter()
        self._refresh_filter_bar()

    def _refresh_filter_bar(self):
        """把当前筛选显示到条上：Chip、结果数、范围。"""
        popover = self.filter_popover
        self.filter_bar.set_entries(popover.active_summary())
        self.filter_bar.set_count(self._visible_item_count())
        self._refresh_filter_scope()

    def _refresh_filter_scope(self):
        """「范围：当前画布」——筛选作用在画布素材上，不跨页。

        （D1 的文件夹总览做出来之后，这里再按"是不是在浏览文件夹"切换成
        「当前文件夹」。）
        """
        self.filter_bar.set_scope('当前画布')

    def _visible_item_count(self):
        # A count needs neither Qt stacking order nor the full save traversal.
        return sum(1 for item in self.view.scene.iter_assets()
                   if hasattr(item, 'save_id') and item.isVisible())


    # `_build_workspace_header` / `_align_workspace_header` 原来在这里。
    # 用户要求（2026-10-01，带红框截图）删掉顶部那条 48 px 的工作区头部
    # ——「未命名项目 / 画布 / 画布 / 检查器」。它显示的东西现在这样找：
    #   · 项目名与文件路径 → 窗口标题（`update_window_title`）
    #   · 左/右面板的显示开关 → 画布右键菜单「界面布局」里的两项
    #     （`_category_action` / `_inspector_action`，折叠会释放中间区域宽度）
    #   · 当前页面位置 → 中央页签本身（文档 / 脑图面板）

    def resizeEvent(self, event):
        super().resizeEvent(event)
        if hasattr(self, '_category_action'):
            QtCore.QTimer.singleShot(0, self._apply_responsive_panels)

    def _sidebar_menu(self):
        menu = QtWidgets.QMenu(self)
        for title, action in [('显示左栏   Ctrl+Alt+1', self._category_action),
                              ('显示右栏   Ctrl+Alt+2', self._inspector_action)]:
            entry = menu.addAction(title)
            entry.setCheckable(True)
            entry.setChecked(action.isChecked())
            entry.toggled.connect(action.setChecked)
        menu.addSeparator()
        menu.addAction('折叠 / 展开左右栏   Tab / Ctrl+Alt+3', self._toggle_sidebars)
        return menu

    def _show_sidebar_menu(self, *_args):
        self._sidebar_menu().exec(QtGui.QCursor.pos())

    def _toggle_sidebars(self):
        expand = not (self._category_action.isChecked() or self._inspector_action.isChecked())
        self._category_action.setChecked(expand)
        self._inspector_action.setChecked(expand)

    def _toggle_sidebar(self, side):
        action = self._category_action if side == 'left' else self._inspector_action
        action.setChecked(not action.isChecked())

    def _apply_responsive_panels(self):
        """Collapse layout columns; never reparent a sidebar over the canvas."""
        if not hasattr(self, '_category_action'):
            return
        sizes = self.splitter.sizes()
        before_left = not self._left_column.isHidden()
        before_right = not self._right_column.isHidden()
        if before_left and sizes[0]:
            self._sidebar_widths['left'] = sizes[0]
        if before_right and sizes[2]:
            self._sidebar_widths['right'] = sizes[2]
        show_left = self._category_action.isChecked()
        from prism.widgets.mindmap_panel import MindMapPanel
        is_mindmap = isinstance(self.tabs.currentWidget(), MindMapPanel)
        show_right = self._inspector_action.isChecked() and not is_mindmap
        self._left_column.setVisible(show_left)
        self._right_column.setVisible(show_right)
        controller = getattr(self, 'search_controller', None)
        material_mode = self.tabs.currentWidget() in (self.canvas_page, self.asset_browser, getattr(controller, 'page', None))
        self.view._detail_panel.setVisible(material_mode)
        library_mode = self.tabs.currentWidget() is self.material_library
        self.material_library.details.setVisible(library_mode)
        model_library = getattr(self, 'model_library', None)
        model_mode = model_library is not None and self.tabs.currentWidget() is model_library
        if model_library is not None:
            model_library.details.setVisible(model_mode)
        self._right_empty.setVisible(not material_mode and not library_mode and not model_mode)
        self._restore_sidebars.setVisible(not show_left)
        host = self._right_controls_layout if show_right else self._collapsed_window_layout
        if self._window_controls.parentWidget() is not (self._right_header if show_right else self._collapsed_window_host):
            host.addWidget(self._window_controls)
        self._collapsed_window_host.setVisible(not show_right)
        if before_left != show_left or before_right != show_right:
            left = self._sidebar_widths['left'] if show_left else 0
            right = self._sidebar_widths['right'] if show_right else 0
            self.splitter.setSizes([left, max(1, self.splitter.width() - left - right), right])
        if self.filter_popover.isVisible():
            self._position_filter_popover()

    def eventFilter(self, watched, event):
        if (event.type() == QtCore.QEvent.Type.ShortcutOverride
                and event.key() == Qt.Key.Key_Tab
                and isinstance(watched, QtWidgets.QWidget)
                and (watched.window() is self)):
            inspector = getattr(self, '_right_column', None)
            inputs = (QtWidgets.QLineEdit, QtWidgets.QTextEdit, QtWidgets.QPlainTextEdit,
                      QtWidgets.QAbstractSpinBox, QtWidgets.QComboBox, QtWidgets.QAbstractSlider)
            if (isinstance(watched, inputs)
                    or (inspector is not None and inspector.isAncestorOf(watched))):
                # Tab completes a field and moves focus inside inspectors. It
                # must not invoke the canvas shortcut and hide that editor.
                event.accept()
                return True
        # 方案 §9.2：无边框后，菜单栏空白区负责窗口拖动 / 双击最大化。
        # 只拦截落在菜单栏、且不在任何菜单项上的鼠标事件；命中菜单项时
        # 返回 False 让 QMenuBar 正常开菜单，不影响菜单交互。
        # 用缓存引用 `_title_menubar` 而不是 self.menuBar() —— 本窗口被
        # 装成了 application 级过滤器（见 __init__ 的 installEventFilter），
        # 每个事件都会进来；menuBar() 在菜单栏为空时会**惰性新建**一个并发
        # 出子事件，那些事件又被本过滤器收到 → 无限递归。缓存引用无副作用。
        menubar = getattr(self, '_title_menubar', None)
        if menubar is not None and watched is menubar:
            if self._handle_titlebar_mouse(event):
                return True
        popover = getattr(self, 'filter_popover', None)
        if popover is not None and popover.isVisible():
            close = event.type() == QtCore.QEvent.Type.KeyPress and event.key() == Qt.Key.Key_Escape
            if event.type() == QtCore.QEvent.Type.MouseButtonPress and isinstance(watched, QtWidgets.QWidget):
                close = not (watched is popover or popover.isAncestorOf(watched) or watched is self.canvas_toolbar.filter_button)
            if close:
                popover.hide()
                self.filter_bar.set_open(False)
                self.canvas_toolbar.filter_button.setChecked(False)
                if event.type() == QtCore.QEvent.Type.KeyPress:
                    return True
        return super().eventFilter(watched, event)

    def _handle_titlebar_mouse(self, event):
        """菜单栏空白区的拖动 / 双击最大化。命中菜单项则不接管。"""
        etype = event.type()
        if etype not in (QtCore.QEvent.Type.MouseButtonPress,
                         QtCore.QEvent.Type.MouseButtonDblClick):
            return False
        bar = getattr(self, '_title_menubar', None)
        if bar is None:
            return False
        try:
            pos = event.position().toPoint()
        except AttributeError:  # pragma: no cover - 非鼠标事件
            return False
        if bar.actionAt(pos) is not None:
            # 落在某个菜单标题上：交回 QMenuBar，照常下拉菜单。
            return False
        if etype == QtCore.QEvent.Type.MouseButtonDblClick:
            self._toggle_maximize()
            return True
        if event.button() == Qt.MouseButton.LeftButton:
            handle = self.windowHandle()
            if handle is not None and hasattr(handle, 'startSystemMove'):
                # 交给系统移动：贴靠、多显示器、DPI 全部原生处理，
                # 比自己按鼠标位移 move() 稳（方案 §9.2）。
                if handle.startSystemMove():
                    return True
        return False

    def _toggle_maximize(self):
        if self.isMaximized():
            self.showNormal()
        else:
            self.showMaximized()

    def set_native_titlebar(self, visible):
        """切换「系统原生标题栏 / 无边框外壳」，并同步窗口控制按钮显隐。

        无边框（visible=False，默认）时显示我们自己的最小化/最大化/关闭
        按钮；切回系统原生标题栏（visible=True）时隐藏它们，避免和系统
        的按钮重复。缩放/贴靠在两种状态下都保留（WS_THICKFRAME 没动）。
        """
        super().set_native_titlebar(visible)
        controls = getattr(self, '_title_controls', None)
        if controls is not None:
            controls.setVisible(not visible)

    def _build_layout_menu(self):
        menu = self.view.context_menu.addMenu(_('Interface Layout'))
        self._category_action = menu.addAction(
            _('Show the Page and Tag Bar'))
        self._category_action.setCheckable(True)
        self._category_action.setChecked(self.view.settings.value(
            'MainWindow/showCategories', True, type=bool))
        self._category_action.toggled.connect(self._apply_responsive_panels)
        self._category_action.toggled.connect(
            lambda checked: self.view.settings.setValue(
                'MainWindow/showCategories', checked))
        self._inspector_action = menu.addAction('显示检查器')
        self._inspector_action.setCheckable(True)
        self._inspector_action.setChecked(self.view.settings.value(
            'MainWindow/showInspector', True, type=bool))
        self._inspector_action.toggled.connect(self._apply_responsive_panels)
        self._inspector_action.toggled.connect(
            lambda checked: self.view.settings.setValue(
                'MainWindow/showInspector', checked))
        menu.addAction(_('Reset Panel Width'), self.reset_layout)
        fonts = menu.addMenu(_('Font Size'))
        self.font_actions = QtGui.QActionGroup(self)
        self.font_actions.setExclusive(True)
        # These labels are deliberately left untranslated: they double as the
        # persisted value of Interface/font_preset, so translating them would
        # invalidate the setting of every existing user. The library
        # regression tests also assert on the Chinese labels.
        current = self.view.settings.value('Interface/font_preset', '中')
        for label, size in [('小', 12), ('中', 13), ('标准', 14)]:
            action = fonts.addAction(label)
            action.setCheckable(True)
            action.setData(size)
            self.font_actions.addAction(action)
            action.setChecked(label == current)
            action.triggered.connect(lambda checked, a=action: self._change_font_size(a))
        selected = self.font_actions.checkedAction() or self.font_actions.actions()[1]
        selected.setChecked(True)
        self._change_font_size(selected)

        # 界面缩放（§3.1）：Qt 只接受**启动前**设好的缩放比例，所以这一档
        # 是「重启后生效」—— 菜单里就这么写，不做看起来立刻生效的假入口。
        from prism import ui_scale

        scales = menu.addMenu('界面缩放')
        scales.setToolTip('重启 Prism 之后生效')
        self.scale_actions = QtGui.QActionGroup(self)
        self.scale_actions.setExclusive(True)
        current_scale = ui_scale.remembered_scale(self.view.settings)
        for percent in ui_scale.SCALES:
            action = scales.addAction(f'{percent}%')
            action.setCheckable(True)
            action.setChecked(percent == current_scale)
            action.setToolTip('重启 Prism 之后生效')
            self.scale_actions.addAction(action)
            action.triggered.connect(
                lambda checked, value=percent: self._change_ui_scale(value))

    def _change_ui_scale(self, percent):
        """记下界面缩放档位；下一次启动时由 ui_scale 交给 Qt。"""
        from prism import ui_scale

        if not ui_scale.remember_scale(percent, self.view.settings):
            return
        QtWidgets.QMessageBox.information(
            self, '界面缩放',
            f'界面缩放已设为 {percent}%。\n重启 Prism 之后生效。')

    def _change_font_size(self, action):
        self.view.settings.setValue('Interface/font_preset', action.text())
        size = action.data()
        font = QtGui.QFont('Microsoft YaHei UI')
        font.setPixelSize(size)
        self.setFont(font)

    def _scene_alive(self):
        """Whether the scene's C++ object still exists.

        Scene signals also fire while the scene is being torn down, at which
        point the handlers below would touch a deleted C++ object and log an
        unhandled RuntimeError during shutdown.
        """
        scene = self.view.scene
        return scene is not None and not is_deleted(scene)

    def _on_scene_selection_changed(self):
        if not self._scene_alive():
            return
        self._update_status(None)

    def _on_tab_changed(self, index):
        """Flush the tab being left, refresh the one being entered.

        Both editable panels keep their content in the scene while they are
        hidden and in their own widget while visible; this keeps the two in
        step across tab switches and file loads.
        """
        previous = self.tabs.widget(self._previous_tab)
        token = getattr(self, '_navigation_committed_page', None)
        # `self._previous_tab` can name a slot that no longer holds a widget:
        # the first page is still being built, the panel was removed, or the
        # index is out of range. There is no "tab being left" to flush then, so
        # skip that bookkeeping instead of dereferencing None.
        if previous is not None and not (
                token is not None and token[0] is previous
                and token[1] == self.view.scene.project_id):
            # Restore the outgoing panel before requesting its single async
            # commit. A synchronous pre-flush would read it twice and suppress
            # newer tab intents raised inside that read-back's nested loop.
            target = self.tabs.widget(index)
            blocker = QtCore.QSignalBlocker(self.tabs)
            previous._preserve_editor_on_show = True
            try:
                self.tabs.setCurrentWidget(previous)
            finally:
                previous._preserve_editor_on_show = False
                del blocker
            from prism.item_lifecycle import is_deleted
            self.request_navigation(lambda: self.tabs.setCurrentWidget(target)
                                    if not is_deleted(target) else None)
            return
        if previous is not None and self._store_current_page(previous) is False:
            blocker = QtCore.QSignalBlocker(self.tabs)
            previous._preserve_editor_on_show = True
            try:
                self.tabs.setCurrentIndex(self._previous_tab)
            finally:
                previous._preserve_editor_on_show = False
                del blocker
            return
        current = self.tabs.widget(index)
        with operation('page.tab_changed.enter', index=index,
                       panel=type(current).__name__ if current is not None else None):
            self.view.set_material_navigation_active(
                current in (getattr(self, 'material_library', None),
                            getattr(self, 'model_library', None)))
            if hasattr(self, '_eagle_header_stack'):
                browsing = current is self.asset_browser
                material_canvas = browsing or current is self.canvas_page
                self._eagle_header_stack.setCurrentWidget(self.canvas_toolbar if material_canvas else self._eagle_drag)
                self._centre_header.setFixedHeight(self.canvas_toolbar.height() if material_canvas else 36)
                self._centre_status.setVisible(material_canvas)
                self.view.category_panel.search.setVisible(not hasattr(self, 'search_controller') and not browsing)
                self.view.category_panel.search_type.setVisible(not hasattr(self, 'search_controller') and not browsing)
                self.view.category_panel.hint.setVisible(
                    not browsing and current not in
                    (self.material_library, self.model_library))
            if hasattr(current, 'load_from_scene'):
                with operation('page.tab_changed.load_from_scene',
                               panel=type(current).__name__):
                    current.load_from_scene()
            self._previous_tab = index
            is_canvas = index == 0
            # 常驻颜色条没有了（§8.3）：跟着画布页显隐的是这条 36 px 筛选条。
            self.filter_bar.hide()
            if not is_canvas:
                self.filter_popover.hide()
                self.canvas_toolbar.filter_button.setChecked(False)
            self._apply_responsive_panels()

            preview = getattr(self.view, '_upgrade_preview_session', None)
            if preview is not None:
                preview.guard.enable_browsing()

    def _sync_detail_visibility(self):
        """Compatibility hook: selection must not change pane visibility.

        它以前顺手刷新工作区头部里那个「检查器 · 已选 N 项」的标题；
        头部按用户要求删掉之后，这里没有可刷的东西了。
        """

    def reset_layout(self):
        self._category_action.setChecked(True)
        self._inspector_action.setChecked(True)
        self._apply_responsive_panels()
        shell = ui_tokens.LAYOUT
        self.splitter.setSizes([
            shell['left-panel-width'],
            max(shell['centre-min-width'], self.width() -
                shell['left-panel-width'] - shell['right-panel-width']),
            shell['right-panel-width']])

    def _update_status(self, counts, *, interaction=False):
        """Publish the bottom bar.

        ``interaction=True`` is the zoom / wheel path.  It must not open the
        directory, must not stat the archive and must not rebuild the tag
        tree, so it only reads what was published last.  When nothing has
        been confirmed for this revision yet, the bar asks for a publication
        and waits for it - it never invents a number, and it never shows an
        unknown count as ``0``.
        """
        scene = self.view.scene
        panel = self.view.category_panel
        incoming = counts.get('snapshot') if isinstance(counts, dict) else None
        if isinstance(incoming, CountPublication):
            self._accept_publication(incoming)
        snapshot = getattr(self, '_status_count_snapshot', None)
        publication = getattr(self, '_latest_publication', None)
        if interaction:
            # Pure cache: no directory, no archive stat, no tag tree.
            scope = getattr(self.view, '_open_scope', None)
            if (snapshot is None
                    or snapshot[0][:2] != (scene.project_id, scene._asset_revision)
                    or getattr(self, '_status_count_scope', None) is not scope
                    or publication is None
                    or publication.is_stale(
                        project=scene.project_id,
                        open_epoch=getattr(self.view, '_trace_open_epoch', None),
                        overlay_revision=getattr(panel, '_count_revision', None))):
                # Nothing confirmed for this revision: ask for one.  The
                # request is merged with the pending one, so a whole zoom
                # gesture still costs a single recount.
                #
                # A zoom is not a change: it must not move the edit
                # revision, and it must not publish anything - the
                # interaction path stays free of scans and of I/O.
                self._schedule_recount(bump_revision=False, preview=False)
        else:
            key = (scene.project_id, scene._asset_revision, self._open_scope_key())
            if not getattr(self.view, '_project_opening', False) and (
                    snapshot is None or snapshot[0] != key or bool(counts)):
                # The publication carries the whole project numbers, so the
                # sidebar and the status bar answer from one computation.
                count_snapshot = panel.count_snapshot_for_display()
                snapshot = self._status_count_snapshot = (
                    key, count_snapshot['total'], count_snapshot['visible'])
                self._status_count_scope = getattr(self.view, '_open_scope', None)
        self._render_status()

    def _accept_publication(self, incoming):
        """Apply a publication - if it really belongs to this state.

        The token alone is not enough: a newer token can still be another
        project's answer, another open of the same project, another query
        or an older edit revision.  Those are dropped.
        """
        reason = incoming.rejection_reason(
            **self.view.category_panel.count_identity(),
            current=getattr(self, '_latest_publication', None))
        if reason is not None:
            logger.debug('dropped a stale count publication: %s', reason)
            return False
        self._latest_publication = self._with_external_counts(incoming)
        return True

    def _with_external_counts(self, publication):
        """Add the counters that came from outside the panel.

        The canvas (how much is really painted) and the result list (how
        many rows the current answer has) are published into the window,
        not into the panel.  They survive only publications for the same
        project, open, query and revisions.
        """
        external = getattr(self, '_external_counts', None)
        if not external or publication is None:
            return publication
        counts = dict(publication.counts)
        bases = dict(publication.bases)
        generations = dict(publication.generations)
        for kind, entry in list(external.items()):
            if not isinstance(entry, CountPublication) or any(
                    getattr(entry, name) != getattr(publication, name)
                    for name in ('project', 'open_epoch', 'query',
                                 'source_revision', 'overlay_revision')):
                external.pop(kind, None)
                continue
            counts[kind] = entry.value(kind)
            bases[kind] = entry.basis_of(kind)
            generation = entry.generations.get(kind)
            if generation is not None:
                generations[kind] = generation
        return CountPublication(
            request_token=publication.request_token,
            project=publication.project,
            query=publication.query,
            source_revision=publication.source_revision,
            overlay_revision=publication.overlay_revision,
            open_epoch=publication.open_epoch,
            complete=publication.complete,
            counts=counts,
            tag_counts=publication.tag_counts,
            bases=bases,
            pending=publication.pending,
            tag_counts_by_basis=publication.tag_counts_by_basis,
            generations=generations,
        )

    def _on_counts_previewed(self, counts):
        """The immediate, partial publication: the status bar only.

        No directory query, no navigation refresh, no colour recount: this
        is the fast path that answers the user's action.  The whole project
        counters keep their last confirmed value and are marked *pending*,
        which the bar shows as "updating…".
        """
        incoming = counts.get('snapshot') if isinstance(counts, dict) else None
        if not isinstance(incoming, CountPublication):
            return
        if self._accept_publication(incoming):
            self._render_status()

    def _on_hot_result_published(self):
        """A result set was published: the counters follow immediately.

        The result rows are **their own basis** - they are not the whole
        library, so they never replace the library counters - and they are
        published as ``result.count``.

        No recount is scheduled here on purpose: the project did not change,
        only the answer to a question about it.  A recount would re-run the
        filter and emit ``scene.changed``, which would make the canvas
        repaint for a result list that only the browser shows.
        """
        browser = getattr(self, 'asset_browser', None)
        if browser is None:
            return
        try:
            rows = len(browser.model.items)
        except (AttributeError, RuntimeError):       # pragma: no cover
            return
        self.publish_count(RESULT_COUNT, rows, basis=BASIS_RESULT_ROWS)

    def _render_status(self):
        """Draw the bottom bar from what is already known.

        Pure rendering: the last publication, the last confirmed catalog
        answer, the selection and the zoom.  It never opens the directory
        and never stats the archive, so the canvas can call it after a
        layout commit.
        """
        scene = self.view.scene
        publication = getattr(self, '_latest_publication', None)
        snapshot = getattr(self, '_status_count_snapshot', None)
        if snapshot is not None:
            total, visible = snapshot[1], snapshot[2]
        elif publication is not None:
            total = publication.value(LIBRARY_TOTAL)
            visible = publication.value(LIBRARY_VISIBLE)
        else:
            total = visible = None
        if publication is not None:
            # The preview knows the visible count right now - it is counted
            # from the loaded scene, without the directory.
            preview_visible = publication.value(LIBRARY_VISIBLE)
            if preview_visible is not None:
                visible = preview_visible
        selected = len(scene.selectedItems(user_only=True))
        zoom = self.view.transform().m11() * 100.0
        browser = getattr(self, 'asset_browser', None)
        if browser is not None and self.tabs.currentWidget() is browser:
            visible = len(browser.model.items)
            selected = len(browser.list.selectedIndexes())
            zoom = browser.list.transform().m11()*100.0
        grid_active = bool(browser is not None and self.tabs.currentWidget() is browser
                           and getattr(browser.list, 'is_asset_grid', False))
        drawn = (publication.value(CANVAS_DRAWN)
                 if publication is not None and not grid_active else None)
        panel = self.view.category_panel
        revision = getattr(panel, '_count_revision', None)
        updating = bool(publication is not None and publication.pending)
        if publication is not None and revision is not None \
                and publication.overlay_revision != revision:
            updating = True
        if total is None or (publication is not None and not publication.complete):
            # The whole project answer is partial (the directory is not
            # ready): "shown" would only count the loaded page.  That is
            # unknown, not zero.
            visible = None
        total_text = _('Preparing…') if total is None else f'{total:,}'
        # Unknown is written as "…", never as 0: "0 shown" would be a number
        # nobody measured.
        visible_text = '…' if visible is None else visible
        text = _('{total} items · {visible} shown · {selected} selected').format(
            total=total_text, visible=visible_text, selected=selected)
        if drawn is not None:
            text += _(' · {drawn} {basis}').format(
                drawn=f'{drawn:,}',
                basis=self._drawn_basis_text(
                    publication.basis_of(CANVAS_DRAWN) if publication else ''))
        if updating:
            text += _(' · updating…')
        if grid_active:
            text += _(' · Scroll to browse')
        else:
            text += (_(' · Zoom {zoom:.0f}% · Ctrl+Shift+drag to drag out to '
                       'another application').format(zoom=zoom))
        self._status_text.setText(text)

    @staticmethod
    def _drawn_basis_text(basis):
        """How the painted count has to be read.

        A synthetic far scene can cover many assets with one drawn object,
        and the model length is not a painted count at all - the bar has to
        say which of the three it is showing.
        """
        if basis == BASIS_COMPOSITE:
            return _('drawn (composite group)')
        if basis == BASIS_MODEL_ROWS:
            return _('model rows (not painted)')
        if basis == BASIS_PAINTED:
            return _('drawn')
        return _('drawn')

    def publish_count(self, count_kind, value, *, basis=BASIS_PAINTED,
                      generation=None, complete=True, query=None, identity=None):
        """Publish one counter the directory cannot answer.

        The canvas knows how many assets are actually painted; that is not
        the same as "how many rows the model holds", and the directory
        cannot answer it.  The canvas calls this after a layout commit
        instead of reaching into the catalog, so the zoom path stays free of
        SQLite and archive stats.

        ``basis`` says what the number was counted over: ``painted`` for
        one painted id per asset, ``composite`` for a synthetic far scene
        that covers many assets, ``model-rows`` for the model length.  A
        model length is **not** a painted count and must not be reported as
        one.

        ``generation`` names the query generation the painted set belongs
        to.  A publication of an older generation is refused, so a slow
        layout cannot overwrite the numbers of a newer one.
        """
        current_identity = self.view.category_panel.count_identity()
        # Async producers pass the identity captured when their work began.
        # Legacy synchronous callers use the current cached identity.
        supplied_identity = dict(current_identity if identity is None else identity)
        if query is not None:
            supplied_identity['query'] = query
        if supplied_identity != current_identity:
            logger.debug('dropped an external count of another identity: %s',
                         count_kind)
            return getattr(self, '_latest_publication', None)
        publication = getattr(self, '_latest_publication', None)
        if publication is not None and any(
                getattr(publication, name) != expected
                for name, expected in current_identity.items()):
            publication = None
        if not hasattr(self, '_external_counts'):
            self._external_counts = {}
        if publication is not None:
            publication = self._with_external_counts(publication)
        counts = dict(publication.counts) if publication is not None else {}
        bases = dict(publication.bases) if publication is not None else {}
        generations = (dict(publication.generations)
                       if publication is not None else {})
        if generation is not None:
            current = generations.get(count_kind)
            if current is not None:
                try:
                    if generation < current:
                        return getattr(self, '_latest_publication', None)
                except TypeError:
                    # Incomparable generations cannot prove freshness.
                    return getattr(self, '_latest_publication', None)
        counts[count_kind] = int(value) if value is not None else None
        bases[count_kind] = basis
        if generation is not None:
            generations[count_kind] = generation
        self._external_counts[count_kind] = CountPublication(
            **current_identity, counts={count_kind: counts[count_kind]},
            bases={count_kind: basis},
            generations=({count_kind: generation}
                         if generation is not None else {}))
        self._latest_publication = CountPublication(
            request_token=next_request_token(), **current_identity,
            complete=bool(complete) and (publication.complete
                                         if publication is not None else False),
            counts=counts, bases=bases, generations=generations,
            pending=(publication.pending if publication is not None
                     else frozenset()),
            tag_counts=(publication.tag_counts if publication is not None else {}),
            tag_counts_by_basis=(publication.tag_counts_by_basis
                                 if publication is not None else {}),
        )
        self._render_status()
        return self._latest_publication

    def _open_scope_key(self):
        """按页打开时的目录身份，混进状态栏的缓存 key。

        只做 stat 级指纹（`source_state`），不读目录表，所以每次刷新多花的
        是两次 stat，而不是一次 SQLite 打开。
        """
        scope = getattr(self.view, '_open_scope', None)
        if scope is None:
            return None
        try:
            return (str(scope.project_path), str(scope.source_state()))
        except Exception:
            logger.exception('Could not fingerprint the project directory')
            return None

    @property
    def _recount_timer(self):
        """The one recount timer of the application.

        The sidebar panel owns it, because the panel is what computes and
        publishes the counts.  There used to be a second timer here, so a
        single notification could schedule the same work twice.  Without a
        panel the window falls back to its own timer.
        """
        panel = getattr(getattr(self, 'view', None), 'category_panel', None)
        if panel is not None:
            return panel._recount_timer
        return self._fallback_recount_timer

    def _schedule_recount(self, *, bump_revision=True, preview=True):
        # A playing video continuously changes the scene; don't starve updates.
        if getattr(self.view, '_project_opening', False):
            return
        panel = getattr(self.view, 'category_panel', None)
        if panel is not None:
            # One snapshot publishes both sidebar and status counts.
            panel._schedule_recount(bump_revision=bump_revision,
                                    preview=preview)
            return
        if not self._recount_timer.isActive():
            self._recount_timer.start()

    def closeEvent(self, event):
        storage=getattr(self.view,'_storage_operation',None)
        try:
            from prism.model_library.events import record_model_event
            operation = getattr(self.view, '_opening_operation', None)
            record_model_event('app_closing',
                               storage_running=bool(storage is not None
                                                    and storage.isRunning()),
                               opening_pending=bool(operation is not None
                                                    and not operation.terminal),
                               state='closing')
        except Exception:                              # noqa: BLE001 - logging only
            pass
        if storage is not None and storage.isRunning():
            storage.requestInterruption()
            self.statusBar().showMessage('正在安全结束工程存储操作，请稍后关闭',5000)
            event.ignore()
            return
        operation = getattr(self.view, '_opening_operation', None)
        if operation is not None and not operation.terminal:
            # Do not store an old editor into a scene the reader is replacing.
            self.view._allow_project_replacement()
            event.ignore()
            return
        if self._store_current_page() is False:
            event.ignore()
            return
        worker = getattr(self.view, 'worker', None)
        if isinstance(worker, QtCore.QThread) and worker.isRunning():
            event.ignore()
            return
        if not self.view.get_confirmation_unsaved_changes(
                _('There are unsaved changes. Discard them and close the window?')):
            event.ignore()
            return
        self._autosave_timer.stop()
        self._recovery_closing = True
        self._recovery_timer.stop()
        self._recovery_cleanup_timer.stop()
        capture_timer = getattr(self, '_recovery_capture_timer', None)
        if capture_timer is not None:
            try:
                capture_timer.stop()
            except RuntimeError:
                pass
        self._recovery_capture = None
        self._finish_recovery_session()
        # Catalog builders are daemon workers, not ``view.worker`` QThreads.
        # Invalidate their subscriptions and request cooperative cancellation
        # before QObject teardown; never join them from this GUI callback.
        self.view._cancel_catalog_builds()
        self.view._release_project_resource_scope()
        self.model_composition.close()
        self.view.settings.setValue('MainWindow/geometry', self.saveGeometry())
        self._apply_responsive_panels()
        self.view.settings.setValue('MainWindow/splitter', self.splitter.saveState())
        for side, width in self._sidebar_widths.items():
            self.view.settings.setValue('MainWindow/'+side+'Width', width)
        # The existing quit guard keeps Qt/services alive until this final
        # snapshot is durable. Never wait on the writer from the close event.
        self.view.settings.request_sync()
        self._arm_capture_exit()
        self.view.scene.deselect_all_items()
        # Stop and detach even paused videos before the event loop exits.
        # Retained originals are removed later, after Qt releases its handles.
        from prism.items import PrismVideoItem
        for item in self.view.scene.iter_assets():
            if isinstance(item, PrismVideoItem):
                item.cleanup()
        # The capture server is only asked to stop here. `socketserver.
        # shutdown()` blocks until the serving thread leaves its loop, and
        # that wait must not happen inside a GUI callback: `stop()` hands
        # the request to a helper thread and the `stopped` signal reports
        # when the server is really gone.
        if self.browser_capture.is_running():
            self._arm_capture_exit()
        self.browser_capture.stop()
        capture = getattr(self.view, '_capture_service', None)
        if capture is not None:
            capture.close()
        self._capture_hotkey.close()
        downloads = getattr(self.view, '_video_download_service', None)
        if downloads is not None:
            downloads.close()
        visual = getattr(getattr(self, 'search_controller', None), 'visual_search', None)
        if visual is not None:
            visual.service.close()
        # Only stop after all close/save vetoes have passed.  Do not create
        # a materialization service just to close an unused one, and do not
        # wait for archive I/O inside this GUI callback.
        from prism.source_store import stop_accepting_materialization
        stop_accepting_materialization()
        event.accept()

    def _arm_capture_exit(self):
        """Keep Qt alive until settings and owned capture/download helpers finish."""
        app = QtWidgets.QApplication.instance()
        if app is None or self._capture_exit_pending:
            return
        app.setQuitOnLastWindowClosed(False)
        self._capture_exit_pending = True
        self._capture_exit_timer = QtCore.QTimer(self)
        self._capture_exit_timer.setSingleShot(True)
        self._capture_exit_timer.setInterval(50)
        self._capture_exit_timer.timeout.connect(self._on_capture_stopped)
        self._capture_exit_timer.start()

    def _on_capture_stopped(self):
        """Quit only after every owned shutdown dependency has finished."""
        if not self._capture_exit_pending:
            return
        pending_settings = self.view.settings.sync_pending()
        processes = []
        capture = getattr(self.view, '_capture_service', None)
        if capture is not None:processes.append(capture.process)
        downloads = getattr(self.view, '_video_download_service', None)
        if downloads is not None:
            processes.extend(job.get('process') for job in downloads.jobs.values())
        visual = getattr(getattr(self, 'search_controller', None), 'visual_search', None)
        if visual is not None:processes.append(visual.service.process)
        running_processes = any(process is not None and
            process.state() != QtCore.QProcess.ProcessState.NotRunning for process in processes)
        if getattr(self, '_recovery_closing', False):
            self._finish_recovery_session()
        # Wait for GUI delivery/release as well as the native thread exit.
        recovery_pending = getattr(self, '_recovery_worker', None) is not None
        if self.browser_capture.is_running() or pending_settings or running_processes or recovery_pending:
            # A tick is not proof of completion; keep processing Qt signals
            # while the real writer/server/child process finishes.
            timer = self._capture_exit_timer
            if timer is not None:
                timer.start(50)
            return
        self._capture_exit_pending = False
        timer = self._capture_exit_timer
        self._capture_exit_timer = None
        if timer is not None:
            timer.stop()
        app = QtWidgets.QApplication.instance()
        if app is not None:
            app.quit()


#: Timers created by :func:`safe_timer`.  A QTimer that only Python holds
#: would be collected and stop firing, so the process keeps them here.
_TIMERS = []


def safe_timer(timeout, func, *args, **kwargs):
    """Create a timer that is safe against garbage collection and
    overlapping calls.
    See: http://ralsina.me/weblog/posts/BB974.html

    One timer is reused for the whole life of the callback.  Rescheduling
    with ``QTimer.singleShot`` built a new QObject and a new connection on
    every tick -- twenty of them a second, forever, for a callback that
    exists only to let the interpreter handle signals.  A repeating timer
    cannot overlap either: Qt never re-enters a slot that is still running.
    """
    timer = QtCore.QTimer()

    def timer_event():
        func(*args, **kwargs)

    timer.timeout.connect(timer_event)
    timer.start(timeout)
    _TIMERS.append(timer)
    return timer


def handle_sigint(signum, frame):
    logger.info('Received interrupt. Exiting...')
    QtWidgets.QApplication.quit()


def handle_uncaught_exception(exc_type, exc, traceback):
    app = QtWidgets.QApplication.instance()
    trace = getattr(app, '_performance_trace', None) if app else None
    if trace is not None:
        report = trace.crash_report(exc_type, exc, traceback)
        logger.critical('Crash summary: %s', report)
    logger.critical('Unhandled exception',
                    exc_info=(exc_type, exc, traceback))
    app = QtWidgets.QApplication.instance()
    if app is not None:
        try:
            QtWidgets.QMessageBox.critical(
                None, _('Prism encountered an error'),
                _('{error}\n\nThe diagnostic log is here:\n{path}')
                .format(error=str(exc) or exc_type.__name__,
                        path=logfile_name()))
        except RuntimeError:
            # Qt may already be tearing down; the exception is still logged.
            pass
        app.quit()


def _register_file_association():
    """Register .prism file type with Windows (HKCU, no admin needed)."""
    if sys.platform != 'win32':
        return
    try:
        import winreg
        # Determine exe path
        if getattr(sys, 'frozen', False):
            exe_path = sys.executable
            # The executable icon is persistent, including after uninstall/restart.
            icon_path = exe_path
        else:
            # Source runs must not replace the installed application's association.
            return

        # Convert backslashes for registry
        exe_path = exe_path.replace('/', '\\')
        icon_path = icon_path.replace('/', '\\')

        # Register file extension
        key = winreg.CreateKey(winreg.HKEY_CURRENT_USER, r'Software\Classes\.prism')
        winreg.SetValueEx(key, '', 0, winreg.REG_SZ, 'Prism.File')
        winreg.CloseKey(key)

        # Register file type
        key = winreg.CreateKey(winreg.HKEY_CURRENT_USER, r'Software\Classes\Prism.File')
        winreg.SetValueEx(key, '', 0, winreg.REG_SZ, 'Prism Project')
        winreg.CloseKey(key)

        # Set default icon
        key = winreg.CreateKey(winreg.HKEY_CURRENT_USER, r'Software\Classes\Prism.File\DefaultIcon')
        winreg.SetValueEx(key, '', 0, winreg.REG_SZ, f'"{icon_path}"')
        winreg.CloseKey(key)

        # Set open command
        key = winreg.CreateKey(
            winreg.HKEY_CURRENT_USER,
            r'Software\Classes\Prism.File\shell\open\command')
        winreg.SetValueEx(key, '', 0, winreg.REG_SZ, f'"{exe_path}" "%1"')
        winreg.CloseKey(key)

        logger.debug(f'Registered .prism file association: {exe_path}')
    except Exception as e:
        logger.debug(f'File association registration failed: {e}')


def main():
    # Frozen multiprocessing children must enter their worker bootstrap
    # before argument parsing or creating another application window.
    import multiprocessing
    multiprocessing.freeze_support()
    if '--packaged-duplicate-smoke' in sys.argv:
        from prism.release_smoke import run
        output = sys.argv[sys.argv.index('--packaged-duplicate-smoke')+1]
        return sys.exit(run(output))
    if '--ai-inference-worker' in sys.argv:
        from prism.ai_inference_worker import main as inference_main
        return inference_main()
    # Only the running desktop app owns the process exception hook.
    # Importing window classes must not replace an embedding host's hook
    # (including pytest-qt's asynchronous exception capture).
    sys.excepthook = handle_uncaught_exception
    from prism.library_observability import install_failure_hooks
    install_failure_hooks()
    # 启用 faulthandler：ONNX / Qt 原生崩溃（segfault）时把 Python 栈写到日志
    import faulthandler
    _log_path = logfile_name()
    try:
        _fault_path = os.path.join(os.path.dirname(_log_path), f'Prism-native-crash-{os.getpid()}.log')
        _fault_fd = open(_fault_path, 'a', encoding='utf-8')
        faulthandler.enable(file=_fault_fd, all_threads=True)
    except Exception:
        faulthandler.enable()  # fallback: 写 stderr

    logger.info(f'Starting {constants.APPNAME} version {constants.VERSION}')
    logger.debug('System: %s', ' '.join(platform.uname()))
    logger.debug('Python: %d.%d.%d', *sys.version_info[:3])
    logger.debug('LD_LIBRARY_PATH: %s', os.environ.get('LD_LIBRARY_PATH'))
    settings = PrismSettings()
    logger.info(f'Using settings: {settings.fileName()}')
    logger.info(f'Logging to: {logfile_name()}')
    settings.on_startup()
    args = CommandlineArgs(with_check=True)  # Force checking
    assert not args.debug_raise_error, args.debug_raise_error

    # 界面缩放要在 QApplication 之前交给 Qt（§3.1，见 prism/ui_scale.py）
    from prism import ui_scale
    ui_scale.apply_scale_environment(settings)

    app = PrismApplication(sys.argv)
    # TEMPORARY diagnostic: report every timer that is stopped or destroyed on
    # a thread other than its owner.  Qt leaves the timer id registered in the
    # owner thread's dispatcher in that case, and the dispatcher fires the
    # freed timer later.  Remove together with prism/_cross_thread_probe.py.
    from prism._cross_thread_probe import install as _install_thread_probe
    try:
        _install_thread_probe()
    except Exception:
        logger.exception('The cross-thread timer probe could not be installed')
    # Establish the same 13px base font before constructing the widget tree.
    # A universal QSS font rule otherwise repolishes every descendant later.
    base_font = QtGui.QFont()
    base_font.setFamilies(["Microsoft YaHei UI", "Microsoft YaHei", "Segoe UI", "sans-serif"])
    base_font.setPixelSize(13)
    app.setFont(base_font)
    palette = create_palette_from_dict(constants.COLORS)
    app.setPalette(palette)
    # Load Apple-style QSS theme stylesheet
    # In PyInstaller, assets are in sys._MEIPASS; in dev, relative to __file__
    if getattr(sys, 'frozen', False):
        _assets_dir = os.path.join(sys._MEIPASS, 'prism', 'assets')
    else:
        _assets_dir = os.path.join(os.path.dirname(__file__), 'assets')
    qss_path = os.path.join(_assets_dir, 'theme.qss')
    qss = None
    if os.path.exists(qss_path):
        with open(qss_path, 'r', encoding='utf-8') as f:
            qss = f.read()
        # Resolve icon paths to absolute; use forward slashes for QSS
        # (backslashes are treated as escape chars by Qt's CSS parser)
        icons_dir = os.path.join(_assets_dir, 'icons').replace(os.sep, '/')
        qss = qss.replace('url(assets/icons/', f'url({icons_dir}/')
        logger.debug(f'Loaded theme from {qss_path}')

    from prism.widgets.dialog_theme import install as install_dialog_theme
    install_dialog_theme(app)
    # Start tracing before the main window is built, so startup itself is
    # covered. The window is attached (actions hooked) once it exists.
    from prism.performance_trace import install
    try:
        trace = install(app, None, os.path.dirname(logfile_name()))
        logger.info('Operation/performance log: %s', trace.path)
    except OSError:
        trace = None
        logger.exception('Could not start operation/performance log')
    from prism.webengine_runtime import (configure_webengine, warm_text_layout,
                                         warm_web_engine)
    configure_webengine()
    # Chromium starts once per process and that start-up costs ~200-450 ms of
    # GUI-thread time.  Pay it here, while there is no window yet: a window
    # that is frozen before it appears is not a frozen window.
    from prism.recent_usage import ledger
    ledger().refresh()  # Read the small user ledger while startup constructs Qt UI.
    warm_web_engine(app)
    # The platform font engine is paid once per process as well: the first
    # text widget costs 138-202 ms, and in this application it was charged to
    # the welcome overlay's first label - 502 ms of stall after the window
    # was already on its way to the screen.
    warm_text_layout(app)
    bee = PrismMainWindow(app, theme_qss=qss)  # NOQA:F841
    if trace is not None:
        trace.attach_window(bee)
    # TEMPORARY diagnostic: record what changes on screen while a project
    # opens -- the resource tree's model (a reset loses the expansion), the
    # expanded categories, the visible page and the scene's item count --
    # with the Python stack of whoever caused it.  Remove together with
    # prism/_open_diagnostic.py.
    try:
        from prism._open_diagnostic import install as _install_open_diagnostic
        _install_open_diagnostic(bee)
    except Exception:
        logger.exception('The open-path diagnostic could not be installed')
    # The widget tree built above is alive for the whole session, and the
    # collector has no way to know that: every later generation-2 pass
    # walked the whole tree, which the trace recorded as 100-600 ms pauses
    # on the GUI thread.  Collecting once and then freezing moves the
    # startup objects into the permanent generation, where the collector
    # leaves them alone.  Objects created later are still collected
    # normally, so this does not turn the tree into a leak.
    tune_gc_for_large_projects()
    gc.collect()
    gc.freeze()

    # Register .prism file association (HKCU, no admin needed)
    _register_file_association()

    signal.signal(signal.SIGINT, handle_sigint)
    # Repeatedly run python-noop to give the interpreter time to
    # handle signals
    safe_timer(50, lambda: None)

    app.exec()
    # The event loop returned, so the application is leaving on purpose.
    # This line separates "the user closed it" from "the process died":
    # a hard death never reaches it, and the trace writer stops writing
    # its checkpoints at the same moment.
    logger.info('Prism is shutting down')
    # A thread pool deletes its runnable **in the worker thread**, and that
    # runnable can hold the last reference to a GUI object.  When such an
    # object owns a running timer, Qt cannot stop the timer from another
    # thread and leaves the timer id registered in the GUI dispatcher:
    # "QObject::killTimer: Timers cannot be stopped from another thread".
    # The dispatcher fires the dead timer later and the process dies with
    # an access violation.  Draining the pools first keeps every release
    # on a thread that still exists.
    from prism.qt_thread_safety import drain_thread_pools
    busy_pools = drain_thread_pools(timeout_ms=5000)
    if busy_pools:
        logger.warning('%s thread pool(s) were still busy at shutdown', busy_pools)
    # ``aboutToQuit`` normally closes the trace, but a session that ends any
    # other way -- the last window closes, a helper quits the application --
    # would leave the log without its end, and an end-less session reads as
    # a crash. ``close()`` is idempotent, so this is only a fallback, and it
    # still runs while the application exists.
    if trace is not None:
        trace.close()
    # Keep the QObject owners alive until accepted mandatory source reads
    # have completed.  The workers only use archive/file I/O, not queued
    # GUI callbacks.  Optional queued previews may be cancelled by shutdown.
    from prism.source_store import shutdown_materialization_service
    shutdown_materialization_service(wait=True)
    # The model library gives its lease working copies back on a background
    # thread.  A process that ends before that thread finishes leaves the
    # copies on the disk, so the real end waits for it here, where no GUI
    # callback is running and no event loop is needed.  The composition holds
    # every service that was closed, so a project switch in between is
    # covered too.
    composition = getattr(bee, 'model_composition', None)
    wait_closed = getattr(composition, 'wait_closed', None)
    if callable(wait_closed) and not wait_closed(timeout=10):
        logger.warning('The model library did not release its working copies in time')
    # The close path only requested the capture server to stop. Wait for
    # the real end here, with the event loop already finished, so the port
    # is released and no server thread survives this process.
    bee.browser_capture.stop()
    if not bee.browser_capture.wait_for_stop(timeout=5):
        logger.warning('Waiting for the browser capture server to finish stopping')
        # No event loop remains here. Keep QObject owners alive until the real
        # helper/server exit rather than deleting them after an arbitrary timer.
        bee.browser_capture.wait_for_stop(timeout=None)
    # Closing a top-level widget does not delete its C++ object. Python
    # references (including diagnostic/signal cycles) can otherwise defer its
    # destruction until SIP's interpreter shutdown, after QApplication.
    # Drain only deferred deletion while the application still exists.
    bee.deleteLater()
    QtCore.QCoreApplication.sendPostedEvents(None, QtCore.QEvent.Type.DeferredDelete)
    del bee
    del app
    logger.debug('Prism closed')
    QtCore.qInstallMessageHandler(None)


if __name__ == '__main__':
    main()  # pragma: no cover
