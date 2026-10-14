"""Match native secondary-window chrome to the application's palette."""
import ctypes
import sys

from PyQt6 import QtCore, QtGui, QtWidgets


def apply_native_theme(widget):
    if sys.platform != 'win32' or QtGui.QGuiApplication.platformName() != 'windows':
        return
    if not isinstance(widget, QtWidgets.QDialog) or not widget.isWindow():
        return
    hwnd = int(widget.winId())
    setter = ctypes.windll.dwmapi.DwmSetWindowAttribute
    setter.argtypes = [ctypes.c_void_p, ctypes.c_uint, ctypes.c_void_p, ctypes.c_uint]
    setter.restype = ctypes.c_long
    palette = widget.palette()
    background = palette.color(QtGui.QPalette.ColorRole.Window)
    dark = ctypes.c_int(background.lightness() < 128)
    # Windows 10 used 19 before adopting 20. Unsupported color attributes
    # are ignored by older Windows versions, which still get dark chrome.
    if setter(hwnd, 20, ctypes.byref(dark), ctypes.sizeof(dark)) != 0:
        setter(hwnd, 19, ctypes.byref(dark), ctypes.sizeof(dark))
    for attribute, color in (
            (34, background), (35, background),
            (36, palette.color(QtGui.QPalette.ColorRole.WindowText))):
        value = ctypes.c_uint(color.red() | color.green() << 8 | color.blue() << 16)
        setter(hwnd, attribute, ctypes.byref(value), ctypes.sizeof(value))


class DialogThemeFilter(QtCore.QObject):
    def eventFilter(self, watched, event):
        if isinstance(watched, QtWidgets.QDialog) and event.type() in (
                QtCore.QEvent.Type.Show, QtCore.QEvent.Type.PaletteChange):
            if watched.isVisible():
                apply_native_theme(watched)
        return False


def install(app):
    if not hasattr(app, '_dialog_theme_filter'):
        app._dialog_theme_filter = DialogThemeFilter(app)
        app.installEventFilter(app._dialog_theme_filter)
    return app._dialog_theme_filter
