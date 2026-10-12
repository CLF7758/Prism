"""Windows 无边框窗口外壳（方案 §9）。

平台相关代码全部集中在这一个模块里，不散落到侧栏、画布或菜单事件里
（方案 §9.2 的硬要求）。它做两件事：

1. `FramelessShellMixin`：接管 `WM_NCCALCSIZE`，把系统那条白色标题栏从
   客户区里砍掉，但**保留** Windows 原生的四周边框、DWM 阴影、贴靠
   （Aero Snap）和最大化行为。因为 `WS_THICKFRAME` 样式没动，缩放、
   贴靠、多显示器、DPI 全部走系统原生逻辑 —— 不是自己按鼠标位移硬拖，
   那样贴靠和最大化拖回会出问题（方案 §9.2）。

2. `TitleBarControls`：放进顶部深色菜单区右侧的最小化 / 最大化-还原 /
   关闭三个按钮。关闭走主窗口原有的 `closeEvent`（含未保存确认），
   绝不直接退进程绕过保存提示（方案 §9.1）。

拖动和双击最大化由标题栏空白区的 `TitleBarDragArea` 用
`windowHandle().startSystemMove()` 交给系统，天然支持贴靠和 DPI。
"""
from __future__ import annotations

import ctypes
import logging
import sys
from ctypes import wintypes

from PyQt6 import QtCore, QtGui, QtWidgets

logger = logging.getLogger(__name__)

IS_WINDOWS = sys.platform.startswith('win')

# ── Win32 常量 ────────────────────────────────────────────────
WM_NCCALCSIZE = 0x0083
WM_NCHITTEST = 0x0084

SWP_NOSIZE = 0x0001
SWP_NOMOVE = 0x0002
SWP_NOZORDER = 0x0004
SWP_FRAMECHANGED = 0x0020

GWL_STYLE = -16
WS_MAXIMIZEBOX = 0x00010000
WS_MINIMIZEBOX = 0x00020000
WS_THICKFRAME = 0x00040000
WS_CAPTION = 0x00C00000
WS_SYSMENU = 0x00080000

if IS_WINDOWS:
    class _RECT(ctypes.Structure):
        _fields_ = [('left', wintypes.LONG), ('top', wintypes.LONG),
                    ('right', wintypes.LONG), ('bottom', wintypes.LONG)]

    class _NCCALCSIZE_PARAMS(ctypes.Structure):
        _fields_ = [('rgrc', _RECT * 3), ('lppos', ctypes.c_void_p)]

    _user32 = ctypes.windll.user32
    _DefWindowProcW = _user32.DefWindowProcW
    _DefWindowProcW.restype = ctypes.c_ssize_t
    _DefWindowProcW.argtypes = [wintypes.HWND, ctypes.c_uint,
                                wintypes.WPARAM, wintypes.LPARAM]
else:  # pragma: no cover - 非 Windows 平台只是占位
    _RECT = None
    _NCCALCSIZE_PARAMS = None
    _user32 = None
    _DefWindowProcW = None


