"""T8 的「实际大小 1:1」验收。

任务书 T8 的七项预览能力里，这一项原先没有。测的不只是「动作注册上了」，
更关键是**那个 1:1 真的等于 1:1** —— 视图 transform 和素材自己的
``scale()`` 是两个独立的放大倍数，只看前者会在素材被缩放过时给出错的
结果（一张缩到 50% 的图，在 100% 的视图里看着还是半尺寸）。
"""
import pytest
from PyQt6 import QtCore

from prism.actions.actions import actions
from prism.actions.menu_structure import menu_structure
from prism.i18n import _


@pytest.fixture
def pixmap_item(qapp):
    from PyQt6 import QtGui

    from prism.items import PrismPixmapItem

    image = QtGui.QImage(40, 30, QtGui.QImage.Format.Format_RGB32)
    image.fill(QtGui.QColor(120, 60, 30))
    return PrismPixmapItem(image)


# ── 注册与菜单 ───────────────────────────────────────────────────

def test_action_is_registered():
    # actions 是 ActionList(OrderedDict)：迭代给的是 key，不是 Action 对象
    assert 'actual_size' in actions


def test_action_has_the_expected_callback():
    assert actions['actual_size'].callback == 'on_action_actual_size'


def test_action_sits_after_the_two_fit_actions():
    """1 / 2 / 3 是连贯的一组，别插到别处去。"""
    ids = list(actions)
    assert ids.index('fit_scene') < ids.index('fit_selection')
    assert ids.index('fit_selection') < ids.index('actual_size')


def test_action_is_in_the_view_menu():
    view_menu = next(m for m in menu_structure if m['menu'] == _('&View'))
    assert 'actual_size' in view_menu['items']


def test_action_has_a_translation():
    # 测试环境强制英文界面（conftest 的 english_ui_strings 是 autouse），
    # 所以 _() 会返回英文原文。这里直接查翻译表本身。
    from prism.i18n.zh_CN import zh_CN as translations
    assert 'Actual Si&ze (1:1)' in translations


def test_view_implements_the_callback(view):
    assert callable(view.on_action_actual_size)


# ── 那个 1:1 到底对不对 ──────────────────────────────────────────

def test_no_items_does_nothing(view):
    """空场景里调用不该炸，也不该改动视图。"""
    from PyQt6 import QtGui

    before = QtGui.QTransform(view.transform())
    view.on_action_actual_size()
    assert view.transform() == before


def test_whole_scene_goes_to_100_percent(view, pixmap_item):
    view.scene.addItem(pixmap_item)
    view.on_action_actual_size()
    assert view.transform().m11() == pytest.approx(1.0, abs=1e-6)


def test_single_selection_goes_to_100_percent(view, pixmap_item):
    view.scene.addItem(pixmap_item)
    view.scene.clearSelection()
    pixmap_item.setSelected(True)
    view.scale(2.5, 2.5)                 # 先把视图弄乱
    view.on_action_actual_size()
    assert view.transform().m11() == pytest.approx(1.0, abs=1e-6)


def test_scaled_item_is_compensated(view, pixmap_item):
    """素材自己缩到 50% 时，视图要到 200% 才是真的 1:1。

    这是这个动作唯一容易写错的地方：只把 transform 设成 1 的话，屏幕上
    看到的还是半尺寸。
    """
    view.scene.addItem(pixmap_item)
    pixmap_item.setScale(0.5)
    view.scene.clearSelection()
    pixmap_item.setSelected(True)
    view.on_action_actual_size()
    assert view.transform().m11() == pytest.approx(2.0, abs=1e-6)
    # 两者相乘才是屏幕上的实际倍数
    assert view.transform().m11() * pixmap_item.scale() == pytest.approx(
        1.0, abs=1e-6)


def test_enlarged_item_is_compensated(view, pixmap_item):
    """反过来也一样：放大到 300% 的素材，视图要到 33%。"""
    view.scene.addItem(pixmap_item)
    pixmap_item.setScale(3.0)
    view.scene.clearSelection()
    pixmap_item.setSelected(True)
    view.on_action_actual_size()
    assert view.transform().m11() == pytest.approx(1.0 / 3.0, abs=1e-6)
    assert view.transform().m11() * pixmap_item.scale() == pytest.approx(
        1.0, abs=1e-6)


def test_selection_wins_over_the_whole_scene(view, pixmap_item, qapp):
    """有选中时只看选中的那个，而不是整个场景。"""
    from PyQt6 import QtGui

    from prism.items import PrismPixmapItem

    view.scene.addItem(pixmap_item)
    far = PrismPixmapItem(
        QtGui.QImage(20, 20, QtGui.QImage.Format.Format_RGB32))
    far.setPos(4000, 4000)
    view.scene.addItem(far)

    view.scene.clearSelection()
    far.setSelected(True)
    view.on_action_actual_size()

    # 视图中心应该落在 far 上，而不是两张图的中间
    centre = view.mapToScene(view.viewport().rect().center())
    assert (centre - far.mapRectToScene(far.boundingRect()).center()).manhattanLength() < 40


def test_multiple_selection_falls_back_to_the_whole_scene(
        view, pixmap_item, qapp):
    """多个选中和零个选中的处理一样 —— 看整个场景。

    任务书没规定多选怎么办；选「整个场景」是因为「1:1 看几张大小不同的图」
    本来就没有单一答案，而整个场景至少是确定的。
    """
    from PyQt6 import QtGui

    from prism.items import PrismPixmapItem

    view.scene.addItem(pixmap_item)
    second = PrismPixmapItem(
        QtGui.QImage(20, 20, QtGui.QImage.Format.Format_RGB32))
    view.scene.addItem(second)

    view.scene.clearSelection()
    pixmap_item.setSelected(True)
    second.setSelected(True)
    view.on_action_actual_size()
    assert view.transform().m11() == pytest.approx(1.0, abs=1e-6)


def test_previous_transform_is_cleared(view, pixmap_item):
    """recalc_scene_rect() 在 previous_transform 非空时直接返回。

    不清掉它，场景矩形不会跟着新缩放走 —— 「适应场景」之后马上按 1:1
    会留下一个不匹配的滚动范围。
    """
    view.scene.addItem(pixmap_item)
    from PyQt6 import QtGui

    view.previous_transform = {'transform': QtGui.QTransform(),
                               'center': QtCore.QPointF(),
                               'toggle_item': pixmap_item}
    view.on_action_actual_size()
    assert view.previous_transform is None


def test_zoom_signal_is_emitted(view, pixmap_item, qtbot):
    view.scene.addItem(pixmap_item)
    with qtbot.waitSignal(view.zoom_changed, timeout=1000) as blocker:
        view.on_action_actual_size()
    assert blocker.args[0] == pytest.approx(100.0, abs=1e-6)


def test_scene_rect_grows_after_zooming_in(view, pixmap_item):
    """1:1 之后场景矩形要跟着放大，否则拖不到图的边缘。"""
    view.scene.addItem(pixmap_item)
    view.resize(400, 300)
    view.on_action_actual_size()
    rect = view.sceneRect()
    assert rect.width() > 0 and rect.height() > 0
