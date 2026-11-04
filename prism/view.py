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

from functools import partial, wraps
import hashlib
import logging
import os
import os.path
from pathlib import Path
import threading
import re
import tempfile
import uuid
import weakref

from prism.performance_trace import record as trace_record, operation as trace_operation, open_phase as trace_open_phase
from prism.library_observability import project_boundary as library_project_boundary
from prism.performance_trace import (capture_context as trace_capture_context,
    current_context as trace_current_context, restore_context as trace_restore_context,
    bind_project as trace_bind_project, span as trace_span)
import time as trace_time

from PyQt6 import QtCore, QtGui, QtWidgets
from PyQt6.QtCore import Qt

from prism.actions import ActionsMixin, actions
from prism import camera_policy
from prism import commands
from prism.config import CommandlineArgs, PrismSettings, KeyboardSettings
from prism import constants
from prism import fileio
from prism.fileio.errors import IMG_LOADING_ERROR_MSG
from prism.i18n import _
from prism import widgets
from prism.items import (ARROW_NONE, ARROW_STYLES, DEFAULT_PEN_COLOR,
                         DEFAULT_PEN_WIDTH, DRAW_MODES, DRAW_TOOL_ERASER,
                         DRAW_TOOL_LINE, DRAW_TOOL_PEN, LINE_SOLID,
                         LINE_STYLES, PrismPathItem,
                         PrismPixmapItem, PrismTextItem)
from prism.actions.drawing_workflow import CanvasDrawingMixin, DRAW_TOOLBAR_MARGIN
from prism.canvas_overview import CanvasOverviewRenderer
from prism.canvas_navigation import CanvasNavigationMixin
from prism.main_controls import MainControlsMixin
from prism.scene import PrismGraphicsScene
from prism.scene_loading import SceneLoadScheduler
from prism.export_service import ExportService
from prism.workspace_service import WorkspaceService
from prism.utils import qcolor_to_hex
from prism.widgets.drawing_toolbar import DrawingToolbar

commandline_args = CommandlineArgs()
logger = logging.getLogger(__name__)

#: `do_save` sentinel: the partially loaded project refused to build a snapshot.
_PARTIAL_REFUSED = object()


def project_identity(path):
    """A comparable identity for one project.

    The same project can be named by its entry file, by its managed folder or
    by its database; ``database_path`` maps all three onto the database file,
    and the database file belongs to the project's folder.
    """
    from prism.storage.managed import database_path
    try:
        resolved = str(database_path(path))
    except Exception:
        resolved = str(path)
    if os.path.basename(resolved).lower() == 'metadata.sqlite':
        resolved = os.path.dirname(resolved)
    try:
        return os.path.normcase(os.path.abspath(resolved))
    except Exception:
        return os.path.normcase(resolved)


def same_project(first, second):
    """Whether two paths name the same project."""
    return project_identity(first) == project_identity(second)


def _catalog_cache_path(filename):
    """Return a stable cache path unique to the complete source path."""
    from prism.thumbnail_cache import cache_directory
    source = os.path.normcase(os.path.abspath(os.path.normpath(str(filename))))
    digest = hashlib.sha256(source.encode('utf-8', 'surrogatepass')).hexdigest()[:16]
    name = os.path.basename(source) or 'project.prism'
    return os.path.join(os.path.dirname(cache_directory()), 'catalog',
                        f'{name}-{digest}.sqlite')


def _with_open_trace(function):
    @wraps(function)
    def restored(self, *args, **kwargs):
        with trace_restore_context(getattr(self, '_trace_open_context', trace_current_context())):
            return function(self, *args, **kwargs)
    return restored


def _read_project_with_trace(context, *args, **kwargs):
    """ThreadedIO does not inherit the GUI's ContextVar."""
    with trace_restore_context(context):
        with trace_span('project.reader', stage='archive_read'):
            return fileio.load_prism(*args, **kwargs)


class _TracedSceneLoadScheduler(SceneLoadScheduler):
    def __init__(self, view):
        super().__init__(view)
        self._trace_view = view

    def drain(self):
        operation = self.operation
        context = (operation.context if operation is not None else
                   getattr(self._trace_view, '_trace_open_context', None))
        if context is None:
            return super().drain()
        with trace_restore_context(context):
            return super().drain()

    def _install_batch(self):
        with trace_span('project.gui_install', stage='install_batch') as stage:
            stage.set(queue_before=self.scene.items_to_add.qsize())
            try:
                return super()._install_batch()
            finally:
                # Queue depths include concurrent producer arrivals.
                stage.set(queue_after=self.scene.items_to_add.qsize(),
                          **getattr(self.scene, '_last_install_metrics', {}))

    def _complete(self, callback):
        with trace_span('project.gui_complete', stage='completion_callback'):
            return super()._complete(callback)


class _CatalogBuildDelivery(QtCore.QObject):
    delivered = QtCore.pyqtSignal(object)