class FramelessShellMixin:
    """给 QMainWindow 混入无边框外壳。放在继承列表**最前面**。

    只在 Windows 上生效；其它平台 `nativeEvent` 直接交给父类，界面照常
    是系统窗口（离屏测试跑在 offscreen 平台，不会进这些分支）。
    """

    # 默认砍掉系统标题栏（需求 4）。`set_native_titlebar(True)` 可把系统
    # 原生标题栏放回来——两种状态都保留 WS_THICKFRAME，所以缩放/贴靠在
    # 两种模式下都不丢。
    _native_titlebar = False

    def _frameless_hwnd(self):
        try:
            return int(self.winId())
        except Exception:  # pragma: no cover
            return None

    @staticmethod
    def _is_headless_platform():
        """离屏/最小化测试平台没有真实 HWND，跳过所有 Win32 调用。

        否则 `winId()` 会返回一个假句柄，GetWindowLongPtrW/SetWindowPos
        对着它调用虽不会崩，但会让离屏测试结果依赖平台细节。
        """
        app = QtWidgets.QApplication.instance()
        if app is None:
            return False
        return app.platformName() in ('offscreen', 'minimal')

    def enable_frameless_shell(self):
        """打开无边框外壳：砍标题栏、留边框和阴影，并刷新窗口框架。

        在 `__init__` 里、窗口 show 之前调用。它不设置
        `Qt.FramelessWindowHint` —— 那会把 `WS_THICKFRAME` 一起去掉，
        贴靠和原生缩放就没了。改由 `WM_NCCALCSIZE` 精确地只砍标题栏。
        """
        self._native_titlebar = False
        if not IS_WINDOWS or self._is_headless_platform():
            return
        # 保证窗口句柄已经创建。
        hwnd = self._frameless_hwnd()
        if not hwnd:
            return
        # 补上最小化/最大化框和系统菜单样式，最大化/贴靠才正常。
        style = _user32.GetWindowLongPtrW(hwnd, GWL_STYLE)
        style |= WS_MAXIMIZEBOX | WS_MINIMIZEBOX | WS_THICKFRAME | WS_SYSMENU
        _user32.SetWindowLongPtrW(hwnd, GWL_STYLE, style)
        # 强制重算非客户区，让 WM_NCCALCSIZE 立刻生效。
        _user32.SetWindowPos(hwnd, 0, 0, 0, 0, 0,
                             SWP_NOMOVE | SWP_NOSIZE | SWP_NOZORDER
                             | SWP_FRAMECHANGED)

    def set_native_titlebar(self, visible):
        """在「无边框外壳」和「系统原生标题栏」之间切换（方案 §9.2）。

        不用 `FramelessWindowHint`+destroy/create —— 那条路会连
        `WS_THICKFRAME` 一起去掉，贴靠和八向缩放就失效了。这里只翻一个
        标志位再让系统重算非客户区：`_handle_nccalcsize` 看到标志为真时
        直接返回默认结果，系统就把白色标题栏画回来。
        """
        self._native_titlebar = bool(visible)
        if not IS_WINDOWS or self._is_headless_platform():
            return
        hwnd = self._frameless_hwnd()
        if hwnd:
            _user32.SetWindowPos(hwnd, 0, 0, 0, 0, 0,
                                 SWP_NOMOVE | SWP_NOSIZE | SWP_NOZORDER
                                 | SWP_FRAMECHANGED)

    def nativeEvent(self, eventType, message):
        # ⚠️ PyQt6 6.7.0 实测：在 nativeEvent 里调用 `super().nativeEvent(...)`
        # 会触发原生 access violation 崩溃（窗口句柄一创建就崩）。返回
        # `(False, 0)` 表示"我不处理这条消息"，Qt 平台层会照常把它交给
        # DefWindowProc —— 效果与调用父类实现完全一致，但不会崩。所以这里
        # 全程绝不调用 super().nativeEvent。
        if not IS_WINDOWS or eventType not in (
                b'windows_generic_MSG', b'windows_dispatcher_MSG'):
            return False, 0
        try:
            msg = wintypes.MSG.from_address(int(message))
        except Exception:  # pragma: no cover - 拿不到消息就交给父类
            return False, 0

        if msg.message == WM_NCCALCSIZE and msg.wParam:
            return self._handle_nccalcsize(eventType, message, msg)
        return False, 0

    def _handle_nccalcsize(self, eventType, message, msg):
        """只砍掉标题栏那一条，其余边框/阴影保持系统默认。

        做法：先让系统按默认规则算一遍客户区（它会把标题栏和四周边框都
        从客户区里刨掉），然后把客户区收回到窗口矩形 —— 等于把标题栏占的
        那块还给客户区。普通状态下只收**上边缘**，左右下的可缩放边框原样
        保留；这样既有 PureRef/Eagle 那种无白条的观感，又保住了原生缩放和
        阴影。

        最大化时（Eagle 最大化也没有白条）把客户区铺满整个窗口矩形：系统
        给最大化窗口算的矩形本身就被限制在工作区内，所以铺满后依然不会遮
        任务栏。全屏时不动（全屏本来就没有标题栏）。
        用户显式要回原生标题栏时（`_native_titlebar`）也不动。
        """
        if getattr(self, '_native_titlebar', False):
            return False, 0
        if self.isFullScreen():
            return False, 0
        params = ctypes.cast(
            msg.lParam, ctypes.POINTER(_NCCALCSIZE_PARAMS)).contents
        original = params.rgrc[0]
        original_rect = (original.left, original.top,
                         original.right, original.bottom)
        result = _DefWindowProcW(msg.hWnd, WM_NCCALCSIZE,
                                 msg.wParam, msg.lParam)
        if self.isMaximized():
            # 铺满工作区矩形：砍掉白条且不遮任务栏。
            (params.rgrc[0].left, params.rgrc[0].top,
             params.rgrc[0].right, params.rgrc[0].bottom) = original_rect
        else:
            # 把标题栏高度还给客户区（左右下边框保持不变）。
            params.rgrc[0].top = original_rect[1]
        return True, result


