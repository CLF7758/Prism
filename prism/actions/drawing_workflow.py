"""Canvas drawing tools and stroke lifecycle, separate from project navigation."""
import logging
from PyQt6 import QtCore, QtGui
from PyQt6.QtCore import Qt
from prism import commands
from prism.items import (ARROW_NONE, ARROW_STYLES, DRAW_MODES, DRAW_TOOL_ERASER,
                         DRAW_TOOL_LINE, DRAW_TOOL_PEN, LINE_STYLES, PrismPathItem)

logger = logging.getLogger(__name__)
DRAW_TOOLBAR_MARGIN = 12


class CanvasDrawingMixin:
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
        radius = 7/max(abs(self.transform().m11()), .000001)
        point = self.mapToScene(view_pos)
        area = QtCore.QRectF(point.x()-radius, point.y()-radius, radius*2, radius*2)
        for item in self.scene.items(area, QtCore.Qt.ItemSelectionMode.IntersectsItemShape):
            if getattr(item, 'TYPE', '') == PrismPathItem.TYPE and item.isVisible():
                self._erase_stroke(item)
                break

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

