"""绘制工具条：结构、线型下拉，以及两个"画不出来"缺陷的回归。

缺陷背景（都在本轮修掉）：

1. `mousePressEvent` 里鼠标动作（平移/缩放）的判断排在绘制模式**之前**，
   用户一旦把左键绑成平移或缩放，`active_mode` 就被改掉，画笔、直线、
   箭头全部画不出东西。
2. 绘制模式下用中键拖了一下画布，松开后 `active_mode` 被清成 None，
   而工具栏还挂在画布上 —— 接着按左键只会拉出橡皮筋选择框，松开什么都
   不剩，看起来就是"画一笔就消失"。
"""
import pytest
from PyQt6 import QtCore
from PyQt6.QtCore import Qt
from PyQt6.QtTest import QTest

from prism.items import (ARROW_BOTH, ARROW_END, ARROW_NONE, ARROW_START,
                         LINE_DASHED, LINE_DOTTED, LINE_SOLID, LINE_STYLES,
                         PrismPathItem)
from prism.view import DRAW_TOOL_LINE, DRAW_TOOL_PEN, DRAW_TOOL_ERASER


@pytest.fixture
def bar(view):
    """进入绘制模式，返回工具条。"""
    view.start_draw_mode()
    view.set_drawing_tool(DRAW_TOOL_PEN)
    yield view._drawing_toolbar
    view.cancel_draw_mode()


@pytest.fixture(autouse=True)
def clean_paths(view):
    """测试之间把画出来的线清干净，免得互相干扰。"""
    yield
    for item in list(view.scene.items()):
        if getattr(item, 'TYPE', '') == PrismPathItem.TYPE:
            view.scene.removeItem(item)


def paths(view):
    return [i for i in view.scene.items()
            if getattr(i, 'TYPE', '') == PrismPathItem.TYPE]


def drag(view, start, offsets, button=Qt.MouseButton.LeftButton,
         mods=Qt.KeyboardModifier.NoModifier):
    """在画布上按下一路拖过去再松开，返回新画出来的图元（没有就是 None）。"""
    before = {id(i) for i in paths(view)}
    vp = view.viewport()
    QTest.mousePress(vp, button, mods, QtCore.QPoint(*start))
    for dx in offsets:
        QTest.mouseMove(vp, QtCore.QPoint(start[0] + dx, start[1] + dx))
    QTest.mouseRelease(vp, button, mods,
                       QtCore.QPoint(start[0] + offsets[-1],
                                     start[1] + offsets[-1]))
    fresh = [i for i in paths(view) if id(i) not in before]
    return fresh[0] if fresh else None


# ── 工具条结构 ───────────────────────────────────────────────

def test_toolbar_has_the_three_tools(bar):
    assert set(bar._tool_buttons) == {'pen', 'line', 'eraser'}


def test_line_button_carries_the_line_style_menu(bar):
    menu = bar._tool_buttons['line'].menu()
    assert menu is not None
    assert len(menu.actions()) == len(LINE_STYLES)


def test_line_style_menu_entries_show_a_preview(bar):
    for action in bar._tool_buttons['line'].menu().actions():
        assert not action.icon().isNull(), action.text()


def test_width_menu_keeps_the_six_presets(bar):
    assert len(bar.width_button.menu().actions()) == len(bar.WIDTHS)


def test_arrow_menu_keeps_the_four_endings(bar):
    assert len(bar.arrow_button.menu().actions()) == 4


def test_opening_the_line_menu_picks_the_line_tool(view, bar):
    """那个按钮既是工具又是它自己的选项入口。"""
    view.set_drawing_tool(DRAW_TOOL_PEN)
    bar._tool_buttons['line'].menu().aboutToShow.emit()
    assert view._drawing_tool == DRAW_TOOL_LINE


def test_only_one_tool_button_is_lit(bar):
    bar.set_tool(DRAW_TOOL_ERASER)
    lit = [k for k, b in bar._tool_buttons.items() if b.isChecked()]
    assert lit == [DRAW_TOOL_ERASER]


# ── 线型 ─────────────────────────────────────────────────────

def test_line_style_reaches_the_item_drawn(view, bar):
    view.set_line_style(LINE_DASHED)
    made = drag(view, (300, 300), (20, 40, 60))
    assert made is not None
    assert made.style()['line_style'] == LINE_DASHED


def test_each_line_style_sets_the_matching_qt_pen(view, bar):
    expected = {
        LINE_SOLID: Qt.PenStyle.SolidLine,
        LINE_DASHED: Qt.PenStyle.DashLine,
        LINE_DOTTED: Qt.PenStyle.DotLine,
    }
    for style, pen_style in expected.items():
        view.set_line_style(style)
        made = drag(view, (300, 300), (20, 40, 60))
        assert made is not None
        assert made.pen().style() == pen_style, style


def test_choosing_a_line_style_does_not_switch_tool(view, bar):
    """线型对画笔也成立，不该把画笔换成直线。"""
    view.set_drawing_tool(DRAW_TOOL_PEN)
    view.set_line_style(LINE_DOTTED)
    assert view._drawing_tool == DRAW_TOOL_PEN


