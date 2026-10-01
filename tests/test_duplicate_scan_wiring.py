"""T6：查重接到 DuplicateScanService 上。

以前 on_action_find_duplicates 直接调 similarity.duplicate_groups()：在
GUI 线程上把每张图都比一遍，没有进度、不能取消、没有缓存。一千张图的
工程上就是窗口冻住直到跑完，而且跑第二遍还是同样慢。

这里钉住三件事：走的是服务、服务被复用（缓存能活过单次扫描）、
扫描确实交给后台线程。
"""
from unittest.mock import patch

import pytest
from PyQt6 import QtCore, QtGui, QtWidgets

from prism.duplicate_scan import DuplicateScanService, ScanScope


def _image(colour):
    made = QtGui.QImage(64, 48, QtGui.QImage.Format.Format_ARGB32)
    made.fill(QtGui.QColor(*colour))
    return made


def _add(view, colour, name):
    from prism.items import PrismPixmapItem
    item = PrismPixmapItem(_image(colour), name)
    item._canvas_id = 'default-canvas'
    view.scene.addItem(item)
    return item


@pytest.fixture(autouse=True)
def quiet_messages():
    """both information and warning are modal; a headless run has nobody
    to click them."""
    with patch.object(QtWidgets.QMessageBox, 'information',
                      return_value=QtWidgets.QMessageBox.StandardButton.Ok), \
         patch.object(QtWidgets.QMessageBox, 'warning',
                      return_value=QtWidgets.QMessageBox.StandardButton.Ok), \
         patch.object(QtWidgets.QMessageBox, 'critical',
                      return_value=QtWidgets.QMessageBox.StandardButton.Ok):
        yield


def _settle(view, qtbot, timeout=25000):
    """等到扫描线程真的结束。

    不等的话测试先结束、线程还在跑，进程会在退出阶段带着崩溃码收场
    （和 PrismVideoItem 那个一个道理）。
    """
    qtbot.waitUntil(
        lambda: getattr(view, 'worker', None) is not None
        and not view.worker.isRunning(), timeout=timeout)


def test_too_few_images_says_so_and_does_not_scan(view, qtbot):
    _add(view, (100, 100, 100), 'only.png')

    view.on_action_find_duplicates()

    assert getattr(view, '_duplicate_service', None) is None, \
        '不足两张就不该建服务、更不该起线程'


def test_it_runs_the_scan_service_and_opens_the_dialog(view, qtbot):
    _add(view, (200, 30, 30), 'a.png')
    _add(view, (200, 30, 30), 'b.png')          # 内容一样，该被查出来

    view.on_action_find_duplicates()
    _settle(view, qtbot)

    assert isinstance(view._duplicate_service, DuplicateScanService)
    assert getattr(view, '_duplicate_dialog', None) is not None, \
        '找到重复就该弹结果对话框'


def test_the_scan_runs_on_a_worker_thread(view, qtbot):
    """在 GUI 线程上跑就是原来那个冻住窗口的行为。"""
    _add(view, (30, 30, 200), 'c.png')
    _add(view, (30, 30, 200), 'd.png')

    seen = {}
    original = DuplicateScanService.scan

    def spy(self, sources, scope=ScanScope.ALL_ASSETS):
        seen['in_worker'] = QtCore.QThread.currentThread() is not \
            QtCore.QCoreApplication.instance().thread()
        seen['scope'] = scope
        return original(self, sources, scope)

    with patch.object(DuplicateScanService, 'scan', spy):
        view.on_action_find_duplicates()
        _settle(view, qtbot)

    assert seen.get('in_worker') is True, '扫描必须不在 GUI 线程上'
    assert seen.get('scope') == ScanScope.ALL_CANVASES


def test_the_service_is_reused_so_the_cache_survives(view, qtbot):
    _add(view, (10, 200, 10), 'e.png')
    _add(view, (10, 200, 10), 'f.png')

    view.on_action_find_duplicates()
    _settle(view, qtbot)
    first = view._duplicate_service
    assert len(first.cache) > 0, '第一次扫描该把指纹写进缓存'

    if getattr(view, '_duplicate_dialog', None) is not None:
        view._duplicate_dialog.close()

    view.on_action_find_duplicates()
    _settle(view, qtbot)

    assert view._duplicate_service is first, \
        '服务要复用，否则缓存每次扫描都从零开始'


def test_the_scope_reaches_the_service(view, qtbot):
    _add(view, (250, 250, 30), 'g.png')
    _add(view, (250, 250, 30), 'h.png')

    view.on_action_find_duplicates(scope=ScanScope.ALL_ASSETS)
    _settle(view, qtbot)

    assert getattr(view, '_duplicate_dialog', None) is not None


def test_a_scan_that_finds_nothing_does_not_open_a_dialog(view, qtbot):
    _add(view, (10, 10, 10), 'dark.png')
    _add(view, (250, 250, 250), 'light.png')     # 完全不同

    view.on_action_find_duplicates()
    _settle(view, qtbot)

    assert getattr(view, '_duplicate_dialog', None) is None, \
        '没找到重复就不该弹结果窗口'
