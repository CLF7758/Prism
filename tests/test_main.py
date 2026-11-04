from unittest.mock import patch, MagicMock
import gc
import subprocess
import sys

from PyQt6 import QtCore, QtTest

from prism.__main__ import GC_THRESHOLDS, PrismMainWindow, main, tune_gc_for_large_projects
from prism.assets import BeeAssets
from prism.view import PrismGraphicsView
from prism import ui_tokens


def test_the_collector_walks_the_whole_heap_rarely():
    """全量回收在大工程里是 17-144ms 的停顿，默认阈值太密。

    线上 trace：一次开工程 6 次全量回收（425.2ms，其中 247.3ms 在 GUI
    线程）；最重的会话一次开工程累计 2.32s，其中 1.75s 落在 GUI 线程。
    真实 100k 工程实测（collector 计量罩住打开过程）：
    (700,10,10) 全量回收 5 次 / 349.8ms / 最长 100.5ms；
    (700,10,100) 全量回收 0 次 / 49.2ms / 最长 12.8ms。
    """
    previous = gc.get_threshold()
    try:
        tune_gc_for_large_projects()
        assert gc.get_threshold() == GC_THRESHOLDS
        # 默认 (700, 10, 10)：每 70000 次分配就走一遍全部存活对象。
        assert gc.get_threshold()[2] >= 100
    finally:
        gc.set_threshold(*previous)


def test_closing_the_window_leaves_the_window_state_on_disk(qapp, qtbot, main_window):
    """窗口状态是退出时最后写的设置，必须在关窗返回前就落盘。

    后台写线程按 150ms 合并窗口写盘，`aboutToQuit` 在之后才兜底。
    进程在关窗后立刻被杀时，只有这里同步落盘才能保住窗口位置。
    """
    from PyQt6 import QtCore

    window = main_window
    window.show()
    qapp.processEvents()
    path = window.view.settings.fileName()
    window.close()
    disk = QtCore.QSettings(path, QtCore.QSettings.Format.IniFormat)
    disk.sync()
    assert disk.contains('MainWindow/geometry')


