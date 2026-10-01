"""回归测试：用户报的「划线就消失」（2026-10-01）。

根因：左栏素材库的 ``MediaLibraryPanel._apply_filter`` 把**所有**带
``save_id`` 属性的图元都当素材来筛。画笔线（``PrismPathItem``）没有
分类 / 标签 / 评分 / 文件名，只要用户开着任何一种筛选（分类、标签、
最低评分、搜索词），线就永远算「不匹配」：

    scene.changed → _schedule_recount（300ms 定时器）
        → update_counts → _apply_filter → item.setVisible(False)

表现就是画完线约 300 毫秒后线自己消失，和粗细无关；数据其实还在，
存盘也没丢（用户的工程里躺着 26 条被隐藏的线）。

画笔线和文字是画布上的**标注**，不是素材：筛选可以收窄素材，
但绝不能把它们藏起来。
"""
import pytest
from PyQt6 import QtCore, QtGui, QtWidgets
from PyQt6.QtCore import Qt
from PyQt6.QtTest import QTest

from prism.items import DRAW_TOOL_PEN, PrismPathItem, PrismTextItem


@pytest.fixture
def view(qapp):
    from prism.view import PrismGraphicsView

    host = QtWidgets.QMainWindow()
    result = PrismGraphicsView(qapp, host)
    yield result
    host.close()


@pytest.fixture
def panel(view):
    """带筛选面板的视图 —— 用户的左栏就是这么挂上去的。"""
    from prism.widgets.media_library import MediaLibraryPanel

    return MediaLibraryPanel(view)


def draw_stroke(view, start=(100, 100), end=(240, 180), steps=8):
    """用真实鼠标事件画一笔，返回那一笔的图元。"""
    view.set_drawing_tool(DRAW_TOOL_PEN)
    QTest.mousePress(view.viewport(), Qt.MouseButton.LeftButton,
                     Qt.KeyboardModifier.NoModifier, QtCore.QPoint(*start))
    for i in range(1, steps + 1):
        x = start[0] + (end[0] - start[0]) * i // steps
        y = start[1] + (end[1] - start[1]) * i // steps
        QTest.mouseMove(view.viewport(), QtCore.QPoint(x, y), 1)
    QTest.mouseRelease(view.viewport(), Qt.MouseButton.LeftButton,
                       Qt.KeyboardModifier.NoModifier, QtCore.QPoint(*end))
    paths = [item for item in view.scene.items()
             if getattr(item, 'TYPE', '') == PrismPathItem.TYPE]
    assert paths, '这一笔根本没进场景'
    return paths[0]


def make_photo(view, colour=(200, 30, 30)):
    from prism.items import PrismPixmapItem

    image = QtGui.QImage(30, 30, QtGui.QImage.Format.Format_RGB32)
    image.fill(QtGui.QColor(*colour))
    item = PrismPixmapItem(image)
    view.scene.addItem(item)
    return item


# ── 画笔线：任何筛选都不许把它藏起来 ───────────────────────────


def test_stroke_survives_a_category_filter(view, panel):
    """用户场景之一：左栏选着某个分类，画一笔 —— 线必须还在。"""
    view.start_draw_mode()
    stroke = draw_stroke(view)

    panel._active_filter = ('category', 'Stray')
    panel._apply_filter()

    assert stroke.isVisible() is True, '分类筛选把画笔线藏起来了'


def test_stroke_survives_a_rating_filter(view, panel):
    """用户场景之二：最低评分筛选（画笔线没有评分，0 星）。"""
    view.start_draw_mode()
    stroke = draw_stroke(view)

    panel._min_rating = 4
    panel._apply_filter()

    assert stroke.isVisible() is True, '评分筛选把画笔线藏起来了'


def test_stroke_survives_a_search_query(view, panel):
    """用户场景之三：搜索框里有词（画笔线没有文件名/标题）。"""
    view.start_draw_mode()
    stroke = draw_stroke(view)

    panel.search.setText('ref_050')
    panel._apply_filter()

    assert stroke.isVisible() is True, '搜索把画笔线藏起来了'


def test_stroke_survives_the_scheduled_recount(view, panel):
    """端到端：场景变化 → 300ms 定时器 → 重算筛选。线必须还看得见。

    这条走的是**完整链路**（scene.changed → _schedule_recount →
    update_counts → _apply_filter），也就是用户真正踩到的那条。
    """
    view.start_draw_mode()
    panel._min_rating = 4          # 用户开着筛选（比如 4 星以上）
    stroke = draw_stroke(view)     # 这一笔会触发 scene.changed

    QTest.qWait(500)               # 300ms 定时器 + 余量

    assert stroke.isVisible() is True, '重算筛选之后线不见了'


def test_hidden_stroke_comes_back_when_the_filter_runs_again(view, panel):
    """自愈：线要是已经被旧逻辑藏过（或手改过状态），下一次筛选重算
    要把它放回来 —— 不能永远是隐藏的。"""
    view.start_draw_mode()
    stroke = draw_stroke(view)
    stroke.setVisible(False)

    panel._apply_filter()

    assert stroke.isVisible() is True


# ── 文字标注同理 ──────────────────────────────────────────────


def test_text_annotation_survives_a_filter(view, panel):
    """文字是标注，不是素材 —— 筛选同样不许藏它。"""
    item = PrismTextItem(text='批注')
    view.scene.addItem(item)

    panel._min_rating = 3
    panel._apply_filter()

    assert item.isVisible() is True, '筛选把文字标注藏起来了'


# ── 素材本身照旧会被筛选收窄 ─────────────────────────────────


def test_photo_is_still_hidden_by_the_filter(view, panel):
    """修复不能把筛选本身废掉：不匹配的图片照旧隐藏。"""
    matched = make_photo(view)
    other = make_photo(view)
    matched._categories = ['Stray']

    panel._active_filter = ('category', 'Stray')
    panel._apply_filter()

    assert matched.isVisible() is True
    assert other.isVisible() is False, '不匹配的素材该隐藏'
