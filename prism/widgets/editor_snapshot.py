"""Bounded WebEngine read-back shared by saving/exporting web editors."""
from PyQt6 import QtCore
from prism.item_lifecycle import is_deleted
import logging

logger = logging.getLogger(__name__)


def owner_alive(owner):
    return not isinstance(owner, QtCore.QObject) or not is_deleted(owner)


def _valid(valid):
    try:
        return valid is None or bool(valid())
    except Exception:
        logger.exception('Editor snapshot ownership validation failed; content rejected')
        return False


def request_editor_snapshot(page, script, callback, *, valid=None, timeout_ms=3000):
    """Read once without a nested event loop; late/invalid results fail closed."""
    timer = QtCore.QTimer()
    timer.setSingleShot(True)
    active = True
    page_gone = False

    def destroyed(*_args):
        nonlocal page_gone
        page_gone = True
        finished()

    def finished(value=None):
        nonlocal active
        if not active:
            return
        active = False
        timer.stop()
        timer.deleteLater()
        if isinstance(page, QtCore.QObject) and owner_alive(page):
            try:
                page.destroyed.disconnect(destroyed)
            except (TypeError, RuntimeError):
                pass
        if page_gone or not owner_alive(page) or not _valid(valid):
            value = None
        callback(value if isinstance(value, str) else None)

    timer.timeout.connect(finished)
    if not owner_alive(page) or page is None:
        finished()
    else:
        if isinstance(page, QtCore.QObject):
            page.destroyed.connect(destroyed)
        timer.start(timeout_ms)
        try:
            page.runJavaScript(script, finished)
        except RuntimeError:
            finished()
    return finished


def read_editor_snapshot(page, script, *, valid=None, timeout_ms=3000):
    if not owner_alive(page):
        return None
    result = []
    loop = QtCore.QEventLoop()

    def finished(value):
        result.append(value)
        loop.quit()

    settle = request_editor_snapshot(page, script, finished, valid=valid, timeout_ms=timeout_ms)
    try:
        # Test adapters and some cached callbacks complete synchronously.
        if not result:
            loop.exec()
    finally:
        # App shutdown can end a nested loop before the renderer or timeout.
        # Close this request now; late replies must never target that loop.
        settle()
    if not owner_alive(page) or not _valid(valid):
        return None
    return result[0] if result and isinstance(result[0], str) else None
