from unittest.mock import patch, MagicMock

from PyQt6 import QtCore, QtTest

from prism.__main__ import PrismMainWindow, main
from prism.assets import BeeAssets
from prism.view import PrismGraphicsView
from prism import ui_tokens


@patch('PyQt6.QtWidgets.QWidget.show')
def test_prism_mainwindow_init(show_mock, qapp):
    window = PrismMainWindow(qapp)
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


def test_narrow_window_inspector_drawer_restores_panel(qapp, main_window):
    window = main_window
    window.resize(*ui_tokens.LAYOUT['window-min'])
    qapp.processEvents()
    window._apply_responsive_panels()
    assert window.view._detail_panel.isHidden()
    window._toggle_sidebar('right')
    assert window._drawer_side == 'right'
    assert window.view._detail_panel.parent() is window._drawer
    QtTest.QTest.keyClick(window._drawer_scrim, QtCore.Qt.Key.Key_Escape)
    assert window._drawer_side is None
    assert window.splitter.indexOf(window.view._detail_panel) == 2
    window._category_action.setChecked(False)
    window._toggle_sidebar('left')
    assert window._drawer_side == 'left'
    QtTest.QTest.mouseClick(
        window._drawer_scrim, QtCore.Qt.MouseButton.LeftButton,
        pos=QtCore.QPoint(window._drawer_scrim.width() - 10, 20))
    assert window._drawer_side is None
    window._category_action.setChecked(True)


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

    with patch.object(qapp, 'exec') as exec_mock, \
            patch('prism.__main__.platform.python_version',
                  side_effect=ValueError('failed to parse vendor sys.version')) as version_parser:
        main()
        exec_mock.assert_called_once_with()
        version_parser.assert_not_called()

    args_mock.assert_called_once_with(with_check=True)
    startup_mock.assert_called()
