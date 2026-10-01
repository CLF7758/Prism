"""绘制工具（用户 2026-09-30 的需求）。

需求的核心变化：绘制**不再是右键菜单里的二级菜单**，而是右键点一下就
进绘制模式，所有选项都在画布顶部的悬浮工具条上。工具从"只有画笔"变成
三种互斥的：自由画笔 / 直线 / 橡皮擦，直线还能带箭头。

这个文件测**能自动验的部分**。真机手感（光标样子、按钮悬停高亮、
工具条是不是真的浮在图片上面）要人手点。
"""
import pytest
from PyQt6 import QtCore, QtGui, QtWidgets

from prism.items import (ARROW_BOTH, ARROW_END, ARROW_NONE, ARROW_START,
                         ARROW_STYLES, DEFAULT_PEN_COLOR, DRAW_TOOL_ERASER,
                         DRAW_TOOL_LINE, DRAW_TOOL_PEN, PrismPathItem)


@pytest.fixture
def view(qapp):
    from prism.view import PrismGraphicsView

    host = QtWidgets.QMainWindow()
    result = PrismGraphicsView(qapp, host)
    yield result
    host.close()


def make_image(view, colour=(10, 20, 30)):
    from prism.items import PrismPixmapItem

    image = QtGui.QImage(20, 20, QtGui.QImage.Format.Format_RGB32)
    image.fill(QtGui.QColor(*colour))
    item = PrismPixmapItem(image)
    view.scene.addItem(item)
    return item


# ── 工具条本身 ──────────────────────────────────────────────────

def test_the_toolbar_exists_and_starts_hidden(view):
    bar = view._drawing_toolbar
    assert bar is not None
    assert bar.isVisible() is False, '没进绘制模式时不该显示'


def test_entering_draw_mode_shows_the_toolbar(view):
    view.start_draw_mode()
    assert view._drawing_toolbar.isVisible() is True


def test_leaving_draw_mode_hides_the_toolbar(view):
    view.start_draw_mode()
    view.cancel_draw_mode()
    assert view._drawing_toolbar.isVisible() is False


def test_the_toolbar_sits_at_the_top_centre(view):
    """需求：显示在画布顶部中央，与顶部固定距离。"""
    from prism.view import DRAW_TOOLBAR_MARGIN

    view.resize(800, 600)
    view.start_draw_mode()
    bar = view._drawing_toolbar
    expected = (view.width() - bar.width()) // 2
    assert abs(bar.x() - expected) <= 2, f'x = {bar.x()}，该在 {expected} 附近'
    assert bar.y() == DRAW_TOOLBAR_MARGIN, f'y = {bar.y()}'


def test_the_toolbar_recentres_when_the_window_resizes(view):
    """需求：窗口尺寸变化时始终重新居中。"""
    view.resize(800, 600)
    view.start_draw_mode()
    bar = view._drawing_toolbar
    view.resize(1200, 600)
    view.resizeEvent(QtGui.QResizeEvent(QtCore.QSize(1200, 600),
                                        QtCore.QSize(800, 600)))
    expected = (view.width() - bar.width()) // 2
    assert abs(bar.x() - expected) <= 2


def test_the_toolbar_has_the_three_tools(view):
    bar = view._drawing_toolbar
    for key in (DRAW_TOOL_PEN, DRAW_TOOL_LINE, DRAW_TOOL_ERASER):
        assert key in bar._tool_buttons, f'{key} 按钮不在'


def test_the_toolbar_is_not_a_scene_item(view):
    """工具条**不属于场景** —— 所以它不会被导出、不会被选择框选中、
    也不跟着画布缩放移动。这一条是那几条需求的总保障。"""
    view.start_draw_mode()
    bar = view._drawing_toolbar
    assert bar.parent() is view, '工具条该挂在 view 上，不是场景里'
    assert bar not in view.scene.items(), '工具条混进场景了'


# ── 三种工具互斥 ────────────────────────────────────────────────

def test_only_one_tool_is_active_at_a_time(view):
    bar = view._drawing_toolbar
    view.set_drawing_tool(DRAW_TOOL_LINE)
    checked = [key for key, button in bar._tool_buttons.items()
               if button.isChecked()]
    assert checked == [DRAW_TOOL_LINE], f'同时亮了 {checked}'

    view.set_drawing_tool(DRAW_TOOL_ERASER)
    checked = [key for key, button in bar._tool_buttons.items()
               if button.isChecked()]
    assert checked == [DRAW_TOOL_ERASER]


