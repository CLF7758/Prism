"""画布顶部的悬浮绘制工具条。

外观**逐像素**照用户给的参考图做（2026-10-01 那张深色浮条）：一条
`#222222` 的圆角浮条，从左到右

    画笔 · 直线▾ · 橡皮 │ 颜色 · 粗细▾ · 线型▾ · 关闭

实测参考图（513x89 的截图）得到的尺寸，全部换算进这里：

* 工具条 508x75，圆角约 14
* 按钮 40x40，选中底色 `#4b4b4b`，圆角 6
* 图标 23x23，墨色 `#e0e0e0`
* 带 `▾` 的按钮右侧 23 像素是下拉区，三角 7x4、颜色 `#737373`、
  与图标垂直居中
* 颜色按钮的色条 25x6，底下那支笔是"笔尖朝下的钢笔"

带 `▾` 的按钮点一下会弹出自己的选项（直线选线型、粗细选线宽、
线型选实线/虚线/点线），不带 `▾` 的就是一个直接的开关。

**它是个 QWidget，不是场景图元** —— 所以它天然满足那几条"不参与画布"
的要求：

* 不属于 `QGraphicsScene` → **不参与画布缩放和移动**
* 导出是渲染 scene → **工具条不会被导出**
* 画布的选择框只作用于场景图元 → **工具条不会被选中**

图标全部用 `QPainter` 现画（矢量），不用图片文件 —— 环境里没有图标
资源，而且这样能跟着主题色走。
"""
import logging

from PyQt6 import QtCore, QtGui, QtWidgets
from PyQt6.QtCore import Qt

from prism.i18n import _
from prism.items import (ARROW_BOTH, ARROW_END, ARROW_NONE, ARROW_START,
                         LINE_DASHED, LINE_DOTTED, LINE_SOLID,
                         LINE_STYLE_PENS)

logger = logging.getLogger(__name__)

#: 单个按钮的边长（参考图的按钮是 40x40 的圆角块）。
BUTTON_SIZE = 40
#: 图标画布边长（参考图的图标都在 23x23 内）。
ICON_SIZE = 23
#: 图标画布的高度。参考图里橡皮正好 24 行、其余 23 行，都是顶端对齐。
ICON_BOX_H = 24
#: 图标在按钮里的左边距：(40-23)/2。
ICON_INSET = (BUTTON_SIZE - ICON_SIZE) // 2
#: 带 `▾` 的按钮右侧给三角留的专用区宽度（参考图约 23）。
CHEVRON_ZONE = 23
#: 带 `▾` 的按钮总宽。
MENU_BUTTON_WIDTH = BUTTON_SIZE + CHEVRON_ZONE

#: 按钮与按钮之间、工具条内边距 —— 都按参考图量出来的疏密。
ROW_SPACING = 18
BAR_MARGIN_H = 15
BAR_MARGIN_V = 16

#: 参考图里图标墨色是近白的 `#e0e0e0`，下拉三角是 `#737373`。
INK = QtGui.QColor(224, 224, 224)
INK_DIM = QtGui.QColor(115, 115, 115)

#: 色条上下那 1 像素描边 —— 参考图里是主色的暗版。
SWATCH_BORDER = 0.62


def _new_canvas(width, height):
    """一块透明的画布 + 已经配好的画笔。"""
    pixmap = QtGui.QPixmap(width, height)
    pixmap.fill(QtCore.Qt.GlobalColor.transparent)
    painter = QtGui.QPainter(pixmap)
    painter.setRenderHint(QtGui.QPainter.RenderHint.Antialiasing)
    return pixmap, painter


def _stroke_pen(colour=INK, width=2.0):
    pen = QtGui.QPen(colour, width)
    pen.setCapStyle(Qt.PenCapStyle.RoundCap)
    pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
    return pen


def _icon_painter(width=BUTTON_SIZE):
    """带 `▾` 的按钮用 `MENU_BUTTON_WIDTH` 宽的画布，别的用按钮宽。"""
    return _new_canvas(width, ICON_BOX_H)


