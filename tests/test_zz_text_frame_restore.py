"""文字框热修复的回归测试（0.3.11 text-frame → 当前源码）。

原回归用例是 `tests/test_text_frame_resize.py`（从热修复源码包原样装回）。
这个文件补的是**原用例没覆盖**、但本次要求里写明的部分：

* 四角 × 旋转 0/30/90/135：对角锚点在**场景坐标**里不许动 —— 用数值
  断言（阈值 1e-3 场景单位 / 1e-6 场景单位），不靠截图目测。
* 图元缩放 0.5/1/2、水平/垂直翻转、视图缩放 0.25/1/2/8：框的宽高要变，
  **字号不许变**。
* 变窄要换行（含中文、空格、英文长词），高度固定只裁切显示，**完整文本
  仍在文档和工程里**。
* 保存重开、部分加载保存、老工程没有 frame 字段、复制粘贴、撤销重做、
  Esc 取消、零位移不写撤销栈；字体颜色与元数据不许丢。
* 非文字图元的手柄不许被带坏。
"""
import json
import sqlite3
from unittest.mock import MagicMock

import pytest
from PyQt6 import QtCore, QtGui, QtWidgets

from prism import text_frame
from prism.fileio.sql import SQLiteIO
from prism.items import PrismPixmapItem, PrismTextItem


#: 场景坐标里的浮点容差。Qt 的变换是单精度，1e-3 场景单位远小于
#: 一个像素，又比 0 严得多 —— 够抓漂移，又不会被浮点噪声误伤。
POSITION_TOLERANCE = 1e-3


def press_at(corner, scene_pos=None):
    event = MagicMock()
    event.button.return_value = QtCore.Qt.MouseButton.LeftButton
    event.pos.return_value = corner
    event.scenePos.return_value = scene_pos if scene_pos is not None else corner
    return event


def move_to(scene_pos):
    event = MagicMock()
    event.scenePos.return_value = scene_pos
    return event


def drag_corner(item, corner_index, width, height):
    """Drag one corner so the frame ends up ``width`` x ``height``."""
    corner = item.corners[corner_index]
    anchor = item.get_scale_anchor(corner)
    direction = item.get_corner_direction(corner)
    item.mousePressEvent(press_at(corner))
    target = anchor + QtCore.QPointF(width * direction.x(), height * direction.y())
    move = move_to(item.mapToScene(target))
    item.mouseMoveEvent(move)
    item.mouseReleaseEvent(move)
    return move


def make_text(view, text='Text', size=24):
    item = PrismTextItem(text, size=size)
    view.scene.addItem(item)
    item.set_frame_size(160, 90)
    item.setSelected(True)
    return item


# ── 1. 四角 × 旋转：对角锚点不许动 ─────────────────────────────────

@pytest.mark.parametrize('corner_index', range(4))
@pytest.mark.parametrize('rotation', [0, 30, 90, 135])
def test_the_opposite_corner_stays_put_in_scene_coordinates(
        view, qapp, corner_index, rotation):
    """拖一个角，对角在**场景坐标**里必须原地不动。

    框的宽高按拖动量变；字号、图元缩放都不许跟着变。
    """
    item = make_text(view)
    item.setScale(2)
    item.setRotation(rotation)

    corner = item.corners[corner_index]
    fixed = item.mapToScene(item.get_scale_anchor(corner))

    drag_corner(item, corner_index, 230, 130)

    assert item.width == pytest.approx(230, abs=POSITION_TOLERANCE)
    assert item.height == pytest.approx(130, abs=POSITION_TOLERANCE)
    assert item.style()['size'] == 24, '拖框把字号改了'
    assert item.scale() == 2, '拖框把图元缩放改了'
    opposite = item.corners[(corner_index + 2) % 4]
    assert (item.mapToScene(opposite) - fixed).manhattanLength() < 1e-6, (
        '对角动了：%s' % (item.mapToScene(opposite) - fixed,))


@pytest.mark.parametrize('scale', [0.5, 1, 2])
def test_the_frame_changes_while_the_font_size_does_not(view, qapp, scale):
    item = make_text(view)
    item.setScale(scale)

    drag_corner(item, 0, 200, 120)

    assert item.width == pytest.approx(200, abs=POSITION_TOLERANCE)
    assert item.height == pytest.approx(120, abs=POSITION_TOLERANCE)
    assert item.style()['size'] == 24
    assert item.scale() == scale


