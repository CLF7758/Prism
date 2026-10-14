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
import os
import platform
import signal
import time
import sys

from PyQt6 import QtCore, QtGui, QtWidgets
from PyQt6.QtCore import Qt

# QtWebEngineWidgets refuses to load once a QApplication exists, and the mind
# map panel imports it lazily, so pull it in here at import time. Importing
# the module is cheap; Chromium itself only starts when the panel is shown.
try:
    from PyQt6 import QtWebEngineCore, QtWebEngineWidgets  # noqa: F401
except ImportError:                       # pragma: no cover - optional
    pass

from prism import constants, ui_tokens
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

logger = logging.getLogger(__name__)


class PrismApplication(QtWidgets.QApplication):

    def notify(self, receiver, event):
        trace = getattr(self, '_performance_trace', None)
        if trace is None:
            return super().notify(receiver, event)
        started, event_type = time.perf_counter(), event.type().value
        try:
            return super().notify(receiver, event)
        finally:
            trace.dispatch_finished(receiver, event_type, (time.perf_counter()-started)*1000)

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

    def __init__(self, app):
        super().__init__()
        app.setOrganizationName(constants.APPNAME)
        app.setApplicationName(constants.APPNAME)
        self.setWindowIcon(BeeAssets().logo)
        self.view = PrismGraphicsView(app, self)
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
        self._recount_timer = QtCore.QTimer(self)
        self._recount_timer.setSingleShot(True)
        self._recount_timer.setInterval(300)
        self._recount_timer.timeout.connect(self._on_recount)
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

        self.filter_popover = FilterPopover(self.view, outer, color_host=self.color_filter)
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
        self.view.zoom_changed.connect(
            lambda _value: self._update_status(None))
        self._fit_all_button = QtWidgets.QPushButton(_('Show All'))
        self._fit_all_button.setToolTip(
            _('Zoom out to fit everything on this canvas'))
        self._fit_all_button.clicked.connect(self.view.on_action_fit_scene)
        self._fit_all_button.setFixedHeight(22)
        self._centre_status.setFixedHeight(shell['statusbar-height'])
        self._centre_status.addPermanentWidget(self._fit_all_button)
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
        self.show()
        QtCore.QTimer.singleShot(0, self._apply_responsive_panels)
        self._recovery_timer = QtCore.QTimer(self)
        self._recovery_timer.setInterval(15000)
        self._recovery_timer.timeout.connect(self._write_recovery_snapshot)
        self._recovery_timer.start()
        # 移除自动恢复弹窗：恢复功能只支持文档/脑图文字，不支持画布图片，
        # 弹出后点 Yes 也不能真正恢复用户工作，反而造成困惑。
        # QtCore.QTimer.singleShot(0, self._offer_recovery_snapshot)

        # Autosave writes the project back to its own file, so a crash or a
        # forgotten Ctrl+S costs at most one interval of work.  Projects that
        # have never been saved keep using the recovery snapshot instead.
        self._autosave_timer = QtCore.QTimer(self)
        minutes = self.view.settings.valueOrDefault('Autosave/minutes')
        if minutes > 0 and self.view.settings.valueOrDefault(
                'Autosave/enabled'):
            self._autosave_timer.setInterval(minutes * 60 * 1000)
            self._autosave_timer.timeout.connect(self._perform_autosave)
            self._autosave_timer.start()

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
        self._eagle_menu().exec(self._left_header.mapToGlobal(QtCore.QPoint(6, 36)))

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

    def _perform_autosave(self):
        if not self.view.is_dirty():
            return
        if not self.view.filename:
            self._write_recovery_snapshot()
            return
        self.view.do_save(self.view.filename, create_new=False, silent=True)

    def _write_recovery_snapshot(self):
        """Keep text/page work recoverable even when it bypasses undo stack."""
        if not self.view.is_dirty():
            return
        current = self.tabs.currentWidget()
        if hasattr(current, 'store_to_scene'):
            current.store_to_scene()
        payload = {
            'filename': self.view.filename,
            'note_html': self.view.scene.note_html,
            'mindmap_tree': self.view.scene.mindmap_tree,
            'workspace_pages': self.view.scene.workspace_pages,
        }
        try:
            self.view.settings.setValue(
                'Recovery/snapshot', json.dumps(payload, ensure_ascii=False))
        except (TypeError, ValueError):
            logger.exception('Could not write recovery snapshot')

    def clear_recovery_snapshot(self):
        self.view.settings.remove('Recovery/snapshot')

    def _offer_recovery_snapshot(self):
        raw = self.view.settings.value('Recovery/snapshot')
        if not raw:
            return
        answer = QtWidgets.QMessageBox.question(
            self, _('Automatic Recovery'),
            _('Unsaved document, mind map or page changes were found '
              'from last time. Restore them?'),
            QtWidgets.QMessageBox.StandardButton.Yes |
            QtWidgets.QMessageBox.StandardButton.No)
        if answer != QtWidgets.QMessageBox.StandardButton.Yes:
            self.clear_recovery_snapshot()
            return
        try:
            payload = json.loads(raw)
            self.view.scene.note_html = payload.get('note_html', '')
            self.view.scene.mindmap_tree = payload.get('mindmap_tree')
            self.view.scene.workspace_pages = payload.get('workspace_pages', [])
            self.document_panel.load_from_scene()
            self.mindmap_panel.load_from_scene()
            self._reload_workspace_pages()
            self.view.mark_content_dirty()
        except (TypeError, ValueError):
            logger.exception('Invalid recovery snapshot')
            self.clear_recovery_snapshot()


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
        return sum(1 for item in self.view.scene.items_for_save()
                   if item.isVisible())


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
        self._right_empty.setVisible(not material_mode)
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
        if hasattr(previous, 'store_to_scene'):
            previous.store_to_scene()
        current = self.tabs.widget(index)
        if hasattr(self, '_eagle_header_stack'):
            browsing = current is self.asset_browser
            material_canvas = browsing or current is self.canvas_page
            self._eagle_header_stack.setCurrentWidget(self.canvas_toolbar if material_canvas else self._eagle_drag)
            self._centre_header.setFixedHeight(self.canvas_toolbar.height() if material_canvas else 36)
            self._centre_status.setVisible(material_canvas)
            self.view.category_panel.search.setVisible(not hasattr(self, 'search_controller') and not browsing)
            self.view.category_panel.search_type.setVisible(not hasattr(self, 'search_controller') and not browsing)
            self.view.category_panel.hint.setVisible(not browsing)
        if hasattr(current, 'load_from_scene'):
            current.load_from_scene()
        self._previous_tab = index
        is_canvas = index == 0
        # 常驻颜色条没有了（§8.3）：跟着画布页显隐的是这条 36 px 筛选条。
        self.filter_bar.hide()
        if not is_canvas:
            self.filter_popover.hide()
            self.canvas_toolbar.filter_button.setChecked(False)
        self._apply_responsive_panels()

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

    def _update_status(self, counts):
        selected = len(self.view.scene.selectedItems(user_only=True))
        catalog = self.view.category_panel.catalog
        total = catalog.total_count()
        visible = sum(i.isVisible() for i in catalog.active_items())
        zoom = self.view.transform().m11() * 100.0
        browser = getattr(self, 'asset_browser', None)
        if browser is not None and self.tabs.currentWidget() is browser:
            visible = len(browser.model.items)
            selected = len(browser.list.selectedIndexes())
            zoom = browser.list.transform().m11()*100.0
        self._status_text.setText(
            _('{total} items · {visible} shown · {selected} selected').format(
                total=total, visible=visible, selected=selected) +
            _(' · Zoom {zoom:.0f}% · Ctrl+Shift+drag to drag out to '
              'another application').format(zoom=zoom))

    def _schedule_recount(self):
        # A playing video continuously changes the scene; don't starve updates.
        if not self._recount_timer.isActive():
            self._recount_timer.start()

    def closeEvent(self, event):
        worker = getattr(self.view, 'worker', None)
        if isinstance(worker, QtCore.QThread) and worker.isRunning():
            event.ignore()
            return
        if not self.view.get_confirmation_unsaved_changes(
                _('There are unsaved changes. Discard them and close the window?')):
            event.ignore()
            return
        self.view.settings.setValue('MainWindow/geometry', self.saveGeometry())
        self._apply_responsive_panels()
        self.view.settings.setValue('MainWindow/splitter', self.splitter.saveState())
        for side, width in self._sidebar_widths.items():
            self.view.settings.setValue('MainWindow/'+side+'Width', width)
        self.view.scene.deselect_all_items()
        self.browser_capture.stop()
        event.accept()


