"""取证：QtWebEngine 面（QWebEngineView）的创建成本是**一次性**还是**每个视图**。

背景：线上会话 `Prism-operations-156040.jsonl` 里 `materials.cards.create_surface`
n=4，max=445.3ms，其余三次各约 1ms。若代价是一次性的（QtWebEngine 子进程/全局
profile 初始化），就可以在**窗口还没显示**的时候预热掉它，窗口从此不会被它冻住。

用法：
    $env:PYTHONIOENCODING='utf-8'; $env:QT_QPA_PLATFORM='offscreen'
    .\\.venv\\Scripts\\python.exe tests\\_audit_webengine_warmup.py
"""
from __future__ import annotations

import os
import time

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

from PyQt6 import QtCore, QtWidgets  # noqa: E402


def main() -> int:
    import sys
    # Qt WebEngine needs these before QCoreApplication exists.
    QtCore.QCoreApplication.setAttribute(
        QtCore.Qt.ApplicationAttribute.AA_ShareOpenGLContexts, True)
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication(sys.argv)
    from PyQt6 import QtWebEngineWidgets

    timings = []
    views = []
    for index in range(4):
        started = time.perf_counter()
        view = QtWebEngineWidgets.QWebEngineView()
        created = time.perf_counter()
        view.setHtml('<html><body>warm</body></html>')
        app.processEvents()
        loaded = time.perf_counter()
        timings.append((index, (created - started) * 1000, (loaded - created) * 1000))
        views.append(view)
    for index, created_ms, loaded_ms in timings:
        print(f'  view #{index}: create={created_ms:8.1f}ms  first-paint={loaded_ms:8.1f}ms')
    total = sum(created for _, created, _ in timings)
    print(f'\n  4 views create total = {total:.1f}ms')
    print('  => 一次性初始化' if timings[1][1] < timings[0][1] / 4 else '  => 每个视图都要付')
    for view in views:
        view.deleteLater()
    app.processEvents()
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