def test_unknown_line_style_is_ignored(view, bar):
    view.set_line_style(LINE_DASHED)
    view.set_line_style('wobbly')
    assert view._line_style == LINE_DASHED


def test_line_style_is_remembered_in_settings(view, bar, settings):
    view.set_line_style(LINE_DOTTED)
    assert view.settings.value('Canvas/line_style') == LINE_DOTTED


@pytest.mark.parametrize('arrow', [ARROW_START, ARROW_END, ARROW_BOTH])
def test_arrow_styles_reach_the_item(view, bar, arrow):
    made = drag(view, (300, 300), (20, 40, 60))
    view.set_arrow_style(arrow)
    made = drag(view, (200, 200), (20, 40, 60))
    assert made is not None
    assert made.arrow() == arrow


def test_a_freehand_stroke_never_wears_an_arrow(view, bar):
    """箭头只对直线有意义 —— 手绘轨迹带箭头很怪。"""
    made = drag(view, (300, 300), (20, 40, 60))
    assert made is not None
    assert made.arrow() == ARROW_NONE


# ── 存盘往返 ─────────────────────────────────────────────────

def test_line_style_survives_save_and_load():
    original = PrismPathItem([[0, 0], [10, 10]], color='#123456',
                             width=3.0, tool=DRAW_TOOL_LINE,
                             arrow=ARROW_END, line_style=LINE_DASHED)
    data = dict(original.get_extra_save_data())
    data['points'] = [[0, 0], [10, 10]]

    rebuilt = PrismPathItem.create_from_data(data=data)

    assert rebuilt.style()['line_style'] == LINE_DASHED
    assert rebuilt.style()['arrow'] == ARROW_END
    assert rebuilt.style()['width'] == 3.0
    assert rebuilt.pen().style() == Qt.PenStyle.DashLine


def test_a_file_without_line_style_loads_as_a_solid_line():
    """旧工程里没有这个字段，不能因此报错或者画不出来。"""
    rebuilt = PrismPathItem.create_from_data(data={
        'points': [[0, 0], [5, 5]], 'color': '#ffffff', 'width': 2.0,
        'tool': 'line', 'arrow': 'none'})
    assert rebuilt.style()['line_style'] == LINE_SOLID
    assert rebuilt.pen().style() == Qt.PenStyle.SolidLine


def test_a_corrupt_line_style_falls_back_instead_of_raising():
    rebuilt = PrismPathItem([[0, 0], [5, 5]], line_style='squiggly')
    assert rebuilt.style()['line_style'] == LINE_SOLID


# ── 缺陷一：鼠标动作不该抢走绘制模式 ─────────────────────────

def test_left_button_with_alt_still_draws(view, bar):
    """用户的设置里 `左键 + Alt` 是平移，绘制模式下要归画笔。"""
    made = drag(view, (300, 300), (20, 40, 60),
                mods=Qt.KeyboardModifier.AltModifier)
    assert made is not None, '带 Alt 的左键画不出东西'


def test_left_button_with_alt_shift_still_draws(view, bar):
    """用户的设置里 `左键 + Alt + Shift` 是缩放。"""
    made = drag(view, (300, 300), (20, 40, 60),
                mods=(Qt.KeyboardModifier.AltModifier
                      | Qt.KeyboardModifier.ShiftModifier))
    assert made is not None, '带 Alt+Shift 的左键画不出东西'


def test_drawing_mode_survives_a_left_button_press(view, bar):
    drag(view, (300, 300), (20, 40, 60), mods=Qt.KeyboardModifier.AltModifier)
    assert view.active_mode == view.DRAW_MODE


# ── 缺陷二：中键平移之后要能接着画 ───────────────────────────

def test_middle_drag_returns_to_drawing_mode(view, bar):
    drag(view, (300, 300), (20, 40, 60), button=Qt.MouseButton.MiddleButton)
    assert view.active_mode == view.DRAW_MODE, (
        '中键平移之后 active_mode 掉成 %s，工具栏还挂着但已经画不了'
        % view.active_mode)


def test_can_still_draw_after_a_middle_drag(view, bar):
    drag(view, (300, 300), (20, 40, 60), button=Qt.MouseButton.MiddleButton)
    made = drag(view, (300, 300), (20, 40, 60))
    assert made is not None, '中键平移之后画不出东西了'


def test_the_toolbar_stays_visible_after_a_middle_drag(view, bar):
    drag(view, (300, 300), (20, 40, 60), button=Qt.MouseButton.MiddleButton)
    assert bar.isVisible()


def test_middle_drag_outside_drawing_mode_leaves_it_off(view):
    """普通状态下中键平移，不该把绘制模式给弄回来。"""
    view.cancel_draw_mode()
    drag(view, (300, 300), (20, 40, 60), button=Qt.MouseButton.MiddleButton)
    assert view.active_mode is None


def test_leaving_drawing_mode_clears_the_resume_flag(view, bar):
    view.cancel_draw_mode()
    assert view._mode_before_transient is None