def test_importing_window_classes_preserves_host_exception_hook():
    result = subprocess.run([sys.executable, '-c',
        'import sys; sentinel = lambda *args: None; sys.excepthook = sentinel; '
        'import prism.__main__; assert sys.excepthook is sentinel'],
        capture_output=True, text=True, timeout=20,
        creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
    assert result.returncode == 0, result.stderr


@patch('PyQt6.QtWidgets.QWidget.show')
def test_prism_mainwindow_init(show_mock, qapp, qtbot):
    with patch('prism.browser_capture.BrowserCaptureService.start'), \
         patch('prism.capture_hotkey.CaptureHotkey'), \
         patch('prism.clipboard_capture.ClipboardCaptureAdapter.start'):
        window = PrismMainWindow(qapp)
    qtbot.addWidget(window)
    assert window.windowTitle() == 'Prism'
    assert BeeAssets().logo == BeeAssets().logo
    assert window.windowIcon()
    assert window.contentsMargins() == QtCore.QMargins(0, 0, 0, 0)
    assert isinstance(window.view, PrismGraphicsView)
    show_mock.assert_called()


def test_shell_geometry_and_inspector_stays_open(qapp, main_window):
    window = main_window
    shell = ui_tokens.LAYOUT
    window.resize(*shell['window-default'])
    qapp.processEvents()
    window._apply_responsive_panels()
    assert window.minimumWidth() == shell['window-min'][0]
    assert window.minimumHeight() == shell['window-min'][1]
    # 顶部那条 48 px 的工作区头部按用户要求删掉了（2026-10-01）。
    assert not hasattr(window, 'workspace_header')
    assert window.statusBar().height() == shell['statusbar-height']
    assert window.splitter.handleWidth() == shell['splitter-hotzone']
    assert window.view._detail_panel.isVisible()
    before = window.splitter.sizes()
    window._sync_detail_visibility()
    assert window.view._detail_panel.isVisible()
    assert window.splitter.sizes() == before


def test_narrow_window_sidebars_collapse_without_overlay(qapp, main_window):
    window = main_window
    window.resize(*ui_tokens.LAYOUT['window-min'])
    window.show()
    qapp.processEvents()
    window._apply_responsive_panels()
    before = window._centre_column.width()
    window._toggle_sidebar('right')
    qapp.processEvents()
    assert window._right_column.isHidden()
    assert window._centre_column.width() > before
    assert window.view._detail_panel.parentWidget() is window._right_column
    assert window.splitter.indexOf(window._right_column) == 2
    assert window._window_controls.parentWidget() is window._collapsed_window_host
    window._toggle_sidebar('right')
    qapp.processEvents()
    assert window._right_column.isVisible()
    assert window._window_controls.parentWidget() is window._right_header
    window._sidebar_toggle.click()
    qapp.processEvents()
    assert window._left_column.isHidden() and window._right_column.isHidden()
    assert window._centre_column.width() == window.splitter.width()
    assert not hasattr(window, '_drawer_scrim')
    window._restore_sidebars.click()
    qapp.processEvents()
    assert window._left_column.isVisible() and window._right_column.isVisible()
    assert window._centre_column.width() == before


def test_column_headers_align_with_their_contents(qapp, main_window):
    w = main_window
    w.resize(1440, 900)
    w.show()
    qapp.processEvents()
    assert w._left_header.parentWidget() is w._left_column
    assert w._centre_header.parentWidget() is w._centre_column
    assert w._right_header.parentWidget() is w._right_column
    assert w.filter_bar.isHidden()
    assert w._eagle_header_stack.currentWidget() is w.canvas_toolbar
    assert w._title_controls.parentWidget() is w._window_controls
    assert w._window_controls.parentWidget() is w._right_header
    assert w._right_header.width() == w._right_column.width()
    assert w._left_header.width() == w._left_column.width()
    assert w._centre_header.width() == w.tabs.width()
    left_start = w._left_header.mapTo(w.centralWidget(), QtCore.QPoint()).y()
    right_start = w._right_header.mapTo(w.centralWidget(), QtCore.QPoint()).y()
    assert left_start == right_start == 0


def test_tab_shortcut_toggles_layout_columns(qapp, main_window):
    w = main_window
    w.show()
    w.activateWindow()
    w.asset_browser.open('all')
    w.asset_browser.list.setFocus()
    qapp.processEvents()
    QtTest.QTest.keyClick(w.asset_browser.list, QtCore.Qt.Key.Key_Tab)
    qapp.processEvents()
    assert w._left_column.isHidden() and w._right_column.isHidden()
    QtTest.QTest.keyClick(w.asset_browser.list, QtCore.Qt.Key.Key_Tab)
    qapp.processEvents()
    assert w._left_column.isVisible() and w._right_column.isVisible()


@patch('prism.view.PrismGraphicsView.open_from_file')
def test_prismapplication_fileopenevent(open_mock, qapp, main_window):
    event = MagicMock()
    event.type.return_value = QtCore.QEvent.Type.FileOpen
    event.file.return_value = 'test.prism'
    assert qapp.event(event) is True
    open_mock.assert_called_once_with('test.prism')


@patch('prism.__main__.PrismApplication')
@patch('prism.__main__.CommandlineArgs')
@patch('prism.config.PrismSettings.on_startup')
def test_main(startup_mock, args_mock, app_mock, qapp):
    app_mock.return_value = qapp
    args_mock.return_value.filename = None
    args_mock.return_value.loglevel = 'WARN'
    args_mock.return_value.debug_raise_error = ''

    # main() owns and deletes this window. A qtbot-managed fixture would try
    # to close the already-deleted C++ object during its own teardown.
    with patch('prism.browser_capture.BrowserCaptureService.start'), \
         patch('prism.capture_hotkey.CaptureHotkey'), \
         patch('prism.clipboard_capture.ClipboardCaptureAdapter.start'):
        owned_window = PrismMainWindow(qapp)

    with patch.object(qapp, 'exec') as exec_mock, \
            patch.object(sys, 'excepthook'), \
            patch('prism.__main__.PrismMainWindow', return_value=owned_window), \
            patch('prism.__main__.platform.python_version',
                  side_effect=ValueError('failed to parse vendor sys.version')) as version_parser:
        from prism import performance_trace
        try:
            main()
            exec_mock.assert_called_once_with()
            version_parser.assert_not_called()
        finally:
            # A real app.exec() emits aboutToQuit; this mocked event loop
            # does not. Stop its diagnostic timers/watchdog explicitly.
            if performance_trace._active is not None:
                performance_trace._active.close()

    args_mock.assert_called_once_with(with_check=True)
    startup_mock.assert_called()


def test_sidebar_context_menu_controls_each_column(main_window, qapp):
    w = main_window
    w.show()
    menu = w._sidebar_menu()
    entries = menu.actions()
    entries[0].setChecked(False)
    qapp.processEvents()
    assert w._left_column.isHidden()
    assert w._right_column.isVisible()
    entries[1].setChecked(False)
    qapp.processEvents()
    assert w._right_column.isHidden()
    assert w._window_controls.isVisible()
    assert w._title_controls.close_button.isVisible()
    assert w._sidebar_shortcuts[0].key().toString() == 'Ctrl+Alt+1'
    w._toggle_sidebars()
    qapp.processEvents()
    assert w._left_column.isVisible() and w._right_column.isVisible()