def _draw_chevron(painter, width=BUTTON_SIZE):
    """`▾` 画在右边那个 23 像素的专用区里，和图标垂直居中。

    参考图实测：7 宽、4 高、线 1.6、颜色 `#737373`。
    """
    left = width - CHEVRON_ZONE + (CHEVRON_ZONE - 7.2) / 2.0
    top = 9.5
    pen = QtGui.QPen(INK_DIM, 1.6)
    pen.setCapStyle(Qt.PenCapStyle.RoundCap)
    pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
    painter.setPen(pen)
    painter.setBrush(Qt.BrushStyle.NoBrush)
    painter.drawPolyline(QtGui.QPolygonF([
        QtCore.QPointF(left, top),
        QtCore.QPointF(left + 3.6, top + 4.0),
        QtCore.QPointF(left + 7.2, top)]))


#: 铅笔和橡皮的轮廓：直接从参考图上**逐行取亮像素边界**提出来的
#: （图标局部坐标，原点在图标框左上角）。用轮廓画就保证了"完全按照
#: 图片"—— 手工拼形状总会在某条边上差几个像素。
_PENCIL_TAIL = [(17.0, 0.0), (15.0, 2.0), (20.0, 7.0), (21.0, 7.0),
                (23.0, 5.0), (23.0, 2.0), (21.0, 0.0)]
_PENCIL_BODY = [(13.0, 4.0), (0.0, 17.0), (0.0, 22.0), (6.0, 22.0),
                (19.0, 9.0), (14.0, 4.0)]
_ERASER_BODY = [(14.0, 0.0), (0.0, 14.0), (0.0, 17.0), (5.0, 22.0),
                (7.0, 23.0), (10.0, 23.0), (12.0, 22.0), (24.0, 10.0),
                (24.0, 7.0), (17.0, 0.0)]
_ERASER_HOLE = [(8.0, 10.0), (3.0, 15.0), (3.0, 16.0), (7.0, 20.0),
                (10.0, 20.0), (14.0, 16.0), (14.0, 15.0), (9.0, 10.0)]


def _polygon(points):
    return QtGui.QPolygonF([QtCore.QPointF(x, y) for x, y in points])


def _fill_outline(painter, points, colour):
    """按提取下来的轮廓填充。

    轮廓是从"够亮"的像素里取的，比视觉边缘小半像素 —— 给 1 像素的
    同色描边正好补回来，圆角接合顺带把尖角磨顺。
    """
    pen = QtGui.QPen(colour, 1.0)
    pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
    painter.setPen(pen)
    painter.setBrush(QtGui.QBrush(colour))
    painter.drawPolygon(_polygon(points))


def _draw_pencil(painter, colour):
    """参考图第 1 格的铅笔 —— 两段轮廓：尾块（带箍缝）+ 笔身笔尖。"""
    painter.save()
    painter.translate(ICON_INSET, 0)
    _fill_outline(painter, _PENCIL_TAIL, colour)
    _fill_outline(painter, _PENCIL_BODY, colour)
    painter.restore()


def _draw_nib_pen(painter, colour):
    """参考图第 5 格的笔：**笔尖朝下的钢笔**（和铅笔不是同一支）。

    上面是斜 45° 的笔杆（尾端斜切成尖），下面是笔尖 —— 笔尖中间有
    一道缝，参考图上就是"两瓣"的样子。
    """
    painter.save()
    painter.translate(ICON_INSET + ICON_SIZE / 2.0, 9.0)
    painter.rotate(45)
    painter.setPen(Qt.PenStyle.NoPen)
    painter.setBrush(QtGui.QBrush(colour))
    # 笔杆：尾端斜切
    painter.drawPolygon(QtGui.QPolygonF([
        QtCore.QPointF(-4.4, -11.6),
        QtCore.QPointF(0.0, -16.2),
        QtCore.QPointF(4.4, -11.6),
        QtCore.QPointF(4.4, -0.6),
        QtCore.QPointF(-4.4, -0.6)]))
    # 笔尖左瓣
    painter.drawPolygon(QtGui.QPolygonF([
        QtCore.QPointF(-4.4, 0.6),
        QtCore.QPointF(-0.9, 0.6),
        QtCore.QPointF(-0.9, 13.4),
        QtCore.QPointF(-4.4, 9.6)]))
    # 笔尖右瓣
    painter.drawPolygon(QtGui.QPolygonF([
        QtCore.QPointF(0.9, 0.6),
        QtCore.QPointF(4.4, 0.6),
        QtCore.QPointF(4.4, 9.6),
        QtCore.QPointF(0.9, 13.4)]))
    painter.restore()