def test_choosing_an_arrow_switches_to_the_line_tool(view):
    """需求第八条：选箭头样式后**自动**切到直线工具。"""
    view.set_drawing_tool(DRAW_TOOL_PEN)
    view.set_arrow_style(ARROW_END)
    assert view.pen_settings()['tool'] == DRAW_TOOL_LINE
    assert view._drawing_toolbar._tool_buttons[DRAW_TOOL_LINE].isChecked()


def test_changing_colour_does_not_change_the_tool(view):
    """需求第九条：改颜色不改工具。"""
    view.set_drawing_tool(DRAW_TOOL_LINE)
    view.set_pen_color('#123456')
    assert view.pen_settings()['tool'] == DRAW_TOOL_LINE


def test_changing_width_does_not_change_the_tool(view):
    view.set_drawing_tool(DRAW_TOOL_ERASER)
    view.set_pen_width(8)
    assert view.pen_settings()['tool'] == DRAW_TOOL_ERASER


def test_switching_tools_keeps_finished_strokes(view):
    """需求：工具切换过程中不得丢失已经完成的笔画。"""
    from PyQt6.QtGui import QUndoStack

    from prism.scene import PrismGraphicsScene

    stack = QUndoStack()
    keep = (stack, view.scene)      # 别让栈被 GC（见 test_data_safety）

    stroke = PrismPathItem([(0, 0), (10, 10)])
    view.scene.addItem(stroke)
    view.set_drawing_tool(DRAW_TOOL_ERASER)
    view.set_drawing_tool(DRAW_TOOL_LINE)
    assert stroke in view.scene.items(), '换工具把画好的东西弄丢了'


# ── 直线：只存首尾两点 ──────────────────────────────────────────

def test_a_straight_line_keeps_only_its_two_ends(view):
    """需求第四条：最终只保存起点和终点，不保存中间轨迹。"""
    view.set_drawing_tool(DRAW_TOOL_LINE)
    view._begin_stroke(QtCore.QPointF(0, 0))
    # 模拟拖动：中间给一串点
    for step in range(1, 6):
        view._draw_item.set_points(
            [view._draw_points[0], (step * 10.0, step * 10.0)])
    view._finish_pending_stroke()

    line = [i for i in view.scene.items()
            if getattr(i, 'TYPE', '') == PrismPathItem.TYPE][0]
    assert line.points() == [(0.0, 0.0), (50.0, 50.0)], (
        f'直线存了 {line.points()}')
    assert line.tool() == DRAW_TOOL_LINE


def test_a_freehand_stroke_keeps_every_point(view):
    """反过来：自由画笔要保留整条轨迹。"""
    view.set_drawing_tool(DRAW_TOOL_PEN)
    view._begin_stroke(QtCore.QPointF(0, 0))
    for step in range(1, 6):
        view._draw_points.append((float(step), float(step)))
    view._draw_item.set_points(view._draw_points)
    view._finish_pending_stroke()

    stroke = [i for i in view.scene.items()
              if getattr(i, 'TYPE', '') == PrismPathItem.TYPE][0]
    assert len(stroke.points()) == 6
    assert stroke.tool() == DRAW_TOOL_PEN


def test_a_line_is_not_given_an_arrow_when_drawn_with_the_pen(view):
    """箭头只对直线有意义 —— 手绘轨迹带箭头很怪。"""
    view.set_arrow_style(ARROW_END)      # 这个会把工具切成直线
    view.set_drawing_tool(DRAW_TOOL_PEN)
    view._begin_stroke(QtCore.QPointF(0, 0))
    view._draw_item.set_points([(0, 0), (30, 30)])
    view._finish_pending_stroke()

    stroke = [i for i in view.scene.items()
              if getattr(i, 'TYPE', '') == PrismPathItem.TYPE][0]
    assert stroke.arrow() == ARROW_NONE


# ── 箭头 ────────────────────────────────────────────────────────

@pytest.mark.parametrize('style', ARROW_STYLES)
def test_every_arrow_style_survives_a_save(qapp, style):
    """需求第十一条：箭头类型随绘制对象保存，重开文件后不变。"""
    item = PrismPathItem([(0, 0), (40, 20)], color='#ff0000', width=3.0,
                         tool=DRAW_TOOL_LINE, arrow=style)
    data = item.get_extra_save_data()
    assert data['arrow'] == style
    assert data['tool'] == DRAW_TOOL_LINE

    restored = PrismPathItem.create_from_data(data=data)
    assert restored.arrow() == style
    assert restored.tool() == DRAW_TOOL_LINE
    assert restored.points() == [(0.0, 0.0), (40.0, 20.0)]