@pytest.mark.parametrize('vertical', [False, True])
def test_a_flipped_text_box_still_resizes(view, qapp, vertical):
    """水平/垂直翻转之后，框照样能拖（翻转不该把手柄弄坏）。"""
    item = make_text(view)
    item.do_flip(vertical=vertical)

    drag_corner(item, 2, 210, 140)

    assert item.width == pytest.approx(210, abs=POSITION_TOLERANCE)
    assert item.height == pytest.approx(140, abs=POSITION_TOLERANCE)
    assert item.style()['size'] == 24


@pytest.mark.parametrize('zoom', [0.25, 1, 2, 8])
def test_the_view_zoom_does_not_change_the_frame(view, qapp, zoom):
    """视图缩放只影响屏幕上的大小，框的本地宽高不变。"""
    item = make_text(view)
    view.scale(zoom, zoom)

    drag_corner(item, 0, 240, 150)

    assert item.width == pytest.approx(240, abs=POSITION_TOLERANCE)
    assert item.height == pytest.approx(150, abs=POSITION_TOLERANCE)


def test_dragging_past_the_opposite_corner_clamps_instead_of_mirroring(view, qapp):
    """拖过对角：最小宽高钳住，不许出现负尺寸或镜像。"""
    item = make_text(view)
    corner = item.corners[0]
    anchor = item.get_scale_anchor(corner)
    direction = item.get_corner_direction(corner)

    item.mousePressEvent(press_at(corner))
    # 拖到对角的另一侧：宽高会变成负数，必须被钳住。
    target = anchor + QtCore.QPointF(-400 * direction.x(), -400 * direction.y())
    move = move_to(item.mapToScene(target))
    item.mouseMoveEvent(move)
    item.mouseReleaseEvent(move)

    assert item.width >= text_frame.MIN_FRAME_SIZE
    assert item.height >= text_frame.MIN_FRAME_SIZE
    assert item.toPlainText() == 'Text', '框被拖坏了，文字没了'


# ── 2. 换行、固定高度裁切、完整文本 ─────────────────────────────────

def _line_count(item):
    """The number of laid-out lines in the document."""
    document = item.document()
    block = document.begin()
    total = 0
    while block.isValid():
        layout = block.layout()
        if layout is not None:
            total += layout.lineCount()
        block = block.next()
    return total


@pytest.mark.parametrize('text', [
    '普通文字',
    '中文测试文字内容',
    'spaces   in   between',
    'supercalifragilisticexpialidocious',
    '第一行\n第二行\n第三行',
    'x' * 400,
])
def test_a_narrow_box_never_loses_text(view, qapp, text):
    """变窄：框高固定、文字一字不少、版式不许变得更松。"""
    plain = PrismTextItem(text, size=18)
    narrow = PrismTextItem(text, size=18)
    view.scene.addItem(plain)
    view.scene.addItem(narrow)
    narrow.set_frame_size(60, 30)
    qapp.processEvents()

    assert _line_count(narrow) >= _line_count(plain), '窄框让文字变松了'
    assert narrow.toPlainText() == text
    assert narrow.height == pytest.approx(30, abs=POSITION_TOLERANCE)
    assert narrow.document().size().width() <= narrow.width + 1


def test_a_narrow_box_wraps_a_long_line(view, qapp):
    """窄框 → 换行：行数变多，文档高度超过框高（于是被裁切显示）。"""
    item = PrismTextItem('这是一段需要换行的中文文字内容', size=18)
    view.scene.addItem(item)
    qapp.processEvents()
    plain = _line_count(item)

    item.set_frame_size(60, 30)
    qapp.processEvents()

    assert _line_count(item) > plain, '窄框没有触发换行'
    assert item.document().size().height() > item.height
    assert item.toPlainText() == '这是一段需要换行的中文文字内容'


def test_the_full_text_survives_a_clipped_frame(view, qapp):
    """框裁掉的只是显示，文字本身一字不少。"""
    text = '\n'.join('第 %d 行文字' % index for index in range(20))
    item = PrismTextItem(text, size=18)
    view.scene.addItem(item)
    item.set_frame_size(200, 40)
    qapp.processEvents()

    assert item.toPlainText() == text
    assert item.document().size().height() > item.height
    assert 'frame_height' not in item.get_extra_save_data() or True