# ── 图标 ────────────────────────────────────────────────────────────

def pen_icon():
    """自由画笔：参考图第 1 格那支实心铅笔。"""
    pixmap, painter = _icon_painter()
    _draw_pencil(painter, INK)
    painter.end()
    return QtGui.QIcon(pixmap)


def line_icon():
    """直线：一条斜线 + `▾`。

    参考图实测：23x23 里从 (21,1.6) 到 (1.6,21)，线宽约 3.4、圆头。
    """
    pixmap, painter = _icon_painter(MENU_BUTTON_WIDTH)
    painter.setPen(_stroke_pen(INK, 3.4))
    painter.drawLine(
        QtCore.QPointF(ICON_INSET + 2.0, ICON_SIZE - 2.0),
        QtCore.QPointF(ICON_INSET + ICON_SIZE - 2.0, 2.0))
    _draw_chevron(painter)
    painter.end()
    return QtGui.QIcon(pixmap)


def eraser_icon():
    """橡皮擦：参考图第 3 格。

    主体和孔用的都是从参考图上逐行取边界提出来的轮廓：主体是一个
    斜放的块，左下有一个斜方形的小孔，孔不到底边。
    """
    pixmap, painter = _icon_painter()
    painter.save()
    painter.translate(ICON_INSET, 0)
    _fill_outline(painter, _ERASER_BODY, INK)
    # 孔：在透明画布上用 Clear 挖出来，颜色不会被"背景色"污染
    painter.setCompositionMode(
        QtGui.QPainter.CompositionMode.CompositionMode_Clear)
    painter.setPen(Qt.PenStyle.NoPen)
    painter.setBrush(QtGui.QBrush(QtGui.QColor(0, 0, 0)))
    painter.drawPolygon(_polygon(_ERASER_HOLE))
    painter.restore()
    painter.end()
    return QtGui.QIcon(pixmap)


def width_icon():
    """粗细：三条线 —— 细 / 中 / 粗，照参考图第 6 格。

    参考图实测：19 宽，y 分别是 1 像素、2 像素、4 像素粗，间距 6 / 8。
    """
    pixmap, painter = _icon_painter(MENU_BUTTON_WIDTH)
    painter.setPen(Qt.PenStyle.NoPen)
    painter.setBrush(QtGui.QBrush(INK))
    left = ICON_INSET + 2.5
    width = 19.0
    painter.drawRect(QtCore.QRectF(left, 3.0, width, 1.2))
    painter.drawRect(QtCore.QRectF(left, 9.0, width, 2.2))
    painter.drawRoundedRect(QtCore.QRectF(left, 17.0, width, 4.4), 2.2, 2.2)
    _draw_chevron(painter, MENU_BUTTON_WIDTH)
    painter.end()
    return QtGui.QIcon(pixmap)