def safe_timer(timeout, func, *args, **kwargs):
    """Create a timer that is safe against garbage collection and
    overlapping calls.
    See: http://ralsina.me/weblog/posts/BB974.html
    """
    def timer_event():
        try:
            func(*args, **kwargs)
        finally:
            QtCore.QTimer.singleShot(timeout, timer_event)
    QtCore.QTimer.singleShot(timeout, timer_event)


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


sys.excepthook = handle_uncaught_exception


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
    app.setFont(QtGui.QFont("Microsoft YaHei UI", 12))
    palette = create_palette_from_dict(constants.COLORS)
    app.setPalette(palette)

    # Load Apple-style QSS theme stylesheet
    # In PyInstaller, assets are in sys._MEIPASS; in dev, relative to __file__
    if getattr(sys, 'frozen', False):
        _assets_dir = os.path.join(sys._MEIPASS, 'prism', 'assets')
    else:
        _assets_dir = os.path.join(os.path.dirname(__file__), 'assets')
    qss_path = os.path.join(_assets_dir, 'theme.qss')
    if os.path.exists(qss_path):
        with open(qss_path, 'r', encoding='utf-8') as f:
            qss = f.read()
        # Resolve icon paths to absolute; use forward slashes for QSS
        # (backslashes are treated as escape chars by Qt's CSS parser)
        icons_dir = os.path.join(_assets_dir, 'icons').replace(os.sep, '/')
        qss = qss.replace('url(assets/icons/', f'url({icons_dir}/')
        app.setStyleSheet(qss)
        logger.debug(f'Loaded theme from {qss_path}')

    from prism.widgets.dialog_theme import install as install_dialog_theme
    install_dialog_theme(app)
    bee = PrismMainWindow(app)  # NOQA:F841
    from prism.performance_trace import install
    try:
        trace = install(app, bee, os.path.dirname(logfile_name()))
        logger.info('Operation/performance log: %s', trace.path)
    except OSError:
        logger.exception('Could not start operation/performance log')

    # Register .prism file association (HKCU, no admin needed)
    _register_file_association()

    signal.signal(signal.SIGINT, handle_sigint)
    # Repeatedly run python-noop to give the interpreter time to
    # handle signals
    safe_timer(50, lambda: None)

    app.exec()
    del bee
    del app
    logger.debug('Prism closed')
    QtCore.qInstallMessageHandler(None)


if __name__ == '__main__':
    main()  # pragma: no cover