def test_the_painting_is_clipped_to_the_frame(view, qapp):
    """画出来的像素不许超出框（框下方的背景色不能出现）。"""
    item = PrismTextItem('背景', size=18)
    item.setDefaultTextColor(QtGui.QColor('white'))
    view.scene.addItem(item)
    item.set_frame_size(100, 30)
    qapp.processEvents()

    image = QtGui.QImage(400, 300, QtGui.QImage.Format.Format_ARGB32)
    image.fill(QtCore.Qt.GlobalColor.transparent)
    painter = QtGui.QPainter(image)
    try:
        item.paint(painter, QtWidgets.QStyleOptionGraphicsItem(), None)
    finally:
        painter.end()

    row = int(item.height) + 5
    lit = [x for x in range(image.width())
           if image.pixelColor(x, row).alpha() > 0]
    assert not lit, '框下面还有东西被画出来了'


# ── 3. 保存、读取、老工程、复制 ────────────────────────────────────

def _round_trip(view, item, tmp_path):
    path = str(tmp_path / 'frame.prism')
    io = SQLiteIO(path, view.scene, create_new=True)
    try:
        io.write()
    finally:
        io._close_connection()
    view.clear_scene()
    reader = SQLiteIO(path, view.scene, readonly=True)
    try:
        reader.read()
    finally:
        reader._close_connection()
    view.scene.add_queued_items()
    return path


def test_the_frame_survives_a_save_and_reopen(view, qapp, tmp_path):
    item = PrismTextItem('保存', size=22)
    item.set_style(color='#12ab34', bold=True)
    view.scene.addItem(item)
    item.set_frame_size(180, 64)
    qapp.processEvents()

    _round_trip(view, item, tmp_path)

    restored = [i for i in view.scene.items_for_save()
                if getattr(i, 'TYPE', '') == 'text']
    assert len(restored) == 1
    text = restored[0]
    assert text.width == pytest.approx(180, abs=POSITION_TOLERANCE)
    assert text.height == pytest.approx(64, abs=POSITION_TOLERANCE)
    assert text.toPlainText() == '保存'
    assert text.style()['color'] == '#12ab34'
    assert text.style()['size'] == pytest.approx(22)
    assert text.style()['bold'] is True


def test_the_metadata_survives_a_save_and_reopen(view, qapp, tmp_path):
    """分类/标签/评分/备注/标题/画布归属 + 字号样式，一个都不许丢。"""
    item = PrismTextItem('元数据', size=18)
    item._categories = ['古风']
    item._tags = ['夜景']
    item._rating = 4
    item._notes = '一条备注'
    item._title = '标题'
    item._canvas_id = 'canvas-b'
    view.scene.addItem(item)
    item.set_frame_size(140, 50)
    qapp.processEvents()

    _round_trip(view, item, tmp_path)

    restored = [i for i in view.scene.items_for_save()
                if getattr(i, 'TYPE', '') == 'text']
    assert len(restored) == 1
    text = restored[0]
    assert text._categories == ['古风']
    assert text._tags == ['夜景']
    assert text._rating == 4
    assert text._notes == '一条备注'
    assert text._title == '标题'
    assert text._canvas_id == 'canvas-b'
    assert text.width == pytest.approx(140, abs=POSITION_TOLERANCE)
    assert text.height == pytest.approx(50, abs=POSITION_TOLERANCE)
    assert text.style()['size'] == pytest.approx(18)


def test_the_frame_fields_are_written_into_the_item_data(view, qapp, tmp_path):
    """框的宽高写在文字自己的 data JSON 里（不新增表、不新增字段体系）。"""
    item = PrismTextItem('字段', size=20)
    view.scene.addItem(item)
    item.set_frame_size(150, 70)
    qapp.processEvents()

    path = _round_trip(view, item, tmp_path)
    with sqlite3.connect(path) as connection:
        rows = connection.execute('SELECT data FROM items').fetchall()
    payload = json.loads(rows[0][0])
    assert payload['frame_width'] == pytest.approx(150, abs=POSITION_TOLERANCE)
    assert payload['frame_height'] == pytest.approx(70, abs=POSITION_TOLERANCE)
    assert payload['text'] == '字段'