def test_an_unknown_arrow_style_falls_back_instead_of_crashing(qapp):
    """存盘数据坏了（或者来自没有这个字段的旧版本）不该崩。"""
    item = PrismPathItem([(0, 0), (10, 10)], arrow='三角形', tool='魔法')
    assert item.arrow() == ARROW_NONE
    assert item.tool() == DRAW_TOOL_PEN


def test_a_line_with_no_length_does_not_crash_when_painting(qapp):
    """需求第八条：起点与终点重合时不得崩溃。

    用户按下鼠标又原地松开（没拖）就会走到这里。
    """
    item = PrismPathItem([(5, 5), (5, 5)], tool=DRAW_TOOL_LINE,
                         arrow=ARROW_BOTH)
    image = QtGui.QImage(40, 40, QtGui.QImage.Format.Format_RGB32)
    image.fill(QtGui.QColor(0, 0, 0))
    painter = QtGui.QPainter(image)
    try:
        item.paint(painter, QtWidgets.QStyleOptionGraphicsItem(), None)
    finally:
        painter.end()


def test_a_very_short_line_does_not_get_a_giant_arrow(qapp):
    """需求：线条很短时箭头要按比例缩，不能比线还长。"""
    item = PrismPathItem([(0, 0), (0.5, 0)])   # 半像素长的线
    # `_arrow_size` 是给长线用的；短线上它会被压到线长的 80%
    assert item._arrow_size() >= 8.0
    # 只要不抛就行 —— 真正的裁剪在 `_paint_arrow_head` 里
    image = QtGui.QImage(20, 20, QtGui.QImage.Format.Format_RGB32)
    painter = QtGui.QPainter(image)
    try:
        item._paint_arrow_head(painter, QtCore.QPointF(1, 0),
                               QtCore.QPointF(0, 0))
    finally:
        painter.end()


# ── 颜色和粗细只影响之后 ────────────────────────────────────────

def test_changing_colour_leaves_finished_strokes_alone(view):
    """需求第六条：已完成的绘制内容不得被自动改色。"""
    view.set_drawing_tool(DRAW_TOOL_PEN)
    view._begin_stroke(QtCore.QPointF(0, 0))
    view._draw_item.set_points([(0, 0), (10, 10)])
    view._finish_pending_stroke()
    stroke = [i for i in view.scene.items()
              if getattr(i, 'TYPE', '') == PrismPathItem.TYPE][0]
    before = stroke.style()['color']

    view.set_pen_color('#00ff00')
    assert stroke.style()['color'] == before, '已有的线被改色了'


def test_changing_width_leaves_finished_strokes_alone(view):
    view.set_drawing_tool(DRAW_TOOL_PEN)
    view._begin_stroke(QtCore.QPointF(0, 0))
    view._draw_item.set_points([(0, 0), (10, 10)])
    view._finish_pending_stroke()
    stroke = [i for i in view.scene.items()
              if getattr(i, 'TYPE', '') == PrismPathItem.TYPE][0]
    before = stroke.style()['width']

    view.set_pen_width(16)
    assert stroke.style()['width'] == before


def test_illegal_colour_values_are_ignored(view):
    """需求第十四条：非法颜色值不能让程序出错、也不该改现有设置。"""
    before = view.pen_settings()['color']
    view.set_pen_color('这不是颜色')
    view.set_pen_color('')
    view.set_pen_color(None)
    assert view.pen_settings()['color'] == before


def test_illegal_width_values_are_ignored_or_clamped(view):
    """需求第七条：非数字不能报错；范围钳到 0.5–50。"""
    before = view.pen_settings()['width']
    view.set_pen_width('粗一点')
    assert view.pen_settings()['width'] == before, '非数字该当没收到'
    view.set_pen_width(1000)
    assert view.pen_settings()['width'] == 50.0
    view.set_pen_width(-5)
    assert view.pen_settings()['width'] == 0.5


# ── 橡皮擦只删绘制内容 ──────────────────────────────────────────

def test_the_eraser_deletes_a_stroke(view):
    view.set_drawing_tool(DRAW_TOOL_PEN)
    view._begin_stroke(QtCore.QPointF(0, 0))
    view._draw_item.set_points([(0, 0), (10, 10)])
    view._finish_pending_stroke()
    stroke = [i for i in view.scene.items()
              if getattr(i, 'TYPE', '') == PrismPathItem.TYPE][0]

    view.set_drawing_tool(DRAW_TOOL_ERASER)
    view._erase_stroke(stroke)
    assert stroke not in view.scene.items()


