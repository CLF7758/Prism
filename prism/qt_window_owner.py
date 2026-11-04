"""Validate a foreign native owner before Qt wraps its window handle."""
import sys


def present_owned_preview(widget, title, owner_id=0):
    """Map a prewarmed hidden preview before raising it, retaining its owner."""
    from PyQt6 import QtCore, QtWidgets
    app = QtWidgets.QApplication.instance()
    hidden_surface = QtCore.Qt.WidgetAttribute.WA_DontShowOnScreen
    if widget.testAttribute(hidden_surface):
        # Qt already considers the prewarmed widget visible. Changing this
        # flag alone does not map its native window; showNormal can be a no-op.
        widget.hide()
        widget.setAttribute(hidden_surface, False)
    owner = bind_window_owner(widget, owner_id) if owner_id else None
    widget.setWindowTitle(title)
    widget.showNormal()
    widget.raise_()
    widget.activateWindow()
    visible = None
    if sys.platform == 'win32' and app.platformName() == 'windows':
        import ctypes
        from ctypes import wintypes
        user = ctypes.WinDLL('user32', use_last_error=True)
        user.IsWindowVisible.argtypes = [wintypes.HWND]
        user.IsWindowVisible.restype = wintypes.BOOL
        visible = bool(user.IsWindowVisible(wintypes.HWND(int(widget.winId()))))
    rect = widget.frameGeometry()
    return owner, dict(qt_visible=widget.isVisible(), native_visible=visible,
                      minimized=widget.isMinimized(),
                      window_rect=[rect.x(), rect.y(), rect.width(), rect.height()])


def bind_window_owner(widget, owner_id):
    from PyQt6 import QtGui, QtWidgets
    app = QtWidgets.QApplication.instance()
    if (not app or app.closingDown() or app.platformName() != 'windows'
            or sys.platform != 'win32' or not owner_id):
        return None
    import ctypes
    from ctypes import wintypes
    user = ctypes.WinDLL('user32', use_last_error=True)
    user.IsWindow.argtypes = [wintypes.HWND]
    user.IsWindow.restype = wintypes.BOOL
    if not user.IsWindow(wintypes.HWND(int(owner_id))):
        return None
    widget.winId()
    handle = widget.windowHandle()
    if handle is None:
        return None
    owner = QtGui.QWindow.fromWinId(int(owner_id))
    if owner is not None:
        handle.setTransientParent(owner)
    return owner