class TitleBarDragArea(QtWidgets.QWidget):
    """标题栏里的空白拖动区：按住拖动窗口，双击最大化 / 还原。

    用 `startSystemMove()` 交给系统，贴靠、多显示器、DPI 全部原生处理
    （方案 §9.2"使用系统窗口移动能力优先"）。它铺在菜单栏和窗口控制按钮
    之间的空白处，所以菜单和按钮的点击不受影响。
    """

    def __init__(self, window, parent=None):
        super().__init__(parent)
        self._window = window
        self.setObjectName('titleBarDragArea')
        self.setCursor(QtGui.QCursor(QtCore.Qt.CursorShape.ArrowCursor))
        self.setAttribute(QtCore.Qt.WidgetAttribute.WA_StyledBackground, True)
        self._drag_pos = None

    def mousePressEvent(self, event):
        if event.button() == QtCore.Qt.MouseButton.LeftButton:
            handle = self._window.windowHandle()
            if handle is not None and hasattr(handle, 'startSystemMove'):
                if handle.startSystemMove():
                    event.accept()
                    return
            # 兜底：极老的平台没有 startSystemMove，才手动拖。
            self._drag_pos = (event.globalPosition().toPoint()
                              - self._window.frameGeometry().topLeft())
            event.accept()
            return
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event):
        if self._drag_pos is not None and (
                event.buttons() & QtCore.Qt.MouseButton.LeftButton):
            self._window.move(event.globalPosition().toPoint() - self._drag_pos)
            event.accept()
            return
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event):
        self._drag_pos = None
        super().mouseReleaseEvent(event)

    def mouseDoubleClickEvent(self, event):
        if event.button() == QtCore.Qt.MouseButton.LeftButton:
            if self._window.isMaximized():
                self._window.showNormal()
            else:
                self._window.showMaximized()
            event.accept()
            return
        super().mouseDoubleClickEvent(event)


class TitleBarControls(QtWidgets.QWidget):
    """标题栏右侧的最小化 / 最大化-还原 / 关闭按钮。"""

    def __init__(self, window, parent=None):
        super().__init__(parent)
        self._window = window
        self.setObjectName('titleBarControls')
        layout = QtWidgets.QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        self.min_button = self._make('titleBarMin', '最小化', '─')
        self.max_button = self._make('titleBarMax', '最大化', '□')
        self.close_button = self._make('titleBarClose', '关闭', '✕')

        self.min_button.clicked.connect(window.showMinimized)
        self.max_button.clicked.connect(self._toggle_maximize)
        # 关闭走主窗口的 closeEvent（含未保存确认），不直接退进程。
        self.close_button.clicked.connect(window.close)

        for button in (self.min_button, self.max_button, self.close_button):
            layout.addWidget(button)

        # QWidget 没有 windowStateChanged 信号（那是 QWindow 的）。用事件
        # 过滤器监听 WindowStateChange，最大化 / 还原时同步按钮字形。
        window.installEventFilter(self)
        self._sync_max_button(window.windowState())

    def eventFilter(self, watched, event):
        if watched is self._window and (
                event.type() == QtCore.QEvent.Type.WindowStateChange):
            self._sync_max_button(self._window.windowState())
        return super().eventFilter(watched, event)

    def _make(self, name, tip, glyph):
        button = QtWidgets.QToolButton()
        button.setObjectName(name)
        button.setText(glyph)
        button.setToolTip(tip)
        button.setAccessibleName(tip)
        button.setFocusPolicy(QtCore.Qt.FocusPolicy.NoFocus)
        button.setFixedSize(46, 30)
        return button

    def _toggle_maximize(self):
        if self._window.isMaximized():
            self._window.showNormal()
        else:
            self._window.showMaximized()

    def _sync_max_button(self, state):
        maximized = bool(state & QtCore.Qt.WindowState.WindowMaximized)
        self.max_button.setText('❐' if maximized else '□')
        self.max_button.setToolTip('还原' if maximized else '最大化')
        self.max_button.setAccessibleName(self.max_button.toolTip())
