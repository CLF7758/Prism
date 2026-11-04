"""切换到库面板时，面板必须**已经有内容**。

库面板是惰性构造的，卡片面还要等自己的网页加载完。切过去的那一刻如果它
还是空的，中间那块就是一块空板，随后卡片才出现 —— 用户看到的那一下"闪"。
所以切换之前先等面板就绪。
"""
from PyQt6 import QtCore, QtWidgets

from prism.widgets.deferred_library import DeferredLibrary


class _Page(QtWidgets.QWidget):
    """A stand-in library page with the readiness contract."""

    content_ready = QtCore.pyqtSignal()

    def __init__(self):
        super().__init__()
        self.details = QtWidgets.QWidget()
        self.ready = False

    def is_content_ready(self):
        return self.ready


def test_library_host_reports_content_readiness(qtbot):
    page = _Page()
    host = DeferredLibrary(lambda: page)
    qtbot.addWidget(host)
    assert not host.is_content_ready()          # 还没有构造
    host.ensure_created()
    assert not host.is_content_ready()          # 页面还没准备好
    page.ready = True
    assert host.is_content_ready()


def test_wait_for_content_ends_when_the_page_reports_ready(qtbot):
    page = _Page()
    host = DeferredLibrary(lambda: page)
    qtbot.addWidget(host)
    host.ensure_created()
    QtCore.QTimer.singleShot(
        20, lambda: (setattr(page, 'ready', True), page.content_ready.emit()))
    assert host.wait_for_content(timeout_ms=2000) is True
    assert host.is_content_ready()


def test_wait_for_content_gives_up_at_the_deadline(qtbot):
    page = _Page()
    host = DeferredLibrary(lambda: page)
    qtbot.addWidget(host)
    host.ensure_created()
    # 页面一直不报告就绪：等待到点就结束，导航不能被卡死。
    assert host.wait_for_content(timeout_ms=100) is False


def test_switching_to_the_material_library_never_shows_an_empty_panel(main_window, qtbot):
    window = main_window
    window.show()
    window.open_material_library()
    page = window.material_library
    assert window.tabs.currentWidget() is page
    # 切过去的时候卡片面已经就绪 —— 不是"先空着，卡片随后才到"。
    assert page.materials.ready


def test_switching_to_the_model_library_never_shows_an_empty_panel(main_window, qtbot):
    window = main_window
    window.show()
    window.open_model_library()
    qtbot.waitUntil(
        lambda: window.tabs.currentWidget() is window.model_library, timeout=10000)
    assert window.model_library.is_content_ready()