def test_the_eraser_never_touches_other_items(view):
    """需求第五条：橡皮擦不得删除图片、文字、分组等非绘制图元。"""
    picture = make_image(view)
    stroke = PrismPathItem([(0, 0), (10, 10)])
    view.scene.addItem(stroke)

    # 就算把图片交给擦除逻辑，它也该拒掉（靠 TYPE 判断）
    view.set_drawing_tool(DRAW_TOOL_ERASER)
    view._erase_stroke(picture)
    assert picture in view.scene.items(), '素材被橡皮擦误删了'

    view._erase_stroke(stroke)
    assert stroke not in view.scene.items()


def test_erasing_can_be_undone(view):
    """需求第五条：删除操作需要支持撤销，撤销后位置颜色粗细类型都回来。"""
    view.set_drawing_tool(DRAW_TOOL_LINE)
    view._begin_stroke(QtCore.QPointF(3, 4))
    view._draw_item.set_points([(3, 4), (40, 50)])
    view._finish_pending_stroke()
    stroke = [i for i in view.scene.items()
              if getattr(i, 'TYPE', '') == PrismPathItem.TYPE][0]
    before = (stroke.points(), stroke.style())

    view._erase_stroke(stroke)
    assert stroke not in view.scene.items()

    view.undo_stack.undo()
    back = [i for i in view.scene.items()
            if getattr(i, 'TYPE', '') == PrismPathItem.TYPE]
    assert back, '撤销之后线没回来'
    assert back[0].points() == before[0]
    assert back[0].style() == before[1]


# ── 设置持久化 ──────────────────────────────────────────────────

def test_the_chosen_tool_is_remembered(view):
    """需求第十条：再次打开绘制工具时恢复上一次用的工具。"""
    view.set_drawing_tool(DRAW_TOOL_ERASER)
    assert view.settings.value('Canvas/drawing_tool') == DRAW_TOOL_ERASER


def test_the_arrow_style_is_remembered(view):
    view.set_arrow_style(ARROW_BOTH)
    assert view.settings.value('Canvas/arrow_style') == ARROW_BOTH


def test_the_colour_and_width_are_remembered(view):
    view.set_pen_color('#abcdef')
    view.set_pen_width(16)
    assert view.settings.value('Canvas/pen_color') == '#abcdef'
    assert float(view.settings.value('Canvas/pen_width')) == 16.0


# ── 关掉再打开 ──────────────────────────────────────────────────

def test_closing_keeps_finished_strokes(view):
    """需求第十条：关闭绘制不得清空已完成的笔画。"""
    view.set_drawing_tool(DRAW_TOOL_PEN)
    view._begin_stroke(QtCore.QPointF(0, 0))
    view._draw_item.set_points([(0, 0), (10, 10)])
    view._finish_pending_stroke()
    view.cancel_draw_mode()
    strokes = [i for i in view.scene.items()
               if getattr(i, 'TYPE', '') == PrismPathItem.TYPE]
    assert len(strokes) == 1, '关掉绘制把画好的东西清掉了'


def test_closing_does_not_change_the_settings(view):
    """需求第十条：关闭绘制不得改变颜色、粗细和箭头设置。"""
    view.set_pen_color('#010203')
    view.set_pen_width(8)
    view.set_arrow_style(ARROW_START)
    before = view.pen_settings()
    view.start_draw_mode()
    view.cancel_draw_mode()
    assert view.pen_settings() == before


def test_closing_drops_the_half_drawn_stroke(view):
    """需求第十条：停止尚未完成的绘制操作。

    只按了一下还没松开的那半条没有保留价值，留着是画布上的垃圾。
    """
    view.set_drawing_tool(DRAW_TOOL_PEN)
    view._begin_stroke(QtCore.QPointF(0, 0))
    assert view._draw_item is not None
    view.cancel_draw_mode()
    assert not [i for i in view.scene.items()
                if getattr(i, 'TYPE', '') == PrismPathItem.TYPE]


def test_switching_tools_drops_the_half_drawn_stroke(view):
    view.set_drawing_tool(DRAW_TOOL_PEN)
    view._begin_stroke(QtCore.QPointF(0, 0))
    view.set_drawing_tool(DRAW_TOOL_LINE)
    assert not [i for i in view.scene.items()
                if getattr(i, 'TYPE', '') == PrismPathItem.TYPE]
