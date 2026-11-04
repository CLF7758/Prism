"""取证：WebEngine 预热视图在**进程退出**时是否安全。

上一轮实测：`QWebEngineView()` 只要 0.4ms，贵的是第一次 `setHtml()`（起渲染
进程）273ms；而探针脚本在退出时崩了（0xC0000005），Qt 打印
"Release of profile requested but WebEnginePage still not deleted"。

所以要比较两种收尾方式：
    keep  —— 视图挂在 app 上活到退出（生产实现目前的做法）
    drop  —— 先把 page 摘掉再删视图
两种方式都必须让进程**干净退出**（退出码 0），否则不能用在启动路径上。

用法：
    .\\.venv\\Scripts\\python.exe -u tests\\_audit_webengine_teardown.py keep
    .\\.venv\\Scripts\\python.exe -u tests\\_audit_webengine_teardown.py drop
"""
from __future__ import annotations

import os
import sys
import time

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

from PyQt6 import QtCore, QtWidgets  # noqa: E402


def main() -> int:
    mode = sys.argv[1] if len(sys.argv) > 1 else 'keep'
    QtCore.QCoreApplication.setAttribute(
        QtCore.Qt.ApplicationAttribute.AA_ShareOpenGLContexts, True)
    app = QtWidgets.QApplication(sys.argv)
    from PyQt6 import QtWebEngineWidgets

    started = time.perf_counter()
    view = QtWebEngineWidgets.QWebEngineView()
    page = view.page()
    view.setHtml('<html><body></body></html>')
    app.processEvents()
    print(f'[{mode}] warm-up {(time.perf_counter()-started)*1000:.1f}ms', flush=True)

    second = time.perf_counter()
    other = QtWebEngineWidgets.QWebEngineView()
    other.page()
    other.setHtml('<html><body></body></html>')
    app.processEvents()
    print(f'[{mode}] second surface {(time.perf_counter()-second)*1000:.1f}ms', flush=True)

    if mode == 'keep':
        app._warm = view
    elif mode == 'drop':
        view.setPage(None)
        view.deleteLater()
        app.processEvents()
    else:
        raise SystemExit(f'unknown mode {mode}')
    print(f'[{mode}] leaving', flush=True)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
