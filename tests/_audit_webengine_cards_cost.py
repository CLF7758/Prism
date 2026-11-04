"""Measure what Chromium's window creation really costs the GUI thread.

The live trace charged 445.3 ms to ``materials.cards.create_surface`` - the
card surface's WebEngine view - while the pre-warm was running with the
window already visible, and the stack histogram put 4 of 9 samples inside
``capture_hotkey.py:67 nativeEventFilter``.

Two things could own that time: the filter (every native message crosses
into Python for it) or Chromium's own start-up work.  This probe measures
both by running the same phases with and without a filter installed:

* ``nofilter`` - no native event filter at all
* ``filter``   - the screenshot hotkey's filter, replicated

Run: ``python -u tests/_audit_webengine_cards_cost.py [nofilter|filter] [html|file]``
"""
from __future__ import annotations

import ctypes
import os
import sys
import time
from ctypes import wintypes
from pathlib import Path

from PyQt6 import QtCore

ASSETS = Path(__file__).resolve().parents[1] / 'prism' / 'assets' / 'materials'
CARDS = ASSETS / 'cards.html'
WM_HOTKEY = 0x0312


class HotkeyFilter(QtCore.QAbstractNativeEventFilter):
    """The screenshot hotkey's native filter, byte for byte as the app has it."""

    IDENTIFIER = 0x4353

    def __init__(self):
        super().__init__()
        self.calls = 0
        self.seconds = 0.0

    def nativeEventFilter(self, event_type, message):
        start = time.perf_counter()
        self.calls += 1
        try:
            native = ctypes.cast(int(message), ctypes.POINTER(wintypes.MSG)).contents
            if native.message == WM_HOTKEY and native.wParam == self.IDENTIFIER:
                pass
        except (TypeError, ValueError):
            pass
        self.seconds += time.perf_counter() - start
        return False, 0

    def report(self, label):
        print(f'  {label:10s} native_messages={self.calls:7d} '
              f'in_filter={self.seconds * 1000:8.1f}ms')
        self.calls = 0
        self.seconds = 0.0


class EventLoopWatch:
    """The longest gap between two event-loop turns, the way a user feels it."""

    def __init__(self):
        from PyQt6 import QtCore
        self._timer = QtCore.QTimer()
        self._timer.setInterval(5)
        self._timer.timeout.connect(self._tick)
        self._last = time.perf_counter()
        self.max_gap = 0.0

    def _tick(self):
        now = time.perf_counter()
        self.max_gap = max(self.max_gap, now - self._last)
        self._last = now

    def __enter__(self):
        self._last = time.perf_counter()
        self._timer.start()
        return self

    def __exit__(self, *exc):
        self._timer.stop()
        return False


def pump(app, seconds: float) -> None:
    from PyQt6 import QtCore
    deadline = time.perf_counter() + seconds
    while time.perf_counter() < deadline:
        app.processEvents(QtCore.QEventLoop.ProcessEventsFlag.AllEvents, 20)


def make_view():
    from PyQt6 import QtCore, QtWebEngineWidgets
    view = QtWebEngineWidgets.QWebEngineView()
    view.setAttribute(QtCore.Qt.WidgetAttribute.WA_DontShowOnScreen, True)
    view.setAttribute(QtCore.Qt.WidgetAttribute.WA_ShowWithoutActivating, True)
    return view


def cards_phase(app, label: str, filter: HotkeyFilter | None) -> None:
    from PyQt6 import QtCore, QtGui
    start = time.perf_counter()
    view = make_view()
    construct = time.perf_counter() - start
    start = time.perf_counter()
    view.page().setBackgroundColor(QtGui.QColor('#1c1c1e'))
    page_cost = time.perf_counter() - start
    with EventLoopWatch() as watch:
        start = time.perf_counter()
        view.load(QtCore.QUrl.fromLocalFile(str(CARDS)))
        load_cost = time.perf_counter() - start
        pump(app, 2.0)
    print(f'  {label:10s} construct={construct * 1000:7.1f}ms page()={page_cost * 1000:7.1f}ms '
          f'load()={load_cost * 1000:7.1f}ms  max_gap={watch.max_gap * 1000:8.1f}ms')
    if filter is not None:
        filter.report(label)


def main() -> int:
    mode = sys.argv[1] if len(sys.argv) > 1 else 'nofilter'
    warm = sys.argv[2] if len(sys.argv) > 2 else 'html'
    os.environ.setdefault('QTWEBENGINE_CHROMIUM_FLAGS',
                          '--disable-features=RendererCodeIntegrity')
    from PyQt6 import QtWidgets
    app = QtWidgets.QApplication(sys.argv[:1])
    hotkey_filter = HotkeyFilter() if mode == 'filter' else None
    if hotkey_filter is not None:
        app.installNativeEventFilter(hotkey_filter)
    print(f'mode={mode} warm={warm} cards_exists={CARDS.exists()}')

    with EventLoopWatch() as watch:
        start = time.perf_counter()
        warm_view = make_view()
        construct = time.perf_counter() - start
        start = time.perf_counter()
        warm_view.page()
        page_cost = time.perf_counter() - start
        start = time.perf_counter()
        if warm == 'file':
            warm_view.load(QtCore.QUrl.fromLocalFile(str(CARDS)))
        else:
            warm_view.setHtml('<html><body></body></html>')
        load_cost = time.perf_counter() - start
        warm_view.show()
        warm_view.hide()
        pump(app, 2.0)
    print(f'  {"warm":10s} construct={construct * 1000:7.1f}ms page()={page_cost * 1000:7.1f}ms '
          f'load()={load_cost * 1000:7.1f}ms  max_gap={watch.max_gap * 1000:8.1f}ms')
    if hotkey_filter is not None:
        hotkey_filter.report('warm')

    cards_phase(app, 'cards#1', hotkey_filter)
    cards_phase(app, 'cards#2', hotkey_filter)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
