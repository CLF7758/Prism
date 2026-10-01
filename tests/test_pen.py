"""画笔（用户的第 9 条：能调颜色、粗细，有橡皮擦）。

这条里最要紧的不是"能不能调" —— 而是**画完的东西存不存得下来**。

排查时确认了：画笔原来走 `scene.addPath()`，那产生的是**裸的
`QGraphicsPathItem`**，它没有 `save_id`，而 `scene.items_for_save()`
只收有 `save_id` 的。所以**画完一存盘就消失，用户白画**。

而且画完立刻把 `ItemIsSelectable` / `ItemIsMovable` 设成 False ——
画错的线既选不中也删不掉，只能撤销。

所以 `PrismPathItem` 要同时解决三件事：参与保存、能被选中、样式能存。
"""
import pytest
from PyQt6 import QtGui, QtWidgets

from prism.items import DEFAULT_PEN_COLOR, DEFAULT_PEN_WIDTH, PrismPathItem


@pytest.fixture
def scene(qapp):
    from PyQt6.QtGui import QUndoStack

    from prism.scene import PrismGraphicsScene

    stack = QUndoStack()
    result = PrismGraphicsScene(stack)
    # 留住栈，不然场景跟着崩（同 test_data_safety 里的说明）
    result._test_stack = stack
    yield result


# ── 存不存得下来（核心）────────────────────────────────────────

def test_a_stroke_is_included_in_items_for_save(scene):
    """**核心那条**：画出来的线要能被 `items_for_save()` 收。

    这条不过的话，用户画的一切在存盘时都会**静默消失** ——
    这正是改之前的状态（用的是没有 `save_id` 的裸 QGraphicsPathItem）。
    """
    item = PrismPathItem([(0, 0), (10, 10), (20, 5)])
    scene.addItem(item)
    saved = list(scene.items_for_save())
    assert item in saved, (
        '画出来的线没进保存列表 —— 存盘之后它会消失')


def test_the_stroke_survives_a_save_and_load(scene):
    """点、颜色、粗细都要能存下来、读回来。"""
    item = PrismPathItem([(1.5, 2.5), (30.0, 40.0)],
                         color='#123456', width=7.5)
    data = item.get_extra_save_data()

    assert data['color'] == '#123456'
    assert data['width'] == pytest.approx(7.5)
    assert data['points'] == [[1.5, 2.5], [30.0, 40.0]]

    restored = PrismPathItem.create_from_data(data=data)
    assert restored.points() == [(1.5, 2.5), (30.0, 40.0)]
    assert restored.style()['color'] == '#123456'
    assert restored.style()['width'] == pytest.approx(7.5)


def test_a_stroke_round_trips_through_the_database(scene, tmp_path):
    """真存一次盘再读回来 —— 上面那条只验了字典，这条走 sqlite。"""
    import json
    import sqlite3

    from prism.fileio.sql import SQLiteIO

    item = PrismPathItem([(0, 0), (5, 9)], color='#abcdef', width=3.0)
    scene.addItem(item)

    path = str(tmp_path / 'pen.prism')
    io_ = SQLiteIO(path, scene, create_new=True)
    try:
        io_.write()
    finally:
        io_._close_connection()

    connection = sqlite3.connect(path)
    try:
        rows = connection.execute('SELECT data FROM items').fetchall()
    finally:
        connection.close()
    assert rows, '没写出任何 item'
    payload = json.loads(rows[0][0])
    assert payload.get('color') == '#abcdef'
    assert payload.get('width') == pytest.approx(3.0)
    assert len(payload.get('points') or []) == 2


# ── 画完的线要能选中、能删 ─────────────────────────────────────

def test_a_finished_stroke_is_selectable(scene):
    """画错要能选、能删 —— 以前画完立刻设成不可选，只能撤销。"""
    from PyQt6.QtWidgets import QGraphicsItem

    item = PrismPathItem([(0, 0), (10, 10)])
    scene.addItem(item)
    flags = item.flags()
    assert flags & QGraphicsItem.GraphicsItemFlag.ItemIsSelectable, \
        '画完的线该能选中'
    assert flags & QGraphicsItem.GraphicsItemFlag.ItemIsMovable, \
        '画完的线该能移动'


def test_the_shape_follows_the_points(scene):
    """路径要真的按点走 —— 不然画出来是空的一条。"""
    item = PrismPathItem([(0, 0), (100, 0)])
    rect = item.boundingRect()
    assert rect.width() > 50, f'路径的包围盒才 {rect.width()} 宽'


def test_a_single_point_leaves_a_dot(scene):
    """只点一下（没有拖动）也要留下东西。

    只 moveTo 不 lineTo 的路径是空的 —— 用户点一下画布会以为画笔坏了。
    """
    item = PrismPathItem([(10, 10)])
    rect = item.boundingRect()
    assert rect.width() > 0 and rect.height() > 0, (
        f'单点该画成一个小圆点，实得包围盒 {rect}')


