"""无边框窗口外壳测试（方案 §9 / §11：无边框窗口交互的有效测试）。

真实 Windows 上的贴靠 / DPI / 八向缩放 / 双显示器必须实机验证（见交付
报告的未完成事项）——离屏平台收不到 WM_NCCALCSIZE 等原生消息。这里覆盖
离屏可确定的逻辑：

* 窗口控制按钮（最小化 / 最大化-还原 / 关闭）接线正确，关闭走 close
  （→ closeEvent，含未保存确认），不直接退进程；
* 标题栏空白区双击最大化 / 还原，按下调用系统 startSystemMove；
* 命中菜单项时不拦截，交回 QMenuBar 正常开菜单；
* 切回系统原生标题栏时隐藏自绘窗口控制按钮，反之显示；
* 无边框外壳在离屏平台安全跳过 Win32 调用，不报错。
"""
from unittest.mock import patch, MagicMock

from PyQt6 import QtCore, QtGui, QtWidgets
from PyQt6.QtCore import Qt

from prism.widgets.window_shell import (
    FramelessShellMixin, TitleBarControls, TitleBarDragArea)


def _mouse_event(etype, pos=QtCore.QPointF(5, 5),
                 button=Qt.MouseButton.LeftButton):
    return QtGui.QMouseEvent(etype, pos, pos, button, button,
                             Qt.KeyboardModifier.NoModifier)


class _SpyWindow(QtWidgets.QMainWindow):
    """记录 closeEvent 是否被走到（验证关闭不绕过未保存确认）。"""

    def __init__(self):
        super().__init__()
        self.close_seen = False

    def closeEvent(self, event):
        self.close_seen = True
        super().closeEvent(event)


# ── TitleBarControls：普通 QMainWindow 上的单元测试 ──────────────
# 注意：按钮的 clicked 是在构造时 connect 到 window 的**绑定方法**上的，
# 事后 patch.object(win, 'showMinimized') 拦不到那条已建立的连接。所以
# 这里断言真实窗口状态变化，而不是 mock 调用次数。

def test_titlebar_controls_minimize(qapp):
    win = QtWidgets.QMainWindow()
    win.show()
    controls = TitleBarControls(win)
    controls.min_button.click()
    assert win.windowState() & Qt.WindowState.WindowMinimized


def test_titlebar_controls_maximize_toggle(qapp):
    win = QtWidgets.QMainWindow()
    win.show()
    controls = TitleBarControls(win)
    controls.max_button.click()
    qapp.processEvents()  # 让 WindowStateChange 事件送达，触发字形同步
    assert win.windowState() & Qt.WindowState.WindowMaximized
    assert controls.max_button.toolTip() == '还原'
    controls.max_button.click()
    qapp.processEvents()
    assert not (win.windowState() & Qt.WindowState.WindowMaximized)
    assert controls.max_button.toolTip() == '最大化'


def test_titlebar_controls_close_goes_through_closeevent(qapp):
    win = _SpyWindow()
    win.show()
    controls = TitleBarControls(win)
    controls.close_button.click()
    # 关闭走 window.close → closeEvent（主窗口在那里做未保存确认），
    # 绝不直接退进程绕过保存提示（方案 §9.1）。
    assert win.close_seen is True


def test_titlebar_controls_sync_max_glyph(qapp):
    win = QtWidgets.QMainWindow()
    controls = TitleBarControls(win)
    controls._sync_max_button(Qt.WindowState.WindowMaximized)
    assert controls.max_button.toolTip() == '还原'
    controls._sync_max_button(Qt.WindowState.WindowNoState)
    assert controls.max_button.toolTip() == '最大化'


# ── TitleBarDragArea：拖动 / 双击 ────────────────────────────────