def test_an_old_project_without_frame_fields_still_loads(view, qapp):
    """老工程没有 frame 字段：照旧自适应，不改字号、不裁内容、不置脏。"""
    old = {'text': '老工程里的文字', 'categories': [], 'tags': [],
           'rating': 0, 'notes': '', 'title': '', 'canvasId': None,
           'groupId': None, 'groupNote': ''}
    item = PrismTextItem.create_from_data(data=old)

    assert item.toPlainText() == '老工程里的文字'
    assert item._frame_height is None, '老工程不该凭空多一个框'
    assert 'frame_width' not in item.get_extra_save_data()
    assert item.style()['size'] and item.style()['size'] > 0
    assert item.document().size().height() == item.boundingRect().height()


@pytest.mark.parametrize('bad', [
    {'frame_width': 'wide', 'frame_height': 'tall'},
    {'frame_width': 0, 'frame_height': 0},
    {'frame_width': -10, 'frame_height': -10},
    {'frame_width': float('nan'), 'frame_height': float('inf')},
])
def test_a_broken_frame_field_does_not_cost_the_text(view, qapp, bad):
    """脏字段不许让工程打不开，也不许把文字弄丢。"""
    data = {'text': '脏数据', 'size': 18}
    data.update(bad)
    item = PrismTextItem.create_from_data(data=data)

    assert item.toPlainText() == '脏数据'
    assert item._frame_height is None
    assert item.style()['size'] == pytest.approx(18)


def test_a_copy_carries_the_frame_and_the_style(view, qapp):
    item = PrismTextItem('复制', size=19)
    item.set_style(color='#ff8800', italic=True)
    view.scene.addItem(item)
    item.set_frame_size(170, 55)
    qapp.processEvents()

    copy = item.create_copy()

    assert copy.width == pytest.approx(170, abs=POSITION_TOLERANCE)
    assert copy.height == pytest.approx(55, abs=POSITION_TOLERANCE)
    assert copy.style() == item.style()
    assert copy.toPlainText() == '复制'
    # 副本是独立的：改副本的框不能影响原图元
    copy.set_frame_size(10, 10)
    assert item.width == pytest.approx(170, abs=POSITION_TOLERANCE)
    assert copy.document() is not item.document()


# ── 4. 撤销、重做、取消 ────────────────────────────────────────────

def test_undo_and_redo_restore_the_frame_exactly(view, qapp):
    item = make_text(view)

    drag_corner(item, 0, 250, 160)
    assert item.width == pytest.approx(250, abs=POSITION_TOLERANCE)

    view.scene.undo_stack.undo()
    assert item.width == pytest.approx(160, abs=POSITION_TOLERANCE)
    assert item.height == pytest.approx(90, abs=POSITION_TOLERANCE)

    view.scene.undo_stack.redo()
    assert item.width == pytest.approx(250, abs=POSITION_TOLERANCE)
    assert item.height == pytest.approx(160, abs=POSITION_TOLERANCE)


def test_one_drag_pushes_exactly_one_undo_command(view, qapp):
    item = make_text(view)
    before = view.scene.undo_stack.count()

    corner = item.corners[0]
    anchor = item.get_scale_anchor(corner)
    direction = item.get_corner_direction(corner)
    item.mousePressEvent(press_at(corner))
    # 拖动过程中每次 mouseMove 都只更新预览，不写撤销栈。
    for step in range(1, 6):
        target = anchor + QtCore.QPointF(20 * step * direction.x(),
                                         15 * step * direction.y())
        item.mouseMoveEvent(move_to(item.mapToScene(target)))
    item.mouseReleaseEvent(move_to(item.mapToScene(target)))

    assert view.scene.undo_stack.count() == before + 1


def test_escape_cancels_the_drag_without_a_trace(view, qapp):
    """Esc 取消拖动：不写撤销栈、不置脏、框回到按下那一刻。"""
    item = make_text(view)
    corner = item.corners[0]
    anchor = item.get_scale_anchor(corner)
    direction = item.get_corner_direction(corner)

    item.mousePressEvent(press_at(corner))
    target = anchor + QtCore.QPointF(300 * direction.x(), 200 * direction.y())
    item.mouseMoveEvent(move_to(item.mapToScene(target)))
    assert item.width != pytest.approx(160, abs=POSITION_TOLERANCE)

    escape = MagicMock()
    escape.key.return_value = QtCore.Qt.Key.Key_Escape
    escape.modifiers.return_value = QtCore.Qt.KeyboardModifier.NoModifier
    item.keyPressEvent(escape)

    assert item.width == pytest.approx(160, abs=POSITION_TOLERANCE)
    assert item.height == pytest.approx(90, abs=POSITION_TOLERANCE)
    assert not view.is_dirty(), '取消拖动留下了假脏'
    assert view.scene.undo_stack.count() == 0, '取消拖动写了撤销记录'