def test_an_empty_stroke_does_not_break(scene):
    """没有点的（理论上不该有）也不能炸。"""
    item = PrismPathItem([])
    scene.addItem(item)
    item.set_points([])
    assert item.path().elementCount() == 0


# ── 样式 ────────────────────────────────────────────────────────

def test_the_default_pen_is_the_expected_colour(scene):
    item = PrismPathItem([(0, 0), (1, 1)])
    assert item.style()['color'].lower() == DEFAULT_PEN_COLOR.lower()
    assert item.style()['width'] == pytest.approx(DEFAULT_PEN_WIDTH)


def test_changing_the_style_repaints_the_pen(scene):
    """改了颜色粗细，画笔要跟着变（不是只记在属性里）。"""
    item = PrismPathItem([(0, 0), (10, 10)])
    item.set_style(color='#00ff00', width=9.0)
    assert item.pen().color().name() == '#00ff00'
    assert item.pen().widthF() == pytest.approx(9.0)


def test_set_style_only_changes_what_is_passed(scene):
    item = PrismPathItem([(0, 0)], color='#ff0000', width=5.0)
    item.set_style(width=12.0)
    assert item.style()['color'] == '#ff0000', '颜色不该被洗掉'
    assert item.style()['width'] == pytest.approx(12.0)


def test_a_bad_width_is_clamped_not_fatal(scene):
    """粗细给个怪值也不能把画笔弄坏。"""
    item = PrismPathItem([(0, 0), (1, 1)])
    item.set_style(width=0.01)
    assert item.pen().widthF() > 0, '零宽的线画不出来'


# ── 画笔设置（颜色 / 粗细 / 橡皮擦）─────────────────────────────

@pytest.fixture
def view(qapp):
    from prism.view import PrismGraphicsView

    host = QtWidgets.QMainWindow()
    result = PrismGraphicsView(qapp, host)
    yield result
    host.close()


def test_the_view_has_pen_settings(view):
    settings = view.pen_settings()
    assert settings['color'], '该有个默认色'
    assert settings['width'] > 0
    assert settings['eraser'] is False, '默认不该是橡皮擦'


def test_pen_width_is_clamped(view):
    view.set_pen_width(1000)
    assert view.pen_settings()['width'] <= 50
    view.set_pen_width(0)
    assert view.pen_settings()['width'] >= 0.5


def test_pen_width_ignores_junk(view):
    before = view.pen_settings()['width']
    view.set_pen_width('粗一点')
    assert view.pen_settings()['width'] == before, '给怪值该当没收到'


def test_the_pen_settings_are_remembered(view):
    """下次开还记着 —— 每画一条线都要重选颜色的话没人用。"""
    view.set_pen_color('#123123')
    view.set_pen_width(11.0)
    assert view.settings.value('Canvas/pen_color') == '#123123'
    assert float(view.settings.value('Canvas/pen_width')) == \
        pytest.approx(11.0)


def test_the_eraser_toggles(view):
    """橡皮擦仍然是三选一里的一档（现在存在 `Canvas/drawing_tool`）。"""
    view.set_eraser(True)
    assert view.pen_settings()['eraser'] is True
    assert view.settings.value('Canvas/drawing_tool') == 'eraser'
    view.set_eraser(False)
    assert view.pen_settings()['eraser'] is False
    assert view.settings.value('Canvas/drawing_tool') == 'pen'


# ── 清除所有绘制 ────────────────────────────────────────────────

def test_clearing_removes_every_stroke(scene, view):
    """「清除所有绘制」要真能清掉。

    它原来靠"**不可选**"的 flag 认画笔的线 —— 那是因为画完会被设成
    不可选。现在画完的线是可选的，那个判据失效，继续用会一条也删不掉。
    """
    for points in ([(0, 0), (10, 10)], [(20, 20), (30, 30)]):
        scene.addItem(PrismPathItem(points))
    view.scene = scene
    assert len([i for i in scene.items()
                if getattr(i, 'TYPE', '') == 'path']) == 2

    view._clear_all_drawings()
    left = [i for i in scene.items()
            if getattr(i, 'TYPE', '') == 'path']
    assert not left, f'还留着 {len(left)} 条'


def test_clearing_leaves_other_items_alone(scene, view):
    """清画笔不该碰素材。"""
    from prism.items import PrismPixmapItem

    image = QtGui.QImage(20, 20, QtGui.QImage.Format.Format_RGB32)
    image.fill(QtGui.QColor(10, 20, 30))
    picture = PrismPixmapItem(image)
    scene.addItem(picture)
    scene.addItem(PrismPathItem([(0, 0), (5, 5)]))
    view.scene = scene

    view._clear_all_drawings()
    assert picture in scene.items(), '素材被误删了'
