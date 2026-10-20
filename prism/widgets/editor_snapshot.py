"""Bounded WebEngine read-back shared by saving/exporting web editors."""
from PyQt6 import QtCore
from prism.item_lifecycle import is_deleted


def read_editor_snapshot(page, script, *, valid=None, timeout_ms=3000):
    if isinstance(page, QtCore.QObject) and is_deleted(page):
        return None
    result = []
    active = True
    loop = QtCore.QEventLoop()
    timer = QtCore.QTimer()
    timer.setSingleShot(True)
    timer.timeout.connect(loop.quit)

    def finished(value):
        if active:
            result.append(value)
            loop.quit()

    try:
        page.runJavaScript(script, finished)
        # Test adapters and some cached callbacks complete synchronously.
        if not result:
            timer.start(timeout_ms)
            loop.exec()
    finally:
        active = False
        timer.stop()
    if valid is not None and not valid():
        return None
    return result[0] if result and isinstance(result[0], str) else None