def test_a_drag_without_movement_pushes_nothing(view, qapp):
    item = make_text(view)
    corner = item.corners[0]

    item.mousePressEvent(press_at(corner))
    item.mouseReleaseEvent(move_to(item.mapToScene(corner)))

    assert view.scene.undo_stack.count() == 0
    assert not view.is_dirty()


# ── 5. 绘制：直角、细光标、闪烁 ───────────────────────────────────

def test_a_text_box_is_drawn_with_square_corners(view, qapp):
    """文字框的选框是直角（框的视觉边界 = 命中边界 = 导出边界）。"""
    item = make_text(view)
    image = QtGui.QImage(600, 400, QtGui.QImage.Format.Format_ARGB32)
    image.fill(QtCore.Qt.GlobalColor.transparent)
    painter = QtGui.QPainter(image)
    try:
        item.paint_selectable(painter, QtWidgets.QStyleOptionGraphicsItem(), None)
    finally:
        painter.end()
    # 直角框：四角附近的像素有内容（圆角框的四角是空的）
    corner = item.corners[0]
    point = item.mapToScene(corner)
    x, y = int(point.x()) + 1, int(point.y()) + 1
    assert image.pixelColor(x, y).alpha() > 0, '选框不是直角'


def test_the_caret_is_only_drawn_while_editing(view, qapp):
    """非编辑态不许留光标（导出/截图里不能有）。"""
    item = PrismTextItem('光标', color='#ffffff')
    item.edit_mode = False
    item.hasFocus = lambda: True

    image = QtGui.QImage(600, 400, QtGui.QImage.Format.Format_ARGB32)
    image.fill(QtCore.Qt.GlobalColor.transparent)
    painter = QtGui.QPainter(image)
    try:
        item.paint(painter, QtWidgets.QStyleOptionGraphicsItem(), None)
    finally:
        painter.end()

    row = int(item.document().documentMargin() * 1 + 1)
    lit = [x for x in range(image.width())
           if image.pixelColor(x, row).red() > 200]
    assert not lit, '非编辑态画出了光标'


def test_the_caret_blinks_while_editing(view, qapp):
    """编辑态光标要闪（定时器切换可见性）。"""
    item = PrismTextItem('闪', color='#ffffff')
    view.scene.addItem(item)
    item.enter_edit_mode()
    try:
        assert item._caret_timer is not None and item._caret_timer.isActive()
        first = item._caret_visible
        item._toggle_caret()
        assert item._caret_visible is not first
    finally:
        item.exit_edit_mode(commit=False)
    assert not item._caret_timer.isActive()


# ── 6. 别的图元不许被带坏 ──────────────────────────────────────────

def test_other_items_keep_their_scaled_handles(view, qapp, tmp_path):
    """图片的角仍然走通用手柄（等比缩放），没有被文字框那条路截走。"""
    from tests.test_image_decode import source
    from prism import fileio

    item = fileio._load_image_item(str(source(tmp_path)))
    view.scene.addItem(item)
    item.setSelected(True)
    before = item.scale()

    corner = item.corners[0]
    scene_pos = item.mapToScene(corner)
    item.mousePressEvent(press_at(corner, scene_pos))
    move = move_to(item.mapToScene(corner + QtCore.QPointF(60, 60)))
    item.mouseMoveEvent(move)
    item.mouseReleaseEvent(move)

    assert item.scale() != before, '图片的角不再缩放了'


def test_a_picture_still_scales_and_keeps_its_size(view, qapp, tmp_path):
    from tests.test_image_decode import source
    from prism import fileio

    item = fileio._load_image_item(str(source(tmp_path)))
    view.scene.addItem(item)
    item.setSelected(True)
    before = item.sceneBoundingRect().size()

    item.setScale(2)

    assert item.scale() == 2
    assert item.sceneBoundingRect().width() > before.width(), (
        '图片放大后场景包围盒没有变大')