class PrismGraphicsView(CanvasDrawingMixin,
                      CanvasNavigationMixin,
                      MainControlsMixin,
                      QtWidgets.QGraphicsView,
                      ActionsMixin):

    zoom_changed = QtCore.pyqtSignal(float)
    drawing_state_changed = QtCore.pyqtSignal()
    project_open_finished = QtCore.pyqtSignal(object)

    PAN_MODE = 1
    ZOOM_MODE = 2
    SAMPLE_COLOR_MODE = 3
    DRAW_MODE = 4

    #: 这些 TYPE 的图元是"有原始字节的素材"，删除时走软删除进回收站；
    #: 其余（文字 text、绘制线 path、错误占位 error）直接删。
    TRASH_TYPES = ('pixmap', 'video', 'glb')

    def __init__(self, app, parent=None):
        super().__init__(parent)
        self.app = app
        self.parent = parent
        self.settings = PrismSettings()
        self._cached_scene_bounds = None
        # One coordinator owns every post-open/post-save catalog build.  An
        # active writer is never replaced until its terminal delivery confirms
        # that the worker (and therefore its SQLite writer lock) has ended.
        self._catalog_to_build = None
        self._catalog_build_active = None
        self._catalog_build_pending = None
        self._catalog_build_cancel = None
        self._catalog_build_epoch = 0
        self._catalog_build_delivery = None
        self._interaction_timer = QtCore.QTimer(self)
        self._interaction_timer.setSingleShot(True)
        self._interaction_timer.setInterval(120)
        self._interaction_timer.timeout.connect(self._end_interaction)
        self.keyboard_settings = KeyboardSettings()
        self.welcome_overlay = widgets.welcome_overlay.WelcomeOverlay(self)

        # 绘制的样子（悬浮工具条上能改）。存进设置，下次开还记着 ——
        # 每画一条线都要重新选颜色的话没人会用。需求第十条也要求
        # "重新打开绘制工具后恢复上一次用的工具 / 颜色 / 粗细 / 箭头"。
        self._pen_color = (self.settings.value('Canvas/pen_color')
                           or DEFAULT_PEN_COLOR)
        self._pen_width = float(self.settings.value('Canvas/pen_width')
                                or DEFAULT_PEN_WIDTH)
        stored_tool = self.settings.value('Canvas/drawing_tool')
        self._drawing_tool = (stored_tool if stored_tool in DRAW_MODES
                              else DRAW_TOOL_PEN)
        stored_arrow = self.settings.value('Canvas/arrow_style')
        self._arrow = (stored_arrow if stored_arrow in ARROW_STYLES
                       else ARROW_NONE)
        stored_line_style = self.settings.value('Canvas/line_style')
        self._line_style = (stored_line_style
                            if stored_line_style in LINE_STYLES
                            else LINE_SOLID)
        self._draw_points = []
        self._draw_item = None
        self._text_tool_armed = False
        # 绘制模式被平移/缩放临时打断时，记着结束后要回到绘制模式。
        # 不记的话，用户在绘制模式下用中键拖一下画布，active_mode 就
        # 被清成 None，而工具栏还挂在画布上 —— 用户接着按左键只会得到
        # 橡皮筋选择框，松开什么都不剩，看起来就是"画一笔就消失"。
        self._mode_before_transient = None

        # 悬浮绘制工具条。**parent 是 view 而不是 viewport** —— 它有自己
        # 固定的位置，不该跟着画布的滚动和缩放跑。
        self._drawing_toolbar = DrawingToolbar(self)
        self._drawing_toolbar.hide()
        self._drawing_toolbar.tool_changed.connect(self.set_drawing_tool)
        self._drawing_toolbar.colour_requested.connect(self.pick_pen_colour)
        self._drawing_toolbar.width_changed.connect(self.set_pen_width)
        self._drawing_toolbar.arrow_changed.connect(self.set_arrow_style)
        self._drawing_toolbar.line_style_changed.connect(self.set_line_style)
        self._drawing_toolbar.close_requested.connect(self.cancel_draw_mode)

        self.setBackgroundBrush(
            QtGui.QBrush(QtGui.QColor(*constants.COLORS['Scene:Canvas'])))
        self.setRenderHint(QtGui.QPainter.RenderHint.Antialiasing)
        self.setFrameShape(QtWidgets.QFrame.Shape.NoFrame)

        from prism.undo import UndoStack
        self.undo_stack = UndoStack(self)
        self.undo_stack.setUndoLimit(100)
        self.undo_stack.canRedoChanged.connect(self.on_can_redo_changed)
        self.undo_stack.canUndoChanged.connect(self.on_can_undo_changed)
        self.undo_stack.cleanChanged.connect(self.on_undo_clean_changed)
        self.undo_stack.indexChanged.connect(self._advance_undo_revision)

        # Debounce recalc_scene_rect: batch operations (import, load)
        # fire scene.changed hundreds of times; the timer coalesces
        # them into a single recalculation.
        self._recalc_timer = QtCore.QTimer(self)
        self._recalc_timer.setSingleShot(True)
        self._recalc_timer.setInterval(0)
        self._recalc_timer.timeout.connect(self.recalc_scene_rect)

        self._content_dirty = False
        # The automatic-save policy has one owner: the view. The main window
        # owns the timer, the view owns the decision, the retry policy and
        # the announcement.
        from prism.fileio.save_preparation import AutosavePolicy
        self.autosave_policy = AutosavePolicy()
        self._autosave_pending = None
        self.current_canvas_id = 'default-canvas'
        self.filename = None
        self.previous_transform = None
        self.active_mode = None
        self._browser_capture_queue = []
        self._active_capture_metadata = None
        self._external_drag_start = None
        from prism.external_drag import DragFileStore
        self._drag_files = DragFileStore()
        self._external_drag_dir = self._drag_files.directory
        # Remembered so a menu-triggered insert can still land where the
        # user last pointed at the canvas.
        self._last_canvas_pos = None
        # 按下时的指针位置与撤销栈深度：松手时用来判断"这一下是不是把
        # 素材拖到左栏的画布上了"（见 `_canvas_drop_target`）。
        self._drag_origin = None
        self._press_stack_count = 0

        self.scene = PrismGraphicsScene(self.undo_stack)
        self._load_scheduler = _TracedSceneLoadScheduler(self)
        self.scene.setParent(self)
        self.scene.changed.connect(self.on_scene_changed)
        self.scene.selectionChanged.connect(self.on_selection_changed)
        self.scene.cursor_changed.connect(self.on_cursor_changed)
        self.scene.cursor_cleared.connect(self.on_cursor_cleared)
        self.setScene(self.scene)
        self._overview_renderer = CanvasOverviewRenderer(self)
        # Page data has one owner.  The main window adapts its signals into
        # tabs and the page rail; it must not edit the list behind the
        # service's back.
        self.workspace_service = WorkspaceService(self.scene, self)
        # 导出意图 -> 导出器的映射。界面只管问用户和显示进度，不自己拼
        # 「原图导出 = 哪个导出器 + 什么参数」。见 prism/export_service.py。
        self.export_service = ExportService(self.scene)

        # Media library side panel
        from prism.widgets.media_library import MediaLibraryPanel
        self.category_panel = MediaLibraryPanel(self)

        # Clipboard capture: a copied image lands in an inbox for review,
        # not straight on the canvas, so a screenshot taken for something
        # else never pollutes the page being worked on.
        from prism.clipboard_capture import (ClipboardCaptureAdapter,
                                             ClipboardCaptureService,
                                             excluded_applications)
        self.clipboard_inbox = ClipboardCaptureService(
            self, max_items=self.settings.valueOrDefault(
                'Clipboard/max_items'))
        # Password managers and credential prompts are refused up front, so
        # a copied password never reaches the inbox.  The setting adds to
        # that list rather than replacing it.
        self.clipboard_adapter = ClipboardCaptureAdapter(
            self, excluded=excluded_applications(self.settings))
        self.clipboard_adapter.snapshot_ready.connect(
            self.clipboard_inbox.accept)
        if self.settings.valueOrDefault('Clipboard/download_image_urls'):
            self.clipboard_adapter.url_ready.connect(
                self.clipboard_inbox.enqueue_url)
        self._clipboard_inbox_dialog = None
        if self.settings.valueOrDefault('Clipboard/auto_capture'):
            QtCore.QTimer.singleShot(0, self.clipboard_adapter.start)

        # Context menu and actions
        self.build_menu_and_actions()
        self.control_target = self
        self.init_main_controls(main_window=parent)

        # Load files given via command line
        if commandline_args.filenames:
            fn = commandline_args.filenames[0]
            if os.path.splitext(fn)[1] == '.prism':
                # The upgrade dialog/worker needs the fully constructed host,
                # not a nested event loop inside this view's constructor.
                QtCore.QTimer.singleShot(0,lambda filename=fn:self.open_from_file(filename))
            else:
                self.do_insert_images(commandline_args.filenames)

        self.update_window_title()

    @property
    def filename(self):
        return self._filename

    @filename.setter
    def filename(self, value):
        self._filename = value
        self.update_window_title()
        if value:
            from prism.storage.managed import root_for
            folder=root_for(value)
            from prism.storage.entrypoint import recent_path
            self.settings.update_recent_files(str(recent_path(folder)) if folder is not None else value)
            self.update_menu_and_actions()

    def cancel_active_modes(self):
        self._text_tool_armed = False
        self.scene.cancel_active_modes()
        self.cancel_sample_color_mode()
        self.active_mode = None

    def cancel_sample_color_mode(self):
        logger.debug('Cancel sample color mode')
        self.active_mode = None
        self.viewport().unsetCursor()
        if hasattr(self, 'sample_color_widget'):
            self.sample_color_widget.hide()
            del self.sample_color_widget
        if self.scene.has_multi_selection():
            self.scene.multi_select_item.bring_to_front()

    def update_window_title(self):
        clean = self.undo_stack.isClean() and not self._content_dirty
        if clean and not self.filename:
            title = constants.APPNAME
        else:
            from prism.storage.entrypoint import display_project_name
            name = display_project_name(self.filename) if self.filename else '[未命名项目]'
            clean = '' if clean else '*'
            title = f'{name}{clean} - {constants.APPNAME}'
        self.parent.setWindowTitle(title)
        if hasattr(self.parent, '_title_bar'):
            self.parent._title_bar.update_title(title)

    def on_scene_changed(self, region):
        # A camera reset that was asked for before the content had arrived
        # is re-applied now that it has.  ``reset_if_stale`` is a no-op
        # unless a reset is actually pending, so this costs one comparison
        # per scene change.
        camera_policy.reset_if_stale(self)
        if not self.scene._item_count and getattr(self, '_project_opening', False):
            # Empty is an installation phase, not a return to the welcome
            # screen. Showing it briefly makes an open look like a restart.
            self.welcome_overlay.hide()
        elif not self.scene._item_count:
            logger.debug('No items in scene')
            self.setTransform(QtGui.QTransform())
            self.welcome_overlay.setFocus()
            self.clearFocus()
            self.welcome_overlay.show()
            self.actiongroup_set_enabled('active_when_items_in_scene', False)
        else:
            if self.welcome_overlay.isVisible():
                self.setFocus()
            self.welcome_overlay.clearFocus()
            self.welcome_overlay.hide()
            self.actiongroup_set_enabled('active_when_items_in_scene', True)
        # Debounce: batch operations fire this hundreds of times;
        # the timer coalesces into a single recalculation.
        if self._cached_scene_bounds is None:
            moving = self.scene.active_mode == self.scene.MOVE_MODE
            loading = (self._import_in_progress or not self.scene.items_to_add.empty()
                       or self._load_scheduler.finished is not None)
            self._recalc_timer.start(120 if moving or loading or self.active_mode == self.DRAW_MODE else 0)

    def on_can_redo_changed(self, can_redo):
        self.actiongroup_set_enabled('active_when_can_redo', can_redo)

    def on_can_undo_changed(self, can_undo):
        self.actiongroup_set_enabled('active_when_can_undo', can_undo)

    def on_undo_clean_changed(self, clean):
        self.update_window_title()

    def _advance_undo_revision(self, _index):
        self._undo_revision = getattr(self, '_undo_revision', 0) + 1

    def mark_content_dirty(self):
        """Mark changes outside the graphics undo stack as unsaved."""
        self._content_revision = getattr(self, '_content_revision', 0) + 1
        self._content_dirty = True
        self.update_window_title()

    def mark_page_modified(self, page):
        """Tell the partial-save session that a page's stored form changed.

        A partly loaded project compares every page with its baseline on
        every save.  Bumping the page's modification version lets that save
        skip a page that did not change, without reading its body at all.
        """
        session = getattr(self.scene, '_partial_project_session', None)
        if session is not None:
            session.mark_page_modified(page)

    def is_dirty(self):
        return self._content_dirty or not self.undo_stack.isClean()

    def on_context_menu(self, point):
        self._context_scene_position = self.mapToScene(point)
        from prism.item_lifecycle import logical_item
        candidate = self.itemAt(point)
        while candidate is not None and not hasattr(candidate, 'save_id'):
            candidate = candidate.parentItem()
        if candidate is not None:
            candidate = logical_item(candidate)
            if not candidate.isSelected():
                self.scene.deselect_all_items()
                candidate.setSelected(True)
        # Remove previously injected dynamic items
        self._remove_dynamic_menu_items()
        # Inject reset camera + drawing tools at top
        self._inject_canvas_tools()
        # Inject tag submenu
        self._inject_category_menu()
        self.context_menu.exec(self.mapToGlobal(point))

    def _remove_dynamic_menu_items(self):
        """Remove all dynamically injected menu items."""
        for attr in ('_cat_menu_action', '_reset_cam_action',
                     '_draw_menu_action', '_canvas_sep_action',
                     '_ai_tag_action', '_pin_menu_action', '_download_node_action', '_crop_image_action'):
            action = getattr(self, attr, None)
            if action:
                self.context_menu.removeAction(action)
                setattr(self, attr, None)

    def _inject_canvas_tools(self):
        """Add reset camera and drawing tools to context menu."""
        # Separator
        sep = self.context_menu.addSeparator()
        self._canvas_sep_action = sep

        action = self._download_node_action = self.context_menu.addAction(_('Download video'))
        position = QtCore.QPointF(getattr(self, '_context_scene_position', self.mapToScene(self.get_view_center())))
        from prism.actions.video_download_workflow import create
        action.triggered.connect(lambda: create(self, position))
        crop = self._crop_image_action = self.context_menu.addAction('裁切图片')
        crop.setEnabled(self.scene.has_single_image_selection() and self.scene.crop_item is None)
        crop.triggered.connect(self.on_action_crop)
        crop.setToolTip('拖动边框调整裁切范围；回车确认，Esc 取消；支持撤销')

        # Reset camera (fit all items)
        reset_action = self.context_menu.addAction(
            "重置相机")
        reset_action.triggered.connect(self.reset_canvas)
        self._reset_cam_action = reset_action

        # 绘制工具：**一级菜单项**，不是子菜单。
        #
        # 需求里明确要求：「绘制工具」必须是可以直接点击的普通菜单项、
        # 不得带二级菜单箭头；原二级里的画笔 / 颜色 / 粗细 / 橡皮擦 /
        # 清除全部删掉 —— 那些统一挪到悬浮工具条上，避免两套重复入口。
        self._draw_menu_action = self.context_menu.addAction(
            "\u7ed8\u5236\u5de5\u5177")
        self._draw_menu_action.triggered.connect(self.start_draw_mode)

        # AI 打标签（仅当有选中素材时显示）

    def pick_pen_colour(self):
        """选画笔颜色（右键菜单里那项）。"""
        current = QtGui.QColor(self._pen_color)
        from prism.widgets.compact_color import choose_color
        chosen = choose_color(current, self, _('Pen color'))
        if chosen.isValid():
            self.set_pen_color(chosen.name())

    def _clear_all_drawings(self):
        """Remove all freehand drawing path items from scene.

        按 `TYPE` 认画笔的线。以前这里靠"**不可选**"的 flag 来区分画笔和
        别的路径图元 —— 那是因为画完的线会被显式设成不可选。现在画完的
        线是可选的（能选中才能删、才能移动），所以那个判据不再成立，
        继续用它会**一条也删不掉**。
        """
        to_remove = [item for item in self.scene.items()
                     if getattr(item, 'TYPE', '') == PrismPathItem.TYPE]
        for item in to_remove:
            self.scene.removeItem(item)

    def _inject_category_menu(self):
        """Add a multi-value tag assignment submenu."""
        # Remove old category menu if exists
        if hasattr(self, '_cat_menu_action') and self._cat_menu_action:
            self.context_menu.removeAction(self._cat_menu_action)
            self._cat_menu_action = None
        # Only show when items are selected
        if not self.scene.has_selection():
            return
        sub = QtWidgets.QMenu(_('Add Tags'), self.context_menu)
        self.category_panel.build_tag_menu(sub)
        self._cat_menu_action = self.context_menu.addMenu(sub)

    def get_supported_image_formats(self, cls):
        formats = []

        for f in cls.supportedImageFormats():
            string = f'*.{f.data().decode()}'
            formats.extend((string, string.upper()))
        if cls is QtGui.QImageReader:
            from importlib.util import find_spec
            if find_spec('OpenImageIO') is not None:
                formats.extend(['*.psd', '*.PSD', '*.exr', '*.EXR', '*.hdr', '*.HDR'])
        return ' '.join(formats)

    def get_view_center(self):
        return QtCore.QPoint(round(self.size().width() / 2),
                             round(self.size().height() / 2))

    def _release_project_resource_scope(self):
        """Release this view's accepted project only, after close is allowed."""
        key = getattr(self, '_resource_project_key', None)
        if key is None:
            return
        from prism.resource_budget import release_project_resources
        report = release_project_resources(key)
        # Save As can leave sources in the original archive while new work
        # addresses the current filename. Cancel both without changing bytes.
        if self.filename and str(self.filename) != str(key):
            release_project_resources(self.filename)
        self._resource_project_key = None
        trace_record('project_resources_released', **report)

    def clear_scene(self):
        operation = getattr(self, '_opening_operation', None)
        if operation is not None and not operation.terminal:
            # The reader writes this same scene. Cancellation is not termination.
            operation.clear_requested = True
            self._cancel_open(operation)
            return False
        self._opening_operation = None
        self._release_project_resource_scope()
        self._pending_canvas_demand = None
        preparation = getattr(self, '_paged_open_preparation', None)
        if preparation is not None:
            preparation.cancel()
        self._paged_preparing = False
        self._paged_open_failed = False
        self._paged_open_failed_reason = None
        self._paged_open_cancelled = False
        self._paged_source_stamp = None
        paged_loader = getattr(self, '_paged_canvas_loader', None)
        if paged_loader is not None:
            paged_loader.cancel(reset=True)
        self._paged_open_target = None
        renderer = getattr(self, '_overview_renderer', None)
        if renderer is not None:
            renderer.tiles.reset()
        self._project_opening = bool(getattr(self, '_replacing_project', False))
        self._load_scheduler.cancel()
        logging.debug('Clearing scene...')
        self.cancel_active_modes()
        if hasattr(self.parent, 'color_filter'):
            self.parent.color_filter.clear_filter()
        self.scene.clear()
        self.scene.note_html = ''
        self.scene.attachments = {}
        self.scene.mindmap_tree = None
        self.scene.workspace_pages = []
        self.scene.workspace_nodes = []
        self.scene._deferred_board_extras = None
        self.scene._deferred_page_ids = set()
        self.scene._board_extras_source = None
        import uuid
        self.scene.project_id = uuid.uuid4().hex
        self.scene.material_records = []
        self.scene.material_library_state = {}
        # 部分加载标记必须随 scene 一起清，否则上一次按页打开留下的标记会让
        # 下一次全量打开后保存被误拒。
        self.scene._partial_project_source = None
        self.scene._partial_project_session = None
        self.scene._loaded_scope_ids = None
        self._paged_scope = None
        self._open_scope = None
        # Invalidate every subscription for the closed project.  Keep the
        # active worker registered until its terminal delivery: a new project
        # may queue work, but it must not race the old writer lock.
        self._cancel_catalog_builds()
        if hasattr(self.parent, '_reload_workspace_pages'):
            # The project that replaces this one is still loading: the closed
            # project loses its panels, but the landing state belongs to the
            # finalization of the open.  Installing it here as well emptied
            # the sidebar tree and swapped the central widget a second time.
            self.parent._reload_workspace_pages(landing=False)
        if not self._project_opening:
            self.category_panel.force_rebuild()
        self.undo_stack.clear()
        self._content_dirty = False
        self.filename = None
        self.setTransform(QtGui.QTransform())

    def reset_canvas(self):
        """The context menu, toolbar and shortcut share one camera policy."""
        self.on_action_fit_scene()

    def reset_previous_transform(self, toggle_item=None):
        if (self.previous_transform
                and self.previous_transform['toggle_item'] != toggle_item):
            self.previous_transform = None

    def fit_rect(self, rect, toggle_item=None):
        if toggle_item and self.previous_transform:
            logger.debug('Fit view: Reset to previous')
            self.setTransform(self.previous_transform['transform'])
            self.centerOn(self.previous_transform['center'])
            self.previous_transform = None
            return
        if toggle_item:
            self.previous_transform = {
                'toggle_item': toggle_item,
                'transform': QtGui.QTransform(self.transform()),
                'center': self.mapToScene(self.get_view_center()),
            }
        else:
            self.previous_transform = None

        logger.debug(f'Fit view: {rect}')
        self.fitInView(rect, Qt.AspectRatioMode.KeepAspectRatio)
        self._recalc_timer.stop()
        self.recalc_scene_rect()
        # It seems to be more reliable when we fit a second time
        # Sometimes a changing scene rect can mess up the fitting
        self.fitInView(rect, Qt.AspectRatioMode.KeepAspectRatio)
        logger.trace('Fit view done')
        self.zoom_changed.emit(self.transform().m11() * 100.0)

    def _file_operation_pending(self):
        worker = getattr(self, 'worker', None)
        operation = getattr(self, '_opening_operation', None)
        models = getattr(getattr(self.parent, 'model_composition', None), 'service', None)
        return (bool(operation is not None and not operation.terminal)
                or bool(models is not None and models.pending_writes)
                or bool(worker is not None and worker.isRunning())
                or getattr(self, '_paged_preparing', False)
                or getattr(self, '_import_in_progress', False)
                or getattr(self, '_save_preparing', False)
                or getattr(self, '_save_board_snapshot', None) is not None
                or getattr(self, '_save_partial_snapshot', None) is not None)

    def _allow_project_replacement(self):
        preview = getattr(self, '_upgrade_preview_session', None)
        if preview is not None and not preview.allow_open:
            self._catalog_status('请先结束只读预览，再打开其他工程。')
            return False
        storage=getattr(self,'_storage_operation',None)
        if storage is not None and storage.isRunning():
            self.parent.statusBar().showMessage('请等待工程存储操作完成',4000)
            return False
        # Canvas IO still reads the current scene. Keep its lifetime stable
        # through both the worker and the queued GUI acknowledgement.
        if not self._file_operation_pending():
            return True
        trace_record('project_replacement_skipped', reason='file_operation_pending')
        if hasattr(self.parent, 'statusBar'):
            self.parent.statusBar().showMessage(
                _('Please wait for the current file operation to finish.'), 4000)
        return False

    def get_confirmation_unsaved_changes(self, msg):
        model_page = getattr(self.parent, 'model_library', None)
        if model_page is not None and model_page._has_draft():
            if model_page.store_to_scene() is False:
                return False
        if not self._allow_project_replacement():
            return False
        confirm = self.settings.valueOrDefault('Save/confirm_close_unsaved')
        if confirm and self.is_dirty():
            answer = QtWidgets.QMessageBox.question(
                self,
                _('Discard unsaved changes?'),
                msg,
                QtWidgets.QMessageBox.StandardButton.Yes |
                QtWidgets.QMessageBox.StandardButton.Cancel)
            return answer == QtWidgets.QMessageBox.StandardButton.Yes

        return True

    def on_action_new_scene(self):
        confirm = self.get_confirmation_unsaved_changes(
            '当前工程有未保存的修改，确定要新建项目吗？')
        if not confirm:
            return
        destination = self._choose_project_destination('新建项目：选择位置和项目名称', suggested_name='新建项目')
        if destination is None:
            return
        if self.clear_scene() is False:
            return
        self.do_save(destination, create_new=True)

    def on_action_fit_scene(self):
        """Reset the camera of the canvas that is on screen.

        The main canvas goes through ``camera_policy`` as well, so the
        toolbar button, the menu entry, the keyboard shortcut and the
        right-click entry all end up in the same place: one content scope
        (the visible content bounds), one margin, one empty-canvas policy
        and one rule that a reset is not a toggle of the previous fit.

        Before this, the main canvas computed its own bounds
        (``items_for_save()`` + ``isVisible()``), used the view's own
        ``fit_rect`` and had its own empty branch - so the two reset
        entries could drift apart, which is the user-visible symptom.
        """
        canvas = self._material_canvas()
        if canvas is not None:
            self.parent.canvas_toolbar.fit_all()
            return
        camera_policy.request_reset(self, camera_policy.visible_asset_bounds)

    def on_action_fit_selection(self):
        canvas = self._material_canvas()
        if canvas is not None:
            if getattr(canvas, 'is_asset_grid', False):
                canvas.scrollTo(canvas.currentIndex())
                return
            selected = [c.sceneBoundingRect() for c in canvas.cards if c.isSelected()]
            if selected:
                bounds = selected[0]
                for rect in selected[1:]:
                    bounds = bounds.united(rect)
                canvas.fitInView(bounds.adjusted(-20, -20, 20, 20), Qt.AspectRatioMode.KeepAspectRatio)
            return
        self.fit_rect(self.scene.itemsBoundingRect(selection_only=True))

    def on_action_actual_size(self):
        """Show one image pixel per screen pixel, centred.

        Task book T8 asks for "1:1 pixel viewing". Two separate things
        scale what reaches the screen: the view's transform *and* the
        item's own ``scale()``. An item shrunk to 50% still looks half
        size at a 100% view, so the transform has to be divided by the
        item's scale to get a true 1:1.

        With exactly one item selected we zoom to that item; otherwise to
        everything visible.
        """
        selected = self.scene.selectedItems(user_only=True)
        if len(selected) == 1:
            item = selected[0]
            rect = item.mapRectToScene(item.boundingRect())
            item_scale = item.scale() or 1.0
        else:
            visible = [i for i in self.scene.items_for_save() if i.isVisible()]
            if not visible:
                logger.debug('Nothing visible to show at actual size')
                return
            rect = self.scene.itemsBoundingRect(items=visible)
            item_scale = 1.0

        # recalc_scene_rect() bails out while previous_transform is set,
        # and we want the scene rect to follow the new zoom.
        self.previous_transform = None
        self.resetTransform()
        target = 1.0 / item_scale
        self.scale(target, target)
        self.centerOn(rect.center())
        self._recalc_timer.stop()
        self.recalc_scene_rect()
        logger.debug('Actual size: view at %.1f%%, item scale %.2f',
                     target * 100.0, item_scale)
        self.zoom_changed.emit(self.transform().m11() * 100.0)

    def on_action_fullscreen(self, checked):
        if checked:
            self.parent.showFullScreen()
        else:
            self.parent.showNormal()

    def on_action_always_on_top(self, checked):
        self.parent.setWindowFlag(
            Qt.WindowType.WindowStaysOnTopHint, on=checked)
        self.parent.destroy()
        self.parent.create()
        self.parent.show()

    def on_action_show_scrollbars(self, checked):
        if checked:
            self.setHorizontalScrollBarPolicy(
                Qt.ScrollBarPolicy.ScrollBarAsNeeded)
            self.setVerticalScrollBarPolicy(
                Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        else:
            self.setHorizontalScrollBarPolicy(
                Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
            self.setVerticalScrollBarPolicy(
                Qt.ScrollBarPolicy.ScrollBarAlwaysOff)

    def on_action_show_menubar(self, checked):
        if checked:
            self.parent.setMenuBar(self.create_menubar())
        else:
            self.parent.setMenuBar(None)

    def on_action_show_titlebar(self, checked):
        # 需求 4 / 方案 §9：无边框外壳是默认状态。切换标题栏交给窗口外壳层
        # 用 WM_NCCALCSIZE 处理，保留 WS_THICKFRAME（原生缩放/贴靠/阴影）。
        # 只有当 parent 不是带外壳的主窗口时（例如单元测试里传入的普通
        # QMainWindow）才退回旧的 FramelessWindowHint 路径。
        shell_toggle = getattr(self.parent, 'set_native_titlebar', None)
        if callable(shell_toggle):
            # checked=True → 显示系统原生标题栏；checked=False → 无边框外壳。
            shell_toggle(checked)
            return
        # The native frame supplies Windows drag-to-restore, snapping and
        # resize, so it stays on by default and is removed when unchecked.
        self.parent.setWindowFlag(
            Qt.WindowType.FramelessWindowHint, not checked)
        self.parent.destroy()
        self.parent.create()
        self.parent.show()

    def on_action_move_window(self):
        if self.welcome_overlay.isHidden():
            self.on_action_movewin_mode()
        else:
            self.welcome_overlay.on_action_movewin_mode()

    def _route_editor_history(self, redo):
        focus = QtWidgets.QApplication.focusWidget()
        if isinstance(focus, QtWidgets.QLineEdit):
            focus.redo() if redo else focus.undo()
            return True
        canvas = self._material_canvas()
        if canvas is not None and (canvas.undo_stack.canRedo() if redo else canvas.undo_stack.canUndo()):
            canvas.undo_stack.redo() if redo else canvas.undo_stack.undo()
            return True
        window = self.window()
        current = window.tabs.currentWidget() if hasattr(window, 'tabs') else None
        controller = getattr(window, 'search_controller', None)
        if controller is not None and current is controller.page:
            # The only explicitly owned project history here is the canvas stack.
            # Do not infer a document/mind-map history from a highlighted card.
            if self.undo_stack.canRedo() if redo else self.undo_stack.canUndo():
                from prism.navigation import defer_navigation
                action = self.on_action_redo if redo else self.on_action_undo
                if defer_navigation(window, action, owner=self):
                    return True
                window.open_workspace_page('canvas', self.current_canvas_id)
            return False
        if current is not None and hasattr(current, 'editor_history'):
            current.editor_history(redo)
            return True
        return False

    def on_action_undo(self):
        if self._route_editor_history(False):
            return
        logger.debug('Undo: %s' % self.undo_stack.undoText())
        self.cancel_active_modes()
        self.undo_stack.undo()

    def on_action_redo(self):
        if self._route_editor_history(True):
            return
        logger.debug('Redo: %s' % self.undo_stack.redoText())
        self.cancel_active_modes()
        self.undo_stack.redo()

    def _material_canvas(self):
        browser = getattr(self.parent, 'asset_browser', None)
        if browser is not None and self.parent.tabs.currentWidget() is browser:
            return browser.list
        return None

    def on_action_select_all(self):
        canvas = self._material_canvas()
        if canvas is not None:
            canvas.select_all()
            return
        self.scene.select_all_items()

    def on_action_deselect_all(self):
        canvas = self._material_canvas()
        if canvas is not None:
            canvas.selectionModel().clearSelection()
            return
        self.scene.deselect_all_items()

    def on_action_delete_items(self):
        logger.debug('Deleting items...')
        self.cancel_active_modes()
        from prism.selection_policy import plan_selection_delete
        plan = plan_selection_delete(self.scene.selectedItems(user_only=True), self.TRASH_TYPES)
        if plan.empty:
            return
        # Scene and undo ownership remain here; classification has no side effects.
        self.undo_stack.push(commands.DeleteSelection(
            self.scene, list(plan.assets), list(plan.annotations)))

    def on_action_cut(self):
        logger.debug('Cutting items...')
        self.on_action_copy()
        self.undo_stack.push(
            commands.DeleteItems(
                self.scene, self.scene.selectedItems(user_only=True)))

    def on_action_raise_to_top(self):
        self.scene.raise_to_top()

    def on_action_lower_to_bottom(self):
        self.scene.lower_to_bottom()




    def on_action_arrange_horizontal(self):
        canvas = self._material_canvas()
        if canvas is not None:
            canvas.arrange_selected('horizontal')
            return
        self.scene.arrange()

    def on_action_arrange_vertical(self):
        canvas = self._material_canvas()
        if canvas is not None:
            canvas.arrange_selected('vertical')
            return
        self.scene.arrange(vertical=True)

    def on_action_arrange_optimal(self):
        canvas = self._material_canvas()
        if canvas is not None:
            canvas.arrange_selected('optimal')
            return
        self.scene.arrange_optimal()

    def on_action_arrange_square(self):
        canvas = self._material_canvas()
        if canvas is not None:
            canvas.arrange_selected('square')
            return
        self.scene.arrange_square()

    def on_action_change_opacity(self):
        images = list(filter(
            lambda item: item.is_image,
            self.scene.selectedItems(user_only=True)))
        widgets.ChangeOpacityDialog(self, images, self.undo_stack)

    def on_action_grayscale(self, checked):
        images = list(filter(
            lambda item: item.is_image,
            self.scene.selectedItems(user_only=True)))
        images = [i for i in images if i.grayscale != checked]
        if images and not self.undo_stack.executing:
            self.undo_stack.push(commands.ToggleGrayscale(images, checked))

    def on_action_crop(self):
        self.scene.crop_items()

    def on_action_flip_horizontally(self):
        self.scene.flip_items(vertical=False)

    def on_action_flip_vertically(self):
        self.scene.flip_items(vertical=True)

    def on_action_reset_scale(self):
        self.cancel_active_modes()
        self.undo_stack.push(commands.ResetScale(
            self.scene.selectedItems(user_only=True)))

    def on_action_reset_rotation(self):
        self.cancel_active_modes()
        self.undo_stack.push(commands.ResetRotation(
            self.scene.selectedItems(user_only=True)))

    def on_action_reset_flip(self):
        self.cancel_active_modes()
        self.undo_stack.push(commands.ResetFlip(
            self.scene.selectedItems(user_only=True)))

    def on_action_reset_crop(self):
        self.cancel_active_modes()
        self.undo_stack.push(commands.ResetCrop(
            self.scene.selectedItems(user_only=True)))

    def on_action_reset_transforms(self):
        self.cancel_active_modes()
        self.undo_stack.push(commands.ResetTransforms(
            self.scene.selectedItems(user_only=True)))


    def on_action_sample_color(self):
        self.cancel_active_modes()
        logger.debug('Entering sample color mode')
        self.viewport().setCursor(Qt.CursorShape.CrossCursor)
        self.active_mode = self.SAMPLE_COLOR_MODE

        if self.scene.has_multi_selection():
            # We don't want to sample the multi select item, so
            # temporarily send it to the back:
            self.scene.multi_select_item.lower_behind_selection()

        pos = self.mapFromGlobal(self.cursor().pos())
        self.sample_color_widget = widgets.SampleColorWidget(
            self,
            pos,
            self.scene.sample_color_at(self.mapToScene(pos)))

    def _is_current_open(self, operation, allow_terminal=False):
        return (getattr(self, '_opening_operation', None) is operation
                and operation.scene is self.scene
                and operation.epoch == getattr(self, '_trace_open_epoch', None)
                and (allow_terminal or not operation.terminal))

    def _cancel_open(self, operation):
        if not self._is_current_open(operation):
            return
        operation.cancel_requested = True
        operation.phase = 'cancelling'
        self._load_scheduler.cancel()
        if operation.preparation_pending:
            self._paged_preparer().cancel()
        if operation.reader is not None and not operation.reader_stopped:
            operation.reader.on_canceled()
        self._catalog_status(_('Cancelling opening; waiting for the reader to stop.'))
        self._settle_cancelled_open(operation)

    def _settle_cancelled_open(self, operation):
        if (not self._is_current_open(operation) or operation.preparation_pending
                or operation.gui_pending
                or (operation.reader is not None and not operation.reader_stopped)):
            return
        error = operation.error
        clear_requested = operation.clear_requested
        self._end_open(operation, 'failed' if error is not None else 'cancelled')
        # Only now is it safe to discard the shared queue/scene.
        if clear_requested or self.filename is None:
            self.clear_scene()
        self._paged_open_failed = error is not None
        self._paged_open_failed_reason = str(error) if error is not None else None
        self._paged_open_cancelled = error is None
        if error is not None:
            self._report_paged_open_failure(operation.filename, error, False)
        else:
            self._catalog_status(_('Opening cancelled; no project was loaded.')
                if self.filename is None else _('Opening cancelled; browsing remains available, saving is not ready.'))

    def _end_open(self, operation, outcome):
        if not self._is_current_open(operation):
            return
        operation.terminal = True
        operation.phase = outcome
        self._project_opening = False
        self._paged_preparing = False
        self._load_scheduler.cancel()
        if operation.bridge is not None:
            operation.bridge.complete_dialog()
            operation.bridge.deleteLater()
            operation.bridge = None
        if getattr(self, 'worker', None) is operation.reader:
            self.worker = None
        if operation.reader is not None and operation.reader_stopped:
            operation.reader.deleteLater()
        if outcome in ('ready', 'baseline_failed') and self.filename == operation.filename:
            # Installation suppresses recounts while _project_opening is true.
            # Finalization's force_rebuild therefore cannot publish the loaded
            # project's counters. Refresh once after lifting the guard, using
            # the whole-project catalog (or honest unknowns, never page totals).
            with trace_operation('project.open.publish_counts'):
                self.category_panel.update_counts()
        elif self.filename is None:
            # No incoming project was installed. Settle the retained sidebar
            # once, now, rather than leaving the closed project's nodes live.
            self.category_panel.force_rebuild()
            self.on_scene_changed([])
        trace_record('project_open_terminal', filename=operation.filename,
                     outcome=outcome, duration_ms=round(
                         (trace_time.perf_counter()-operation.started)*1000, 3))
        # Publish only after the owning operation has settled its GUI install
        # and counters. Consumers must match epoch/project, never guess with a
        # timer or adopt an obsolete reader's result. This is not first paint.
        from prism.opening_lifecycle import OpenCompletion
        self.project_open_finished.emit(OpenCompletion(
            operation.filename, operation.epoch,
            self.scene.project_id if outcome in ('ready', 'baseline_failed') else operation.project_id,
            outcome))

    def _open_install_failed(self, operation, error):
        if not self._is_current_open(operation):
            return
        operation.error = error
        self._cancel_open(operation)

    def _open_reader_stopped(self, operation):
        if not self._is_current_open(operation):
            return
        operation.project_id = self.scene.project_id
        result = operation.reader_result
        if operation.reader.canceled:
            operation.cancel_requested = True
        if not operation.cancel_requested:
            if result is None or not result[0] or result[1]:
                operation.error = RuntimeError('; '.join(result[1])
                    if result and result[1] else 'Reader ended without a successful result')
                operation.cancel_requested = True
            elif operation.source_stamp is not None:
                try:
                    from prism.paged_open_preparation import require_stamp
                    require_stamp(operation.filename, operation.source_stamp)
                except Exception as error:
                    operation.error = error
                    operation.cancel_requested = True
        if operation.cancel_requested:
            self._settle_cancelled_open(operation)
            return
        operation.phase = 'installing'
        self._load_scheduler.finish(lambda: self._open_initial_installed(operation))

    def _open_initial_installed(self, operation):
        if not self._is_current_open(operation) or operation.cancel_requested:
            return
        operation.phase = 'finalizing'
        operation.gui_pending = True
        try:
            with trace_restore_context(operation.context):
                self._finish_loading(operation.filename, [])
            if not self._is_current_open(operation) or operation.cancel_requested:
                return
            if operation.bridge is not None:
                operation.bridge.complete_dialog()
                operation.bridge.deleteLater()
                operation.bridge = None
            if not operation.preparation_pending:
                self._end_open(operation, 'ready')
        except Exception as error:
            logger.exception('Project opening finalization failed')
            # Finalization can already have adopted the filename. A failed
            # installation is not a browsable baseline failure; discard it
            # only after all opening resources and GUI work have settled.
            operation.clear_requested = True
            self._open_install_failed(operation, error)
        finally:
            operation.gui_pending = False
            if operation.cancel_requested:
                self._settle_cancelled_open(operation)

    @_with_open_trace
    def on_items_loaded(self, value):
        self._load_scheduler.schedule()

    @_with_open_trace
    def on_loading_finished(self, filename, errors):
        self._load_scheduler.finish(lambda: self._finish_loading(filename, errors))

    @_with_open_trace
    def _finish_loading(self, filename, errors):
        started = getattr(self, '_trace_open_started', trace_time.perf_counter())
        trace_record('project_open_finalize_start', filename=str(filename))
        failed = False
        try:
            with trace_open_phase('finalize_total', filename=str(filename),
                                  errors=bool(errors)):
                self._finalize_loaded_project(filename, errors)
        except BaseException:
            failed = True
            raise
        finally:
            # This is handler completion, not proof that the first frame has
            # been painted. Previously it was emitted before costly work.
            trace_record('project_open_finished', filename=str(filename),
                         failed=bool(errors) or failed, phase='finalization_complete',
                         duration_ms=round((trace_time.perf_counter()-started)*1000, 3))
            library_project_boundary(self,'open_finalized',failed=bool(errors) or failed,
                                     durable=False,reason='resident_state_not_independent_disk_verification')

    def _finalize_loaded_project(self, filename, errors):
        operation = getattr(self, '_opening_operation', None)
        if operation is None:
            self._project_opening = False
        if errors:
            self._paged_preparing = False
            QtWidgets.QMessageBox.warning(
                self,
                _('Problem loading file'),
                _('<p>Problem loading file %s</p><p>Not accessible or not a proper bee file</p>') % filename)
        else:
            self.filename = filename
            if (operation is not None
                    and getattr(self, '_content_revision', 0) == operation.content_revision
                    and getattr(self, '_undo_revision', 0) == operation.undo_revision):
                # Disk-installed state is clean, but never erase an edit that
                # happened after this operation captured its revision fence.
                self.undo_stack.setClean()
                self._content_dirty = False
            with trace_operation('project.finalize.categories'):
                self.category_panel.force_rebuild()
            if operation is not None and operation.cancel_requested:
                return
            with trace_operation('project.finalize.fit_scene'):
                self.on_action_fit_scene()
            if operation is not None and operation.cancel_requested:
                return
            if hasattr(self.parent, '_reload_workspace_pages'):
                with trace_operation('project.finalize.workspace'):
                    self.parent._reload_workspace_pages()
            if operation is not None and operation.cancel_requested:
                return
            self._register_paged_session(filename)
            if getattr(self.scene, '_partial_project_session', None) is not None:
                self.request_canvas_assets(self.current_canvas_id)
            self._maybe_start_catalog_build(filename,
                on_ready=(lambda cache: self._open_catalog_ready(operation, cache))
                if operation is not None else None)
            # 这里**不能**顺手调 `current_catalog_scope()`：它在范围陈旧时会
            # 立刻排一次重建，而重建会把缓存重写、指纹换掉，正在进行的
            # 画布分页读取随即失败（实测 loaded 320 → 256），状态栏的文案
            # 也被建库提示覆盖。重建必须由保存/范围变化这些真正的触发点
            # 发起，不能由打开收尾顺带发起。
            # Never clear real edits made while asynchronous installation ran.
            # Metadata navigation reuses the existing page-store contract below.
            if operation is None:
                self.undo_stack.setClean()
                self._content_dirty = False
            if getattr(self, '_trace_open_context', None) is not None:
                trace_record('canvas.current_scope_interactive',
                    canvas_id=str(self.current_canvas_id),
                    scope_kind='full' if self._paged_scope is None else 'ids',
                    requested_ids_count=(None if self._paged_scope is None
                                         else len(self._paged_scope)),
                    queue_remaining=self.scene.items_to_add.qsize(),
                    saving_ready=not getattr(self, '_paged_preparing', False),
                    quality_known=False,
                    quality_unknown_reason='main_canvas_draw_observer_not_connected',
                    evidence='finalize_complete_navigation_ready_not_first_paint')

    def _install_catalog_rebind_hook(self, session):
        """Let a partial session report a real Save As to this coordinator."""
        owner = weakref.ref(self)

        def rebound(_old_source, _new_source):
            view = owner()
            if view is not None:
                view._after_partial_save_catalog_rebind(session)
        session.install_source_rebind_hook(rebound)

    def _after_partial_save_catalog_rebind(self, session):
        """Move catalog ownership to the Save As destination, never the old cache."""
        if not session.requires_catalog_rebuild():
            return
        source = str(session.source)
        # Save As supersedes every old-source subscription even when the new
        # file already has a ready cache and therefore needs no replacement
        # worker. Otherwise an old complete callback can publish as current.
        PrismGraphicsView._cancel_catalog_builds(self)
        # Drop the old-file scope and model before any UI refresh. During the
        # transition counts/search must use their honest in-memory fallback.
        self._open_scope = None
        browser = getattr(getattr(self, 'parent', None), 'asset_browser', None)
        if browser is not None:
            browser.invalidate_aggregate_result()
        scope = self._catalog_scope_instance(source)
        panel = getattr(self, 'category_panel', None)
        if panel is not None and hasattr(panel, 'update_counts'):
            panel.update_counts()
        notice = session.catalog_rebuild_notice()
        self._catalog_status(notice['message'])

        def ready(cache_path):
            scene = getattr(self, 'scene', None)
            current_session = getattr(scene, '_partial_project_session', session)
            if (current_session is not session or str(session.source) != source
                    or not session.requires_catalog_rebuild()):
                return
            installed = self._catalog_scope_instance(source, cache_path=cache_path)
            if installed is None or not installed.ready:
                self._catalog_status(
                    _('Project index build failed; it will retry on the next open'))
                return
            self._open_scope = installed
            session.mark_catalog_rebuilt()
            if panel is not None and hasattr(panel, 'update_counts'):
                panel.update_counts()
            self._catalog_status(_('Project index ready.'))

        if scope is not None and scope.ready:
            ready(scope.cache_path)
            return
        self._maybe_start_catalog_build(source, on_ready=ready)

    def _register_paged_session(self, filename):
        """按页打开后登记保存会话，让「编辑一页→保存→重开」闭环。

        只有 `self._paged_scope` 非空（即按页打开）才登记；登记失败时保持
        `scene._partial_project_source`，保存会被明确拒绝，绝不退回全量 writer。
        """
        scope = getattr(self, '_paged_scope', None)
        if scope is None:
            return
        requested = set(getattr(self.scene, '_loaded_scope_ids', ()) or ())
        operation = getattr(self, '_opening_operation', None)
        if operation is not None and self._is_current_open(operation):
            loaded = tuple(sorted(operation.installed_ids & requested))
        else:
            from prism.partial_residency import key_of
            from prism.item_lifecycle import logical_item
            loaded = tuple(key_of(logical_item(item)) for item in self.scene.iter_assets()
                           if key_of(logical_item(item)) in requested)
        self.scene._loaded_scope_ids = loaded
        stamp = getattr(self, '_paged_source_stamp', None)
        if stamp is not None:
            from prism.paged_open_preparation import prepare_session
            self._paged_preparing = True
            self._paged_session_project = self.scene.project_id
            if operation is not None:
                operation.preparation_pending = True
                operation.phase = 'baseline'
            self._catalog_status(_('Preparing safe saving in the background; saving is not ready yet.'))
            self._paged_preparer().start('session', prepare_session, filename,
                loaded, tuple(page['id'] for page in (self.scene.workspace_pages or [])), stamp,
                operation=operation)
            return
        try:
            from prism.partial_project import PartialProjectSession
            page_ids = [page['id'] for page in (self.scene.workspace_pages or [])]
            session = PartialProjectSession.open(
                filename, item_ids=loaded, page_ids=page_ids,
                pages_complete=True, include_boards=True, include_domains=True)
            session.attach(self.scene)
            self._install_catalog_rebind_hook(session)
            trace_record('project_partial_session_attached', filename=str(filename),
                         items=len(loaded), pages=len(page_ids))
            logger.info('按页打开已登记保存会话：%d 条 items，%d 个页面',
                        len(loaded), len(page_ids))
        except Exception:
            logger.exception('按页打开后登记保存会话失败；保存将被拒绝')
            trace_record('project_partial_session_failed', filename=str(filename))

    def _paged_preparer(self):
        preparation = getattr(self, '_paged_open_preparation', None)
        if preparation is None:
            from prism.paged_open_preparation import PagedOpenPreparation
            preparation = self._paged_open_preparation = PagedOpenPreparation(self)
            preparation.completed.connect(self._paged_preparation_completed)
            preparation.operation_completed.connect(self._paged_operation_completed)
            preparation.progress.connect(self._paged_preparation_progress)
        return preparation

    @QtCore.pyqtSlot(int)
    def _paged_preparation_progress(self, indexed):
        """Report first-build cost honestly instead of an open-ended spinner."""
        if getattr(self, '_paged_open_failed', False):
            return
        self._catalog_status(
            _('Indexing this project for on-demand opening: {count} assets')
            .format(count='{:,}'.format(indexed)))

    def _report_paged_open_failure(self, filename, error, cancelled):
        """Tell the user the truth: the canvas is empty and nothing was loaded."""
        name = os.path.basename(str(filename)) or _('the selected file')
        if cancelled:
            # The user asked for this; a dialog would only repeat it back.
            # Saving still has to be blocked: the canvas is empty and no
            # project is open, so a save would either fail on a missing
            # filename or write the wrong thing.
            self._paged_open_cancelled = True
            self._catalog_status(_('Opening cancelled; no project was loaded.'))
            return
        self._catalog_status(_('Project preparation failed: {error}').format(error=error), error=True)
        QtWidgets.QMessageBox.warning(
            self,
            _('Problem opening file'),
            _('<p><b>{name}</b> could not be opened in on-demand mode.</p>'
              '<p>{error}</p>'
              '<p>The canvas is empty and <b>no project is open</b>. '
              'Your file was not modified.</p>').format(name=name, error=error))

    def _paged_operation_completed(self, operation, stage, value, error, duration_ms):
        if not self._is_current_open(operation):
            return
        with trace_restore_context(operation.context):
            self._paged_preparation_completed(stage, value, error, duration_ms,
                                             operation=operation)

    def _paged_preparation_completed(self, stage, value, error, duration_ms, operation=None):
        if operation is None and getattr(self, '_opening_operation', None) is not None:
            return  # Do not also process the legacy duplicate delivery.
        if operation is not None:
            if not self._is_current_open(operation):
                return
            operation.preparation_pending = False
            if operation.cancel_requested:
                self._settle_cancelled_open(operation)
                return
        self._paged_preparing = False
        filename = operation.filename if operation is not None else self._paged_preparation_filename
        trace_record('project_open_preparation', stage=stage, failed=error is not None,
                     duration_ms=round(duration_ms, 3))
        if error is not None:
            from prism.paged_open_preparation import PreparationCancelled
            cancelled = isinstance(error, PreparationCancelled)
            if cancelled:
                logger.info('Paged open %s preparation cancelled', stage)
                trace_record('project_open_cancelled', stage=stage)
            else:
                self._paged_open_failed = True
                self._paged_open_failed_reason = str(error)
                logger.error('Paged open %s preparation failed: %s', stage, error)
            if stage == 'session':
                # The first canvas is already installed: do not call it empty.
                self._catalog_status(_('Browsing is available, but safe saving could not be prepared: {error}').format(error=error), error=True)
                if operation is not None:
                    self._end_open(operation, 'baseline_failed')
                return
            self._report_paged_open_failure(filename, error, cancelled)
            self._project_opening = False
            if operation is not None:
                self._end_open(operation, 'cancelled' if cancelled else 'failed')
            return
        if stage == 'scope':
            self._open_scope = value['scope']
            self._paged_scope = value['ids']
            self._paged_open_target = value['target']
            self._catalog_to_build = value['pending']
            self._paged_source_stamp = value['stamp']
            if operation is not None:
                operation.source_stamp = value['stamp']
            if value['pending']:
                trace_record('project_open_scope_unavailable', reason=value['pending']['reason'])
                self._catalog_status(_('Opening the first canvas from the source; the project index will build in the background.'))
            self._start_project_reader(filename, value['ids'])
            return
        if self.scene.project_id != getattr(self, '_paged_session_project', None):
            return
        try:
            from prism.paged_open_preparation import require_stamp
            require_stamp(filename, self._paged_source_stamp)
            value.attach(self.scene)
            self._install_catalog_rebind_hook(value)
        except Exception as attach_error:
            self._paged_open_failed = True
            self._paged_open_failed_reason = str(attach_error)
            logger.exception('Prepared project session could not attach')
            self._catalog_status(_('Browsing is available, but safe saving could not be prepared: {error}').format(error=attach_error), error=True)
            if operation is not None:
                self._end_open(operation, 'baseline_failed')
            return
        trace_record('project_partial_session_attached', filename=str(filename),
                     items=len(value.loaded()), pages=len(value.loaded_pages),
                     baseline_prepared_off_gui=True)
        trace_record('project_open_ready', filename=str(filename),
                     phase='partial_baseline_attached',
                     duration_ms=round((trace_time.perf_counter() -
                         getattr(self, '_trace_open_started', trace_time.perf_counter())) * 1000, 3))
        self._catalog_status(_('Project saving is ready.'))
        if operation is not None:
            self._end_open(operation, 'ready')
        demand = getattr(self, '_pending_canvas_demand', None)
        self.request_canvas_assets(self.current_canvas_id if demand is None else demand)

    def _open_catalog_ready(self, operation, cache_path):
        if (operation is None or not self._is_current_open(operation, allow_terminal=True)
                or operation.cancel_requested or operation.project_id != self.scene.project_id
                or self.filename != operation.filename):
            return
        try:
            from prism.paged_open_preparation import require_stamp
            from prism.project_open_scope import ProjectOpenScope
            if operation.source_stamp is not None:
                require_stamp(operation.filename, operation.source_stamp)
            scope = self._catalog_scope_instance(operation.filename, cache_path=cache_path)
            if scope is None or not scope.ready:
                return
        except Exception:
            logger.exception('Obsolete source catalog completion was ignored')
            return
        self._open_scope = scope
        if getattr(self.scene, '_partial_project_session', None) is None:
            return
        demand = getattr(self, '_pending_canvas_demand', None)
        self.request_canvas_assets(self.current_canvas_id if demand is None else demand)

    def _subscribe_pending_canvas_catalog(self, filename):
        """A save changes source version; subscribe with a fresh B-only identity."""
        if getattr(self, '_pending_canvas_demand', None) is None:
            return
        session = getattr(self.scene, '_partial_project_session', None)
        scope = getattr(self, '_open_scope', None)
        if session is None or scope is None:
            return
        loader = getattr(self, '_paged_canvas_loader', None)
        if loader is not None:
            loader.cancel(reset=True)
        if scope.ready:
            self.request_canvas_assets(self._pending_canvas_demand)
            return
        from prism.paged_open_preparation import source_stamp, require_stamp
        source = str(session.source)
        stamp = source_stamp(source)
        project_id = self.scene.project_id
        def ready(cache_path):
            if (self.scene.project_id != project_id
                    or self.scene._partial_project_session is not session
                    or str(session.source) != source):
                return
            try:
                require_stamp(source, stamp)
                installed = self._catalog_scope_instance(source, cache_path=cache_path)
                if installed is None or not installed.ready:
                    return
            except Exception:
                logger.exception('Saved-source continuation catalog was obsolete')
                return
            self._open_scope = installed
            demand = getattr(self, '_pending_canvas_demand', None)
            if demand is not None:
                self.request_canvas_assets(demand)
        self._catalog_to_build = dict(project=source, cache=str(scope.cache_path),
                                      reason='canvas_continuation_after_save')
        self._maybe_start_catalog_build(source, on_ready=ready)

    def request_canvas_assets(self, canvas_id):
        """Keep only the latest demand until both baseline and catalog are ready."""
        self._pending_canvas_demand = canvas_id
        operation = getattr(self, '_opening_operation', None)
        if (operation is not None and not operation.terminal
                and operation.phase != 'baseline'):
            return False
        if getattr(self.scene, '_partial_project_session', None) is None:
            return False
        scope = self.current_catalog_scope()
        if scope is None or not scope.ready:
            self._catalog_status(_('The canvas index is still building; browsing loaded assets remains available.'))
            return False
        self._pending_canvas_demand = None
        loader = getattr(self, '_paged_canvas_loader', None)
        if loader is None:
            from prism.paged_canvas_loader import PagedCanvasLoader
            loader = self._paged_canvas_loader = PagedCanvasLoader(self)
            loader.batch_ready.connect(self._paged_canvas_batch_ready)
            loader.finished.connect(self._paged_canvas_finished)
            loader.failed.connect(self._paged_canvas_failed)
        return loader.request(canvas_id)

    def _paged_canvas_batch_ready(self, canvas_id):
        # Filtering uses the current UI scope, never the scope that initiated
        # a now-obsolete navigation. The loader already rejects old projects.
        self.category_panel._apply_filter(keep_pinned=True)
        if canvas_id == self.current_canvas_id:
            camera_policy.reset_if_stale(self)
        self.viewport().update()

    def _paged_canvas_failed(self, canvas_id, error):
        logger.error('Canvas %s page load failed: %s', canvas_id, error)
        trace_record('project_canvas_page_failed', canvas_id=canvas_id, error=error)
        self._catalog_status(_('Canvas loading failed: {error}').format(error=error))

    def _paged_canvas_finished(self, canvas_id):
        pending=getattr(self,'_camera_pending_reset',None)
        if (canvas_id==self.current_canvas_id and pending is not None
                and pending[0][2]==canvas_id and self.transform()==pending[2]):
            # The first batch may have fitted only part of this canvas.
            # Finish the requested landing after all its geometry arrives;
            # a user zoom in the meantime cancels this automatic adjustment.
            camera_policy.request_reset(self,camera_policy.visible_asset_bounds)

    def ensure_asset_loaded(self, item_id):
        """Load one unloaded asset into a partially opened scene (search jump).

        ``item_id`` is the string form of the asset's save id. Reads the item
        row from the ready catalog scope and installs it the same way the paged
        reader does, then registers its baseline with the partial session so a
        later save keeps it instead of refusing ("item was not loaded with this
        page"). Returns the installed item, or ``None`` when the id is unknown,
        the project is not partially loaded, or the type is unsupported (video/
        glb degrade to "page opened, asset not loaded" in the search controller).
        """
        try:
            requested_id = int(item_id)
        except (TypeError, ValueError):
            return None
        for existing in self.scene.iter_assets():
            if getattr(existing, 'save_id', None) == requested_id:
                return existing
        scope = getattr(self, '_open_scope', None)
        if scope is None or not getattr(scope, 'ready', False):
            return None
        if not getattr(self.scene, '_partial_project_source', None):
            # 全量加载时场景已有全部素材，resolve_asset 已能命中，无需增量加载。
            return None
        session = getattr(self.scene, '_partial_project_session', None)
        if session is None:
            return None
        try:
            item_id_int = int(item_id)
        except (TypeError, ValueError):
            return None
        try:
            rows = scope.details([item_id_int])
        except Exception:
            logger.exception('ensure_asset_loaded: details failed for %s', item_id)
            return None
        if not rows:
            return None
        row = rows[0]
        if row.get('type') != 'pixmap':
            return None
        import json
        from PyQt6 import QtCore, QtGui
        from prism.items import PrismPixmapItem
        from prism.partial_project import read_baseline, ITEM_TABLE
        try:
            payload = json.loads(row.get('data') or '{}')
        except (TypeError, ValueError):
            return None
        # 先读基线再安装；失败不触碰 session，避免留下无基线的新行。
        try:
            baseline = read_baseline(self.filename, [(ITEM_TABLE, item_id_int)])
        except Exception:
            logger.exception('ensure_asset_loaded: baseline read failed for %s', item_id)
            return None
        data = {
            'save_id': item_id_int, 'type': 'pixmap',
            'x': row.get('x', 0), 'y': row.get('y', 0), 'z': row.get('z', 0),
            'scale': row.get('scale', 1), 'rotation': row.get('rotation', 0),
            'flip': row.get('flip', 0), 'data': payload,
        }
        item = PrismPixmapItem(QtGui.QImage())
        item.filename = payload.get('filename')
        self.scene._prism_archive_items.add(item)
        item._prism_source = (str(self.filename), item_id_int)
        img_w = payload.get('imageWidth', 0)
        img_h = payload.get('imageHeight', 0)
        if img_w and img_h:
            item._image_size = QtCore.QSize(int(img_w), int(img_h))
            item.reset_crop()
        data['item'] = item
        # 与 read() 同一安装路径：add_item_later + 同步 flush，走 create_from_data
        # 复用 overview/crop/opacity 等字段的恢复逻辑。
        self.scene.add_item_later(data)
        try:
            self.scene.add_queued_items()
        except Exception:
            logger.exception('ensure_asset_loaded: install failed for %s', item_id)
            self.scene._prism_archive_items.discard(item)
            return None
        try:
            session.register_loaded(baseline, [item], self.scene)
        except Exception:
            logger.exception('ensure_asset_loaded: register failed for %s', item_id)
            self.scene.removeItem(item)
            self.scene._prism_archive_items.discard(item)
            return None
        loaded = list(getattr(self.scene, '_loaded_scope_ids', ()) or ())
        if item_id_int not in loaded:
            self.scene._loaded_scope_ids = tuple(loaded) + (item_id_int,)
        return item

    def _cancel_catalog_builds(self):
        """Invalidate this project's requests without waiting on the GUI thread."""
        self._catalog_build_epoch = getattr(self, '_catalog_build_epoch', 0) + 1
        self._catalog_to_build = None
        self._catalog_build_pending = None
        active = getattr(self, '_catalog_build_active', None)
        if active is not None:
            active['callbacks'].clear()
            active['cancel'].set()
        self._catalog_build_cancel = None

    @staticmethod
    def _append_catalog_callback(request, callback):
        if callback is not None and callback not in request['callbacks']:
            request['callbacks'].append(callback)

    def _catalog_build_request(self, pending, on_ready):
        """Freeze one target identity before it enters the coordinator."""
        from prism.project_catalog_index import ProjectCatalogIndex
        index = ProjectCatalogIndex(pending['project'], pending['cache'])
        project_id = getattr(getattr(self, 'scene', None), 'project_id', None)
        request = {
            'build_id': uuid.uuid4().hex,
            'index': index,
            'project': str(index.source),
            'cache': str(index.cache),
            'reason': pending['reason'],
            'revision': index.source_revision(),
            'project_id': project_id,
            'epoch': getattr(self, '_catalog_build_epoch', 0),
            'cancel': threading.Event(),
            'callbacks': [],
            'context': trace_capture_context(),
            'catalog_deltas': tuple(pending.get('catalog_deltas', ())),
            'recover_corrupt': (os.path.normcase(str(index.cache)) ==
                                os.path.normcase(_catalog_cache_path(index.source))),
        }
        request['target'] = (os.path.normcase(request['project']),
                             os.path.normcase(request['cache']),
                             request['revision'])
        PrismGraphicsView._append_catalog_callback(request, on_ready)
        return request

    def _maybe_start_catalog_build(self, filename, on_ready=None):
        """Merge, supersede or start the one coordinated catalog writer.

        A replacement is only launched from the previous worker's terminal GUI
        delivery. This is the hand-off point that proves the old SQLite lock and
        all of its connections have been released; the GUI never joins a worker
        and never sleeps on a database lock.
        """
        pending = getattr(self, '_catalog_to_build', None)
        if not pending:
            return
        self._catalog_to_build = None
        try:
            request = PrismGraphicsView._catalog_build_request(self, pending, on_ready)
        except Exception as error:
            logger.exception('目录建库目标身份读取失败')
            trace_record('project_catalog_build_failed',
                         project=os.path.basename(str(filename)),
                         phase='target_identity', error=str(error)[:200])
            self._catalog_status(
                _('Project index build failed; it will retry on the next open'))
            return

        active = getattr(self, '_catalog_build_active', None)
        if active is not None:
            if active['target'] == request['target'] and not active['cancel'].is_set():
                PrismGraphicsView._append_catalog_callback(active, on_ready)
                trace_record('project_catalog_build_merged',
                             build_id=active['build_id'], reason=request['reason'],
                             project=os.path.basename(request['project']))
                return
            queued = getattr(self, '_catalog_build_pending', None)
            # Retain the receipt chain when a second confirmed save supersedes
            # a still-running index update. The index chooses the matching base.
            predecessors = (queued or active).get('catalog_deltas', ())
            if (predecessors and request['catalog_deltas']
                    and (queued or active)['project'] == request['project']):
                unique = {r.revision: r for r in (*predecessors, *request['catalog_deltas'])}
                request['catalog_deltas'] = tuple(unique.values())
            if queued is not None and queued['target'] == request['target']:
                PrismGraphicsView._append_catalog_callback(queued, on_ready)
            else:
                self._catalog_build_pending = request
            active['cancel'].set()
            trace_record('project_catalog_build_superseded',
                         build_id=active['build_id'], next_build_id=request['build_id'],
                         project=os.path.basename(request['project']))
            self._catalog_status(
                _('Waiting for the previous project index task to finish…'))
            return
        PrismGraphicsView._start_catalog_build(self, request)

    @staticmethod
    def _build_catalog_with_retry(view, request, recovery):
        """Build the catalog, retrying when the source changed mid-build.

        A save that lands while the build is running rewrites the source, and
        the build then refuses to publish a cache that describes the old
        file.  Calling that a failed build throws the work away and leaves
        the project without an index until the next save asks for one - and
        the library pages fall back to the slow scene path meanwhile.  The
        build is therefore restarted against the new fingerprint instead.
        """
        from prism.project_catalog_index import CatalogNotReady, ProjectCatalogIndex
        attempts = 0
        while True:
            try:
                return request['index'].build(
                    cancel=request['cancel'].is_set,
                    lock_wait_seconds=2.0, lock_poll_seconds=0.05, **recovery)
            except CatalogNotReady as caught:
                if 'Source changed' in str(caught):
                    trace_record('project_catalog_source_changed', build_id=request['build_id'],
                                 attempt=attempts, source_before=getattr(caught, 'source_before', None),
                                 source_after=getattr(caught, 'source_after', None),
                                 data_versions=getattr(caught, 'data_versions', None))
                if attempts >= 2 or 'Source changed' not in str(caught):
                    raise
                attempts += 1
                request['index'] = ProjectCatalogIndex(request['project'], request['cache'])
                request['revision'] = request['index'].source_revision()
                request['target'] = (os.path.normcase(request['project']),
                                     os.path.normcase(request['cache']),
                                     request['revision'])
                # The receipts describe the source that is already gone.
                request['catalog_deltas'] = ()
                trace_record('project_catalog_build_retried',
                             build_id=request['build_id'], attempt=attempts)

    def _start_catalog_build(self, request):
        """Start exactly one worker; terminal delivery owns the next start."""
        delivery = getattr(self, '_catalog_build_delivery', None)
        if delivery is None:
            delivery = self._catalog_build_delivery = _CatalogBuildDelivery(self)
            delivery.delivered.connect(
                partial(PrismGraphicsView._catalog_build_received, self),
                Qt.ConnectionType.QueuedConnection)
        self._catalog_build_active = request
        self._catalog_build_cancel = request['cancel']
        trace_record('project_catalog_build_started',
                     build_id=request['build_id'], reason=request['reason'],
                     project=os.path.basename(request['project']),
                     open_phase='post_open_build')
        self._catalog_status(_('Preparing the project index in the background…'))

        def build():
            from prism.project_catalog_index import CatalogBuildCancelled
            status, result, error = 'failed', None, None
            try:
                deltas = request.get('catalog_deltas', ())
                result = (request['index'].apply_save_receipts(deltas, cancel=request['cancel'].is_set)
                          if deltas else None)
                incremental = result is not None
                request['incremental_applied'] = incremental
                if result is None:
                    if request['cancel'].is_set():
                        raise CatalogBuildCancelled('Superseded catalog update')
                    if deltas:
                        from prism.source_fingerprint import file_fingerprint
                        if deltas[-1].source_after != file_fingerprint(request['project']):
                            # A later transaction committed before its GUI
                            # acknowledgement arrived. Keep the usable cache
                            # base for that receipt, rather than invalidate it
                            # with a speculative 100k-row rebuild.
                            request['awaiting_receipt'] = True
                            raise CatalogBuildCancelled('Waiting for the latest save receipt')
                    recovery = ({'recover_corrupt': True} if request.get('recover_corrupt') else {})
                    result = PrismGraphicsView._build_catalog_with_retry(self, request, recovery)
                if incremental and result.complete and not request['cancel'].is_set():
                    changed = {key for receipt in deltas for key in receipt.catalog_item_ids}
                    if len(changed) <= 1024:
                        request['delta_summaries'] = request['index'].summaries(changed)
                if result.complete and not request['cancel'].is_set():
                    # Warm the existing instance cache before handing this
                    # index to the GUI; never perform its cold GROUP BY there.
                    request['index'].tag_groups(cancel=request['cancel'].is_set)
                if request['cancel'].is_set():
                    status = 'cancelled'
                elif result.complete:
                    status = 'complete'
                else:
                    status = 'incomplete'
                trace_record('project_catalog_build_finished',
                             build_id=request['build_id'], status=status,
                             complete=result.complete, rows=result.total_records,
                             seconds=round(result.seconds, 3),
                             lock_wait_ms=round(result.lock_wait_seconds * 1000, 3),
                             resumed=result.resumed, incremental=incremental,
                             processed_records=result.processed_records)
                logger.info('后台目录建库终态：build=%s，%s 条 / %.2f 秒 / 等锁 %.3f 秒 / %s',
                            request['build_id'], result.total_records, result.seconds,
                            result.lock_wait_seconds, status)
            except CatalogBuildCancelled as caught:
                status, error = 'cancelled', caught
                trace_record('project_catalog_build_cancelled',
                             build_id=request['build_id'],
                             phase='receipt_advanced' if request.get('awaiting_receipt') else 'writer_lock')
            except Exception as caught:
                error = caught
                logger.exception('后台目录建库失败')
                trace_record('project_catalog_build_failed',
                             build_id=request['build_id'],
                             project=os.path.basename(request['project']),
                             error=str(caught)[:200])
            finally:
                try:
                    delivery.delivered.emit(
                        (request, status, result, error, request['context']))
                except RuntimeError:
                    # QObject owner was destroyed; the daemon may finish quietly.
                    pass

        def traced_build():
            with trace_restore_context(request['context']):
                with trace_span('project.catalog_build', stage='post_open_build',
                                build_id=request['build_id']):
                    build()
        thread = threading.Thread(target=traced_build,
                                  name='prism-catalog-build', daemon=True)
        request['thread'] = thread
        thread.start()

    @QtCore.pyqtSlot(object)
    def _catalog_build_received(self, payload):
        request, status, result, error, context = payload
        if request is not getattr(self, '_catalog_build_active', None):
            return
        self._catalog_build_active = None
        self._catalog_build_cancel = None
        scene = getattr(self, 'scene', None)
        current = (request['epoch'] == getattr(self, '_catalog_build_epoch', 0)
                   and request['project_id'] == getattr(scene, 'project_id', None))
        with trace_restore_context(context):
            trace_record('project_catalog_build_terminal',
                         build_id=request['build_id'], status=status,
                         current=current)
            if (current and status == 'complete' and result is not None
                    and not request['cancel'].is_set()):
                installed = PrismGraphicsView._catalog_scope_instance(
                    self, request['project'], cache_path=request['cache'], index=request['index'])
                if installed is not None and installed.ready:
                    self._catalog_status(_('Project index ready ({rows} items)').format(
                        rows=result.total_records))
                    panel = getattr(self, 'category_panel', None)
                    recount = getattr(panel, '_recount_timer', None)
                    if recount is not None and not recount.isActive():
                        recount.start()
                    browser = getattr(getattr(self, 'parent', None), 'asset_browser', None)
                    if browser is not None:
                        resume = getattr(browser, 'resume_saved_projection', None)
                        if resume is None or not resume(request):
                            browser._save_display_snapshot = None
                            browser.invalidate_aggregate_result()
                    for callback in tuple(request['callbacks']):
                        try:
                            callback(request['cache'])
                        except Exception:
                            logger.exception('目录建库完成回调失败')
            elif current and status in ('failed', 'incomplete'):
                self._catalog_status(
                    _('Project index build failed; it will retry on the next open'))
                browser = getattr(getattr(self, 'parent', None), 'asset_browser', None)
                if browser is not None:
                    browser.directory_build_failed()

        queued = getattr(self, '_catalog_build_pending', None)
        self._catalog_build_pending = None
        if queued is None:
            # The source can move after the worker's successful commit but
            # before this GUI receipt. Reuse the coordinator, preserving the
            # subscribers instead of leaving a pending canvas with no worker.
            if current and status == 'complete' and getattr(self, '_catalog_to_build', None):
                callbacks = tuple(request['callbacks'])
                PrismGraphicsView._maybe_start_catalog_build(
                    self, request['project'], on_ready=callbacks[0] if callbacks else None)
                active = getattr(self, '_catalog_build_active', None)
                if active is not None:
                    for callback in callbacks[1:]:
                        PrismGraphicsView._append_catalog_callback(active, callback)
            return
        scene = getattr(self, 'scene', None)
        if (queued['epoch'] != getattr(self, '_catalog_build_epoch', 0)
                or queued['project_id'] != getattr(scene, 'project_id', None)):
            return
        PrismGraphicsView._start_catalog_build(self, queued)

    def _refresh_catalog_after_save(self, filename, *, receipt=None, receipts=(), reason='source_changed_by_save'):
        """Re-point the read-only catalog at the file this save just wrote.

        A save produces a NEW source version.  The catalog cache stores the
        fingerprint of the file it was built from, so after a successful save
        it is genuinely stale: ``_ready`` refuses it, the counts fall back to
        "unknown" and the interface shows "Preparing...".  That is the honest
        state and it is what the aggregation must say.

        A confirmed partial-save receipt can advance the exact changed rows
        in the background. Only a continuous, validated before/after chain
        allows that transition; otherwise use the full rebuild path.

        While a proven delta is pending, an existing frame may stay visible
        explicitly labelled "updating", with interaction disabled and current
        whole-project counts unknown. Never relabel stale rows as ready.
        """
        scope = getattr(self, '_open_scope', None)
        if scope is None:
            # No catalog is in use for this project: nothing to re-point.
            return
        browser = getattr(getattr(self, 'parent', None), 'asset_browser', None)
        invalidated = False
        scope_source = getattr(scope, 'project_path', None)
        if scope_source is not None:
            old_source = os.path.normcase(os.path.abspath(str(scope_source)))
            new_source = os.path.normcase(os.path.abspath(str(filename)))
            if old_source != new_source:
                # Save As owns a different cache key. Invalidate the old model
                # and worker subscriptions before checking the new cache's
                # ready fast path; no replacement worker may be needed.
                PrismGraphicsView._cancel_catalog_builds(self)
                self._open_scope = None
                if browser is not None:
                    browser.invalidate_aggregate_result()
                    invalidated = True
                scope = self._catalog_scope_instance(filename)
                if scope is None:
                    return
        try:
            if scope.ready:
                # The catalog still describes this exact file.  Either the
                # save did not touch the indexed rows, or the cache was
                # already rebuilt.  Do not start a pointless rebuild.
                return
        except Exception:
            pass
        try:
            cache = scope.cache_path
        except Exception:
            cache = None
        if not cache:
            return
        self._catalog_to_build = {'project': str(filename), 'cache': cache,
                                  'reason': reason}
        incoming = tuple(receipts) + ((receipt,) if receipt is not None else ())
        if incoming and all(getattr(item, 'source_before', None) is not None
                            and getattr(item, 'source_after', None) is not None for item in incoming):
            chain_key = (getattr(self.scene, 'project_id', None),
                         os.path.normcase(os.path.abspath(str(filename))))
            previous_chain = getattr(self, '_catalog_save_receipts', None)
            chain = list(previous_chain[1]) if previous_chain and previous_chain[0] == chain_key else []
            for item in incoming:
                if not chain or chain[-1].revision != item.revision:
                    chain.append(item)
            chain = tuple(chain[-64:])
            self._catalog_save_receipts = (chain_key, chain)
            self._catalog_to_build['catalog_deltas'] = chain
        else:
            self._catalog_save_receipts = None
        trace_record('project_catalog_rebuild_requested', filename=str(filename),
                     reason=reason,
                     project=os.path.basename(str(filename)))
        # The old aggregate rows describe the file as it was BEFORE this save.
        # Drop them now rather than keep serving them as "the whole project".
        # Save As already invalidated while switching the scope above.
        if browser is not None and not invalidated:
            if self._catalog_to_build.get('catalog_deltas'):
                browser.suspend_aggregate_result()
            else:
                browser.invalidate_aggregate_result()
        self._maybe_start_catalog_build(filename)

    def _catalog_status(self, text, *, error=False):
        """目录四态进度：把后台建库状态写到主窗口状态栏（尽力而为）。"""
        epoch = getattr(self, '_trace_open_epoch', 0)
        if error:
            self._catalog_error_status = (epoch, text)
        elif (getattr(self, '_paged_open_failed', False)
              and getattr(self, '_catalog_error_status', (None,))[0] == epoch):
            # Index progress cannot clear a failed safe-saving baseline. Only
            # recovery or a new open may replace this project's failure state.
            return
        try:
            if hasattr(self.parent, 'statusBar'):
                self.parent.statusBar().showMessage(text, 0 if error else 8000)
        except RuntimeError:
            # 建库回调可能落在已销毁的窗口上；状态栏只是提示，忽略。
            pass

    def on_action_open_recent_file(self, filename):
        confirm = self.get_confirmation_unsaved_changes(
            _('There are unsaved changes. Are you sure you want to open a new scene?'))
        if confirm:
            # `open_from_file` honours the read-only marker itself, so the
            # licence is the same however the project was opened.
            self.open_from_file(filename)

    def _recent_entry_is_readonly(self, filename):
        """Whether this path is marked read-only in the settings.

        The marker is a separate key rather than a field next to the recent
        entry, because `PrismSettings.update_recent_files` rewrites the whole
        `RecentFiles` array on every open and would drop a field stored
        inside it.
        """
        try:
            value = self.settings.value('Project/readonly_paths', '')
        except Exception:
            return False
        if not value:
            return False
        wanted = os.path.normcase(os.path.abspath(str(filename)))
        for entry in str(value).split(';'):
            entry = entry.strip()
            if not entry:
                continue
            if os.path.normcase(os.path.abspath(entry)) == wanted:
                return True
        return False

    @staticmethod
    def _last_canvas_setting_key(filename):
        """QSettings key for the last-viewed canvas of a given project file."""
        return 'PagedOpen/last_canvas/' + os.path.normcase(os.path.abspath(str(filename)))

    def _preferred_open_canvas(self, filename, pixmaps):
        """3A 选目标：上次有效页面（跨会话）优先，否则当前画布，再否则最大画布。

        ``pixmaps`` 是 ``{canvas_id: count}``（来自目录）。三者都不可用时不猜。
        """
        try:
            last = self.settings.value(self._last_canvas_setting_key(filename))
        except Exception:
            last = None
        if last in pixmaps:
            return last
        current = getattr(self, 'current_canvas_id', None) or 'default-canvas'
        if current in pixmaps:
            return current
        return max(pixmaps.items(), key=lambda item: item[1])[0]

    def _remember_last_canvas(self):
        """成功保存后记录当前画布，供下次按页打开恢复上次页面。"""
        if not self.filename:
            return
        try:
            self.settings.setValue(
                self._last_canvas_setting_key(self.filename), self.current_canvas_id)
        except Exception:
            logger.exception('failed to remember last canvas')

    def _paged_open_scope(self, filename):
        """按需打开：目录有效时给出当前页的 item id；否则返回 None。

        **开关**：环境变量 `PRISM_PAGED_OPEN=1`（默认关闭）。目录缓存放在设置
        目录下的 `catalog/`，与预览缓存并列。缓存缺失、不完整或源已变时一律
        返回 None —— 退回全量读取（旧路径），绝不猜一个范围。

        返回的 id 列表会交给 `load_prism(scope=...)`，它只读这些行，并在 scene
        上登记 `_partial_project_source`，让保存必须走部分保存快照。
        """
        if os.environ.get('PRISM_PAGED_OPEN') != '1':
            return None
        if not fileio.is_prism_file(filename):
            return None
        self._catalog_to_build = None
        self._open_scope = None
        try:
            from prism.project_open_scope import ProjectOpenScope
            cache = _catalog_cache_path(filename)
            scope = ProjectOpenScope(filename, cache)
            # 保存范围实例，供聚合计数/查询（ProjectAssetCatalog）在按页打开
            # 后仍以全项目口径读目录。目录不可用时它 not ready，建库完成后
            # 自动可用；不可用期间聚合计数回退 scene 内存（安全）。
            self._open_scope = scope
            if not scope.ready:
                reason = ('catalog_missing' if not os.path.exists(cache)
                          else 'catalog_stale_or_incomplete')
                trace_record('project_open_scope_unavailable', reason=reason,
                             project=os.path.basename(str(filename)))
                # 目录不可用时退回全量（安全、不猜范围），同时登记"打开完成后
                # 后台渐进建库"，让下一次打开能走按需路径。
                self._catalog_to_build = {'project': str(filename), 'cache': cache,
                                          'reason': reason}
                return None
            counts = scope.counts() or {}
            pixmaps = {canvas_id: count for (canvas_id, kind), count
                       in counts.items() if kind == 'pixmap'}
            if not pixmaps:
                trace_record('project_open_scope_unavailable', reason='no_pixmap_scope')
                return None
            target = self._preferred_open_canvas(filename, pixmaps)
            self._paged_open_target = target
            page = scope.page(target, kind='pixmap', limit=256)
            ids = [row['id'] for row in page]
            trace_record('project_open_scope_page', canvas_id=target,
                         ids=len(ids), scopes=len(pixmaps))
            logger.info('按需打开：画布 %s 的一页 %d 条（目录共 %d 个范围）',
                        target, len(ids), len(pixmaps))
            return ids
        except Exception:
            logger.exception('按需打开的范围解析失败，退回全量读取')
            trace_record('project_open_scope_unavailable', reason='exception')
            return None

    def current_catalog_scope(self):
        """Return the live scope and repair a stale fingerprint in the background."""
        scope = getattr(self, '_open_scope', None)
        if scope is None or scope.ready:
            return scope
        if getattr(self, '_catalog_build_active', None) is not None:
            return scope
        import time
        now = time.monotonic()
        if scope is not getattr(self, '_catalog_repair_scope', None):
            self._catalog_repair_scope = scope
            self._catalog_repair_after = 0
        if now < getattr(self, '_catalog_repair_after', 0):
            return scope
        self._catalog_repair_after = now + 5
        scene = self.scene
        source, cache = str(scope.index.source), str(scope.cache_path)
        def repair():
            if isinstance(self, QtCore.QObject):
                from PyQt6 import sip
                if sip.isdeleted(self):
                    return
            if self.scene is not scene or self._open_scope is not scope:
                return
            # A confirmed save may have started its exact delta after this
            # callback was queued. Do not replace that receipt chain with an
            # unproven full rebuild or start a second repair after ready.
            if scope.ready or getattr(self, '_catalog_build_active', None) is not None:
                return
            if not getattr(self, '_catalog_to_build', None):
                self._catalog_to_build = dict(project=source, cache=cache, reason='scope_became_stale')
            trace_record('project_catalog_repair_requested', reason='scope_became_stale')
            def ready(path):
                if self.scene is not scene or self._open_scope is not scope:
                    return
                installed = self._catalog_scope_instance(source, cache_path=path)
                if installed is not None and installed.ready:
                    self._open_scope = installed
                    self.category_panel.update_counts()
                    self.category_panel.search_content_changed()
                    demand = getattr(self, '_pending_canvas_demand', None)
                    if demand is not None:
                        self.request_canvas_assets(demand)
            self._maybe_start_catalog_build(source, on_ready=ready)
        QtCore.QTimer.singleShot(0, repair)
        return scope

    def _catalog_scope_instance(self, filename, *, cache_path=None, index=None):
        """给**默认（全量）打开**路径也准备一个只读目录范围实例（T1 的前提）。

        按需打开路径的实例由 `paged_open_preparation.resolve_scope` 建；全量
        打开路径原来一个都没有 —— 于是 `ProjectAssetCatalog._catalog_scope()`
        恒为 `None`，目录口径（计数下推、检索下推、预聚合、trigram）在默认
        路径上一条都用不上，只能退回 scene 内存扫描（10 万档实测那 5.78 s
        的一段：`asset_browser.refresh → project_catalog.query → _scene_rows`）。

        这里只负责**准备好实例**，不做任何按页范围计算：目录还没建好时登记
        `_catalog_to_build`，让打开完成后走后台渐进建库（`_maybe_start_catalog_build`），
        下一次打开就有目录可用了。建库是只读旁路缓存，源文件始终是权威。

        目录不可用时一律退回 scene 内存口径，绝不拿"准备中"冒充"0 条"——
        `ProjectAssetCatalog._catalog_scope()` 会把这种情况记成
        `catalog_scope_miss(reason='not_ready')`，口径不漂。

        返回建好的实例（可能尚未 ready），或 `None`（连实例都建不出来）。
        """
        if not fileio.is_prism_file(filename):
            return None
        try:
            from prism.project_open_scope import ProjectOpenScope
            cache = cache_path if cache_path is not None else _catalog_cache_path(filename)
            def same_path(left, right):
                left = os.path.normcase(os.path.abspath(str(left)))
                right = os.path.normcase(os.path.abspath(str(right)))
                return left == right or os.path.normcase(os.path.realpath(left)) == os.path.normcase(os.path.realpath(right))
            current = getattr(self, '_open_scope', None)
            if (current is not None and same_path(current.project_path, filename)
                    and same_path(current.cache_path, cache)):
                scope = current
            else:
                scope = ProjectOpenScope(filename, cache)
            if index is not None:
                if not (same_path(index.source, filename) and same_path(index.cache, cache)):
                    return None
                scope.index = index
                scope._telemetry_cache = None
        except Exception:
            logger.exception('目录范围实例创建失败；退回 scene 内存口径')
            trace_record('project_open_scope_unavailable', reason='exception')
            return None
        self._open_scope = scope
        if scope.ready:
            trace_record('project_open_scope_ready',
                         project=os.path.basename(str(filename)))
            # A ready directory remains readable while the existing background
            # writer adds a covering index. Do not invalidate it or rescan rows.
            needs_upgrade = getattr(scope.index, 'needs_index_upgrade', None)
            self._catalog_to_build = ({'project': str(filename), 'cache': cache,
                                       'reason': 'additive_index_upgrade'}
                                      if needs_upgrade is not None and needs_upgrade() else None)
            return scope
        reason = ('catalog_missing' if not os.path.exists(cache)
                  else 'catalog_stale_or_incomplete')
        trace_record('project_open_scope_unavailable', reason=reason,
                     project=os.path.basename(str(filename)))
        # 目录不可用时退回 scene 口径（安全、不猜范围），同时登记"打开完成后
        # 后台渐进建库"，让下一次打开能用上目录。
        self._catalog_to_build = {'project': str(filename), 'cache': cache,
                                  'reason': reason}
        return scope

    def _paged_open_enabled(self):
        """Whether a scoped, on-demand open may be attempted.

        On by default since 2026-10-23 (coordination/决策-20261023.md): the
        registered default `PAGED_OPEN_DEFAULT` governs when the user never
        touched the switch. The environment variable remains a hard override
        so an existing reproduction keeps working untouched.
        """
        override = os.environ.get('PRISM_PAGED_OPEN')
        if override is not None:
            return override == '1'
        try:
            value = self.settings.valueOrDefault('Project/paged_open')
        except Exception:
            logger.exception('Cannot read the scoped-open setting; using the full reader')
            return False
        if isinstance(value, bool):
            return value
        return str(value).strip().lower() in ('1', 'true', 'on', 'yes')

    def _already_open_project(self, filename):
        """Is this the project the application already shows?"""
        current = getattr(self, 'filename', None)
        if not current:
            return False
        return same_project(current, filename)

    def _show_open_project(self):
        """Show the project that is already open, without rebuilding it."""
        window = self.parent
        select = getattr(window, '_select_resource_page', None)
        if callable(select):
            try:
                select(self.current_canvas_id)
            except Exception:
                logger.exception('Could not select the canvas of the open project')
        status = getattr(window, 'statusBar', None)
        if callable(status):
            status().showMessage(_('This project is already open.'), 4000)

    def open_from_file(self, filename, readonly=False, *, _upgrade_preview=False):
        # The project that is already open is not opened a second time.
        # Opening it again clears the scene -- the canvas goes blank and the
        # whole interface flashes -- replaces the resource tree, which loses
        # the expansion it had, and loads every asset again.  The user sees
        # the application blank out and come back, and reads it as "it
        # crashed and restarted".
        current = getattr(self, 'filename', None)
        if current and same_project(current, filename):
            self._show_open_project()
            return False
        if (not _upgrade_preview and str(filename).lower().endswith('.prism.data')
                and os.path.isdir(filename)):
            if not self._allow_project_replacement():
                return False
            from prism.storage.entrypoint import ensure_entry
            from prism.actions.storage_workflow import run
            epoch = getattr(self, '_trace_open_epoch', None)
            project = self.scene.project_id
            def open_entry(entry):
                if (getattr(self, '_trace_open_epoch', None) == epoch
                        and self.scene.project_id == project):
                    self.open_from_file(str(entry), readonly=readonly)
            run(self, '准备工程入口', lambda cancel: ensure_entry(filename), open_entry)
            return False  # Entry repair owns the eventual open, not this call.
        from prism.storage.managed import database_path
        try:filename=database_path(filename)
        except Exception as error:
            QtWidgets.QMessageBox.warning(self,'无法打开工程',str(error))
            return False
        # Do not mutate the existing project for a request we may reject.
        if not self._allow_project_replacement():
            return False
        if getattr(self,'require_directory_upgrade',True) and not _upgrade_preview:
            from prism.storage.managed import root_for
            if (root_for(filename) is None and str(filename).lower().endswith('.prism')
                    and os.path.isfile(filename)):
                from prism.actions.storage_workflow import upgrade_on_open
                upgrade_on_open(self,filename,readonly=readonly)
                return False  # Conversion owns the eventual open; old data is not installed.
        readonly_reason = 'user_requested' if readonly else 'settings_marker'
        readonly = readonly or self._recent_entry_is_readonly(filename)
        self._trace_open_started = trace_time.perf_counter()
        epoch = getattr(self, '_trace_open_epoch', 0) + 1
        self._trace_open_epoch = epoch
        # A different project means the automatic-save failures of the old
        # one say nothing about the new one.
        self._autosave_project_changed()
        trace_bind_project(uuid.uuid4().hex, open_epoch=epoch, generation=epoch)
        self._trace_open_context = trace_capture_context(trace_id=uuid.uuid4().hex)
        trace_record('project_open_start', filename=str(filename))
        logger.info(f'Opening file {filename}')
        self._replacing_project = True
        try:
            self.clear_scene()
        finally:
            self._replacing_project = False
        from prism.resource_budget import open_project_resources
        self._resource_project_key = open_project_resources(filename).key
        trace_record('project_resources_opened', scope=self._resource_project_key)
        self._project_opening = True
        from prism.opening_lifecycle import OpenOperation
        operation = self._opening_operation = OpenOperation(
            str(filename), self.scene, epoch, self._trace_open_context,
            self._trace_open_started, getattr(self, '_content_revision', 0),
            getattr(self, '_undo_revision', 0))
        if readonly:
            fileio.request_readonly_project(self.scene, filename)
            trace_record('project_open_readonly_requested', filename=str(filename),
                         reason=readonly_reason)
        self._paged_preparation_filename = str(filename)
        self._paged_open_failed_reason = None
        from prism.storage.managed import root_for
        if self._paged_open_enabled() or root_for(filename) is not None:
            from prism.paged_open_preparation import resolve_scope
            cache = _catalog_cache_path(filename)
            try:
                last = self.settings.value(self._last_canvas_setting_key(filename))
            except Exception:
                last = None
            self._paged_preparing = True
            operation.preparation_pending = True
            self._catalog_status(_('Preparing the current canvas in the background.'))
            preparation = self._paged_preparer()
            with trace_restore_context(self._trace_open_context):
                preparation.start('scope', resolve_scope, str(filename), cache,
                    last, getattr(self, 'current_canvas_id', None) or 'default-canvas',
                    progress=preparation.progress.emit, operation=operation)
            # Nothing is open yet. _paged_preparation_completed owns adopting the
            # filename once preparation actually succeeded, so a later failure
            # cannot leave an empty scene pointing at a real project: saving
            # that scene would rewrite the file with nothing in it.
            return False
        # 默认（全量）打开路径也要有目录实例 —— 这是 T1 生效的最后一米：
        # 没有它，`ProjectAssetCatalog._catalog_scope()` 恒为 None，目录口径
        # 在默认路径上一条都用不上。按需打开路径的实例由
        # `paged_open_preparation.resolve_scope` 建，这里不碰它。
        with trace_open_phase('scope_instance', filename=str(filename),
                              paged=self._paged_open_enabled()):
            self._catalog_scope_instance(filename)
        # The database read itself is asynchronous (`fileio.ThreadedIO`), so
        # this stage is only "handing off". The read is timed where it
        # actually runs -- see `fileio.load_prism`.
        with trace_open_phase('reader_handoff', filename=str(filename)):
            self._start_project_reader(filename, None)
        return False  # Accepted but asynchronous, not an adopted filename.

    def _start_project_reader(self, filename, scope):
        from prism.opening_lifecycle import OpenReader, OpenReaderBinding
        operation = self._opening_operation
        if not self._is_current_open(operation) or operation.cancel_requested:
            return
        self._paged_scope = scope
        operation.phase = 'reading'
        if operation.source_stamp is None and os.path.isfile(filename):
            try:
                from prism.paged_open_preparation import source_stamp
                operation.source_stamp = source_stamp(filename)
            except Exception as error:
                self._open_install_failed(operation, error)
                return
        if scope is not None:
            self.scene._partial_project_source = str(filename)
        self.worker = operation.reader = OpenReader(
            partial(_read_project_with_trace, operation.context),
            filename, self.scene,
            **({'scope': scope} if scope is not None else {}))
        bridge = operation.bridge = OpenReaderBinding(self, operation, self.worker)
        def installed(item):
            from prism.partial_residency import key_of
            from prism.item_lifecycle import logical_item
            key = key_of(logical_item(item))
            if key is not None:
                operation.installed_ids.add(key)
        self._load_scheduler.bind(operation,
            lambda op: self._is_current_open(op) and not op.cancel_requested,
            installed, lambda error: self._open_install_failed(operation, error))
        # 打开工程的进度条不允许"闪一下就没了"。
        #
        # 这个对话框是 WindowModal：它一出现就盖住窗口中间一条，
        # 每次重画都会逼着底下的区域（素材库实测 1 万张卡片 ≈ 433 ms）
        # 跟着重画。实测一次打开：进度条 22:23:12.373 → 12.839 = 466 ms，
        # 其中一次 gui_stall 206.6 ms，栈顶正是 on_finished 的 setValue(0)。
        # 466 ms 的方块弹一下又消失，用户看到的就是"整个界面闪了一次"，
        # 比不显示还糟——操作早就结束了，根本来不及读。
        #
        # 所以只有"真的要做很久"的操作才配拥有进度条：让它先在屏幕外
        # 等 QUIET_MIN_DURATION_MS，超过了这个时间才弹出来。
        # 导出/保存/导入这些必须能被"取消"的流程仍然立刻弹框——
        # 不显示的进度条是没法取消的。
        self.progress = widgets.PrismProgressDialog(
            f'Loading {filename}', worker=bridge, parent=self,
            min_duration_ms=widgets.PrismProgressDialog.QUIET_MIN_DURATION_MS)
        self.worker.start()

    def on_action_open(self):
        confirm = self.get_confirmation_unsaved_changes(
            _('There are unsaved changes. Are you sure you want to open a new scene?'))
        if not confirm:
            return

        self.cancel_active_modes()
        filename, f = QtWidgets.QFileDialog.getOpenFileName(
            parent=self,
            caption=_('Open file'),
            filter=f'{constants.APPNAME} 工程 (*.prism project.json *.prismproj);;所有文件 (*)')
        if filename:
            filename = os.path.normpath(filename)
            self.open_from_file(filename)

    def on_action_open_readonly(self):
        """Open a project that must not be written back.

        Looking at a project should never rewrite it. The reader is a
        read-only connection, but the autosave writes through a different
        path minutes later -- so the licence has to be recorded on the scene
        and honoured by every writer, not just by the reader.
        """
        confirm = self.get_confirmation_unsaved_changes(
            _('There are unsaved changes. Are you sure you want to open a new scene?'))
        if not confirm:
            return
        self.cancel_active_modes()
        filename, f = QtWidgets.QFileDialog.getOpenFileName(
            parent=self,
            caption=_('Open file (read only)'),
            filter=f'{constants.APPNAME} File (*.prism)')
        if not filename:
            return
        filename = os.path.normpath(filename)
        self.open_from_file(filename, readonly=True)

    def on_saving_finished(self, filename, errors, silent=False, outcome=None):
        from prism.fileio.save_preparation import (
            ACK_CONFIRMED, ACK_FAILED, ACK_NOT_APPLICABLE, COMMIT_COMPLETE,
            DELIVERY_STALE_IGNORED, TERMINAL_CANCELLED, TERMINAL_CONFIRMED,
            TERMINAL_FAILED, TERMINAL_WRITTEN_UNCONFIRMED)
        started = getattr(self, '_trace_save_started', trace_time.perf_counter())
        trace_record('project_save_finished', filename=str(filename), failed=bool(errors), silent=silent,
                     duration_ms=round((trace_time.perf_counter()-started)*1000, 3))
        operation = getattr(self, '_save_operation', None)
        snapshot = getattr(self, '_save_board_snapshot', None)
        self._save_board_snapshot = None
        partial = getattr(self, '_save_partial_snapshot', None)
        self._save_partial_snapshot = None
        stale = False
        if snapshot is not None and self.scene.project_id != snapshot.state.project_id:
            trace_record('project_save_result_ignored', filename=str(filename), reason='project_changed')
            stale = True
        if partial is not None and partial.project_id != self.scene.project_id:
            trace_record('project_save_result_ignored', filename=str(filename),
                         reason='partial_project_changed')
            stale = True
        if stale:
            # The result belongs to a project the user has already left. It
            # must not touch the new project's dirty flag, counts or catalog.
            # It also must not pretend that nothing was written: the old file
            # may well have been saved, and the record has to say so.
            self._record_save_outcome(operation, errors, outcome,
                                      ack_state=ACK_NOT_APPLICABLE, stale=True)
            return
        if errors:
            final = self._record_save_outcome(operation, errors, outcome,
                                              ack_state=ACK_NOT_APPLICABLE)
            # The disk fact decides what the user is told. An error that
            # arrived after the commit does NOT mean that nothing was
            # written, and saying so would send the user looking for a lost
            # file that is in fact on disk.
            committed = final is not None and final.commit_state == COMMIT_COMPLETE
            cancelled = final is not None and final.terminal == TERMINAL_CANCELLED
            reason = ((final.reason if final is not None else '')
                      or (str(errors[0]) if errors else '')).strip()
            if silent:
                # An automatic save has to feed the retry policy, no matter
                # who started it.
                self._autosave_record(final, errors)
                # Autosave runs unattended; a modal warning every few
                # minutes would be worse than a log line. The failure is
                # still recorded and still announced by the autosave policy.
                logger.error('Autosave failed for %s: %s', filename, errors)
                return
            if cancelled:
                # A cancel is not a failure. It must not be dressed up as
                # "the file is not accessible", and it must not be a
                # warning the user has to dismiss.
                if hasattr(self.parent, 'statusBar'):
                    self.parent.statusBar().showMessage(
                        _('Save cancelled. Nothing was written.'), 6000)
                return
            if committed:
                # The bytes are on disk and the confirmation did not go
                # through. The user has to know that, and has to verify the
                # file before continuing.
                warning = _(
                    'The file was written but saving could not be confirmed. '
                    'Your edits remain unsaved; reopen the saved copy before continuing.')
                if hasattr(self.parent, 'statusBar'):
                    self.parent.statusBar().showMessage(warning, 12000)
                QtWidgets.QMessageBox.warning(
                    self, _('Problem saving file'), warning)
                return
            # The dialog has to name the real reason. "File/directory not
            # accessible" was true for one failure and wrong for all others.
            QtWidgets.QMessageBox.warning(
                self,
                _('Problem saving file'),
                _('<p>Problem saving file %s</p><p>%s</p>') % (filename, reason))
            return
        if snapshot is not None:
            snapshot.acknowledge(self.scene, filename)
        if partial is not None:
            if not self._acknowledge_partial_save(partial, filename):
                # The bytes are on disk but the session could not confirm
                # them. Staying silent here would let the user keep editing
                # and assume the next save is safe.
                self._content_dirty = True
                self.update_window_title()
                warning = _(
                    'The file was written but saving could not be confirmed. '
                    'Your edits remain unsaved; reopen the saved copy before continuing.')
                self._record_save_outcome(operation, errors, outcome,
                                          ack_state=ACK_FAILED,
                                          terminal=TERMINAL_WRITTEN_UNCONFIRMED,
                                          reason=warning, error=warning)
                if hasattr(self.parent, 'statusBar'):
                    self.parent.statusBar().showMessage(warning, 12000)
                if not silent:
                    QtWidgets.QMessageBox.warning(
                        self, _('Problem saving file'), warning)
                return
        self.filename = filename
        models = getattr(self.parent, 'model_composition', None)
        if models is not None and models._scope is not None and models._scope != models._key():
            models.invalidate()
            if self.parent.tabs.currentWidget() is self.parent.model_library:
                models.ensure()
        self._remember_last_canvas()
        for captured in getattr(getattr(self,'worker',None),'managed_captured_items',()):
            canonical=getattr(operation,'canonical',{}).get(id(captured))
            if canonical is None:continue
            canonical.save_id=captured.save_id
            old=getattr(captured,'_save_original_source',None)
            current=(getattr(canonical,'_source_blob',None),getattr(canonical,'_source_path',None),getattr(canonical,'_prism_source',None))
            if old is not None and current==old and captured.TYPE=='pixmap':
                canonical._prism_source=(str(filename),captured.save_id)
                canonical._source_path=None;canonical._source_blob=None
        managed_session=getattr(getattr(self,'worker',None),'managed_session',None)
        if managed_session is not None:
            managed_session.attach(self.scene)
            self._install_catalog_rebind_hook(managed_session)
        rebind=getattr(getattr(self,'worker',None),'managed_rebind',None)
        if rebind is not None:
            from prism.partial_residency import rebind_asset_sources
            rebind_asset_sources(list(self.scene.iter_assets()),*rebind)
        unchanged = (getattr(self, '_save_revision', getattr(self, '_content_revision', 0))
                     == getattr(self, '_content_revision', 0)
                     and getattr(self, '_save_undo_index', self.undo_stack.index())
                     == self.undo_stack.index()
                     and getattr(self, '_save_undo_revision', getattr(self, '_undo_revision', 0))
                     == getattr(self, '_undo_revision', 0)
                     and not getattr(self, '_save_editor_incomplete', False))
        if unchanged:
            self.undo_stack.setClean()
            self._content_dirty = False
        self.update_window_title()
        # The file on disk is a new source version now.  Re-point the
        # read-only catalog at it.  This runs only after the save was
        # CONFIRMED: the unconfirmed path above returned already, so a
        # failed save never switches the catalog version and never clears
        # the dirty flag.
        receipt = (getattr(getattr(self, 'worker', None), 'partial_save_receipt', None)
                   if partial is not None else None)
        if receipt is not None:
            self._refresh_catalog_after_save(filename, receipt=receipt)
        else:
            self._refresh_catalog_after_save(filename)
        self._subscribe_pending_canvas_catalog(filename)
        if unchanged and hasattr(self.parent, 'clear_recovery_snapshot'):
            self.parent.clear_recovery_snapshot()
        final = self._record_save_outcome(operation, errors, outcome,
                                          ack_state=ACK_CONFIRMED,
                                          terminal=TERMINAL_CONFIRMED)
        if silent:
            # An automatic save has to feed the retry policy, no matter who
            # started it.
            self._autosave_record(final, errors)
        if not silent and hasattr(self.parent, 'statusBar'):
            stamp = QtCore.QTime.currentTime().toString('HH:mm:ss')
            message = (f'已保存 · {stamp}' if unchanged else
                       f'已保存本次快照 · {stamp}；之后的新编辑尚未保存')
            self.parent.statusBar().showMessage(message, 6000)
            trace_record('project_save_feedback', phase='confirmed',
                         newer_edits_unsaved=not unchanged)
        if silent and hasattr(self.parent, 'statusBar'):
            self.parent.statusBar().showMessage(
                _('Saved automatically at {time}').format(
                    time=QtCore.QTime.currentTime().toString('HH:mm:ss')),
                4000)

    def _record_save_outcome(self, operation, errors, outcome, *, ack_state,
                             terminal=None, stale=False, reason='', error=''):
        """Build the final result of this operation and publish it.

        The result is the only description of what happened. The progress
        dialog reads it so that a failed or cancelled save never ends on a
        bar that reads "100%".
        """
        from prism.fileio.save_preparation import DELIVERY_STALE_IGNORED
        final = outcome
        if operation is not None:
            try:
                final = operation.build_outcome(
                    errors, ack_state=ack_state, terminal=terminal,
                    ui_delivery=(DELIVERY_STALE_IGNORED if stale else None),
                    reason=reason, error=error)
            except Exception:
                logger.exception('Could not assemble the save outcome')
        progress = getattr(self, 'progress', None)
        if progress is not None:
            from PyQt6 import sip
            if not sip.isdeleted(progress):
                progress.save_outcome = final
                if final is not None and not final.confirmed:
                    progress.setLabelText(final.reason or final.error or _('Save did not complete'))
        if final is not None:
            self.last_save_outcome = final
            from prism.fileio.save_preparation import COMMIT_COMPLETE
            library_project_boundary(self,'save_outcome',operation_id=final.operation_id,
                phase=final.terminal,durable=final.commit_state==COMMIT_COMPLETE,
                failed=not final.confirmed,reason='commit='+str(final.commit_state)+';ack='+str(final.ack_state))
            trace_record('project_save_outcome', operation_id=final.operation_id,
                         terminal=final.terminal, commit_state=final.commit_state,
                         ack_state=final.ack_state, ui_delivery=final.ui_delivery,
                         mode=final.mode, changed_entities_complete=final.changed_entities_complete,
                         cancel_too_late=final.cancel_too_late)
        return final

    def _refuse_save(self, filename, reason):
        """Record a save that was refused before it was ever accepted.

        A refusal is a result too. Without one, "nothing happened" would be
        a silence that nobody can query.
        """
        from prism.fileio.save_preparation import refused_outcome
        try:
            outcome = refused_outcome(self, filename, reason)
        except Exception:
            logger.exception('Could not record the refused save')
            return None
        self.last_save_outcome = outcome
        trace_record('project_save_outcome', operation_id=outcome.operation_id,
                     terminal=outcome.terminal, commit_state=outcome.commit_state,
                     ack_state=outcome.ack_state, ui_delivery=outcome.ui_delivery,
                     mode=outcome.mode, changed_entities_complete=False,
                     cancel_too_late=False, reason=reason)
        return outcome

    def _on_save_commit_started(self):
        """The writer entered the region that can no longer be rolled back."""
        progress = getattr(self, 'progress', None)
        if progress is not None:
            from PyQt6 import sip
            if not sip.isdeleted(progress):
                # Cancel is not offered any more: interrupting the commit is
                # not something this application is allowed to do.
                cancel = progress.findChild(QtWidgets.QPushButton)
                if cancel is not None:
                    cancel.setEnabled(False)
                progress.setLabelText('正在完成提交…')
        trace_record('project_save_commit_started')

    def _on_save_cancel_too_late(self):
        """A cancel arrived after the commit was no longer interruptible."""
        trace_record('project_save_cancel_too_late')
        if hasattr(self.parent, 'statusBar'):
            self.parent.statusBar().showMessage(
                _('The cancel arrived too late: the file was already committed.'), 8000)

    # -- automatic saving ----------------------------------------------------
    def autosave_tick(self):
        """One automatic-save attempt.

        The main window owns the timer and the interval; this method owns the
        decision, the retry policy and the announcement. The policy object
        lives here, so the failure counter, the backoff and the announcement
        cadence have a single owner.

        The main window polls revisions and delegates every attempt here.
        """
        from prism.fileio.save_preparation import AutosavePolicy
        policy = self.autosave_policy
        if not self.is_dirty():
            # Nothing to lose. Drop the backoff so the next edit is covered by
            # a normal-interval attempt instead of inheriting the last failure.
            policy.record_success()
            self._autosave_pending = None
            return 'nothing_to_save'
        if (getattr(self, '_save_preparing', False) or
                getattr(self, '_save_operation', None) is not None or
                getattr(self, '_storage_operation', None) is not None):
            return 'already_running'
        pending = getattr(self, '_autosave_pending', None)
        if pending is not None and self._worker_running(pending):
            # A previous attempt is still writing; queueing another one would
            # only add contention.
            return 'already_running'
        previous = getattr(self, 'worker', None)
        refused_before = getattr(self, '_paged_save_refused', None)
        target = self.filename
        if not target:
            try:
                target = self.parent._temporary_project_target()
            except (OSError, ValueError) as error:
                policy.record_failure(str(error))
                self._announce_autosave_failure()
                return 'refused'
        self.do_save(target, create_new=not bool(self.filename), silent=True)
        worker = getattr(self, 'worker', None)
        if worker is None or worker is previous:
            # do_save refused before it started a write: the partial baseline
            # is missing, the paged open failed, or the editor snapshot was
            # unavailable. A silent refusal still has to be discoverable.
            if self._worker_running(previous):
                return 'already_running'
            refused = getattr(self, '_paged_save_refused', None)
            reason = (refused[1] if refused and refused != refused_before
                      else _('saving was refused before any data was written'))
            policy.record_failure(reason)
            self._announce_autosave_failure()
            return 'refused'
        self._autosave_pending = worker
        self._autosave_operation_id = getattr(getattr(self, '_save_operation', None), 'operation_id', None)
        worker.finished.connect(self._autosave_finished)
        if hasattr(self.parent, 'statusBar'):
            self.parent.statusBar().showMessage('正在自动保存…')
        return 'started'

    def _autosave_finished(self, name, errors):
        """The automatic save that this view started has finished."""
        worker = getattr(self, '_autosave_pending', None)
        sender = self.sender()
        if (worker is None or worker is not getattr(self, 'worker', None)
                or (sender is not None and sender is not worker)):
            return  # A newer attempt already replaced this one.
        self._autosave_pending = None
        outcome = getattr(self, 'last_save_outcome', None)
        operation_id = getattr(self, '_autosave_operation_id', None)
        incomplete = getattr(self, '_save_editor_incomplete', False)
        if errors or incomplete or (operation_id is not None and
                      (outcome is None or outcome.operation_id != operation_id or
                       not outcome.confirmed or outcome.ui_delivery != 'current')):
            # The policy was already updated by the outcome path; announce
            # according to its cadence.
            if incomplete:
                self.autosave_policy.record_failure('脑图编辑器尚未响应，其余内容已提交，脑图仍未保存')
            elif not errors and not self.autosave_policy.failures:
                self.autosave_policy.record_failure('写入结果未能确认，修改仍未标记为已保存')
            self._announce_autosave_failure()
        else:
            recovered = self.autosave_policy.record_success()
            if recovered:
                self._autosave_recovered(recovered)
            if hasattr(self.parent, 'statusBar'):
                stamp = QtCore.QTime.currentTime().toString('HH:mm:ss')
                suffix = '；之后的新编辑尚未保存' if self.is_dirty() else ''
                self.parent.statusBar().showMessage('已自动保存 · ' + stamp + suffix, 6000)
        settled = getattr(self.parent, '_autosave_settled', None)
        if callable(settled):
            settled()

    def _autosave_record(self, outcome, errors):
        """Feed one save result into the automatic-save policy.

        Every save passes through here, no matter who started it, so the
        failure counter cannot drift away from reality.
        """
        policy = self.autosave_policy
        failed = bool(errors) or (outcome is not None and not outcome.confirmed)
        if failed:
            reason = ''
            if outcome is not None:
                reason = outcome.reason or outcome.error
            if not reason and errors:
                reason = str(errors[0])
            policy.record_failure(reason or _('the automatic save did not complete'))
        else:
            recovered = policy.record_success()
            if recovered:
                self._autosave_recovered(recovered)

    def _announce_autosave_failure(self):
        """Make a failing automatic save visible, without nagging.

        The announcement widens with the failure count, so a permanent
        failure stays visible without repeating itself on every attempt.
        """
        policy = self.autosave_policy
        if not policy.should_announce():
            return False
        if hasattr(self.parent, 'statusBar'):
            self.parent.statusBar().showMessage(
                _('Automatic saving has failed {count} time(s) ({reason}). '
                  'Your changes are still unsaved - save manually and check '
                  'that the project file is writable.').format(
                      count=policy.failures, reason=policy.last_error), 0)
        return True

    def _autosave_recovered(self, count):
        if hasattr(self.parent, 'statusBar'):
            self.parent.statusBar().showMessage(
                _('Automatic saving recovered after {count} failure(s)').format(
                    count=count), 6000)

    @staticmethod
    def _worker_running(worker):
        try:
            return worker is not None and worker.isRunning()
        except RuntimeError:
            return False  # The underlying C++ thread is already gone.

    def _autosave_project_changed(self):
        """Forget the automatic-save failures when the project changes."""
        self.autosave_policy.reset()
        self._autosave_pending = None

    def _capture_partial_save(self, filename, silent=False, *, prepare_only=False):
        """Snapshot a partially loaded project, or refuse and explain why.

        A partial save may only write records whose baseline it holds. When the
        page cannot prove a record, the save stops here -- before a worker is
        started -- instead of falling back to the full writer, which would treat
        every unloaded record as deleted.
        """
        if getattr(self, '_paged_open_cancelled', False):
            # Terminal as well: the canvas is empty because the user stopped the
            # open. Saving it would either crash on a missing filename or, if a
            # previous filename survived, write that empty scene over it.
            trace_record('project_save_refused', filename=str(filename),
                         reason='paged_open_cancelled', silent=silent)
            message = _('Saving is unavailable: opening this project was cancelled. '
                        'Open a project before saving.')
            if hasattr(self.parent, 'statusBar'):
                self.parent.statusBar().showMessage(message, 10000)
            return _PARTIAL_REFUSED
        session = getattr(self.scene, '_partial_project_session', None)
        if session is None:
            if getattr(self, '_paged_open_failed', False):
                # Terminal: preparation already failed and the canvas is empty.
                # Saving now would write that empty scene over a real project.
                reason = getattr(self, '_paged_open_failed_reason', '') or _('preparation failed')
                trace_record('project_save_refused', filename=str(filename),
                             reason='paged_open_failed', silent=silent)
                if self.filename is not None:
                    message = _('Saving is unavailable: browsing is available, but safe saving could not be prepared ({reason}).').format(reason=reason)
                else:
                    message = _('Saving is unavailable: this project could not be opened '
                                '({reason}). No project is currently open.').format(reason=reason)
                if silent:
                    logger.warning('Partial save refused after a failed open: %s', reason)
                self._catalog_status(message)
                if hasattr(self.parent, 'statusBar'):
                    self.parent.statusBar().showMessage(message, 10000)
                return _PARTIAL_REFUSED
            if (getattr(self.scene, '_partial_project_source', None)
                    or getattr(self, '_paged_preparing', False)):
                trace_record('project_save_refused', filename=str(filename),
                             reason='partial_baseline_not_ready', silent=silent)
                if not silent:
                    self._catalog_status(_('Safe saving is not ready yet; project preparation must finish first.'))
                return _PARTIAL_REFUSED
            return None
        try:
            if prepare_only:
                session.validate_capture(self.scene)
                return session
            return session.capture(self.scene)
        except Exception as error:
            # A silent caller (autosave) still needs this to be discoverable, so
            # it is always logged at warning level and always shown.
            logger.warning('Partial save refused for %s: %s', filename, error)
            trace_record('project_save_refused', filename=str(filename),
                         reason=str(error)[:200], silent=silent)
            self._paged_save_refused = (trace_time.time(), str(error))
            if hasattr(self.parent, 'statusBar'):
                self.parent.statusBar().showMessage(
                    _('This project is only partly open, so saving stopped: {reason}')
                    .format(reason=error), 10000)
            return _PARTIAL_REFUSED

    def _acknowledge_partial_save(self, snapshot, filename):
        """Retire only the revisions the worker actually wrote to disk.

        A partially loaded project had better admit that it cannot confirm a
        write than pretend everything it holds in memory is now saved.
        """
        session = getattr(self.scene, '_partial_project_session', None)
        receipt = getattr(getattr(self, 'worker', None), 'partial_save_receipt', None)
        if session is None or receipt is None:
            trace_record('project_partial_save_unacknowledged', filename=str(filename))
            if session is not None:
                session.invalid = ('The file was written without a save receipt. '
                                   'Reopen the saved copy before saving again.')
            return False
        try:
            session.acknowledge(snapshot, receipt, scene=self.scene, filename=str(filename))
            if session.invalid:
                raise RuntimeError(session.invalid)
        except Exception as error:
            logger.exception('Partial save was written but its receipt could not be applied')
            trace_record('project_partial_save_ack_failed', filename=str(filename))
            session.invalid = (
                f'The last save was written, but its receipt could not be applied ({error}). '
                f'Reopen the project before saving again so nothing is overwritten.'
            )
            return False
        trace_record('project_partial_save_acknowledged', filename=str(filename),
                     changed_rows=receipt.changed_rows, revision=receipt.revision)
        from prism.image_pyramid import acknowledge_archive_save
        try:
            acknowledge_archive_save(filename, receipt)
        except Exception:
            # The pyramid proof is a cache. The receipt was already applied to
            # the session and the bytes are on disk, so a failing cache must
            # not turn a confirmed save into a failure.
            logger.exception('The archive pyramid proof failed after a '
                             'confirmed save')
        return True

    def _show_save_phase(self, phase):
        labels = {'accepted': '正在准备保存…', 'preparing': '正在准备保存…',
                  'writing': '正在写入工程…', 'confirming': '正在确认保存结果…'}
        text = labels.get(phase, phase)
        if hasattr(self.parent, 'statusBar'):
            self.parent.statusBar().showMessage(text)
        progress = getattr(self, 'progress', None)
        if progress is not None:
            from PyQt6 import sip
            if not sip.isdeleted(progress):
                progress.setLabelText(text)
        trace_record('project_save_feedback', phase=phase,
                     feedback_set_ms=round((trace_time.perf_counter()
                                             - getattr(self, '_trace_save_started',
                                                       trace_time.perf_counter())) * 1000, 3))

    def do_save(self, filename, create_new, silent=False):
        models = getattr(getattr(self.parent, 'model_composition', None), 'service', None)
        if models is not None and models.pending_writes:
            self._catalog_status('模型原件正在写入，请等待完成后保存')
            return
        if getattr(self, '_upgrade_preview_readonly', False):
            self._catalog_status('只读预览期间不能保存；请等待转换及切换完成。')
            trace_record('project_save_refused', reason='upgrade_preview', silent=silent)
            return
        if getattr(self,'_storage_operation',None) is not None:
            self.parent.statusBar().showMessage('请等待工程存储操作完成',4000)
            return
        # A project that was opened read-only must not be written back, and
        # the refusal has to happen *here* -- before any worker starts.
        #
        # This is the second half of the 2026-10-22 accident. The reader was
        # always a read-only connection, so the file itself was safe while it
        # was being read, but the autosave wrote to the same path through a
        # separate writer 45 minutes later and produced a 59 MB rollback
        # journal next to a 51 GB fixture. `is_readonly_project` is the
        # licence the GUI grants or withholds; honour it before a single byte
        # is written.
        #
        # "Save as" onto a *different* file is not affected: the user is
        # explicitly choosing where the work goes.
        if not create_new and fileio.is_readonly_project(self.scene, filename):
            trace_record('project_save_refused', filename=str(filename),
                         reason='readonly_project', silent=silent)
            message = _('This project was opened read-only and was not written. '
                        'Use "Save as" to keep your changes.')
            logger.warning('Save refused: %s is a read-only project', filename)
            if not silent:
                self._catalog_status(message)
            if hasattr(self.parent, 'statusBar'):
                self.parent.statusBar().showMessage(message, 10000)
            self._paged_save_refused = (trace_time.time(), 'readonly_project')
            self._refuse_save(filename, 'readonly_project')
            return
        if (getattr(self, '_paged_preparing', False)
                or getattr(self, '_paged_open_failed', False)
                or getattr(self, '_paged_open_cancelled', False)
                or (getattr(self.scene, '_partial_project_source', None)
                    and getattr(self.scene, '_partial_project_session', None) is None)):
            self._capture_partial_save(filename, silent)
            return
        operation = getattr(self, '_opening_operation', None)
        if operation is not None and not operation.terminal:
            trace_record('project_save_refused', filename=str(filename),
                         reason='project_opening', phase=operation.phase, silent=silent)
            self._catalog_status(_('Safe saving is not ready yet; opening must finish first.'))
            self._refuse_save(filename, 'project_opening')
            return
        worker = getattr(self, 'worker', None)
        busy_worker = worker is not None and worker.isRunning()
        preparing = getattr(self, '_save_preparing', False)
        awaiting = (getattr(self, '_save_board_snapshot', None) is not None or
                    getattr(self, '_save_partial_snapshot', None) is not None)
        if busy_worker or preparing or awaiting:
            reason = ('worker_running' if busy_worker else 'preparing_save' if preparing
                      else 'awaiting_save_ack')
            trace_record('project_save_skipped', filename=str(filename), reason=reason, silent=silent)
            if not silent and hasattr(self.parent, 'statusBar'):
                self.parent.statusBar().showMessage(_('Saving is already in progress.'), 4000)
            self._refuse_save(filename, reason)
            return
        self._save_preparing = True
        self._save_editor_incomplete = False
        try:
            self._trace_save_started = trace_time.perf_counter()
            trace_record('project_save_start', filename=str(filename), create_new=create_new, silent=silent)
            library_project_boundary(self,'save_accepted',durable=False,phase='before_editor_flush')
            if not silent:
                # The request is accepted here, before any work starts. The
                # first visible feedback must not wait for the first row of
                # the file: the user asked to save and the interface has to
                # say so.
                self._show_save_phase('accepted')
            # Flush editors whose debounce timer may not have fired yet.
            tabs = getattr(self.parent, 'tabs', None)
            if tabs is not None:
                panel = tabs.currentWidget()
                store = getattr(self.parent, '_store_current_page', None)
                if callable(store):
                    stored = store(panel)
                elif hasattr(panel, 'store_to_scene'):
                    stored = panel.store_to_scene()
                else:
                    stored = True
                if stored is False:
                    if type(panel).__name__ == 'MindMapPanel':
                        self._save_editor_incomplete = True
                        # 决策-20261023 第 4 条：脑图页面没响应时跳过脑图、
                        # 其余内容照常保存。脑图内容平时随编辑持续回传，
                        # 丢的只是最后几秒；下次保存会重试。
                        trace_record('project_save_mindmap_skipped',
                                     filename=str(filename), silent=silent)
                        if not silent and hasattr(self.parent, 'statusBar'):
                            self.parent.statusBar().showMessage(
                                _('脑图页面未响应，本次未保存脑图，其余内容已保存。'), 8000)
                    else:
                        trace_record('project_save_skipped', filename=str(filename),
                                     reason='editor_snapshot_unavailable', silent=silent)
                        if not silent and hasattr(self.parent, 'statusBar'):
                            self.parent.statusBar().showMessage(
                                _('Editor content could not be read. Please retry saving.'), 6000)
                        return
            if not fileio.is_prism_file(filename):
                filename = f'{filename}.prism'
            self._save_revision = getattr(self, '_content_revision', 0)
            self._save_undo_index = self.undo_stack.index()
            self._save_undo_revision = getattr(self, '_undo_revision', 0)
            session = self._capture_partial_save(filename, silent, prepare_only=True)
            if session is _PARTIAL_REFUSED:
                return
            from prism.fileio.save_preparation import SavePreparation, SaveWorker
            preparation = SavePreparation(self, filename, create_new, silent, session)
            is_recovery = getattr(self.parent, '_is_recovery_project', lambda _: False)
            if is_recovery(filename):
                preparation.recovery_checkpoint = (self.parent._recovery_store,
                    dict(project_id=self.scene.project_id, filename=str(filename), full_project=True),
                    self.parent._recovery_session)
            self._save_operation = preparation
            # Autosave observes this worker immediately after do_save returns.
            # The same identity owns capture, preparation, writing and receipt.
            # The save thread carries two save-specific signals on top of the
            # generic ThreadedIO contract; opening and exporting are untouched.
            self.worker = SaveWorker(preparation.run)
            preparation.worker = self.worker
            self.worker.finished.connect(preparation.finish)
            # ThreadedIO.finished reports the receipt before run() returns.
            # Reuse the native QThread end to release private captures safely.
            stopped = QtCore.QThread.finished.__get__(self.worker, type(self.worker))
            stopped.connect(preparation._thread_finished)
            if not silent:
                self.progress = widgets.PrismProgressDialog(
                    f'Saving {filename}', worker=self.worker, parent=self)
                # Mark it as a save, so that a save which ends without a
                # result cannot end on a bar that reads "100%".
                self.progress.is_save = True
                self.progress.setLabelText('正在准备保存…')
                self.progress.show()
                # Once the writer enters the commit region the transaction can
                # no longer be rolled back, so Cancel must not be offered any
                # more. A cancel that still arrives is reported as "too late"
                # rather than pretending the save was undone.
                self.worker.commit_started.connect(self._on_save_commit_started)
                self.worker.cancel_too_late.connect(self._on_save_cancel_too_late)
            self.worker.start()
            preparation.start_capture()
        finally:
            if getattr(self, '_save_operation', None) is None:
                self._save_preparing = False

    def on_action_save_as(self):
        self.cancel_active_modes()
        destination = self._choose_project_destination('项目另存为：选择位置和项目名称')
        if destination is not None:
            self.do_save(destination, create_new=True)

    def _choose_project_destination(self, caption, *, suggested_name=None):
        directory = os.path.dirname(self.filename) if self.filename else None
        from prism.storage.managed import root_for
        from prism.storage.entrypoint import save_as_entry
        folder = root_for(self.filename) if self.filename else None
        if folder is not None:
            # User-facing destinations belong beside the project, never
            # inside its metadata/object storage directory.
            directory = str(folder.parent)
        location = QtWidgets.QFileDialog.getExistingDirectory(self, caption, directory or '')
        if not location:
            return None
        if folder is not None and Path(location).resolve().is_relative_to(folder.resolve()):
            QtWidgets.QMessageBox.warning(self, '项目位置无效',
                '请选择当前项目文件夹以外的位置，避免把新项目嵌入当前项目的数据目录。')
            return None
        default_name = '新建项目'
        if folder is not None:
            default_name = folder.name.removesuffix('.prism.data') + '-副本'
        if suggested_name is not None:
            default_name = suggested_name
        name, accepted = QtWidgets.QInputDialog.getText(self, '项目名称',
            '项目名称（将在所选位置创建独立项目文件夹）', text=default_name)
        if accepted and name.strip():
            name = name.strip()
            if name in ('.','..') or any(char in name for char in '<>:"/\\|?*') or name.endswith(('.', ' ')):
                QtWidgets.QMessageBox.warning(self, '项目名称无效', '请输入有效的项目名称，不要包含路径或特殊字符。')
                return None
            entry = save_as_entry(os.path.join(location, name))
            if entry.exists() or entry.with_name(entry.name+'.data').exists():
                QtWidgets.QMessageBox.warning(self, '无法另存为',
                    '该名称已有工程文件或数据目录，请选择新名称；现有工程不会被覆盖。')
                return
            return str(entry)+'.data'
        return None

    def on_action_open_directory_project(self):
        folder=QtWidgets.QFileDialog.getExistingDirectory(self,'打开 Prism 目录工程')
        if folder:
            from prism.actions.storage_workflow import _open_result, run
            if str(folder).lower().endswith('.prism.data'):
                from prism.storage.entrypoint import ensure_entry
                run(self, '打开工程入口', lambda cancel: ensure_entry(folder),
                    lambda entry: _open_result(self, entry))
            else:
                _open_result(self,folder)

    def on_action_import_eagle(self):
        from prism.actions.eagle_workflow import import_eagle
        import_eagle(self)

    def on_action_convert_project(self):
        from prism.actions.storage_workflow import convert_project
        convert_project(self)

    def on_action_export_legacy_project(self):
        from prism.actions.storage_workflow import export_legacy_project
        export_legacy_project(self)

    def on_action_export_project_bundle(self):
        from prism.actions.storage_workflow import export_bundle
        export_bundle(self)

    def on_action_open_project_bundle(self):
        from prism.actions.storage_workflow import open_bundle
        open_bundle(self)

    def on_action_compact_project(self):
        from prism.actions.storage_workflow import compact_project
        compact_project(self)

    def on_action_recover_directory_project(self):
        from prism.actions.storage_workflow import recover_project
        recover_project(self)

    def on_action_save(self):
        self.cancel_active_modes()
        is_recovery = getattr(self.parent, '_is_recovery_project', lambda _: False)
        if not self.filename or is_recovery(self.filename):
            self.on_action_save_as()
        else:
            self.do_save(self.filename, create_new=False)

    def on_action_export_scene(self):
        from prism.actions.export_workflow import export_scene
        export_scene(self)

    def on_export_finished(self, filename, errors):
        if errors:
            err_msg = '</br>'.join(str(errors))
            QtWidgets.QMessageBox.warning(
                self,
                _('Problem writing file'),
                _('<p>Problem writing file %s</p><p>%s</p>') % (filename, err_msg))

    def _current_panel_of(self, class_name):
        """Return the active tab when it is the requested panel class."""
        tabs = getattr(self.parent, 'tabs', None)
        panel = tabs.currentWidget() if tabs is not None else None
        if panel is None or panel.__class__.__name__ != class_name:
            return None
        return panel

    def _import_target_panel(self, class_name, kind):
        """The panel an import should land in, creating a page if needed.

        Importing used to require the right page to be open already, so the
        menu item just showed a notice and did nothing.  Now it reuses the
        page already in front, then any existing page of that kind, and
        finally makes a new one.
        """
        panel = self._current_panel_of(class_name)
        if panel is not None:
            return panel
        window = self.parent
        if not hasattr(window, 'find_workspace_panel'):
            return None
        if (getattr(window, '_page_store_in_progress', False)
                and not getattr(window, '_navigation_is_committed', lambda: False)()):
            return None
        panel = window.find_workspace_panel(kind)
        if panel is not None:
            window.open_workspace_page(kind, getattr(panel, 'page_id', None))
        else:
            panel = window.create_workspace_page_quietly(kind)
        # A renderer barrier may still be pending (or may have failed). Never
        # hand an importer a destination that has not actually become active.
        if self._current_panel_of(class_name) is not panel:
            return None
        return panel

    def _store_imported_attachment(self, data, extension):
        """Keep an image read out of an imported file, return its URL."""
        from prism.asset_import_service import store_attachment
        from prism.widgets.document_panel import attachment_url
        attachment_id = store_attachment(
            self.scene.attachments, data, extension)
        return attachment_url(attachment_id)

    def _import_directory(self):
        return os.path.dirname(self.filename) if self.filename else None

    def on_action_import_document(self):
        from prism.navigation import defer_navigation
        if defer_navigation(self.window(), self.on_action_import_document, owner=self):
            return
        from prism.actions.import_workflow import import_document
        import_document(self)

    def on_action_import_mindmap(self):
        """Load an existing XMind file into the open mind map page."""
        if not self._allow_import_during_open():
            return
        from prism.navigation import defer_navigation
        if defer_navigation(self.window(), self.on_action_import_mindmap, owner=self):
            return
        from prism.fileio.importers import ImportFileError, xmind_to_tree

        filename, _selected = QtWidgets.QFileDialog.getOpenFileName(
            parent=self, caption=_('Import XMind Mind Map'),
            directory=self._import_directory(),
            filter=_('XMind Mind Map (*.xmind)'))
        if not filename or not self._allow_import_during_open():
            return
        # Only create a page once a file has actually been chosen.
        panel = self._import_target_panel('MindMapPanel', 'mindmap')
        if panel is None:
            return
        try:
            tree = xmind_to_tree(filename)
        except ImportFileError as error:
            widgets.PrismNotification(self, str(error))
            return
        panel.set_tree(tree)
        self.mark_content_dirty()
        if hasattr(self.parent, 'statusBar'):
            self.parent.statusBar().showMessage(
                _('Imported {name}').format(
                    name=os.path.basename(filename)), 4000)

    def on_action_export_note_word(self):
        from prism.actions.export_workflow import export_document
        export_document(self)

    def on_action_export_mindmap(self):
        from prism.actions.export_workflow import export_mindmap_action
        export_mindmap_action(self)

    def _ask_export_mode(self):
        """Ask whether to export the previews or the stored originals.

        Returns ``'adjusted'``, ``'original'``, or ``None`` if cancelled.

        Kept as its own method on purpose: the dialog is modal, and a
        headless test run has nobody to click it.  The suite answers this
        method instead of the dialog (see conftest's
        ``no_blocking_message_boxes``), and individual tests can override
        it to exercise either branch without a user.
        """
        choice = QtWidgets.QMessageBox(self)
        choice.setWindowTitle(_('Export Images'))
        choice.setText(_('Choose what to export. The preview adjustments never '
                'overwrite the original files.'))
        adjusted_button = choice.addButton(
            _('Export with preview adjustments'), QtWidgets.QMessageBox.ButtonRole.AcceptRole)
        original_button = choice.addButton(
            _('Export the originals'), QtWidgets.QMessageBox.ButtonRole.AcceptRole)
        choice.addButton(QtWidgets.QMessageBox.StandardButton.Cancel)
        choice.exec()
        clicked = choice.clickedButton()
        if clicked is adjusted_button:
            return 'adjusted'
        if clicked is original_button:
            return 'original'
        return None

    def on_action_export_images(self, selected_only=False):
        from prism.actions.export_workflow import export_images
        export_images(self, selected_only)

    def on_action_export_selected_images(self):
        from prism.actions.export_workflow import export_images
        export_images(self, selected_only=True)

    def on_action_ai_tag_selected(self):
        """对选中的图片进行 AI 打标签"""
        logger.info("=" * 60)
        logger.info("AI 打标签功能启动")
        
        try:
            from prism import wd14_tagger, fileio
            from prism.widgets import PrismProgressDialog
            logger.debug("模块导入成功")
        except Exception as e:
            logger.error(f"模块导入失败: {e}", exc_info=True)
            QtWidgets.QMessageBox.critical(self, "错误", f"模块导入失败：{e}")
            return
        
        # 获取选中的图片素材
        selected = self.scene.selectedItems(user_only=True)
        logger.info(f"选中素材总数: {len(selected)}")
        
        if not selected:
            QtWidgets.QMessageBox.information(
                self, "提示", "请先选中要打标签的图片。")
            return
        
        # 过滤出有 filename 的素材（即图片）
        image_items = [item for item in selected
                       if getattr(item, 'TYPE', None) == 'pixmap']
        logger.info(f"图片素材数量: {len(image_items)}")
        
        if not image_items:
            QtWidgets.QMessageBox.information(
                self, "提示", "选中的素材中没有图片。")
            return
        
        # 检查模型是否可用
        logger.info("检查 AI 模型可用性...")
        try:
            model_available = wd14_tagger.is_model_available()
            logger.info(f"模型可用性: {model_available}")
        except Exception as e:
            logger.error(f"检查模型可用性时出错: {e}", exc_info=True)
            model_available = False
        
        if not model_available:
            logger.error("AI 模型不可用：内置模型文件缺失")
            QtWidgets.QMessageBox.critical(
                self, "AI 模型缺失",
                "AI 打标签功能需要模型文件，但模型文件缺失。\n"
                "请检查 prism/assets/ai/ 目录下是否有 model.onnx 和 selected_tags.csv 文件。")
            return
        
        # 模型已存在，直接开始打标签
        self._start_ai_tagging(image_items)
    
    def _on_model_download_finished(self, success):
        """模型下载完成回调"""
        if not success:
            QtWidgets.QMessageBox.critical(
                self, "下载失败",
                "模型下载失败，请检查网络连接或手动下载。")
            return
        
        # 下载成功，获取选中的图片并开始打标签
        selected = self.scene.selectedItems(user_only=True)
        image_items = [item for item in selected
                       if getattr(item, 'TYPE', None) == 'pixmap']
        if image_items:
            self._start_ai_tagging(image_items)
    
    def _start_ai_tagging(self, image_items):
        from prism.actions.ai_tag_workflow import start_tagging
        start_tagging(self, image_items)

    def _on_ai_tag_finished(self, results):
        from prism.actions.ai_tag_workflow import apply_results
        apply_results(self, results)

    def on_export_images_file_exists(self, filename):
        dlg = widgets.ExportImagesFileExistsDialog(self, filename)
        if dlg.exec() == QtWidgets.QDialog.DialogCode.Accepted:
            self.exporter.handle_existing = dlg.get_answer()
            directory = self.exporter.dirname
            self.progress = widgets.PrismProgressDialog(
                f'Exporting to {directory}',
                worker=self.worker,
                parent=self)
            self.worker.start()

    def on_action_quit(self):
        self.parent.close()

    def on_action_settings(self):
        widgets.settings.SettingsDialog(self)

    def on_action_keyboard_settings(self):
        widgets.controls.ControlsDialog(self)

    def on_action_help(self):
        widgets.HelpDialog(self)

    def on_action_about(self):
        QtWidgets.QMessageBox.about(
            self,
            _('About {APPNAME}').format(APPNAME=constants.APPNAME),
            (f'<h2>{constants.APPNAME} {constants.VERSION}</h2>'
             f'<p>{constants.APPNAME_FULL}</p>'
             f'<p>{constants.COPYRIGHT}</p>'
             f'<p>{constants.COPYRIGHT_PROJECT}</p>'
             f'<p><a href="{constants.WEBSITE}">'
             f'Visit the {constants.APPNAME} website</a></p>'))

    def on_action_debuglog(self):
        widgets.DebugLogDialog(self)

    def _arrange_imported_items(self, items, *, optimal=False):
        """Pack each imported batch and keep it clear of existing content."""
        if not items:
            return
        imported = set(items)
        existing = {}
        for item in self.scene.items_for_save():
            if item not in imported:
                existing.setdefault(item._canvas_id, []).append(item)
        by_canvas = {}
        for item in items:
            by_canvas.setdefault(item._canvas_id, []).append(item)
        for canvas_id, group in by_canvas.items():
            if len(group) > 200 and not optimal:
                # Keep very large imports responsive using the linear grid.
                self.scene.arrange_square(items=group)
            elif len(group) >= 2:
                # Import placement is always compact, independently of the
                # user's preferred manual arrangement command.
                self.scene.arrange_optimal(items=group)
            occupied = existing.get(canvas_id)
            if occupied:
                bounds = self.scene.itemsBoundingRect(items=group)
                obstacles = self.scene.itemsBoundingRect(items=occupied)
                gap = max(16, self.settings.valueOrDefault('Items/arrange_gap'))
                if bounds.adjusted(-gap, -gap, gap, gap).intersects(obstacles):
                    delta = QtCore.QPointF(obstacles.right() + gap - bounds.left(), 0)
                    self.undo_stack.push(commands.MoveItemsBy(group, delta))

    def on_insert_images_finished(self, new_scene, filename, errors):
        self._load_scheduler.finish(
            lambda: self._finish_insert_images(new_scene, filename, errors))

    def _finish_insert_images(self, new_scene, filename, errors):
        """Callback for when loading of images is finished.

        :param new_scene: True if the scene was empty before, else False
        :param filename: Not used, for compatibility only
        :param errors: List of filenames that couldn't be loaded
        """

        logger.debug('Insert images finished')
        if errors:
            from html import escape
            errornames = [
                f'<li>{escape(str(fn))}</li>' for fn in errors]
            errornames = '<ul>%s</ul>' % '\n'.join(errornames)
            num = len(errors)
            msg = _('%s image(s) could not be opened.<br/>') % num
            # Finish the import callback and progress lifecycle even when a
            # file failed. A blocking warning here trapped gui_complete inside
            # a nested loop, sometimes behind a capture/reference window.
            dialog = QtWidgets.QMessageBox(self.window())
            dialog.setWindowTitle(_('Problem loading images'))
            dialog.setIcon(QtWidgets.QMessageBox.Icon.Warning)
            dialog.setText(msg + IMG_LOADING_ERROR_MSG + errornames)
            dialog.setStandardButtons(QtWidgets.QMessageBox.StandardButton.Ok)
            dialog.setWindowModality(Qt.WindowModality.NonModal)
            dialog.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
            dialogs = getattr(self, '_import_error_dialogs', None)
            if dialogs is None:
                dialogs = self._import_error_dialogs = set()
            dialogs.add(dialog)
            dialog.finished.connect(lambda _result, current=dialog: dialogs.discard(current))
            dialog.show()
            dialog.raise_()
            dialog.activateWindow()

        # Select new items only if count is manageable (avoid MultiSelectItem crash)
        existing_ids = getattr(self, '_existing_item_ids', set())
        new_items = [i for i in self.scene.items()
                     if hasattr(i, 'save_id') and id(i) not in existing_ids]
        for item in new_items:
            item._canvas_id = self.current_canvas_id
        inherited = getattr(self, '_pending_inherited_category', None)
        if inherited:
            for item in new_items:
                if not getattr(item, '_categories', None):
                    item._categories = [inherited]
            self.scene.metadata_changed.emit()
        self._pending_inherited_category = None
        if self._active_capture_metadata and new_items:
            title, tags = self._active_capture_metadata
            for item in new_items:
                if title:
                    item._title = title
                if tags:
                    item._tags = sorted(set(item._tags).union(tags))
            self.scene.metadata_changed.emit()
            widgets.PrismNotification(
                self, _('Image captured from browser'))
        self._active_capture_metadata = None
        if len(new_items) <= 200:
            blocked = self.scene.blockSignals(True)
            try:
                for item in new_items:
                    item.setSelected(True)
            finally:
                self.scene.blockSignals(blocked)
            if not blocked:
                self.scene.selectionChanged.emit()

        # 拖进来的文件夹结构 → 画布结构。
        # 放在分类那段之前：那一段用完 `_existing_item_ids` 就把它清空了，
        # 而这里也要用它认出"这次新插进来的素材"。
        folder_trees = getattr(self, '_pending_folder_trees', None)
        if folder_trees:
            existing_ids = getattr(self, '_existing_item_ids', set())
            inserted = [i for i in self.scene.items()
                        if hasattr(i, 'save_id') and id(i) not in existing_ids]
            self._create_canvases_for_folders(folder_trees, inserted)
            self._pending_folder_trees = None

        # Apply per-folder categories from folder drop
        folder_cats = getattr(self, '_pending_folder_categories', None)
        if folder_cats:
            existing_ids = getattr(self, '_existing_item_ids', set())
            new_items = [i for i in self.scene.items()
                         if hasattr(i, 'save_id') and id(i) not in existing_ids]
            # Ensure all category names exist in scene catalogue
            for cat_name in folder_cats.values():
                if cat_name not in self.scene.category_names:
                    self.scene.category_names.append(cat_name)
            # Assign categories based on each item's filename path
            for item in new_items:
                fn = getattr(item, 'filename', '') or ''
                fn_norm = os.path.normpath(fn)
                for folder_path, cat_name in folder_cats.items():
                    if fn_norm.startswith(folder_path + os.sep) or fn_norm.startswith(folder_path):
                        item._categories = [cat_name]
                        break
            self.scene.metadata_changed.emit()
            self.category_panel.update_counts()
            self._pending_folder_categories = None
        # 只排这次新插入的素材，不排全场（避免大图库卡死）
        existing_ids = getattr(self, '_existing_item_ids', set())
        newly_inserted = [i for i in self.scene.items()
                          if hasattr(i, 'save_id') and id(i) not in existing_ids]
        self._arrange_imported_items(newly_inserted)
        self._existing_item_ids = set()
        self.undo_stack.endMacro()
        if new_scene:
            self.on_action_fit_scene()
        self._import_in_progress = False
        self.viewport().update()
        QtCore.QTimer.singleShot(0, self._process_browser_capture_queue)

    def enqueue_browser_capture(self, url, title='', tags=None):
        """Queue an image received from the loopback browser bridge."""
        self._browser_capture_queue.append((url, title, list(tags or [])))
        self._process_browser_capture_queue()

    def _process_browser_capture_queue(self):
        if self._active_capture_metadata is not None:
            return
        operation = getattr(self, '_opening_operation', None)
        if operation is not None and not operation.terminal:
            QtCore.QTimer.singleShot(200, self._process_browser_capture_queue)
            return
        worker = getattr(self, 'worker', None)
        if worker is not None and worker.isRunning():
            QtCore.QTimer.singleShot(200, self._process_browser_capture_queue)
            return
        if not self._browser_capture_queue:
            return
        url, title, tags = self._browser_capture_queue.pop(0)
        self._active_capture_metadata = (title, tags)
        self.do_insert_images([QtCore.QUrl(url)])

    def _create_canvases_for_folders(self, folder_trees, items):
        """把一个拖进来的文件夹结构变成画布结构，并把素材分过去。

        规则（用户定的）：**一层文件夹一个画布**。

        * 文件夹里直接躺着图片视频 —— 那就只建这一个画布；
        * 套了子文件夹 —— 每层各建一个，父子关系跟着目录层级走；
        * 中间那层只装子文件夹、自己没有媒体的，不建空画布（子画布挂到
          最近的、真正建出来的祖先上）；
        * 同名画布由 ``WorkspaceService.next_title`` 自动加序号。

        建画布走的是现成的服务，不碰它的实现。
        """
        window = getattr(self, 'parent', None)
        service = getattr(window, 'workspace_service', None)
        if service is None:
            logger.info('没有工作区服务，跳过按文件夹建画布')
            return {}

        # 浅的目录先建 —— 子画布要能找到父画布
        canvases = {}
        first_page_id = None
        for path in sorted(folder_trees, key=lambda p: p.count(os.sep)):
            parent_path = os.path.dirname(path)
            while parent_path and parent_path not in canvases:
                shallower = os.path.dirname(parent_path)
                if shallower == parent_path:
                    parent_path = None
                    break
                parent_path = shallower
            page = service.add(
                'canvas',
                service.next_title('canvas', folder_trees[path]),
                parent=canvases.get(parent_path))
            canvases[path] = page['id']
            if first_page_id is None:
                first_page_id = page['id']

        # 素材归到它所在的那个目录的画布。走 `ChangeMetadata` 而不是直接
        # 赋值：归属变化要能撤销（左栏把素材拖到画布行走的是同一条路），
        # 而且它会发 `metadata_changed`，筛选那边才跟着重算。
        moves = {}
        for item in items:
            filename = getattr(item, 'filename', None)
            if not filename:
                continue
            folder = os.path.dirname(os.path.normpath(str(filename)))
            while folder and folder not in canvases:
                shallower = os.path.dirname(folder)
                if shallower == folder:
                    folder = None
                    break
                folder = shallower
            if folder:
                moves.setdefault(canvases[folder], []).append(item)
        for page_id, group in moves.items():
            self.undo_stack.push(commands.ChangeMetadata(
                group, 'canvas_id', [page_id for _ in group]))

        self.mark_content_dirty()
        # 让左侧资源树跟上：它**不会**自己刷新 —— 只有打开工程和回收站
        # 操作会调 `rebuild_workspace_tree`，新建页面走的 `_on_workspace_changed`
        # 只管页签。不叫这一下，画布是建出来了，但用户在看板上哪儿也找不到。
        # （`category_panel` 挂在 view 上，不在主窗口上。）
        library = getattr(self, 'category_panel', None)
        rebuild = getattr(library, 'rebuild_workspace_tree', None)
        if callable(rebuild):
            rebuild()
        if first_page_id:
            opener = getattr(window, 'open_workspace_page', None)
            if callable(opener):
                opener('canvas', first_page_id)
        logger.info('按文件夹建了 %d 个画布：%s', len(canvases),
                    list(canvases.values())[:5])
        return canvases

    def _ensure_current_canvas(self):
        """A deleted last canvas stays empty until the next insertion."""
        if self.current_canvas_id is None:
            self.window().create_workspace_page_quietly('canvas', _('Canvas'))
        return self.current_canvas_id

    def _allow_import_during_open(self):
        if getattr(self, '_upgrade_preview_readonly', False):
            self._catalog_status('只读预览期间不能导入或编辑；截图仍保留在收件箱。')
            return False
        operation = getattr(self, '_opening_operation', None)
        if operation is None or operation.terminal:
            return True
        self._catalog_status(_('Opening is still preparing safe saving; please retry the import when it finishes.'))
        return False

    def do_insert_images(self, filenames, pos=None, folder_categories=None,
                         folder_trees=None, force_video_embed=False):
        if not self._allow_import_during_open():
            return False
        if not filenames:
            return
        if self.current_canvas_id is None:
            from prism.navigation import defer_navigation
            if defer_navigation(self.window(), lambda: self.do_insert_images(
                    filenames, pos=pos, folder_categories=folder_categories,
                    folder_trees=folder_trees, force_video_embed=force_video_embed), owner=self):
                return
        self._ensure_current_canvas()
        if self.current_canvas_id is None:
            return
        # Centralise the extension policy before handing work to the legacy
        # loader.  Keeping unknown entries out avoids starting a worker that
        # cannot possibly produce an item, while preserving the user's order
        # within each supported type.
        from prism.asset_import_service import OTHER, classify, plan
        import_plan = plan(filenames)
        # Keep the exact drag/file-picker order.  ImportReport groups entries
        # for presentation, but feeding those groups to the loader would move
        # every video behind every image.
        filenames = [path for path in filenames if classify(path) != OTHER]
        skipped_paths = [path for path, _reason in import_plan.skipped]
        if not filenames:
            widgets.PrismNotification(self, import_plan.summary())
            return
        if import_plan.skipped:
            logger.info('Import skipped unsupported files: %s',
                        [path for path, _reason in import_plan.skipped])
        # Do not clear the category filter here.  Dropping it made every
        # item on the board reappear at once, which read as the canvas
        # jumping to a different view.  New items inherit the category that
        # is currently selected instead, so they are visible right away
        # without disturbing anything else.
        active = getattr(self.category_panel, '_active_filter', None)
        self._pending_inherited_category = (
            active[1] if active and active[0] == 'category' else None)
        if not pos:
            pos = self.get_view_center()
        self.scene.deselect_all_items()
        # Track existing items so we can assign category to new ones later
        self._existing_item_ids = set(id(i) for i in self.scene.items() if hasattr(i, 'save_id'))
        self._pending_folder_categories = folder_categories
        #: 拖进来的文件夹结构（有媒体的目录 -> 目录名）。插入完成后按它建画布。
        self._pending_folder_trees = folder_trees
        self.undo_stack.beginMacro('Insert Images')
        self.worker = fileio.ThreadedIO(
            fileio.load_media_with_originals if force_video_embed else fileio.load_media,
            filenames,
            self.mapToScene(pos),
            self.scene)
        self._import_in_progress = True
        self.worker.progress.connect(self.on_items_loaded)
        new_scene = not self.scene.items()
        self.worker.finished.connect(
            lambda filename, errors: self.on_insert_images_finished(
                new_scene, filename, list(errors) + skipped_paths))
        self.progress = widgets.PrismProgressDialog(
            'Loading media',
            worker=self.worker,
            parent=self)
        self.worker.start()

    def on_action_video_download(self):
        from prism.widgets.video_download import VideoDownloadDialog
        dialog = getattr(self, '_video_download_dialog', None)
        if dialog is None:
            dialog = self._video_download_dialog = VideoDownloadDialog(self)
        dialog.show()
        dialog.raise_()
        dialog.activateWindow()

    def on_action_screen_capture(self):
        from prism.actions.capture_workflow import capture
        capture(self)

    def on_action_pin_reference(self):
        from prism.actions.capture_workflow import pin
        pin(self)

    def on_action_capture_inbox(self):
        from prism.actions.capture_workflow import inbox
        inbox(self)

    def on_action_visual_search(self):
        canvas = self._material_canvas()
        selected = [canvas.model().items[index.row()] for index in canvas.selectedIndexes()] if canvas else self.scene.selectedItems(user_only=True)
        images = [item for item in selected if getattr(item, 'TYPE', '') == 'pixmap']
        controller = self.window().search_controller.visual_search
        if len(images) == 1:
            controller.start(images[0])
        else:
            controller.choose()

    def on_action_insert_images(self):
        self.cancel_active_modes()
        formats = self.get_supported_image_formats(QtGui.QImageReader)
        logger.debug(f'Supported image types for reading: {formats}')
        filenames, f = QtWidgets.QFileDialog.getOpenFileNames(
            parent=self,
            caption=_('Insert images and videos'),
            filter=_('Images and videos ({formats} *.mp4 *.mov *.mkv *.avi *.webm *.m4v);;All files (*)').format(formats=formats))
        self.do_insert_images(filenames)

    def on_action_clipboard_inbox(self):
        """Show the clipboard inbox, starting the watcher on first use."""
        from prism.widgets.clipboard_inbox import ClipboardInboxDialog

        if not self.clipboard_adapter.enabled:
            self.clipboard_adapter.set_enabled(True)
        self.clipboard_adapter.start()

        if self._clipboard_inbox_dialog is None:
            dialog = ClipboardInboxDialog(
                self.clipboard_inbox, parent=self.window(),
                pages_provider=lambda: self.workspace_service.pages)
            dialog.import_allowed = self._allow_import_during_open
            dialog.import_requested.connect(self._on_inbox_import)
            self._clipboard_inbox_dialog = dialog
        # 页面可能是在对话框上次关闭之后才建的，每次显示前刷新一遍。
        self._clipboard_inbox_dialog.refresh_pages()
        self._clipboard_inbox_dialog.show()
        self._clipboard_inbox_dialog.raise_()
        self._clipboard_inbox_dialog.activateWindow()

    def _on_inbox_import(self, items, page_id=None):
        """Put the chosen clipboard captures on the chosen page.

        ``page_id`` of None means the canvas the user is looking at, which
        is what the inbox did before it grew a target picker.

        The original bytes travel with them, so a capture can still be
        exported byte for byte later.
        """
        from prism.items import PrismPixmapItem

        if not self._allow_import_during_open():
            return False
        if not items:
            return
        target_canvas = page_id or self._ensure_current_canvas()
        active = getattr(self.category_panel, '_active_filter', None)
        inherited = active[1] if active and active[0] == 'category' else None

        created = []
        for index, entry in enumerate(items):
            item = PrismPixmapItem(QtGui.QImage())
            try:
                item.set_source_blob(entry.data, entry.suffix)
            except Exception:                       # noqa: BLE001
                logger.exception('Could not import a clipboard capture')
                continue
            if item.pixmap().isNull():
                continue
            item._canvas_id = target_canvas
            if inherited:
                item._categories = [inherited]
            if getattr(entry, 'tags', None):
                item._tags = sorted(set(item._tags).union(entry.tags))
            item._title = _('Clipboard {n}').format(n=index + 1)
            created.append(item)

        if not created:
            widgets.PrismNotification(
                self, _('No usable images in the selection.'))
            return

        self.scene.deselect_all_items()
        origin = self.mapToScene(self.get_view_center())
        self.undo_stack.beginMacro(_('Import clipboard images'))
        try:
            self.undo_stack.push(
                commands.InsertItems(self.scene, created, origin))
            self._arrange_imported_items(created)
        finally:
            self.undo_stack.endMacro()
        for item in created:
            item.setSelected(True)
        self.scene.metadata_changed.emit()
        self.category_panel.update_counts()
        widgets.PrismNotification(
            self, _('Added {n} image(s) from the clipboard inbox.').format(
                n=len(created)))

    def _insert_position(self):
        """Where a newly inserted item should land.

        Uses the last position the pointer held over the canvas.  Asking for
        the cursor position at insert time would be wrong whenever the
        insert comes from the menu bar, because then the cursor sits over
        the menu rather than over the spot the user was working on.
        """
        spot = getattr(self, '_last_canvas_pos', None)
        if spot is not None:
            return self.mapToScene(spot)
        return self.mapToScene(self.viewport().rect().center())

    def on_action_insert_text(self):
        self._ensure_current_canvas()
        self.cancel_active_modes()
        if self.scene.edit_item:
            self.scene.edit_item.exit_edit_mode()
        item = PrismTextItem(**getattr(self, '_text_insert_style', {}))
        item._canvas_id = self.current_canvas_id
        active = self.category_panel._active_filter
        if active and active[0] == 'category':
            item._categories = [active[1]]
        # Deliberately no clear_filter() here: dropping the category filter
        # made every item on the board pop back into view, which looked like
        # the whole canvas jumping around.  The new text carries the
        # selected category, so it stays visible under the current filter.
        pos = self._insert_position()
        item.setScale(1 / self.get_scale())
        self.undo_stack.push(commands.InsertItems(self.scene, [item], pos))
        self.setFocus(Qt.FocusReason.OtherFocusReason)
        item.enter_edit_mode()
        cursor = item.textCursor()
        cursor.select(QtGui.QTextCursor.SelectionType.Document)
        item.setTextCursor(cursor)

    def on_action_copy(self):
        logger.debug('Copying to clipboard...')
        self.cancel_active_modes()
        from prism.actions.clipboard_workflow import copy_selection
        return copy_selection(self.scene, QtWidgets.QApplication.clipboard())

    def on_action_paste(self):
        from prism.actions.clipboard_workflow import paste
        paste(self)

    def on_action_open_settings_dir(self):
        dirname = os.path.dirname(self.settings.fileName())
        QtGui.QDesktopServices.openUrl(
            QtCore.QUrl.fromLocalFile(dirname))

    def on_selection_changed(self):
        logger.debug('Currently selected items: %s',
                     len(self.scene.selectedItems(user_only=True)))
        self.actiongroup_set_enabled('active_when_selection',
                                     self.scene.has_selection())
        self.actiongroup_set_enabled('active_when_single_image',
                                     self.scene.has_single_image_selection())
        flippable = any(getattr(item, 'TYPE', None) != 'text'
                        for item in self.scene.selectedItems(user_only=True))
        for key in ('flip_horizontally', 'flip_vertically'):
            action = actions.actions[key].qaction
            action.setVisible(flippable or not self.scene.has_selection())
            blocked = getattr(self, '_material_blocked_actions', {})
            if action in blocked:
                blocked[action] = flippable
            else:
                action.setEnabled(flippable)

        if self.scene.has_selection():
            item = self.scene.selectedItems(user_only=True)[0]
            grayscale = getattr(item, 'grayscale', False)
            action = actions.actions['grayscale'].qaction
            blocker = QtCore.QSignalBlocker(action)
            action.setChecked(grayscale)
            del blocker
        self.viewport().update()

    def on_cursor_changed(self, cursor):
        if self.active_mode is None:
            self.viewport().setCursor(cursor)

    def on_cursor_cleared(self):
        if self.active_mode is None:
            self.viewport().unsetCursor()

    def mousePressEvent(self, event):
        if isinstance(self.itemAt(event.position().toPoint()), QtWidgets.QGraphicsProxyWidget):
            super().mousePressEvent(event)
            return
        self._last_canvas_pos = event.position().toPoint()
        if self._text_tool_armed and event.button() == Qt.MouseButton.LeftButton and event.modifiers() == Qt.KeyboardModifier.NoModifier:
            self._text_tool_armed = False
            self.on_action_insert_text()
            self.drawing_state_changed.emit()
            event.accept()
            return
        self._drag_origin = event.position().toPoint()
        self._press_stack_count = self.undo_stack.count()
        # Dragging an item out to another application is bound to
        # Ctrl+Shift+Left. Plain Alt/Left combinations stay available for the
        # pan and zoom gestures configured in the keyboard settings, and
        # Ctrl/Shift alone are reserved for snapping while transforming.
        modifiers = event.modifiers() or Qt.KeyboardModifier.NoModifier
        if (event.button() == Qt.MouseButton.LeftButton
                and modifiers & Qt.KeyboardModifier.ControlModifier
                and modifiers & Qt.KeyboardModifier.ShiftModifier):
            item = self.itemAt(event.pos())
            while item is not None and not hasattr(item, 'save_id'):
                item = item.parentItem()
            if item is not None:
                if not item.isSelected():
                    self.scene.deselect_all_items()
                    item.setSelected(True)
                self._external_drag_start = event.pos()
                event.accept()
                return

        if (event.button() == Qt.MouseButton.LeftButton
                and modifiers == Qt.KeyboardModifier.ShiftModifier):
            item=self.itemAt(event.pos())
            while item is not None and not hasattr(item,'save_id'):item=item.parentItem()
            if item is not None and not item.isSelected():
                item.setSelected(True);event.accept();return

        if self.mousePressEventMainControls(event):
            return

        if self.active_mode == self.SAMPLE_COLOR_MODE:
            if (event.button() == Qt.MouseButton.LeftButton):
                color = self.scene.sample_color_at(
                    self.mapToScene(event.pos()))
                if color:
                    name = qcolor_to_hex(color)
                    clipboard = QtWidgets.QApplication.clipboard()
                    clipboard.setText(name)
                    self.scene.internal_clipboard = []
                    msg = f'Copied color to clipboard: {name}'
                    logger.debug(msg)
                    widgets.PrismNotification(self, msg)
                else:
                    logger.debug('No color found')
            self.cancel_sample_color_mode()
            event.accept()
            return

        # 绘制模式**必须排在鼠标动作之前**。
        #
        # 它是一个用户主动进入的显式模式，左键在这里只能是画笔。
        # 排在后面的话，"左键 = 平移"这一类鼠标设置会先一步把
        # active_mode 改成 PAN_MODE，下面的绘制分支永远走不到 ——
        # 表现就是画笔、直线、箭头全都画不出东西，松开鼠标什么都不剩。
        #
        # 只拦右键和左键：中键的平移/缩放继续往下走，边画边缩放还在。
        if self.active_mode == self.DRAW_MODE:
            if event.button() == Qt.MouseButton.RightButton:
                # 需求第十条：绘制模式里点右键 = 快速退出，行为和工具条
                # 上的关闭按钮一致。右键在别的模式里另有用途，绘制模式
                # 下它只表示"我画完了"。
                self.cancel_draw_mode()
                event.accept()
                return
            if event.button() == Qt.MouseButton.LeftButton:
                if self._drawing_tool == DRAW_TOOL_ERASER:
                    self._erase_at(event.pos())
                else:
                    self._begin_stroke(self.mapToScene(event.pos()))
                event.accept()
                return

        action, inverted = self.keyboard_settings.mouse_action_for_event(event)

        if action == 'zoom':
            self._begin_transient_mode(self.ZOOM_MODE)
            self.event_start = event.position()
            self.event_anchor = event.position()
            self.event_inverted = inverted
            event.accept()
            return

        if action == 'pan':
            logger.trace('Begin pan')
            self._begin_transient_mode(self.PAN_MODE)
            self.event_start = event.position()
            self.viewport().setCursor(Qt.CursorShape.ClosedHandCursor)
            # ClosedHandCursor and OpenHandCursor don't work, but I
            # don't know if that's only on my system or a general
            # problem. It works with other cursors.
            event.accept()
            return

        super().mousePressEvent(event)

    def mouseMoveEvent(self, event):
        if isinstance(self.scene.mouseGrabberItem(), QtWidgets.QGraphicsProxyWidget):
            super().mouseMoveEvent(event)
            return
        if (self.active_mode == self.DRAW_MODE and self._drawing_tool == DRAW_TOOL_ERASER
                and event.buttons() & Qt.MouseButton.LeftButton):
            self._erase_at(event.position().toPoint())
            event.accept()
            return
        if event.buttons() != Qt.MouseButton.NoButton:
            self._begin_interaction()
        self._last_canvas_pos = event.position().toPoint()
        if (self._external_drag_start is not None
                and event.buttons() & Qt.MouseButton.LeftButton):
            distance = (event.pos() - self._external_drag_start).manhattanLength()
            if distance >= QtWidgets.QApplication.startDragDistance():
                self._external_drag_start = None
                self._start_external_drag()
            event.accept()
            return
        if self.active_mode == self.PAN_MODE:
            self.reset_previous_transform()
            pos = event.position()
            self.pan(self.event_start - pos)
            self.event_start = pos
            event.accept()
            return

        if self.active_mode == self.ZOOM_MODE:
            self.reset_previous_transform()
            pos = event.position()
            delta = (self.event_start - pos).y()
            if self.event_inverted:
                delta *= -1
            self.event_start = pos
            self.zoom(delta * 20, self.event_anchor)
            event.accept()
            return

        if self.active_mode == self.SAMPLE_COLOR_MODE:
            self.sample_color_widget.update(
                event.position(),
                self.scene.sample_color_at(self.mapToScene(event.pos())))
            event.accept()
            return

        if self.active_mode == self.DRAW_MODE and self._draw_item is not None:
            scene_pos = self.mapToScene(event.pos())
            if self._drawing_tool == DRAW_TOOL_LINE:
                # 直线**只要首尾两点**（需求第四条）：拖动中的中间点不来
                # 进数据，否则存盘的会是一条密密麻麻的多段线。
                self._draw_item.set_points(
                    [self._draw_points[0], (scene_pos.x(), scene_pos.y())])
            else:
                self._draw_points.append((scene_pos.x(), scene_pos.y()))
                self._draw_item.set_points(self._draw_points)
            event.accept()
            return

        if self.mouseMoveEventMainControls(event):
            return
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event):
        if isinstance(self.scene.mouseGrabberItem(), QtWidgets.QGraphicsProxyWidget):
            super().mouseReleaseEvent(event)
            return
        if self._external_drag_start is not None:
            self._external_drag_start = None
            event.accept()
            return
        # 左键一松，手上那一笔就算画完了 —— **不看 active_mode**。
        #
        # 绘制模式下按中键平移 / 缩放会临时把 active_mode 换成
        # PAN_MODE / ZOOM_MODE（"边画边缩放"是有意保留的，见
        # mousePressEvent 里那段注释）。这时松开左键如果直接走下面那两个
        # 分支，`_draw_item` 就一直挂着没人管：用户以为画完了，可切工具
        # 或退出绘制模式时 `_discard_pending_stroke()` 会把它当成"没画完
        # 的半条"删掉 —— 观感就是"松手之后线没了"。
        #
        # 先结束它，下面的平移 / 缩放收尾照常跑，互不影响。
        if (event.button() == Qt.MouseButton.LeftButton
                and self._draw_item is not None):
            self._finish_pending_stroke()
        if self.active_mode == self.PAN_MODE:
            logger.trace('End pan')
            self.viewport().unsetCursor()
            self._end_transient_mode()
            event.accept()
            return
        if self.active_mode == self.ZOOM_MODE:
            self._end_transient_mode()
            event.accept()
            return
        if self.active_mode == self.DRAW_MODE:
            # 需求第十三条：鼠标按下时图元就进场景、拖动中更新路径、
            # **松开只是结束编辑** —— 不重复加入场景，也不从场景里移除。
            # 画完的线保持可选可移动（需求第十二条）。
            self._finish_pending_stroke()
            event.accept()
            return
        # 松手时先看落点：指针是不是拖到左栏某个**别的画布**行上了。
        # 是的话这一下不是"在画布上挪素材"，而是"把素材归到那个画布"
        # —— 执行放在 super() 之后，因为 scene 先要收尾（它会把这次
        # 拖动压成一条位移命令，见 `_file_items_into_canvas`）。
        canvas_target = self._canvas_drop_target(event)
        dropped = (self.scene.selectedItems(user_only=True)
                   if canvas_target else [])
        if self.mouseReleaseEventMainControls(event):
            return
        super().mouseReleaseEvent(event)
        if canvas_target and dropped:
            self._file_items_into_canvas(dropped, canvas_target)

    def _canvas_drop_target(self, event):
        """这一下松手是不是落在左栏一个别的画布行上？是就给出它的 pageId。

        判定故意保守：指针要真的走过一段（`startDragDistance`，跟系统里
        拖文件一个阈值），要有选中的素材，落点必须在资源树的画布页节点
        上（`canvas_at` 说了算），而且不能是当前这个画布。
        """
        library = getattr(self, 'category_panel', None)
        origin = getattr(self, '_drag_origin', None)
        if library is None or origin is None:
            return None
        travelled = (event.position().toPoint() - origin).manhattanLength()
        from prism.canvas_drop_policy import canvas_drop_destination
        threshold = QtWidgets.QApplication.startDragDistance()
        selected = bool(self.scene.selectedItems(user_only=True))
        # Avoid a tree hit-test until the gesture is eligible.
        candidate = (library.canvas_at(event.globalPosition().toPoint())
                     if travelled >= threshold and selected else None)
        return canvas_drop_destination(
            travelled=travelled, threshold=threshold, has_selection=selected,
            candidate=candidate, current=self.current_canvas_id)

    def _file_items_into_canvas(self, items, page_id):
        """把拖过去的素材真的归到那个画布上。

        拖动过程中图元是实时跟着指针走的，松手时它们已经偏出去一大截
        （指针都到左栏了），而 scene 刚把这一次位移压进撤销栈。所以：
        先把那条位移撤销掉（图元回到原位），再压一条"改画布归属"的
        命令。`QUndoStack.push()` 会把撤销之后留在栈顶之上的命令丢掉，
        于是用户的一次拖动 = 撤销栈里的一条命令，Ctrl+Z 一步就能把
        素材送回原来的画布。
        """
        library = getattr(self, 'category_panel', None)
        if library is None:
            return
        pending = [item for item in items
                   if getattr(item, 'canvas_id', None) != page_id]
        if not pending:
            return
        stack = self.undo_stack
        top = stack.count() - 1
        if (stack.count() > getattr(self, '_press_stack_count', 0) and top >= 0
                and isinstance(stack.command(top), commands.MoveItemsBy)):
            stack.undo()
        if library.move_items_to_canvas(pending, page_id):
            widgets.PrismNotification(
                self, _('Moved {n} item(s) to another canvas').format(
                    n=len(pending)))

    def _external_path_for_item(self, item, index):
        """Return a real file path suitable for UE/Blender/Photoshop."""
        return self._drag_files.path_for(item, index)

    def _start_external_drag(self):
        items = self.scene.selectedItems(user_only=True)
        paths = [self._external_path_for_item(item, idx)
                 for idx, item in enumerate(items, 1)]
        paths = [path for path in paths if path]
        if not paths:
            widgets.PrismNotification(self, _('No exportable files selected'))
            return
        from prism.external_drag import start_file_drag
        preview = getattr(items[0], 'pixmap', lambda: None)()
        start_file_drag(self,paths,preview)

    def paintEvent(self, event):
        renderer = getattr(self, '_overview_renderer', None)
        self._overview_active = renderer is not None and renderer.eligible()
        self.scene._viewport_items.sync(self)
        if self._overview_active:
            painter = QtGui.QPainter(self.viewport())
            try:
                renderer.paint(painter)
            finally:
                painter.end()
        else:
            if renderer is not None:
                renderer.invalidate()
            super().paintEvent(event)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._recalc_timer.stop()
        self.recalc_scene_rect()
        self.welcome_overlay.resize(self.size())
        # 需求：窗口缩放或尺寸变化时工具条要重新居中。工具条不属于场景，
        # 所以它不会跟着画布缩放 —— 只需要在窗口变了之后重新摆一次。
        if getattr(self, '_drawing_toolbar', None) is not None:
            self._position_drawing_toolbar()

    def keyPressEvent(self, event):
        if isinstance(self.scene.focusItem(), QtWidgets.QGraphicsProxyWidget):
            super().keyPressEvent(event)
            return
        if self.keyPressEventMainControls(event):
            return
        if (event.key() == Qt.Key.Key_Space
                and event.modifiers() == Qt.KeyboardModifier.NoModifier
                and not event.isAutoRepeat()):
            selected = self.scene.selectedItems(user_only=True)
            if len(selected) == 1 and getattr(selected[0], 'TYPE', '') in (
                    'pixmap', 'video', 'glb'):
                item = selected[0]
                self.fit_rect(item.sceneBoundingRect(), toggle_item=item)
                event.accept()
                return
        if (event.key() == Qt.Key.Key_Escape and
                self.previous_transform is not None):
            previous = self.previous_transform
            self.setTransform(previous['transform'])
            self.centerOn(previous['center'])
            self.previous_transform = None
            self.zoom_changed.emit(self.transform().m11() * 100.0)
            event.accept()
            return
        if (event.key() in (Qt.Key.Key_Left, Qt.Key.Key_Right) and
                self.previous_transform is not None):
            images = [item for item in self.scene.items_for_save()
                      if item.isVisible() and getattr(item, 'TYPE', '') in
                      ('pixmap', 'video', 'glb')]
            images.sort(key=lambda item: (item.scenePos().y(),
                                          item.scenePos().x()))
            selected = self.scene.selectedItems(user_only=True)
            if images:
                current = selected[0] if selected and selected[0] in images else images[0]
                step = -1 if event.key() == Qt.Key.Key_Left else 1
                target = images[(images.index(current) + step) % len(images)]
                self.scene.deselect_all_items()
                target.setSelected(True)
                # Keep the original pre-preview transform while switching.
                previous = self.previous_transform
                self.fitInView(target.sceneBoundingRect(),
                               Qt.AspectRatioMode.KeepAspectRatio)
                self.previous_transform = previous
                self.zoom_changed.emit(self.transform().m11() * 100.0)
            event.accept()
            return
        if self.active_mode == self.SAMPLE_COLOR_MODE:
            self.cancel_sample_color_mode()
            event.accept()
            return
        super().keyPressEvent(event)

    # ── Metadata action handlers ───────────────────────────────────

    def add_frames_to_canvas(self, frames, source=None):
        """Drop extracted video frames onto the canvas.

        Each frame carries the moment it came from, so a still can always be
        traced back to its place in the video.  Like every other import the
        frames inherit the selected category, which keeps them visible
        without disturbing the view.
        """
        from prism.items import PrismPixmapItem

        if not frames:
            return
        self._ensure_current_canvas()
        video_name = ''
        if source is not None:
            candidate = (getattr(source, '_title', '')
                         or getattr(source, 'filename', '') or '')
            video_name = os.path.basename(candidate) or candidate

        active = getattr(self.category_panel, '_active_filter', None)
        inherited = active[1] if active and active[0] == 'category' else None

        items = []
        for image, position_ms in frames:
            item = PrismPixmapItem(image)
            # A decoded video frame has no imported file. Preserve its full
            # pixels as a lossless PNG before LOD can replace the display.
            data = QtCore.QByteArray()
            buffer = QtCore.QBuffer(data)
            buffer.open(QtCore.QIODevice.OpenModeFlag.WriteOnly)
            if not image.save(buffer, 'PNG'):
                raise ValueError('Could not preserve extracted video frame')
            from prism.source_store import retain_bytes
            item._source_path = retain_bytes(bytes(data), '.png')
            item._source_format = 'png'
            item._canvas_id = self.current_canvas_id
            if inherited:
                item._categories = [inherited]
            minutes, seconds = divmod(int(position_ms / 1000), 60)
            stamp = f'{minutes:02d}:{seconds:02d}'
            if video_name:
                item._title = f'{video_name} @ {stamp}'
                item._notes = _('Source video: {name}\nAt {stamp}').format(
                    name=video_name, stamp=stamp)
            else:
                item._title = stamp
                item._notes = _('At {stamp}').format(stamp=stamp)
            items.append(item)

        self.scene.deselect_all_items()
        # InsertItems does arithmetic on the position, so it has to be a
        # QPointF - get_view_center() hands back a QPoint.
        origin = self.mapToScene(self.get_view_center())
        self.undo_stack.beginMacro(_('Add video frames'))
        try:
            self.undo_stack.push(
                commands.InsertItems(self.scene, items, origin))
            self._arrange_imported_items(items, optimal=True)
        finally:
            self.undo_stack.endMacro()
        for item in items:
            item.setSelected(True)
        self.scene.metadata_changed.emit()
        self.category_panel.update_counts()

    def on_action_find_duplicates(self, scope=None):
        from prism.actions.duplicate_workflow import find_duplicates
        find_duplicates(self, scope)

    def _on_duplicates_scanned(self, groups, stats):
        self._close_duplicate_progress()
        if stats.get('cancelled'):
            # A cancelled scan deliberately returns no partial groups.  The
            # user must never mistake an incomplete comparison for a safe
            # basis on which to delete images.
            QtWidgets.QMessageBox.information(
                self, _('Duplicate Photos'), _('Duplicate scan cancelled.'))
            return
        if not groups:
            QtWidgets.QMessageBox.information(
                self, _('Duplicate Photos'),
                _('No duplicate photos were found. Nothing on the canvas was changed.'))
            return
        from prism.widgets.duplicate_finder import DuplicateFinderDialog
        dialog = DuplicateFinderDialog(
            self, groups, scope=stats.get('scope', 'all-canvases'))
        dialog.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        dialog.show()
        self._duplicate_dialog = dialog

    def _on_duplicates_failed(self, message):
        self._close_duplicate_progress()
        QtWidgets.QMessageBox.warning(
            self, _('Duplicate Photos'), message)

    def _close_duplicate_progress(self):
        """Reuse the dialog's own cleanup (reset + hide + deleteLater)."""
        progress = getattr(self, '_duplicate_progress', None)
        if progress is not None:
            progress.on_finished()
            self._duplicate_progress = None

    def on_action_group_selection(self):
        if not self.scene.edit_item and not self.scene.crop_item:
            self.scene.groups.create()

    def on_action_ungroup_selection(self):
        self.scene.groups.ungroup()

    def on_action_edit_group_note(self):
        self.scene.groups.edit_note()

    def on_action_toggle_side_panel(self, checked):
        if hasattr(self, '_dock_widget'):
            self._dock_widget.setVisible(checked)

    def on_action_edit_categories(self):
        """给选中的素材分配分类。"""
        from prism.widgets.category_dialog import CategoryAssignmentDialog

        selected = self.scene.selectedItems(user_only=True)
        if not selected:
            return
        available = self.category_panel.categories()
        if not available:
            # 「新建分类」的入口原来在左栏旧列表的画布分组上（那个 +
            # 按钮），随列表一起删掉了。分类现在只剩"记录在素材和工程
            # 目录里"这件事，所以这里如实说没有分类可分配。
            QtWidgets.QMessageBox.information(
                self, _('Categories'),
                _('No categories in this project yet.'))
            return
        current = list(getattr(selected[0], '_categories', []) or [])
        dialog = CategoryAssignmentDialog(available, current, parent=self)
        if dialog.exec() != QtWidgets.QDialog.DialogCode.Accepted:
            return
        chosen = dialog.chosen()
        self.scene.undo_stack.push(commands.ChangeMetadata(
            selected, 'categories', [list(chosen) for _item in selected]))
        self.category_panel.update_counts()

    def on_action_edit_notes(self):
        if hasattr(self, '_detail_panel'):
            self._detail_panel._notes_edit.setFocus()

    def on_action_clear_filter(self):
        self.category_panel.clear_filter()
        if hasattr(self.parent, 'color_filter'):
            self.parent.color_filter.clear_filter()