def test_titlebar_drag_area_press_uses_system_move(qapp):
    win = QtWidgets.QMainWindow()
    drag = TitleBarDragArea(win)
    handle = MagicMock()
    handle.startSystemMove.return_value = True
    with patch.object(win, 'windowHandle', return_value=handle):
        drag.mousePressEvent(
            _mouse_event(QtCore.QEvent.Type.MouseButtonPress))
    # 优先用系统窗口移动（贴靠 / 多屏 / DPI 原生处理），不自己按位移 move。
    handle.startSystemMove.assert_called_once()


def test_titlebar_drag_area_double_click_maximizes(qapp):
    win = QtWidgets.QMainWindow()
    drag = TitleBarDragArea(win)
    with patch.object(win, 'showMaximized') as maximize, \
            patch.object(win, 'isMaximized', return_value=False):
        drag.mouseDoubleClickEvent(
            _mouse_event(QtCore.QEvent.Type.MouseButtonDblClick))
    maximize.assert_called_once()


def test_titlebar_drag_area_double_click_restores_when_maximized(qapp):
    win = QtWidgets.QMainWindow()
    drag = TitleBarDragArea(win)
    with patch.object(win, 'showNormal') as normal, \
            patch.object(win, 'isMaximized', return_value=True):
        drag.mouseDoubleClickEvent(
            _mouse_event(QtCore.QEvent.Type.MouseButtonDblClick))
    normal.assert_called_once()


# ── PrismMainWindow 集成：外壳接入与标题栏交互 ───────────────────

def test_main_window_uses_frameless_shell(main_window):
    assert isinstance(main_window, FramelessShellMixin)
    # 默认无边框（砍掉系统白标题栏）。
    assert main_window._native_titlebar is False
    # 窗口控制按钮存在，无边框时可见。
    assert main_window._title_controls is not None
    assert not main_window._title_controls.isHidden()
    # 标题 + 控制按钮挂在菜单栏右上角（复用现有深色顶栏）。
    corner = main_window.menuBar().cornerWidget(Qt.Corner.TopRightCorner)
    assert corner is main_window._title_corner


def test_set_native_titlebar_toggles_controls(main_window):
    main_window.set_native_titlebar(True)
    assert main_window._native_titlebar is True
    # 切回系统原生标题栏时隐藏自绘按钮，避免和系统按钮重复。
    assert main_window._title_controls.isHidden()
    main_window.set_native_titlebar(False)
    assert main_window._native_titlebar is False
    assert not main_window._title_controls.isHidden()


def test_menubar_empty_area_double_click_maximizes(main_window):
    with patch.object(main_window, 'showMaximized') as maximize, \
            patch.object(main_window, 'isMaximized', return_value=False), \
            patch.object(main_window.menuBar(), 'actionAt', return_value=None):
        handled = main_window._handle_titlebar_mouse(
            _mouse_event(QtCore.QEvent.Type.MouseButtonDblClick))
    assert handled is True
    maximize.assert_called_once()


def test_menubar_menu_item_not_intercepted(main_window):
    # 命中菜单项（actionAt 非空）时必须交回 QMenuBar，不能拦截。
    action = main_window.menuBar().actions()[0]
    with patch.object(main_window.menuBar(), 'actionAt', return_value=action):
        handled = main_window._handle_titlebar_mouse(
            _mouse_event(QtCore.QEvent.Type.MouseButtonPress))
    assert handled is False


def test_enable_frameless_shell_is_headless_safe(main_window):
    # 离屏平台应跳过 Win32 调用但不报错，并把标志复位为无边框。
    main_window._native_titlebar = True
    main_window.enable_frameless_shell()
    assert main_window._native_titlebar is False


def test_native_event_never_calls_super(main_window):
    """PyQt6 6.7.0 实测：nativeEvent 里调用 super().nativeEvent() 会原生崩溃。

    修复方式是全程返回 (False, 0) 交给 Qt 平台层默认处理。这个测试锁定该
    行为：对不认识的 eventType 必须返回二元组 (False, 0)，而不是去调父类。
    真机 Windows 上的 WM_NCCALCSIZE 路径由 probe/实机截图验证。
    """
    result = main_window.nativeEvent(b'not_a_windows_msg', 0)
    assert result == (False, 0)
