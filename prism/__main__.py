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
import sys
import copy
import uuid
from datetime import datetime, timezone

from PyQt6 import QtCore, QtGui, QtWidgets, sip
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
from prism.widgets.color_filter import ColorFilterBar
from prism.widgets.components.icons import icon

logger = logging.getLogger(__name__)


class PrismApplication(QtWidgets.QApplication):

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


class ShellDrawerScrim(QtWidgets.QFrame):
    def __init__(self, on_close, parent):
        super().__init__(parent)
        self.on_close = on_close
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)

    def mousePressEvent(self, event):
        self.on_close()
        event.accept()

    def keyPressEvent(self, event):
        if event.key() == Qt.Key.Key_Escape:
            self.on_close()
            event.accept()
        else:
            super().keyPressEvent(event)


class PrismMainWindow(QtWidgets.QMainWindow):
    """A standard desktop window with independently resizable side panels."""

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
        self._menu_file_title.setContentsMargins(12, 0, 12, 0)
        self._menu_file_title.setStyleSheet('color: #AAB0BB; font-size: 12px; background: transparent;')
        self.menuBar().setCornerWidget(self._menu_file_title, Qt.Corner.TopRightCorner)
        self.windowTitleChanged.connect(lambda title: self._menu_file_title.setText(title.removesuffix(' - Prism')))
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
        self.view.scene.changed.connect(self._schedule_recount)

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
        layout.addWidget(self.filter_bar)
        self.filter_popover = FilterPopover(self.view, outer, color_host=self.color_filter)
        self.filter_popover.hide()
        self.filter_bar.open_requested.connect(self._toggle_filter_popover)
        self.filter_bar.clear_requested.connect(self._clear_filters)
        self.filter_bar.chip_removed.connect(self._remove_filter_chip)
        self.filter_popover.filters_changed.connect(
            self._refresh_filter_bar)
        self.tabs = QtWidgets.QTabWidget()
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

        self._previous_tab = 0
        self.tabs.currentChanged.connect(self._on_tab_changed)

        self.splitter = ShellSplitter(Qt.Orientation.Horizontal)
        self.splitter.setHandleWidth(shell['splitter-hotzone'])
        self.splitter.addWidget(self.view.category_panel)
        self.splitter.addWidget(self.tabs)
        self.splitter.addWidget(self.view._detail_panel)
        self.view.category_panel.setMinimumWidth(shell['left-panel-min'])
        self.view.category_panel.setMaximumWidth(shell['left-panel-max'])
        self.view._detail_panel.setMinimumWidth(shell['right-panel-min'])
        self.view._detail_panel.setMaximumWidth(shell['right-panel-max'])
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
        self._drawer_side = None
        self._drawer_scrim = ShellDrawerScrim(self._close_drawer, outer)
        self._drawer_scrim.setStyleSheet(
            'background: %s;' % ui_tokens.COLOURS_DARK['scrim'])
        self._drawer_scrim.hide()
        self._drawer = QtWidgets.QFrame(self._drawer_scrim)
        self._drawer.setStyleSheet(
            'background: %s; border: 1px solid %s;' % (
                ui_tokens.COLOURS_DARK['panel-bg'],
                ui_tokens.COLOURS_DARK['border-subtle']))
        self._drawer_layout = QtWidgets.QVBoxLayout(self._drawer)
        self._drawer_layout.setContentsMargins(0, 0, 0, 0)
        self._drawer_layout.setSpacing(0)

        self._page_panels = {}
        self._deleted_pages = []
        self._trash_toast = None
        self.current_page_kind = 'canvas'
        self.current_page_id = 'default-canvas'
        self.view.current_canvas_id = self.current_page_id
        self.view.category_panel.workspace_requested.connect(
            self.open_workspace_page)
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

        self._edge_handles = {}
        for side, text in [('left', '›'), ('right', '‹')]:
            button = QtWidgets.QToolButton(outer)
            button.setObjectName('shellEdgeHandle')
            button.setText(text)
            button.setFixedSize(14, 64)
            button.setToolTip('显示资源树' if side == 'left' else '显示素材检查器')
            button.clicked.connect(lambda checked=False, side=side: self._toggle_sidebar(side))
            button.hide()
            self._edge_handles[side] = button
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
        self.statusBar().setFixedHeight(shell['statusbar-height'])
        self.statusBar().addPermanentWidget(self._fit_all_button)
        self.view.scene.selectionChanged.connect(
            self._on_scene_selection_changed)
        self.view.category_panel.update_counts()
        self.color_filter.recount()
        self._refresh_filter_bar()
        self.view.update_window_title()
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

    def _build_workspace_rail(self):
        rail = QtWidgets.QFrame()
        rail.setObjectName('workspaceRail')
        rail.setFixedWidth(104)
        rail.setStyleSheet(
            '#workspaceRail { background: #202024; border-right: 1px solid #34343a; }'
            '#workspaceRail QToolButton { min-height: 38px; padding: 4px 7px; '
            'text-align: left; border: 0; border-radius: 6px; }'
            '#workspaceRail QToolButton:checked { background: #3d5f8a; color: white; }'
            '#workspaceRail QToolButton:hover:!checked { background: #303036; }')
        self._rail_layout = QtWidgets.QVBoxLayout(rail)
        self._rail_layout.setContentsMargins(6, 8, 6, 8)
        self._rail_layout.setSpacing(4)
        self._workspace_buttons = QtWidgets.QButtonGroup(self)
        self._workspace_buttons.setExclusive(True)
        self._dynamic_buttons = []
        self._dynamic_panels = []

        self._add_workspace_button(_('Canvas'), 0, checked=True)
        self._add_workspace_button(_('Document'), 1)
        self._add_workspace_button(_('Mind Map'), 2)
        self._dynamic_start = self._rail_layout.count()
        self._rail_layout.addStretch(1)
        add_button = QtWidgets.QToolButton()
        add_button.setText(_('Add'))
        add_button.setIcon(icon('plus'))
        add_button.setIconSize(QtCore.QSize(16, 16))
        add_button.setToolButtonStyle(
            Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        add_button.setToolTip(_('Add a document or mind-map page'))
        add_button.clicked.connect(self._show_add_workspace_menu)
        self._rail_layout.addWidget(add_button)
        return rail

    def _add_workspace_button(self, title, tab_index, checked=False,
                              page_id=None):
        button = QtWidgets.QToolButton()
        button.setText(title)
        button.setCheckable(True)
        button.setChecked(checked)
        button.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextOnly)
        button.clicked.connect(
            lambda _checked=False, index=tab_index: self.tabs.setCurrentIndex(index))
        if page_id:
            button.setProperty('page_id', page_id)
            button.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
            button.customContextMenuRequested.connect(
                lambda _pos, pid=page_id: self._show_workspace_page_menu(pid))
        self._workspace_buttons.addButton(button)
        # Dynamic buttons belong above the stretch and + button.
        if hasattr(self, '_dynamic_start'):
            self._rail_layout.insertWidget(self._rail_layout.count() - 2, button)
            self._dynamic_buttons.append(button)
        else:
            self._rail_layout.addWidget(button)
        return button

    def _show_add_workspace_menu(self):
        menu = QtWidgets.QMenu(self)
        menu.addAction(_('New Document Page'),
                       lambda: self._create_workspace_page('document'))
        menu.addAction(_('New Mind Map Page'),
                       lambda: self._create_workspace_page('mindmap'))
        menu.exec(QtGui.QCursor.pos())

    def _create_workspace_page(self, kind, parent=None):
        defaults = {'canvas': _('Canvas'), 'document': _('Document'),
                    'mindmap': _('Mind Map')}
        default = defaults.get(kind, _('Page'))
        title, ok = QtWidgets.QInputDialog.getText(
            self, _('New Page'), _('Page name:'), text=default)
        if not ok or not title.strip():
            return
        page = self.workspace_service.add(kind, title.strip(), parent)
        self.open_workspace_page(kind, page['id'])
        self.view.mark_content_dirty()
        return self._page_panels.get(page['id'], self.view)

    def _create_resource_item(self, section, node_type, parent_id):
        """就地新建：先放一条临时行，名字确认了才真写进工程（C5 / §7.4）。

        以前这里是 `QInputDialog`：弹窗、输名字、确定。现在改成临时节点 +
        就地改名 —— Esc 时那一行直接消失，工程里不留任何痕迹。
        """
        defaults = {'canvas': '未命名画布', 'mindmap': '未命名脑图',
                    'document': '未命名文档'}
        base = '未命名文件夹' if node_type == 'folder' else defaults[section]
        panel = self.view.category_panel
        title = panel.resource_service.unique_suggestion(
            section, parent_id, node_type, base)
        panel.resource_tree.begin_create(section, node_type, parent_id, title)

    def _on_resource_created(self, node):
        """就地新建确认之后：补上旧的页面表、打开它、把工程标脏。"""
        if node.get('nodeType') != 'page':
            self.view.mark_content_dirty()
            return
        self.workspace_service.add(node['section'], node['title'],
                                   page_id=node['pageId'])
        self.open_workspace_page(node['section'], node['pageId'])
        self.view.mark_content_dirty()

    def _on_resource_tree_change(self, change):
        # An inline page rename must also update the legacy page payload;
        # the node ID itself stays stable and never depends on the title.
        if not (isinstance(change.get('old'), str) and
                isinstance(change.get('new'), str)):
            return
        for node_id in change['ids']:
            node = self.view.category_panel.resource_service._by_id(node_id)
            if node and node['nodeType'] == 'page':
                self.workspace_service.rename(node['pageId'], node['title'])

    def _delete_resource_item(self, node_id):
        """右键「删除」/ Delete：移到回收站，并给 8 秒反悔时间（§7.6）。

        - **空文件夹、单个页面**：不打断，直接删，然后弹一条带「撤销」的提示；
        - **非空文件夹**：先确认，确认框写清「含 X 个文件夹、Y 个页面」。

        回收站是软删除：节点、页面正文、附件都还在工程里，恢复就能回来 ——
        规范那句「仅有 UI 删除而数据不可恢复视为不合格」说的就是这个。
        """
        service = self.view.category_panel.resource_service
        node = service._by_id(node_id)
        if node is None:
            return
        descendants = service._descendants(node_id)
        pages = sum(service._by_id(i)['nodeType'] == 'page'
                    for i in descendants)
        folders = sum(service._by_id(i)['nodeType'] == 'folder'
                      for i in descendants)
        if descendants:
            reply = QtWidgets.QMessageBox.question(
                self, '移到回收站',
                f'把“{node["title"]}”移到回收站？其中包含 {folders} 个文件夹、'
                f'{pages} 个页面。内容会保留在工程中，可以再恢复。',
                QtWidgets.QMessageBox.StandardButton.Yes
                | QtWidgets.QMessageBox.StandardButton.Cancel,
                QtWidgets.QMessageBox.StandardButton.Yes)
            if reply != QtWidgets.QMessageBox.StandardButton.Yes:
                return
        pages_removed = self._pages_under_node(node_id)
        # 删掉当前页之前，先把面板里的编辑写回 scene —— 回收站里要留着它。
        if self.current_page_id in pages_removed:
            self._store_current_page()
        # 后继必须在删之前算：删完它就不在兄弟列表里了。
        successor = (self._fallback_page_after_delete(node_id)
                     if self.current_page_id in pages_removed else None)
        service.trash([node_id], datetime.now(timezone.utc).isoformat())
        self.view.mark_content_dirty()
        self._open_successor_after_trash(successor, pages_removed)
        text = f'已把“{node["title"]}”移到回收站'
        if pages or folders:
            text += f'（含 {folders} 个文件夹、{pages} 个页面）'
        self._show_trash_toast(text, [node_id])

    def _store_current_page(self):
        """把当前页签里的编辑写回 scene（切页/删除前都该做这一步）。"""
        current = self.tabs.currentWidget()
        if hasattr(current, 'store_to_scene'):
            current.store_to_scene()

    def _pages_under_node(self, node_id):
        """这个节点（含后代）里所有页面的 id。"""
        service = self.view.category_panel.resource_service
        found = []
        node = service._by_id(node_id)
        if node is None:
            return found
        for candidate in [node] + [service._by_id(i)
                                   for i in service._descendants(node_id)]:
            if (candidate and candidate.get('nodeType') == 'page'
                    and candidate.get('pageId')
                    and candidate['pageId'] not in found):
                found.append(candidate['pageId'])
        return found

    def _first_page_of(self, node):
        """这个节点里第一个页面：自己就是页面就用自己，否则找后代。"""
        service = self.view.category_panel.resource_service
        if node.get('nodeType') == 'page' and node.get('pageId'):
            return node['pageId']
        for child in service.listChildren(node['section'], node['id']):
            found = self._first_page_of(child)
            if found:
                return found
        return None

    def _fallback_page_after_delete(self, node_id):
        """§7.6：删掉当前页面之后打开哪一个 —— **后继 → 前驱 → 父级**。"""
        service = self.view.category_panel.resource_service
        node = service._by_id(node_id)
        if node is None:
            return None
        siblings = service.listChildren(node['section'], node.get('parentId'))
        ids = [s['id'] for s in siblings]
        if node_id not in ids:
            return None
        at = ids.index(node_id)
        for candidate in list(siblings[at + 1:]) + list(siblings[:at][::-1]):
            page_id = self._first_page_of(candidate)
            if page_id:
                return candidate['section'], page_id
        parent_id = node.get('parentId')
        if parent_id:
            for candidate in service.listChildren(node['section'], parent_id):
                page_id = self._first_page_of(candidate)
                if page_id:
                    return candidate['section'], page_id
        return None

    def _open_successor_after_trash(self, successor, pages_removed):
        """被删的里面正好有当前打开的页面时，按优先级换一个打开。"""
        if self.current_page_id not in pages_removed:
            return
        # 先把被删页面的面板摘下来：它还在页签上的话，切页时会把
        # 「已经删掉的那一页」的内容再写回 scene。
        for page_id in pages_removed:
            panel = self._page_panels.pop(page_id, None)
            if panel is None:
                continue
            index = self.tabs.indexOf(panel)
            if index >= 0:
                self.tabs.removeTab(index)
            panel.deleteLater()
        if successor is not None:
            self.open_workspace_page(*successor)
            return
        # 一个页面都不剩了：回画布，别停在一个已经不存在的页上。
        self.open_workspace_page('canvas', self._first_canvas_id())

    def _show_trash_toast(self, text, undo_ids):
        """「已移到回收站」+「撤销」，8 秒内点了就能回来（§7.6 / §505）。"""
        from prism.widgets.toast import UndoToast

        if self._trash_toast is not None and self._trash_toast.is_alive():
            self._trash_toast.dismiss()
        ids = list(undo_ids)
        self._trash_toast = UndoToast(
            self, text, lambda: self._undo_trash(ids))
        self._trash_toast.show_at_bottom()

    def _undo_trash(self, node_ids):
        """撤销刚才那次删除：从回收站恢复回来（单个或一批）。"""
        if isinstance(node_ids, str):
            node_ids = [node_ids]
        service = self.view.category_panel.resource_service
        try:
            service.restore_deleted(list(node_ids))
        except ValueError as exc:
            QtWidgets.QMessageBox.information(self, '恢复', str(exc))
            return
        self.view.mark_content_dirty()

    # ── 批量（C7 / §7.5）──────────────────────────────────────────

    def _top_ancestors(self, node_ids):
        """只留最上层祖先：父子都选中时别重复处理（§7.5）。"""
        service = self.view.category_panel.resource_service
        selected = set(node_ids)
        tops = []
        for node_id in node_ids:
            node = service._by_id(node_id)
            if node is None:
                continue
            ancestor = node.get('parentId')
            while ancestor is not None and ancestor not in selected:
                parent = service._by_id(ancestor)
                ancestor = parent.get('parentId') if parent else None
            if ancestor is None:
                tops.append(node_id)
        return tops

    def _count_under(self, node_ids):
        """这批节点**里面**有多少文件夹和页面（不含它们自己，与单个删除一致）。"""
        service = self.view.category_panel.resource_service
        folders = pages = 0
        for node_id in node_ids:
            for child_id in service._descendants(node_id):
                child = service._by_id(child_id)
                if child and child['nodeType'] == 'page':
                    pages += 1
                else:
                    folders += 1
        return folders, pages

    def _delete_resource_items(self, node_ids):
        """批量「删除」：一次确认、一次 trash、一条能撤销的提示。"""
        service = self.view.category_panel.resource_service
        roots = self._top_ancestors([i for i in node_ids if i])
        if not roots:
            return
        folders, pages = self._count_under(roots)
        has_children = any(service._descendants(node_id)
                           for node_id in roots)
        if has_children:
            reply = QtWidgets.QMessageBox.question(
                self, '移到回收站',
                f'把选中的 {len(roots)} 项移到回收站？其中包含 '
                f'{folders} 个文件夹、{pages} 个页面。'
                '内容会保留在工程中，可以再恢复。',
                QtWidgets.QMessageBox.StandardButton.Yes
                | QtWidgets.QMessageBox.StandardButton.Cancel,
                QtWidgets.QMessageBox.StandardButton.Yes)
            if reply != QtWidgets.QMessageBox.StandardButton.Yes:
                return
        current_removed = self.current_page_id in [
            page_id for node_id in roots
            for page_id in self._pages_under_node(node_id)]
        successor = None
        if current_removed:
            # 删的是当前页：先把面板里的编辑存回，再找一个接班页。
            self._store_current_page()
            for node_id in roots:
                successor = self._fallback_page_after_delete(node_id)
                if successor:
                    break
        stamped = datetime.now(timezone.utc).isoformat()
        service.trash(roots, stamped)
        self.view.mark_content_dirty()
        if current_removed:
            self._open_successor_after_trash(
                successor,
                [page_id for node_id in roots
                 for page_id in self._pages_under_node(node_id)])
        text = (f'已把选中的 {len(roots)} 项移到回收站'
                f'（含 {folders} 个文件夹、{pages} 个页面）')
        self._show_trash_toast(text, roots)

    def _move_resource_items(self, node_ids):
        """批量「移动到…」：先选目标，再交给服务层 —— 它会再校验一次。"""
        from prism.widgets.move_to_dialog import MoveToDialog

        node_ids = [i for i in node_ids if i]
        if not node_ids:
            return
        dialog = MoveToDialog(self.view, node_ids, self)
        if dialog.exec() != QtWidgets.QDialog.DialogCode.Accepted:
            return
        target = dialog.target()
        if target is None:
            return
        section, parent_id = target
        service = self.view.category_panel.resource_service
        try:
            moved = service.moveBatch(node_ids, parent_id, section)
        except ValueError as exc:
            QtWidgets.QMessageBox.warning(self, '移动到', str(exc))
            return
        if not moved:
            return
        self.view.category_panel.rebuild_workspace_tree()
        tree = self.view.category_panel.resource_tree
        if parent_id:
            tree.expand(tree.model().index_for_id(parent_id))
        index = tree.model().index_for_id(moved[0])
        tree.setCurrentIndex(index)
        tree.scrollTo(index)
        self.view.mark_content_dirty()

    def _open_trash_panel(self):
        """打开回收站：恢复或者永久删除（§7.6）。"""
        from prism.widgets.trash_panel import TrashPanel

        panel = TrashPanel(self.view, self)
        panel.purged.connect(self._on_trash_purged)
        panel.exec()
        # 面板里改过回收站，树跟着刷新一次。
        self.view.category_panel.rebuild_workspace_tree()

    def _on_trash_purged(self, page_ids):
        """永久删除之后，把对应的页面正文也清掉 —— 这次是真的没了。"""
        if not page_ids:
            self.view.mark_content_dirty()
            return
        scene = self.view.scene
        scene.workspace_pages = [page for page in
                                 (scene.workspace_pages or [])
                                 if page['id'] not in page_ids]
        for item in list(scene.items_for_save()):
            if getattr(item, 'canvas_id', None) in page_ids:
                scene.removeItem(item)
        # Purging is irreversible: old commands must not resurrect its items.
        self.view.undo_stack.clear()
        if 'default-document' in page_ids:
            scene.note_html = ''
            self.document_panel.load_from_scene()
        if 'default-mindmap' in page_ids:
            scene.mindmap_tree = None
            self.mindmap_panel.load_from_scene()
        self._sync_page_panels()
        self.view.category_panel.rebuild_workspace_tree()
        if self.current_page_id in page_ids:
            self.open_workspace_page('canvas', self._first_canvas_id())
        self.view.mark_content_dirty()

    def _export_resource_item(self, section, node_id):
        """左侧树右键「导出…」：导出这一项以及它下面的所有层级。

        形态（文件夹 / .zip）和落地位置都在对话框里问，写盘走
        `export_workflow.export_resource_subtree`。``node_id`` 为 None
        表示整个分区。
        """
        from prism.actions.export_workflow import export_resource_subtree

        export_resource_subtree(self.view, section, node_id)

    def _import_resource_item(self, section, node_id):
        """左侧树右键「导入…」：把外面的目录 / 压缩包 / 文件导进这一项。

        规则和「拖一个文件夹进画布」是同一套（一层目录一个容器、层级跟
        着目录走、中间层不建空容器、重名加序号）。``node_id`` 为 None
        表示整个分区。
        """
        from prism.actions.import_workflow import import_resource_subtree

        import_resource_subtree(self.view, section, node_id)

    def create_workspace_page_quietly(self, kind, title=None):
        """Create a page without asking for a name.

        Imports use this: asking someone to open an empty page before they
        are allowed to import into it is a step with no purpose.
        """
        page = self.workspace_service.add(kind, title or _('Imported'))
        self.open_workspace_page(kind, page['id'])
        self.view.mark_content_dirty()
        return self._page_panels.get(page['id'], self.view)

    def find_workspace_panel(self, kind):
        """Return an existing page panel of this kind, or None."""
        for page in getattr(self.view.scene, 'workspace_pages', []) or []:
            if page.get('kind') != kind:
                continue
            panel = self._page_panels.get(page.get('id'))
            if panel is not None:
                return panel
        return None

    # The canvas/mind map/document entries are generated on demand rather
    # than stored, so the first edit to one has to write it into the
    # project; otherwise a rename would be silently thrown away.
    IMPLICIT_TITLES = {'default-canvas': '默认画布',
                       'default-mindmap': '默认脑图',
                       'default-document': '默认文档'}

    def _materialise_implicit_page(self, page_id):
        title = self.IMPLICIT_TITLES.get(page_id)
        if title is None:
            return None
        kind = page_id.split('-', 1)[1]
        page = {'id': page_id, 'kind': kind, 'title': title,
                'content': None, 'tags': [], 'permanent': True}
        return self.workspace_service.restore_page(page)

    def _on_workspace_changed(self, _change):
        """Reflect a service mutation in the UI without rebuilding everything.

        This used to call `_reload_workspace_pages()` on every change: editing
        one page title threw away and re-created every document/mind map panel
        - each of them a whole Chromium view.  Besides being slow, the panels
        that were only `deleteLater`d are never collected before the
        application dies, so Qt exits with

            Release of profile requested but WebEnginePage still not deleted

        and the process ends in 0xC0000005.  Only the pages that actually
        changed are touched now.
        """
        self._sync_page_panels()

    def _sync_page_panels(self):
        """Make the tabs match the scene's page list, minimally.

        Pages that are still there keep their panel (and the browser state
        inside it); only pages that appeared or disappeared are added or
        removed, and a rename just rewrites the tab text.
        """
        pages = {page['id']: page
                 for page in getattr(self.view.scene, 'workspace_pages', [])
                 or []}
        for page_id, panel in list(self._page_panels.items()):
            if page_id in pages:
                continue
            index = self.tabs.indexOf(panel)
            if index >= 0:
                self.tabs.removeTab(index)
            panel.deleteLater()
            del self._page_panels[page_id]
        for page_id, page in pages.items():
            if page.get('kind') == 'canvas' or page_id in self._page_panels:
                continue
            self._append_workspace_page(page)
        for page_id, panel in self._page_panels.items():
            title = pages[page_id].get('title') or _('Untitled')
            index = self.tabs.indexOf(panel)
            if index >= 0 and self.tabs.tabText(index) != title:
                self.tabs.setTabText(index, title)

    def _append_workspace_page(self, page):
        from prism.widgets.document_panel import DocumentPanel
        from prism.widgets.mindmap_panel import MindMapPanel
        if page.get('kind') == 'canvas':
            return self.view
        if page.get('kind') == 'mindmap':
            panel = MindMapPanel(self.view, self, page_id=page['id'])
        else:
            panel = DocumentPanel(self.view, self, page_id=page['id'])
        self.tabs.addTab(panel, page.get('title') or _('Untitled'))
        self._page_panels[page['id']] = panel
        return panel

    def _reload_workspace_pages(self):
        if not hasattr(self, '_page_panels'):
            return
        self.tabs.setCurrentIndex(0)
        # 增量同步：还存在的页面保留自己的面板，只补新页面、只删已经不在的。
        # 以前这里是「全部删掉重建」，见 _on_workspace_changed 的说明。
        self._sync_page_panels()
        # Land on the first canvas that exists rather than a placeholder, so
        # opening a project shows its content straight away.
        canvas_id = self._first_canvas_id()
        self.current_page_kind = 'canvas'
        self.current_page_id = canvas_id
        self.view.current_canvas_id = canvas_id
        # 打开工程显示**全部**素材。旧列表在的时候这里会自动选中第一个
        # 分类、只显示那一类；分类筛选的界面入口已经随列表删掉了，默认
        # 再藏素材就没人知道为什么看不见了。
        self.view.category_panel.rebuild_workspace_tree()
        self.view.category_panel._apply_filter()

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

    def _first_canvas_id(self):
        """The canvas to show after loading a project."""
        library = getattr(self.view, 'category_panel', None)
        if library is not None:
            deleted = {node.get('pageId') for node in library.resource_service.nodes
                       if node.get('deletedAt') and node.get('pageId')}
            for page in library._pages_with_implicit_defaults():
                if page.get('kind') == 'canvas' and page['id'] not in deleted:
                    return page['id']
            if 'default-canvas' in deleted:
                return None
        return 'default-canvas'

    def open_workspace_page(self, kind, page_id):
        current = self.tabs.currentWidget()
        if hasattr(current, 'store_to_scene'):
            current.store_to_scene()
        self.current_page_kind = kind
        self.current_page_id = page_id
        if kind == 'canvas':
            self.view.current_canvas_id = page_id
            self.tabs.setCurrentWidget(self.canvas_page)
            self.view.category_panel._apply_filter()
            self.filter_popover.color_host.sync_canvas()
            self.color_filter.recount()
            self.canvas_toolbar.refresh()
            self._refresh_filter_bar()
            self.view.on_action_fit_scene()
        else:
            if page_id == 'default-document':
                panel = self.document_panel
            elif page_id == 'default-mindmap':
                panel = self.mindmap_panel
            else:
                panel = self._page_panels.get(page_id)
            if panel is not None:
                self.tabs.setCurrentWidget(panel)

    def _workspace_page_action(self, action, page_id):
        if action == 'restore':
            index = next((i for i in range(len(self._deleted_pages) - 1, -1, -1)
                          if self._deleted_pages[i].get('kind') == page_id), None)
            if index is None:
                QtWidgets.QMessageBox.information(
                    self, _('Restore Pages'),
                    _('This group has no pages to restore.'))
                return
            page = self.workspace_service.restore_page(
                self._deleted_pages.pop(index))
            self.view.mark_content_dirty()
            return
        page = next((p for p in self.view.scene.workspace_pages
                     if p.get('id') == page_id), None)
        if page is None:
            if action != 'rename':
                return
            page = self._materialise_implicit_page(page_id)
        if page is None:
            return
        if action == 'rename':
            self._rename_workspace_page(page)
        elif action == 'newchild':
            self._create_workspace_page(page.get('kind'), parent=page['id'])
        elif action == 'delete':
            self._delete_workspace_page(page)
        elif action == 'duplicate':
            self.workspace_service.duplicate(
                page['id'], page['title'] + _(' Copy'))
            self.view.mark_content_dirty()
        elif action in ('up', 'down'):
            if self.workspace_service.move(
                    page['id'], -1 if action == 'up' else 1):
                self.view.mark_content_dirty()
        elif action == 'tag':
            names = list(getattr(self.view.scene, 'tag_names', []))
            # Which object is being tagged is the whole point of this
            # dialog: it edits the page, and no asset gains or loses a tag
            # because of it.
            text, ok = QtWidgets.QInputDialog.getText(
                self, _('Set Page Tags'),
                _('Tags of the page “{title}” - assets keep their own:')
                .format(title=page.get('title', '')),
                text='，'.join(page.get('tags', [])))
            if ok:
                tags = [part.strip() for part in
                        text.replace(',', '，').split('，') if part.strip()]
                self.workspace_service.set_tags(page['id'], tags)
                for tag in page['tags']:
                    if tag not in names:
                        names.append(tag)
                self.view.scene.tag_names = names
                self.view.mark_content_dirty()

    def _show_workspace_page_menu(self, page_id):
        page = next((page for page in self.view.scene.workspace_pages
                     if page.get('id') == page_id), None)
        if page is None:
            return
        menu = QtWidgets.QMenu(self)
        menu.addAction(_('Rename'), lambda: self._rename_workspace_page(page))
        menu.addAction(_('Delete Page'), lambda: self._delete_workspace_page(page))
        menu.exec(QtGui.QCursor.pos())

    def _rename_workspace_page(self, page):
        title, ok = QtWidgets.QInputDialog.getText(
            self, _('Rename Page'), _('Page name:'), text=page['title'])
        if not ok or not title.strip():
            return
        if self.workspace_service.rename(page['id'], title):
            self.view.mark_content_dirty()

    def _delete_workspace_page(self, page):
        answer = QtWidgets.QMessageBox.question(
            self, _('Delete Page'),
            _('Delete “{title}”? This can be undone only by closing without saving.')
            .format(title=page['title']))
        if answer != QtWidgets.QMessageBox.StandardButton.Yes:
            return
        removed = self.workspace_service.remove(page['id'])
        if removed:
            self._deleted_pages.append(copy.deepcopy(page))
            self.view.mark_content_dirty()

    # `_build_workspace_header` / `_align_workspace_header` 原来在这里。
    # 用户要求（2026-10-01，带红框截图）删掉顶部那条 48 px 的工作区头部
    # ——「未命名项目 / 画布 / 画布 / 检查器」。它显示的东西现在这样找：
    #   · 项目名与文件路径 → 窗口标题（`update_window_title`）
    #   · 左/右面板的显示开关 → 画布右键菜单「界面布局」里的两项
    #     （`_category_action` / `_inspector_action`，窄窗口会自动收成抽屉）
    #   · 当前页面位置 → 中央页签本身（文档 / 脑图面板）

    def resizeEvent(self, event):
        super().resizeEvent(event)
        if hasattr(self, '_drawer_scrim'):
            QtCore.QTimer.singleShot(0, self._apply_responsive_panels)

    def _position_drawer(self):
        if self._drawer_side is None:
            return
        outer = self.centralWidget()
        # 工作区头部删掉之后，抽屉从窗口最上沿开始（以前要给它让出
        # 一条 header 的高度）。
        self._drawer_scrim.setGeometry(
            0, 0, outer.width(), outer.height())
        width = ui_tokens.LAYOUT[
            'left-panel-width' if self._drawer_side == 'left'
            else 'right-panel-width']
        x = 0 if self._drawer_side == 'left' else max(
            0, self._drawer_scrim.width() - width)
        self._drawer.setGeometry(x, 0, width, self._drawer_scrim.height())

    def _open_drawer(self, side):
        if self._drawer_side == side:
            return
        self._close_drawer()
        panel = (self.view.category_panel if side == 'left'
                 else self.view._detail_panel)
        self._drawer_side = side
        panel.setParent(self._drawer)
        self._drawer_layout.addWidget(panel)
        self._position_drawer()
        panel.show()
        self._drawer_scrim.show()
        self._drawer_scrim.raise_()
        self._drawer_scrim.setFocus()

    def _close_drawer(self):
        if self._drawer_side is None:
            return
        side = self._drawer_side
        self._drawer_side = None
        panel = (self.view.category_panel if side == 'left'
                 else self.view._detail_panel)
        self._drawer_layout.removeWidget(panel)
        panel.setParent(None)
        panel.hide()
        self.splitter.insertWidget(0 if side == 'left' else 2, panel)
        self._drawer_scrim.hide()
        self._apply_responsive_panels()

    def _toggle_sidebar(self, side):
        if self._drawer_side == side:
            self._close_drawer()
            return
        panel = (self.view.category_panel if side == 'left'
                 else self.view._detail_panel)
        if panel.isHidden() or self.splitter.indexOf(panel) < 0:
            self._open_drawer(side)
        else:
            action = (self._category_action if side == 'left'
                      else self._inspector_action)
            action.setChecked(False)

    def _apply_responsive_panels(self):
        if not hasattr(self, '_category_action'):
            return
        if self._drawer_side is not None:
            self._position_drawer()
            return
        shell = ui_tokens.LAYOUT
        width = self.centralWidget().width()
        handle = shell['splitter-hotzone']
        want_left = self._category_action.isChecked()
        want_right = self._inspector_action.isChecked() and self.tabs.currentIndex() == 0
        show_left = want_left and width >= (
            shell['left-panel-min'] + shell['centre-min-width'] + handle)
        show_right = want_right and width >= (
            shell['right-panel-min'] + shell['centre-min-width'] + handle +
            (shell['left-panel-min'] + handle if show_left else 0))
        self.view.category_panel.setVisible(show_left)
        self.view._detail_panel.setVisible(show_right)
        if hasattr(self, '_edge_handles'):
            for side, shown, wanted in [('left', show_left, want_left), ('right', show_right, want_right)]:
                button = self._edge_handles[side]
                button.setVisible(wanted and not shown)
                button.move(0 if side == 'left' else width - button.width(), max(0, (self.centralWidget().height() - button.height()) // 2))
                button.raise_()
        if self.filter_popover.isVisible():
            self._position_filter_popover()

    def eventFilter(self, watched, event):
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
        return scene is not None and not sip.isdeleted(scene)

    def _on_scene_selection_changed(self):
        if not self._scene_alive():
            return
        self._update_status(self.view.category_panel._calc_counts())

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
        if hasattr(current, 'load_from_scene'):
            current.load_from_scene()
        self._previous_tab = index
        is_canvas = index == 0
        # 常驻颜色条没有了（§8.3）：跟着画布页显隐的是这条 36 px 筛选条。
        self.filter_bar.setVisible(is_canvas)
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
        self._close_drawer()
        self._apply_responsive_panels()
        shell = ui_tokens.LAYOUT
        self.splitter.setSizes([
            shell['left-panel-width'],
            max(shell['centre-min-width'], self.width() -
                shell['left-panel-width'] - shell['right-panel-width']),
            shell['right-panel-width']])

    def _update_status(self, counts):
        selected = len(self.view.scene.selectedItems(user_only=True))
        total = sum(1 for i in self.view.scene.items() if hasattr(i, 'save_id'))
        visible = sum(i.isVisible() for i in self.view.scene.items_for_save())
        zoom = self.view.transform().m11() * 100.0
        self.statusBar().showMessage(
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
        self.view.settings.setValue('MainWindow/splitter', self.splitter.saveState())
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
    if '--ai-inference-worker' in sys.argv:
        from prism.ai_inference_worker import main as inference_main
        return inference_main()
    # 启用 faulthandler：ONNX / Qt 原生崩溃（segfault）时把 Python 栈写到日志
    import faulthandler
    _log_path = logfile_name()
    try:
        _fault_fd = open(_log_path, 'a', encoding='utf-8')
        faulthandler.enable(file=_fault_fd, all_threads=True)
    except Exception:
        faulthandler.enable()  # fallback: 写 stderr

    logger.info(f'Starting {constants.APPNAME} version {constants.VERSION}')
    logger.debug('System: %s', ' '.join(platform.uname()))
    logger.debug('Python: %s', platform.python_version())
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

    bee = PrismMainWindow(app)  # NOQA:F841

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
