"""Audit: cost of ``enable_frameless_shell`` on a real Windows platform.

Splits the single call into its four Win32 steps and checks whether the
``SetWindowPos(SWP_FRAMECHANGED)`` step actually changes anything the
Windows already did when the HWND was created.

Run it with the *real* platform plugin (not offscreen)::

    $env:PYTHONIOENCODING='utf-8'
    python -u tests/_audit_frameless_cost.py
"""
import ctypes
import os
import sys
import time

os.environ.setdefault('QT_QPA_PLATFORM', 'windows')

from PyQt6 import QtCore, QtWidgets  # noqa: E402

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from prism.widgets.window_shell import (  # noqa: E402
    FramelessShellMixin,
    GWL_STYLE,
    SWP_FRAMECHANGED,
    SWP_NOMOVE,
    SWP_NOSIZE,
    SWP_NOZORDER,
    WS_MAXIMIZEBOX,
    WS_MINIMIZEBOX,
    WS_SYSMENU,
    WS_THICKFRAME,
    _user32,
)

WS_OVERLAPPEDWINDOW = 0x00CF0000


class ProbeWindow(FramelessShellMixin, QtWidgets.QMainWindow):
    """The same mixin order the real window uses."""


def build_window():
    window = ProbeWindow()
    central = QtWidgets.QWidget(window)
    layout = QtWidgets.QVBoxLayout(central)
    for index in range(60):
        layout.addWidget(QtWidgets.QPushButton(f'button {index}', central))
    window.setCentralWidget(central)
    window.resize(1200, 800)
    return window


def report(label, window):
    frame = window.frameGeometry()
    geom = window.geometry()
    print(f'  [{label}] frame=({frame.x()},{frame.y()},'
          f'{frame.width()}x{frame.height()}) '
          f'geom=({geom.x()},{geom.y()},{geom.width()}x{geom.height()}) '
          f'titlebar_gap={geom.y() - frame.y()}')


def run_case(with_setwindowpos):
    window = build_window()
    timings = {}
    start = time.perf_counter()
    hwnd = int(window.winId())
    timings['winId'] = (time.perf_counter() - start) * 1000

    start = time.perf_counter()
    style = _user32.GetWindowLongPtrW(hwnd, GWL_STYLE)
    timings['GetWindowLong'] = (time.perf_counter() - start) * 1000

    start = time.perf_counter()
    new_style = style | WS_MAXIMIZEBOX | WS_MINIMIZEBOX | WS_THICKFRAME | WS_SYSMENU
    _user32.SetWindowLongPtrW(hwnd, GWL_STYLE, new_style)
    timings['SetWindowLong'] = (time.perf_counter() - start) * 1000

    if with_setwindowpos:
        start = time.perf_counter()
        _user32.SetWindowPos(hwnd, 0, 0, 0, 0, 0,
                             0x0002 | 0x0001 | 0x0004 | 0x0020)
        timings['SetWindowPos'] = (time.perf_counter() - start) * 1000
    else:
        timings['SetWindowPos'] = 0.0

    after = _user32.GetWindowLongPtrW(hwnd, GWL_STYLE)
    bits = {
        'WS_OVERLAPPEDWINDOW': bool(style & WS_OVERLAPPEDWINDOW == WS_OVERLAPPEDWINDOW),
        'WS_THICKFRAME': bool(style & WS_THICKFRAME),
        'WS_SYSMENU': bool(style & WS_SYSMENU),
        'WS_MINIMIZEBOX': bool(style & WS_MINIMIZEBOX),
        'WS_MAXIMIZEBOX': bool(style & WS_MAXIMIZEBOX),
    }
    start = time.perf_counter()
    window.show()
    app = QtWidgets.QApplication.instance()
    app.processEvents()
    timings['show+events'] = (time.perf_counter() - start) * 1000
    report('shown', window)
    print(f'  style before=0x{style:X} after=0x{after:X} changed={style != after}')
    print(f'  style bits {bits}')
    for name, value in timings.items():
        print(f'  {name:16s} {value:8.2f} ms')
    window.close()
    app.processEvents()
    return timings


def main():
    app = QtWidgets.QApplication(sys.argv)
    print('=== with SetWindowPos(SWP_FRAMECHANGED) ===')
    a = run_case(True)
    print('=== without SetWindowPos ===')
    b = run_case(False)
    print('=== summary ===')
    for name in a:
        print(f'  {name:16s} with={a[name]:8.2f} ms   without={b[name]:8.2f} ms')


if __name__ == '__main__':
    main()
