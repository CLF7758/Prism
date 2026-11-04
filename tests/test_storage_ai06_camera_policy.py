"""AI-06 · storage-20261025 · pkg-01 — 相机统一（F03）。

AI-06 自己的新增测试（`test_storage_ai06_*` 前缀）。不改任何既有测试。

`tests/test_camera_policy.py` 已经守住「两入口同结果 / 幂等 / 空画布回原点 /
不递归 / 不恢复上次适配」这一层。本文件补的是**那一层没有覆盖的缺口**：

* 主画布的重置**走同一条策略**（同一个内容范围、同一个边距、同一个空画布策略），
  而不是自己再算一遍 bounds 与边距；
* 在内容**还没到**时就请求的重置，内容到了之后会被**重新套用**
  （`camera_policy.reset_if_stale` 之前**没有任何调用点**，所以这条路径是死的）；
* 同一个状态下重复重置不会 toggle。

这些断言在**改前**必须失败（否则它证明不了任何事）。
"""
import pytest

from PyQt6 import QtGui, QtWidgets

from prism import camera_policy
from prism.items import PrismPixmapItem


def _show(window, width=1200, height=800):
    """Give the window a real viewport -- an unshown view has no size."""
    window.resize(width, height)
    window.show()
    QtWidgets.QApplication.processEvents()
    return window


def _add_asset(view, x, y, width=120, height=80):
    item = PrismPixmapItem(QtGui.QImage(width, height,
                                        QtGui.QImage.Format.Format_RGB32))
    item.setPos(x, y)
    view.scene.addItem(item)
    return item


def _camera_state(view):
    transform = view.transform()
    centre = view.mapToScene(view.get_view_center())
    return (round(transform.m11(), 9), round(transform.m12(), 9),
            round(transform.m21(), 9), round(transform.m22(), 9),
            round(centre.x(), 6), round(centre.y(), 6))


def _content_bounds(view):
    bounds = camera_policy.visible_asset_bounds(view)
    return None if bounds is None else (
        round(bounds.x(), 3), round(bounds.y(), 3),
        round(bounds.width(), 3), round(bounds.height(), 3))


# ── F03-1：主画布的重置走同一条策略 ──────────────────────────────────

def test_the_main_canvas_reset_goes_through_the_shared_policy(main_window, qapp):
    """主画布的重置必须经过 camera_policy，而不是自己再算一遍。"""
    view = main_window.view
    _show(main_window)
    _add_asset(view, 40, 30)
    _add_asset(view, 260, 180)

    calls = []
    original = camera_policy.request_reset

    def counted(target, provider):
        calls.append((target, provider))
        return original(target, provider)

    camera_policy.request_reset = counted
    try:
        view.on_action_fit_scene()
    finally:
        camera_policy.request_reset = original

    assert calls, 'the main canvas reset must go through camera_policy.request_reset'
    target, provider = calls[-1]
    assert target is view
    assert provider is camera_policy.visible_asset_bounds


def test_both_reset_entries_use_the_same_content_scope(main_window, qapp):
    """菜单/快捷键/右键与工具栏必须用**同一个**内容范围。"""
    view = main_window.view
    _show(main_window)
    _add_asset(view, 40, 30)
    _add_asset(view, 260, 180)

    view.on_action_fit_scene()
    expected = _camera_state(view)
    main_window.canvas_toolbar.fit_all()
    assert _camera_state(view) == expected


def test_the_reset_leaves_the_shared_margin(main_window, qapp):
    """重置后的边距是 camera_policy 的那一个（逻辑屏幕像素）。

    边距是**最小**间隙：被限制的那条轴正好是 margin，另一条轴留得更多
    （内容在宽视口里按宽度缩放，高度就会富余）。
    """
    view = main_window.view
    _show(main_window)
    _add_asset(view, 40, 30)
    _add_asset(view, 260, 180)

    view.on_action_fit_scene()
    bounds = camera_policy.visible_asset_bounds(view)
    assert bounds is not None
    top_left = view.mapFromScene(bounds.topLeft())
    bottom_right = view.mapFromScene(bounds.bottomRight())
    viewport = view.viewport().rect()
    gap_x = min(top_left.x() - viewport.left(), viewport.right() - bottom_right.x())
    gap_y = min(top_left.y() - viewport.top(), viewport.bottom() - bottom_right.y())
    margin = camera_policy.CAMERA_MARGIN
    # The margin is a screen quantity: it does not shrink with the zoom.
    assert min(gap_x, gap_y) == pytest.approx(margin, abs=2.0)
    assert gap_x >= margin - 2.0
    assert gap_y >= margin - 2.0


# ── F03-2：内容没到时的重置，内容到了要重新套用 ─────────────────────

def test_a_reset_asked_for_before_the_content_landed_is_applied_when_it_lands(
        main_window, qapp):
    """空画布上请求的重置，在内容到达之后必须被重新套用。

    改前这条路径是死的：`camera_policy.reset_if_stale` 没有任何调用点，
    所以「内容还没排好时按了重置」的结果永远停在空画布状态。
    """
    view = main_window.view
    _show(main_window)

    view.on_action_fit_scene()
    empty_state = _camera_state(view)
    assert empty_state[0] == pytest.approx(1.0), 'an empty canvas is the identity'

    # 内容到了。
    _add_asset(view, 40, 30)
    _add_asset(view, 260, 180)
    QtWidgets.QApplication.processEvents()

    fitted = _camera_state(view)
    assert fitted != empty_state, \
        'the reset must be re-applied once the content has landed'
    bounds = camera_policy.visible_asset_bounds(view)
    assert bounds is not None
    centre = view.mapToScene(view.get_view_center())
    assert centre.x() == pytest.approx(bounds.center().x(), abs=2.0)
    assert centre.y() == pytest.approx(bounds.center().y(), abs=2.0)


def test_a_reset_is_not_re_applied_after_the_user_moved_the_camera(main_window, qapp):
    """用户自己动过相机之后，迟到的内容不得把相机抢回去。"""
    view = main_window.view
    _show(main_window)

    view.on_action_fit_scene()
    view.setTransform(QtGui.QTransform().scale(0.5, 0.5))
    moved = _camera_state(view)

    _add_asset(view, 40, 30)
    _add_asset(view, 260, 180)
    QtWidgets.QApplication.processEvents()

    assert _camera_state(view) == moved, \
        'the camera the user moved must not be overridden by a late layout'


# ── F03-3：重复重置不 toggle ─────────────────────────────────────────

def test_repeated_resets_do_not_toggle(main_window, qapp):
    """同一个状态下重复重置，画面不能来回跳。"""
    view = main_window.view
    _show(main_window)
    _add_asset(view, 40, 30)
    _add_asset(view, 260, 180)

    view.on_action_fit_scene()
    first = _camera_state(view)
    view.on_action_fit_scene()
    assert _camera_state(view) == first
    main_window.canvas_toolbar.fit_all()
    assert _camera_state(view) == first