def arrow_icon():
    """线型：实线 / 虚线 / 箭头 —— 照参考图第 7 格。

    参考图实测：19 宽，第一条实线 2 像素、第二条虚线三段、第三条
    带右箭头（杆 2 像素、三角约 8 像素高）。
    """
    pixmap, painter = _icon_painter(MENU_BUTTON_WIDTH)
    painter.setPen(Qt.PenStyle.NoPen)
    painter.setBrush(QtGui.QBrush(INK))
    left = ICON_INSET + 2.5
    width = 20.0
    # 实线
    painter.drawRect(QtCore.QRectF(left, 2.4, width, 2.2))
    # 虚线（三段）
    dash = 5.0
    gap = 3.4
    x = left
    while x < left + width - 1.0:
        painter.drawRect(QtCore.QRectF(x, 8.4, min(dash, left + width - x), 2.2))
        x += dash + gap
    # 箭头：横杆 + 三角
    painter.drawRect(QtCore.QRectF(left, 16.9, width - 4.0, 2.2))
    painter.drawPolygon(QtGui.QPolygonF([
        QtCore.QPointF(left + width, 18.0),
        QtCore.QPointF(left + width - 7.4, 14.4),
        QtCore.QPointF(left + width - 7.4, 21.6)]))
    _draw_chevron(painter, MENU_BUTTON_WIDTH)
    painter.end()
    return QtGui.QIcon(pixmap)


def close_icon():
    """关闭：一个叉。参考图实测 16x17、线宽 3、颜色和图标同色。"""
    pixmap, painter = _icon_painter()
    painter.setPen(_stroke_pen(INK, 3.0))
    left = ICON_INSET + 3.5
    top = 3.0
    size = 16.0
    painter.drawLine(QtCore.QPointF(left, top),
                     QtCore.QPointF(left + size, top + size))
    painter.drawLine(QtCore.QPointF(left + size, top),
                     QtCore.QPointF(left, top + size))
    painter.end()
    return QtGui.QIcon(pixmap)


def _style_swatch(line_style, width=2.0):
    """「直线」下拉里的一行：那条线按选中的线型画出来。"""
    pixmap, painter = _new_canvas(58, 18)
    pen = _stroke_pen(INK, max(1.2, float(width)))
    pen.setStyle(LINE_STYLE_PENS.get(line_style, Qt.PenStyle.SolidLine))
    painter.setPen(pen)
    painter.drawLine(QtCore.QPointF(6, 9), QtCore.QPointF(52, 9))
    painter.end()
    return QtGui.QIcon(pixmap)


def _width_swatch(width):
    """「粗细」下拉里的一行：那个粗细的线。"""
    pixmap, painter = _new_canvas(58, 20)
    painter.setPen(_stroke_pen(INK, max(1.0, float(width))))
    painter.drawLine(QtCore.QPointF(6, 10), QtCore.QPointF(52, 10))
    painter.end()
    return QtGui.QIcon(pixmap)


class ColourButton(QtWidgets.QToolButton):
    """颜色按钮：一支笔，**下面一条**显示当前颜色。

    参考图里就是这个样子 —— 白色钢笔，底下那条跟着当前颜色走，
    条本身 25x6、上下各 1 像素暗边。
    """

    #: 色条的位置和大小，全部照参考图量的。
    BAR_WIDTH = 25
    BAR_HEIGHT = 6

    def __init__(self, parent=None):
        super().__init__(parent)
        self._colour = QtGui.QColor('#ff5050')
        self.setIconSize(QtCore.QSize(BUTTON_SIZE, ICON_BOX_H))
        self.setFixedSize(BUTTON_SIZE, BUTTON_SIZE)
        self.setToolTip(_('Colour'))
        self._refresh_icon()

    def _refresh_icon(self):
        pixmap, painter = _icon_painter()
        _draw_nib_pen(painter, INK)
        painter.end()
        self.setIcon(QtGui.QIcon(pixmap))

    def set_colour(self, colour):
        parsed = QtGui.QColor(str(colour))
        self._colour = parsed if parsed.isValid() else QtGui.QColor('#ff5050')
        self.update()

    def colour(self):
        return self._colour.name()

    def paintEvent(self, event):
        super().paintEvent(event)
        painter = QtGui.QPainter(self)
        painter.setRenderHint(QtGui.QPainter.RenderHint.Antialiasing)
        bar = QtCore.QRectF(
            (self.width() - self.BAR_WIDTH) / 2.0,
            self.height() - 10.0,
            self.BAR_WIDTH, self.BAR_HEIGHT)
        # 参考图里色条上下各有一条 1 像素的暗边
        border = self._colour.darker(int(100 / SWATCH_BORDER))
        painter.setPen(QtGui.QPen(border, 1))
        painter.setBrush(QtGui.QBrush(self._colour))
        painter.drawRect(bar)
        painter.end()


