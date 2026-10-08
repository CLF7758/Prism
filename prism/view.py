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

from functools import partial
import logging
import os
import os.path
import re
import tempfile

from PyQt6 import QtCore, QtGui, QtWidgets
from PyQt6.QtCore import Qt

from prism.actions import ActionsMixin, actions
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
from prism.main_controls import MainControlsMixin
from prism.scene import PrismGraphicsScene
from prism.export_service import ExportService
from prism.workspace_service import WorkspaceService
from prism.utils import qcolor_to_hex
from prism.widgets.drawing_toolbar import DrawingToolbar

#: 悬浮绘制工具条离画布顶部多远（像素）。
DRAW_TOOLBAR_MARGIN = 12


commandline_args = CommandlineArgs()
logger = logging.getLogger(__name__)


class PrismGraphicsView(MainControlsMixin,
                      QtWidgets.QGraphicsView,
                      ActionsMixin):

    zoom_changed = QtCore.pyqtSignal(float)
    drawing_state_changed = QtCore.pyqtSignal()

    PAN_MODE = 1
    ZOOM_MODE = 2
    SAMPLE_COLOR_MODE = 3
    DRAW_MODE = 4

    def __init__(self, app, parent=None):
        super().__init__(parent)
        self.app = app
        self.parent = parent
        self.settings = PrismSettings()
        self._cached_scene_bounds = None
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

        self.undo_stack = QtGui.QUndoStack(self)
        self.undo_stack.setUndoLimit(100)
        self.undo_stack.canRedoChanged.connect(self.on_can_redo_changed)
        self.undo_stack.canUndoChanged.connect(self.on_can_undo_changed)
        self.undo_stack.cleanChanged.connect(self.on_undo_clean_changed)

        # Debounce recalc_scene_rect: batch operations (import, load)
        # fire scene.changed hundreds of times; the timer coalesces
        # them into a single recalculation.
        self._recalc_timer = QtCore.QTimer(self)
        self._recalc_timer.setSingleShot(True)
        self._recalc_timer.setInterval(0)
        self._recalc_timer.timeout.connect(self.recalc_scene_rect)

        self._content_dirty = False
        self.current_canvas_id = 'default-canvas'
        self.filename = None
        self.previous_transform = None
        self.active_mode = None
        self._browser_capture_queue = []
        self._active_capture_metadata = None
        self._external_drag_start = None
        self._external_drag_dir = tempfile.TemporaryDirectory(
            prefix='prism-external-drag-')
        # Remembered so a menu-triggered insert can still land where the
        # user last pointed at the canvas.
        self._last_canvas_pos = None
        # 按下时的指针位置与撤销栈深度：松手时用来判断"这一下是不是把
        # 素材拖到左栏的画布上了"（见 `_canvas_drop_target`）。
        self._drag_origin = None
        self._press_stack_count = 0

        self.scene = PrismGraphicsScene(self.undo_stack)
        self.scene.setParent(self)
        self.scene.changed.connect(self.on_scene_changed)
        self.scene.selectionChanged.connect(self.on_selection_changed)
        self.scene.cursor_changed.connect(self.on_cursor_changed)
        self.scene.cursor_cleared.connect(self.on_cursor_cleared)
        self.setScene(self.scene)
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
                self.open_from_file(fn)
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
            self.settings.update_recent_files(value)
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
            name = os.path.basename(self.filename or '[Untitled]')
            clean = '' if clean else '*'
            title = f'{name}{clean} - {constants.APPNAME}'
        self.parent.setWindowTitle(title)
        if hasattr(self.parent, '_title_bar'):
            self.parent._title_bar.update_title(title)

    def on_scene_changed(self, region):
        self._cached_scene_bounds = None
        if not self.scene.items():
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
        self._recalc_timer.start()

    def on_can_redo_changed(self, can_redo):
        self.actiongroup_set_enabled('active_when_can_redo', can_redo)

    def on_can_undo_changed(self, can_undo):
        self.actiongroup_set_enabled('active_when_can_undo', can_undo)

    def on_undo_clean_changed(self, clean):
        self.update_window_title()

    def mark_content_dirty(self):
        """Mark changes outside the graphics undo stack as unsaved."""
        self._content_dirty = True
        self.update_window_title()

    def is_dirty(self):
        return self._content_dirty or not self.undo_stack.isClean()

    def on_context_menu(self, point):
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
                     '_ai_tag_action'):
            action = getattr(self, attr, None)
            if action:
                self.context_menu.removeAction(action)
                setattr(self, attr, None)

    def _inject_canvas_tools(self):
        """Add reset camera and drawing tools to context menu."""
        # Separator
        sep = self.context_menu.addSeparator()
        self._canvas_sep_action = sep

        # Reset camera (fit all items)
        reset_action = self.context_menu.addAction(
            "\u91cd\u7f6e\u753b\u5e03\u76f8\u673a")
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
        if self.scene.selectedItems(user_only=True):
            ai_tag_action = self.context_menu.addAction("AI 打标签")
            ai_tag_action.triggered.connect(self.on_action_ai_tag_selected)
            self._ai_tag_action = ai_tag_action

    def pick_pen_colour(self):
        """选画笔颜色（右键菜单里那项）。"""
        current = QtGui.QColor(self._pen_color)
        chosen = QtWidgets.QColorDialog.getColor(current, self, '画笔颜色')
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

    def clear_scene(self):
        logging.debug('Clearing scene...')
        self.cancel_active_modes()
        if hasattr(self.parent, 'color_filter'):
            self.parent.color_filter.clear_filter()
        self.scene.clear()
        self.scene.note_html = ''
        self.scene.attachments = {}
        self.scene.mindmap_tree = None
        self.scene.workspace_pages = []
        if hasattr(self.parent, '_reload_workspace_pages'):
            self.parent._reload_workspace_pages()
        self.category_panel.force_rebuild()
        self.undo_stack.clear()
        self._content_dirty = False
        self.filename = None
        self.setTransform(QtGui.QTransform())

    def reset_canvas(self):
        """Reset camera to fit all items in view (PureRef-style)."""
        if self.scene.items():
            self.fit_rect(self.scene.itemsBoundingRect())
        else:
            self.setTransform(QtGui.QTransform())
        logger.debug('Camera reset - fit to scene')

    def start_draw_mode(self):
        """进绘制模式：显示悬浮工具条，带回上次用的工具。

        需求：点击「绘制工具」后关闭右键菜单、进绘制模式、在画布顶部
        居中显示悬浮工具条、默认选中**上一次使用的**绘制工具、第一次
        使用时默认自由画笔、鼠标指针切成十字光标。
        """
        self.cancel_active_modes()
        self.active_mode = self.DRAW_MODE
        self._mode_before_transient = None
        self._draw_points = []
        self._draw_item = None
        bar = getattr(self, '_drawing_toolbar', None)
        if bar is not None:
            bar.set_tool(self._drawing_tool)
            bar.set_colour(self._pen_color)
            bar.set_width(self._pen_width)
            bar.set_arrow(self._arrow)
            bar.setVisible(not hasattr(self, '_fixed_canvas_toolbar'))
            self._position_drawing_toolbar()
            bar.raise_()
        self._update_draw_cursor()
        logger.debug('Draw mode started (tool=%s)', self._drawing_tool)
        self.drawing_state_changed.emit()

    def cancel_draw_mode(self):
        """退出绘制模式。

        需求：退出模式、隐藏工具条、恢复普通鼠标、停掉没画完的那一笔、
        **已完成的绘制内容继续保留**（不清空）、也**不改**当前的颜色 /
        粗细 / 箭头设置。
        """
        self._discard_pending_stroke()
        self._text_tool_armed = False
        self.active_mode = None
        self._mode_before_transient = None
        self.viewport().unsetCursor()
        bar = getattr(self, '_drawing_toolbar', None)
        if bar is not None:
            bar.hide()
        self.drawing_state_changed.emit()

    # ── 平移 / 缩放：临时模式 ───────────────────────────────────────

    def _begin_transient_mode(self, mode):
        """切到平移 / 缩放这类临时模式，并记下原来是什么模式。

        绘制模式下用中键拖一下画布是很自然的动作。松开之后必须回到
        绘制模式：`active_mode` 被清成 None 而工具栏还挂在画布上的话，
        用户接着按左键只会得到橡皮筋选择框，松开什么都不剩 ——
        看起来就是"画一笔就消失""完全不能画"。
        """
        self._mode_before_transient = self.active_mode
        self.active_mode = mode

    def _end_transient_mode(self):
        """回到临时模式之前的状态。非绘制模式结束就回普通状态。"""
        previous = self._mode_before_transient
        self._mode_before_transient = None
        self.active_mode = (self.DRAW_MODE if previous == self.DRAW_MODE
                            else None)

    # ── 悬浮工具条 ──────────────────────────────────────────────────

    def _position_drawing_toolbar(self):
        """把工具条摆在画布顶部中央。

        需求：显示在画布顶部中央、与顶部保持固定距离、窗口缩放或尺寸
        变化时始终重新居中。工具条是 QWidget 不是场景图元，所以它天然
        不参与画布缩放和移动，也不会被导出或被选择框选中。
        """
        bar = getattr(self, '_drawing_toolbar', None)
        if bar is None:
            return
        if hasattr(self, '_fixed_canvas_toolbar'):
            return
        bar.adjustSize()
        x = max(0, (self.width() - bar.width()) // 2)
        bar.move(x, DRAW_TOOLBAR_MARGIN)
        bar.raise_()

    def set_drawing_tool(self, tool):
        """切工具。三种互斥（需求第九条）。

        **已经完成的**笔画不受影响（需求："工具切换过程中不得丢失已经
        完成的笔画"）；手上没画完的那半条会丢掉 —— 它还没成形，留着
        反而是画布上的垃圾。换工具也**不退出**绘制模式。
        """
        if tool not in DRAW_MODES:
            return
        self._discard_pending_stroke()
        self._drawing_tool = tool
        self.settings.setValue('Canvas/drawing_tool', tool)
        bar = getattr(self, '_drawing_toolbar', None)
        if bar is not None:
            bar.set_tool(tool)
        self._update_draw_cursor()

        self.drawing_state_changed.emit()

    def set_eraser(self, enabled):
        """橡皮擦开关（旧接口，保留给测试和外部调用）。

        开着的时候点中哪条线就删哪条。做成"删掉整条线"而不是"涂白"：
        画布背景不一定是白的，涂白也盖不住底下已有的素材。删整条符合
        "这是个草图工具"的用法，而且能撤销。
        """
        self.set_drawing_tool(DRAW_TOOL_ERASER if enabled else DRAW_TOOL_PEN)

    def set_arrow_style(self, arrow):
        """选箭头样式 —— **同时自动切到直线工具**（需求第八条）。"""
        if arrow not in ARROW_STYLES:
            return
        self._arrow = arrow
        self.settings.setValue('Canvas/arrow_style', arrow)
        bar = getattr(self, '_drawing_toolbar', None)
        if bar is not None:
            bar.set_arrow(arrow)
        # 需求：选了箭头样式就自动进直线工具，用户不用再点直线按钮
        self.set_drawing_tool(DRAW_TOOL_LINE)

    def set_line_style(self, line_style):
        """选线型：实线 / 虚线 / 点线。

        线型对画笔和直线都成立，所以**不切工具** —— 用户多半是想把
        当前这支笔换个线型接着画。
        """
        if line_style not in LINE_STYLES:
            return
        self._line_style = line_style
        self.settings.setValue('Canvas/line_style', line_style)
        self.drawing_state_changed.emit()

    def _update_draw_cursor(self):
        """十字光标；橡皮擦换一个，让用户看得出状态不同（需求第五条）。"""
        if self.active_mode != self.DRAW_MODE:
            return
        self.viewport().setCursor(
            Qt.CursorShape.PointingHandCursor
            if self._drawing_tool == DRAW_TOOL_ERASER
            else Qt.CursorShape.CrossCursor)

    # ── 画笔设置（颜色 / 粗细 / 工具 / 箭头）────────────────────────

    def pen_settings(self):
        """当前画笔的样子。给工具条和测试读。"""
        return {'color': self._pen_color, 'width': self._pen_width,
                'tool': self._drawing_tool, 'arrow': self._arrow,
                'line_style': self._line_style,
                'eraser': self._drawing_tool == DRAW_TOOL_ERASER}

    def set_pen_color(self, color):
        """设画笔颜色。画到一半改也行 —— 从下一条线开始生效。

        需求：「新颜色只影响之后创建的笔画和直线、已经完成的绘制内容
        不得被自动改色」—— 所以这里只记设置，不碰场景里已有的图元。
        """
        parsed = QtGui.QColor(str(color))
        if not parsed.isValid():
            return                  # 非法值当没收到（需求第十四条）
        self._pen_color = parsed.name()
        self.settings.setValue('Canvas/pen_color', self._pen_color)
        bar = getattr(self, '_drawing_toolbar', None)
        if bar is not None:
            bar.set_colour(self._pen_color)

        self.drawing_state_changed.emit()

    def set_pen_width(self, width):
        try:
            width = float(width)
        except (TypeError, ValueError):
            return                  # 非数字当没收到（需求第七条）
        # 需求：最小 0.5、最大 50
        self._pen_width = max(0.5, min(50.0, width))
        self.settings.setValue('Canvas/pen_width', self._pen_width)
        bar = getattr(self, '_drawing_toolbar', None)
        if bar is not None:
            bar.set_width(self._pen_width)

        self.drawing_state_changed.emit()

    # ── 画一笔的三个步骤 ────────────────────────────────────────────

    def _scene_pen_width(self):
        """把「屏幕像素」的线宽换算成当前视图下的场景线宽。

        工具条上的线宽预设（1/2/4/8/16/24）和数值框（0.5–50）都是用户
        按**屏幕**理解的粗细。但图元活在场景坐标里 —— 大工程里
        ``Fit view`` 会把视图压得很小（实测 11464 单位宽的工程压到
        0.095），同一个线宽在屏幕上只剩不到一个像素：点一下等于什么
        都没画，用户看到的就是"画完就消失"。

        所以存进图元之前先除以当前缩放，让屏幕上量到的粗细等于用户
        选的那个数。缩放视图之后线会跟着画面一起放大缩小，和图片一样
        —— 那是"画面的一部分"该有的行为。
        """
        scale = self.transform().m11()
        if scale <= 0:
            return self._pen_width
        return self._pen_width / scale

    def _begin_stroke(self, scene_pos):
        """按下鼠标：开一笔。图元**这时就进场景**（需求第十三条）。"""
        self._ensure_current_canvas()
        # 上一笔要是还挂着就先把引用收干净 —— 直接覆盖的话那半条线会
        # 留在场景里没人认领（切工具时也删不掉它了）。
        self._finish_pending_stroke()
        point = (scene_pos.x(), scene_pos.y())
        self._draw_points = [point]
        if self._drawing_tool == DRAW_TOOL_LINE:
            # 直线一开始就得有两个点，不然预览画不出来（路径只有一个
            # moveTo 是空的，用户会以为工具坏了）。
            self._draw_points.append(point)
        is_line = self._drawing_tool == DRAW_TOOL_LINE
        self._draw_item = PrismPathItem(
            self._draw_points,
            color=self._pen_color,
            width=self._scene_pen_width(),
            tool=DRAW_TOOL_LINE if is_line else DRAW_TOOL_PEN,
            # 箭头只对直线有意义；画笔画出来的是手绘轨迹，带箭头很怪
            arrow=self._arrow if is_line else ARROW_NONE,
            line_style=self._line_style)
        self._draw_item._canvas_id = self.current_canvas_id
        self._draw_item.setZValue(9999)
        self.scene.addItem(self._draw_item)

    def _erase_stroke(self, item):
        """删掉一条绘制线 —— 走撤销栈。

        **只删绘制内容**（需求第五条）：判断靠 `TYPE`，所以图片、视频、
        文字、文档内容、脑图节点、分组、标签一个都不会被误删。

        撤销之后线的**位置、颜色、粗细、类型、箭头**都回来 ——
        `DeleteItems` 保留的是同一个对象，不是重建的副本。
        """
        if item is None or getattr(item, 'TYPE', '') != PrismPathItem.TYPE:
            return
        if item.scene() is not self.scene:
            return
        self.scene.undo_stack.push(
            commands.DeleteItems(self.scene, [item]))

    def _erase_at(self, view_pos):
        """橡皮擦：点中哪条绘制线就删哪条；点空白什么都不做。"""
        self._erase_stroke(self.itemAt(view_pos))

    def _finish_pending_stroke(self):
        """松开鼠标 / 切工具：结束编辑，图元留在场景里。"""
        self._draw_points = []
        self._draw_item = None

    def _discard_pending_stroke(self):
        """没画完就中断了（退出模式 / 切工具）。

        需求第十条说"停止当前尚未完成的绘制操作"，第十四条说"绘制过程中
        点击关闭"也要安全。一条刚按下还没放开的线没有保留的价值 ——
        留着会成为画布上的垃圾。
        """
        item = self._draw_item
        self._draw_points = []
        self._draw_item = None
        if item is not None and item.scene() is self.scene:
            self.scene.removeItem(item)

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

    def get_confirmation_unsaved_changes(self, msg):
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
            _('There are unsaved changes. Are you sure you want to open a new scene?'))
        if confirm:
            self.clear_scene()

    def on_action_fit_scene(self):
        visible = [i for i in self.scene.items_for_save() if i.isVisible()]
        if visible:
            self.fit_rect(self.scene.itemsBoundingRect(items=visible))

    def on_action_fit_selection(self):
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

    def on_action_undo(self):
        logger.debug('Undo: %s' % self.undo_stack.undoText())
        self.cancel_active_modes()
        self.undo_stack.undo()

    def on_action_redo(self):
        logger.debug('Redo: %s' % self.undo_stack.redoText())
        self.cancel_active_modes()
        self.undo_stack.redo()

    def on_action_select_all(self):
        self.scene.select_all_items()

    def on_action_deselect_all(self):
        self.scene.deselect_all_items()

    def on_action_delete_items(self):
        logger.debug('Deleting items...')
        self.cancel_active_modes()
        self.undo_stack.push(
            commands.DeleteItems(
                self.scene, self.scene.selectedItems(user_only=True)))

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
        self.scene.arrange()

    def on_action_arrange_vertical(self):
        self.scene.arrange(vertical=True)

    def on_action_arrange_optimal(self):
        self.scene.arrange_optimal()

    def on_action_arrange_square(self):
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
        if images:
            self.undo_stack.push(
                commands.ToggleGrayscale(images, checked))

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

    def on_items_loaded(self, value):
        logger.debug('On items loaded: add queued items')
        self.scene.add_queued_items()

    def on_loading_finished(self, filename, errors):
        if errors:
            QtWidgets.QMessageBox.warning(
                self,
                _('Problem loading file'),
                _('<p>Problem loading file %s</p><p>Not accessible or not a proper bee file</p>') % filename)
        else:
            self.filename = filename
            self.scene.add_queued_items()
            self.category_panel.force_rebuild()
            self.on_action_fit_scene()
            if hasattr(self.parent, '_reload_workspace_pages'):
                self.parent._reload_workspace_pages()

    def on_action_open_recent_file(self, filename):
        confirm = self.get_confirmation_unsaved_changes(
            _('There are unsaved changes. Are you sure you want to open a new scene?'))
        if confirm:
            self.open_from_file(filename)

    def open_from_file(self, filename):
        logger.info(f'Opening file {filename}')
        self.clear_scene()
        self.worker = fileio.ThreadedIO(
            fileio.load_prism, filename, self.scene)
        self.worker.progress.connect(self.on_items_loaded)
        self.worker.finished.connect(self.on_loading_finished)
        self.progress = widgets.PrismProgressDialog(
            f'Loading {filename}',
            worker=self.worker,
            parent=self)
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
            filter=f'{constants.APPNAME} File (*.prism)')
        if filename:
            filename = os.path.normpath(filename)
            self.open_from_file(filename)
            self.filename = filename

    def on_saving_finished(self, filename, errors, silent=False):
        if errors:
            if silent:
                # Autosave runs unattended; a modal warning every few
                # minutes would be worse than a log line.
                logger.error('Autosave failed for %s: %s', filename, errors)
                return
            QtWidgets.QMessageBox.warning(
                self,
                _('Problem saving file'),
                _('<p>Problem saving file %s</p><p>File/directory not accessible</p>') % filename)
        else:
            self.filename = filename
            self.undo_stack.setClean()
            self._content_dirty = False
            self.update_window_title()
            if hasattr(self.parent, 'clear_recovery_snapshot'):
                self.parent.clear_recovery_snapshot()
            if silent and hasattr(self.parent, 'statusBar'):
                self.parent.statusBar().showMessage(
                    _('Saved automatically at {time}').format(
                        time=QtCore.QTime.currentTime().toString('HH:mm:ss')),
                    4000)

    def do_save(self, filename, create_new, silent=False):
        # Flush editors whose debounce timer may not have fired yet.
        tabs = getattr(self.parent, 'tabs', None)
        if tabs is not None:
            panel = tabs.currentWidget()
            if hasattr(panel, 'store_to_scene'):
                panel.store_to_scene()
        if not fileio.is_prism_file(filename):
            filename = f'{filename}.prism'
        self.worker = fileio.ThreadedIO(
            fileio.save_prism, filename, self.scene, create_new=create_new)
        if silent:
            # Still off the UI thread, but with no progress dialog.
            self.worker.finished.connect(
                lambda name, errors: self.on_saving_finished(
                    name, errors, silent=True))
            self.worker.start()
            return
        self.worker.finished.connect(self.on_saving_finished)
        self.progress = widgets.PrismProgressDialog(
            f'Saving {filename}',
            worker=self.worker,
            parent=self)
        self.worker.start()

    def on_action_save_as(self):
        self.cancel_active_modes()
        directory = os.path.dirname(self.filename) if self.filename else None
        filename, f = QtWidgets.QFileDialog.getSaveFileName(
            parent=self,
            caption=_('Save file'),
            directory=directory,
            filter=f'{constants.APPNAME} File (*.prism)')
        if filename:
            self.do_save(filename, create_new=True)

    def on_action_save(self):
        self.cancel_active_modes()
        if not self.filename:
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
        panel = window.find_workspace_panel(kind)
        if panel is not None:
            window.open_workspace_page(kind, getattr(panel, 'page_id', None))
            return panel
        return window.create_workspace_page_quietly(kind)

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
        from prism.actions.import_workflow import import_document
        import_document(self)

    def on_action_import_mindmap(self):
        """Load an existing XMind file into the open mind map page."""
        from prism.fileio.importers import ImportFileError, xmind_to_tree

        filename, _selected = QtWidgets.QFileDialog.getOpenFileName(
            parent=self, caption=_('Import XMind Mind Map'),
            directory=self._import_directory(),
            filter=_('XMind Mind Map (*.xmind)'))
        if not filename:
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
        panel._tree = tree
        panel.store_to_scene()
        panel.load_from_scene()
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

    def _arrange_imported_items(self, items):
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
            if len(group) > 200:
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
        """Callback for when loading of images is finished.

        :param new_scene: True if the scene was empty before, else False
        :param filename: Not used, for compatibility only
        :param errors: List of filenames that couldn't be loaded
        """

        logger.debug('Insert images finished')
        if errors:
            errornames = [
                f'<li>{fn}</li>' for fn in errors]
            errornames = '<ul>%s</ul>' % '\n'.join(errornames)
            num = len(errors)
            msg = _('%s image(s) could not be opened.<br/>') % num
            QtWidgets.QMessageBox.warning(
                self,
                _('Problem loading images'),
                msg + IMG_LOADING_ERROR_MSG + errornames)
        self.scene.add_queued_items()

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

    def do_insert_images(self, filenames, pos=None, folder_categories=None,
                         folder_trees=None):
        if not filenames:
            return
        self._ensure_current_canvas()
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
            fileio.load_media,
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
        item = PrismTextItem()
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
        clipboard = QtWidgets.QApplication.clipboard()
        items = self.scene.selectedItems(user_only=True)

        # At the moment, we can only copy one image to the global
        # clipboard. (Later, we might create an image of the whole
        # selection for external copying.)
        items[0].copy_to_clipboard(clipboard)

        # However, we can copy all items to the internal clipboard:
        self.scene.copy_selection_to_internal_clipboard()

        # We set a marker for ourselves in the global clipboard so
        # that we know to look up the internal clipboard when pasting:
        mime = clipboard.mimeData()
        if mime is not None:
            from prism.clipboard_capture import ClipboardCaptureAdapter
            ClipboardCaptureAdapter.mark_own_mime(mime)
            mime.setData(
                'prism/items', QtCore.QByteArray.number(len(items)))

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

        if self.scene.has_selection():
            item = self.scene.selectedItems(user_only=True)[0]
            grayscale = getattr(item, 'grayscale', False)
            actions.actions['grayscale'].qaction.setChecked(grayscale)
        self.viewport().update()

    def on_cursor_changed(self, cursor):
        if self.active_mode is None:
            self.viewport().setCursor(cursor)

    def on_cursor_cleared(self):
        if self.active_mode is None:
            self.viewport().unsetCursor()

    @property
    def _import_in_progress(self):
        return getattr(self, '_import_progress_flag', False)

    @_import_in_progress.setter
    def _import_in_progress(self, value):
        self._import_progress_flag = bool(value)
        self.scene._import_in_progress = bool(value)

    def _begin_interaction(self):
        self.scene._interaction_in_progress = True
        self._interaction_timer.start()

    def _end_interaction(self):
        self.scene._interaction_in_progress = False
        self.viewport().update()

    def _content_bounds(self):
        if self._cached_scene_bounds is None:
            self._cached_scene_bounds = self.scene.itemsBoundingRect()
        return self._cached_scene_bounds

    def recalc_scene_rect(self):
        """Resize the scene rectangle so that it is always one view width
        wider than all items' bounding box at each side and one view
        width higher on top and bottom. This gives the impression of
        an infinite canvas."""

        if self.previous_transform:
            return
        logger.trace('Recalculating scene rectangle...')
        try:
            # 包围盒只算一次。itemsBoundingRect() 要遍历场景里每一项，
            # 而剖析显示这个函数在打开工程期间被调 172 次、累计 0.62 秒
            # —— 其中一半是拿同一个包围盒去取第二个角点。
            bounds = self._content_bounds()
            topleft = self.mapFromScene(bounds.topLeft())
            topleft = self.mapToScene(QtCore.QPoint(
                topleft.x() - self.size().width(),
                topleft.y() - self.size().height()))
            bottomright = self.mapFromScene(bounds.bottomRight())
            bottomright = self.mapToScene(QtCore.QPoint(
                bottomright.x() + self.size().width(),
                bottomright.y() + self.size().height()))
            self.setSceneRect(QtCore.QRectF(topleft, bottomright))
        except OverflowError:
            logger.info('Maximum scene size reached')
        logger.trace('Done recalculating scene rectangle')

    def get_zoom_size(self, func):
        """Calculates the size of all items' bounding box in the view's
        coordinates.

        This helps ensure that we never zoom out too much (scene
        becomes so tiny that items become invisible) or zoom in too
        much (causing overflow errors).

        :param func: Function which takes the width and height as
            arguments and turns it into a number, for ex. ``min`` or ``max``.
        """

        bounds = self._content_bounds()
        topleft = self.mapFromScene(bounds.topLeft())
        bottomright = self.mapFromScene(bounds.bottomRight())
        return func(bottomright.x() - topleft.x(),
                    bottomright.y() - topleft.y())

    def scale(self, *args, **kwargs):
        super().scale(*args, **kwargs)
        self.scene.on_view_scale_change()
        self._recalc_timer.stop()
        self.recalc_scene_rect()

    def get_scale(self):
        return self.transform().m11()

    def pan(self, delta):
        self._begin_interaction()
        if not self.scene.items():
            logger.debug('No items in scene; ignore pan')
            return

        hscroll = self.horizontalScrollBar()
        hscroll.setValue(int(hscroll.value() + delta.x()))
        vscroll = self.verticalScrollBar()
        vscroll.setValue(int(vscroll.value() + delta.y()))

    def zoom(self, delta, anchor):
        self._begin_interaction()
        if not self.scene.items():
            logger.debug('No items in scene; ignore zoom')
            return

        # We calculate where the anchor is before and after the zoom
        # and then move the view accordingly to keep the anchor fixed
        # We can't use QGraphicsView's AnchorUnderMouse since it
        # uses the current cursor position while we need the initial mouse
        # press position for zooming with Ctrl + Middle Drag
        anchor = QtCore.QPoint(round(anchor.x()),
                               round(anchor.y()))
        ref_point = self.mapToScene(anchor)
        if delta == 0:
            return
        factor = 1 + abs(delta / 1000)
        if delta > 0:
            if self.get_zoom_size(max) < 10000000:
                self.scale(factor, factor)
            else:
                logger.debug('Maximum zoom size reached')
                return
        else:
            if self.get_zoom_size(min) > 50:
                self.scale(1/factor, 1/factor)
            else:
                logger.debug('Minimum zoom size reached')
                return

        self.pan(self.mapFromScene(ref_point) - anchor)
        self.reset_previous_transform()
        self.zoom_changed.emit(self.transform().m11() * 100.0)

    def wheelEvent(self, event):
        action, inverted\
            = self.keyboard_settings.mousewheel_action_for_event(event)

        delta = event.angleDelta().y()
        if inverted:
            delta = delta * -1

        if action == 'zoom':
            self.zoom(delta, event.position())
            event.accept()
            return
        if action == 'pan_horizontal':
            self.pan(QtCore.QPointF(0, 0.5 * delta))
            event.accept()
            return
        if action == 'pan_vertical':
            self.pan(QtCore.QPointF(0.5 * delta, 0))
            event.accept()
            return

    def mousePressEvent(self, event):
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
        if travelled < QtWidgets.QApplication.startDragDistance():
            return None
        if not self.scene.selectedItems(user_only=True):
            return None
        page_id = library.canvas_at(event.globalPosition().toPoint())
        if page_id is None or page_id == self.current_canvas_id:
            return None
        return page_id

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
        filename = getattr(item, 'filename', '') or ''
        if os.path.isfile(filename):
            return os.path.abspath(filename)
        video_url = getattr(item, '_video_url', None)
        if video_url is not None and video_url.isLocalFile():
            path = video_url.toLocalFile()
            if os.path.isfile(path):
                return os.path.abspath(path)
        pixmap = getattr(item, 'pixmap', lambda: None)()
        if pixmap is None or pixmap.isNull():
            return None
        stem = os.path.splitext(os.path.basename(filename))[0] or f'image-{index}'
        stem = re.sub(r'[^A-Za-z0-9._-]+', '-', stem).strip('-') or f'image-{index}'
        fd, path = tempfile.mkstemp(prefix=stem[:80] + '-', suffix='.png',
                                    dir=self._external_drag_dir.name)
        os.close(fd)
        if not pixmap.save(path, 'PNG'):
            return None
        return path

    def _start_external_drag(self):
        items = self.scene.selectedItems(user_only=True)
        paths = [self._external_path_for_item(item, idx)
                 for idx, item in enumerate(items, 1)]
        paths = [path for path in paths if path]
        if not paths:
            widgets.PrismNotification(self, _('No exportable files selected'))
            return
        mime = QtCore.QMimeData()
        mime.setUrls([QtCore.QUrl.fromLocalFile(path) for path in paths])
        mime.setText('\n'.join(paths))
        drag = QtGui.QDrag(self)
        drag.setMimeData(mime)
        preview = getattr(items[0], 'pixmap', lambda: None)()
        if preview is not None and not preview.isNull():
            drag.setPixmap(preview.scaled(
                160, 120, Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.SmoothTransformation))
        drag.exec(Qt.DropAction.CopyAction)

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
        self.undo_stack.push(
            commands.InsertItems(self.scene, items, origin))
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