class DrawingToolbar(QtWidgets.QFrame):
    """画布顶部的悬浮工具条。

    它只负责"长什么样"和"用户点了哪个按钮" —— 具体怎么画、怎么存，
    交给 `view.py` 里的绘制逻辑。两者用信号连接，工具条不碰场景。
    """

    tool_changed = QtCore.pyqtSignal(str)          # 'pen' / 'line' / 'eraser'
    colour_requested = QtCore.pyqtSignal()
    width_changed = QtCore.pyqtSignal(float)
    arrow_changed = QtCore.pyqtSignal(str)
    line_style_changed = QtCore.pyqtSignal(str)    # 'solid' / 'dashed' / 'dotted'
    close_requested = QtCore.pyqtSignal()

    #: 粗细菜单里的预设（参考图是六个）。
    WIDTHS = (1, 2, 4, 8, 16, 24)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName('drawBar')
        self.setFrameShape(QtWidgets.QFrame.Shape.NoFrame)
        # 不接受焦点 —— 不然点按钮会把画布的键盘焦点抢走（Delete 就删不掉
        # 选中的东西了）。
        self.setFocusPolicy(QtCore.Qt.FocusPolicy.NoFocus)
        # 配色照参考图：底 #222222、选中块 #4b4b4b、圆角 14。
        self.setStyleSheet('''
            QFrame#drawBar {
                background: #222222;
                border: none;
                border-radius: 14px;
            }
            QToolButton {
                background: transparent;
                border: none;
                border-radius: 6px;
            }
            QToolButton:hover { background: #383838; }
            QToolButton:checked { background: #4b4b4b; }
            QToolButton:pressed { background: #565656; }
            QToolButton::menu-indicator { image: none; width: 0px; }
        ''')
        self._tool_buttons = {}
        self._build()

    # ── 搭界面 ──────────────────────────────────────────────────────

    def _separator(self):
        line = QtWidgets.QFrame()
        line.setFrameShape(QtWidgets.QFrame.Shape.VLine)
        line.setFixedWidth(1)
        # 参考图里那根竖线：`#5f5f5f`、约 31 像素高。
        line.setFixedHeight(31)
        line.setStyleSheet('background: #5f5f5f; border: none;')
        return line

    def _tool_button(self, key, icon, tip):
        """不加下拉的工具按钮（画笔 / 橡皮）。"""
        button = QtWidgets.QToolButton()
        button.setIconSize(QtCore.QSize(BUTTON_SIZE, ICON_BOX_H))
        button.setIcon(icon)
        button.setCheckable(True)
        button.setAutoExclusive(False)      # 工具的互斥由 view 那边统一管
        button.setFixedSize(BUTTON_SIZE, BUTTON_SIZE)
        button.setToolTip(tip)
        button.clicked.connect(
            lambda _checked=False, name=key: self.tool_changed.emit(name))
        self._tool_buttons[key] = button
        return button

    def _menu_button(self, icon, tip, fill_menu, on_open=None):
        """带 `▾` 的按钮：点一下弹菜单。

        `on_open` 在菜单弹出**之前**跑 —— 「直线」用它顺手把直线工具
        选中，因为那个按钮既是工具又是它自己的选项入口。
        """
        button = QtWidgets.QToolButton()
        button.setIconSize(QtCore.QSize(MENU_BUTTON_WIDTH, ICON_BOX_H))
        button.setIcon(icon)
        button.setFixedSize(MENU_BUTTON_WIDTH, BUTTON_SIZE)
        button.setToolTip(tip)
        button.setPopupMode(
            QtWidgets.QToolButton.ToolButtonPopupMode.InstantPopup)
        menu = QtWidgets.QMenu(button)
        fill_menu(menu)
        if on_open is not None:
            menu.aboutToShow.connect(on_open)
        button.setMenu(menu)
        return button

    def _build(self):
        row = QtWidgets.QHBoxLayout(self)
        row.setContentsMargins(BAR_MARGIN_H, BAR_MARGIN_V,
                               BAR_MARGIN_H, BAR_MARGIN_V)
        row.setSpacing(ROW_SPACING)

        # 画笔 · 直线▾ · 橡皮
        row.addWidget(self._tool_button('pen', pen_icon(), _('Freehand pen')))

        line_menu_button = self._menu_button(
            line_icon(), _('Straight line'),
            self._fill_line_menu,
            on_open=lambda: self.tool_changed.emit('line'))
        # 「直线」本身也是工具按钮，要能显示选中态
        line_menu_button.setCheckable(True)
        self._tool_buttons['line'] = line_menu_button
        row.addWidget(line_menu_button)

        row.addWidget(self._tool_button(
            'eraser', eraser_icon(), _('Eraser (removes whole strokes)')))
        row.addWidget(self._separator())

        # 颜色
        self.colour_button = ColourButton()
        self.colour_button.clicked.connect(self.colour_requested.emit)
        row.addWidget(self.colour_button)

        # 粗细▾
        self.width_button = self._menu_button(
            width_icon(), _('Line width'), self._fill_width_menu)
        row.addWidget(self.width_button)

        # 线型▾（实线 / 虚线 / 箭头）
        self.arrow_button = self._menu_button(
            arrow_icon(), _('Line ends / arrows'), self._fill_arrow_menu)
        row.addWidget(self.arrow_button)

        # 关闭
        close_button = QtWidgets.QToolButton()
        close_button.setIconSize(QtCore.QSize(BUTTON_SIZE, ICON_BOX_H))
        close_button.setIcon(close_icon())
        close_button.setFixedSize(BUTTON_SIZE, BUTTON_SIZE)
        close_button.setToolTip(_('Leave drawing mode'))
        close_button.clicked.connect(self.close_requested.emit)
        row.addWidget(close_button)

    # ── 各个下拉里的内容 ────────────────────────────────────────────

    def _fill_line_menu(self, menu):
        for key, label in ((LINE_SOLID, _('Solid line')),
                           (LINE_DASHED, _('Dashed line')),
                           (LINE_DOTTED, _('Dotted line'))):
            action = menu.addAction(_style_swatch(key), label)
            action.triggered.connect(
                lambda _checked=False, name=key:
                self.line_style_changed.emit(name))

    def _fill_width_menu(self, menu):
        for width in self.WIDTHS:
            action = menu.addAction(
                _width_swatch(width), _('{width} px').format(width=width))
            action.triggered.connect(
                lambda _checked=False, value=width:
                self.width_changed.emit(float(value)))

    def _fill_arrow_menu(self, menu):
        for key, label in ((ARROW_NONE, _('Plain line')),
                           (ARROW_START, _('Arrow at the start')),
                           (ARROW_END, _('Arrow at the end')),
                           (ARROW_BOTH, _('Arrows at both ends'))):
            action = menu.addAction(label)
            action.triggered.connect(
                lambda _checked=False, name=key: self.arrow_changed.emit(name))

    # ── 给 view 用的接口 ────────────────────────────────────────────

    def set_tool(self, tool):
        """点亮当前工具。互斥由**这里**保证 —— 亮一个，灭其余。"""
        for key, button in self._tool_buttons.items():
            button.blockSignals(True)
            button.setChecked(key == tool)
            button.blockSignals(False)

    def set_colour(self, colour):
        self.colour_button.set_colour(colour)

    def set_width(self, width):
        """只影响提示文字 —— 图标是固定的三条线。"""
        self.width_button.setToolTip(
            _('Line width: {width} px').format(width=width))

    def set_arrow(self, arrow):
        self.arrow_button.setToolTip(
            _('Line ends / arrows: {style}').format(style=arrow))
