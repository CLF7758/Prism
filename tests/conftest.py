import os.path
import sys
import pytest
import uuid

from unittest.mock import MagicMock, patch

from PyQt6 import QtGui, QtWidgets


def pytest_configure(config):
    # Ignore logging configuration for Prism during test runs. This
    # avoids logging to the regular log file and spamming test output
    # with debug messages.
    #
    # This needs to be done before the application code is even loaded since
    # logging configuration happens on module level
    import logging.config
    logging.config.dictConfig = MagicMock

    # The shipped UI language is Chinese, but the suite asserts on the
    # English source strings. Several modules (for example
    # prism.widgets.settings) evaluate _() at import time, so the catalogue
    # has to be cleared here, before any test module is imported - a
    # fixture would run too late to affect those module-level strings.
    from prism.i18n.translator import translator
    translator._translations = {}


@pytest.fixture(autouse=True)
def reset_prism_actions():
    from prism.actions.actions import actions
    for key in list(actions.keys()):
        if key.startswith('recent_files_'):
            actions.pop(key)


@pytest.fixture(autouse=True)
def commandline_args():
    config_patcher = patch('prism.view.commandline_args')
    config_mock = config_patcher.start()
    config_mock.filenames = []
    yield config_mock
    config_patcher.stop()


@pytest.fixture(autouse=True)
def settings(tmpdir):
    from prism.config import PrismSettings
    dir_patcher = patch('prism.config.PrismSettings.get_settings_dir',
                        return_value=tmpdir.dirname)
    dir_patcher.start()
    settings = PrismSettings()
    yield settings
    settings.clear()
    dir_patcher.stop()


@pytest.fixture(autouse=True)
def kbsettings(tmpdir):
    from prism.config import KeyboardSettings
    dir_patcher = patch('prism.config.PrismSettings.get_settings_dir',
                        return_value=tmpdir.dirname)
    dir_patcher.start()
    kbsettings = KeyboardSettings()
    yield kbsettings
    try:
        kbsettings.clear()
    except RuntimeError:
        # 已经随 QApplication 一起销毁了。WebEngine 那组测试会自己管
        # app 的生命周期，清理时机可能已经过了。
        pass
    dir_patcher.stop()


@pytest.fixture(autouse=True)
def english_ui_strings():
    """Run tests against the untranslated source strings.

    The shipped default UI language is zh_CN, but the test suite asserts on
    the English source strings written into the code (for example
    'Confirm when closing an unsaved file:'). Without this the translator
    rewrites those strings and unrelated tests fail on the translation.
    """
    from prism.i18n.translator import translator
    previous = translator._translations
    translator._translations = {}
    yield
    translator._translations = previous


@pytest.fixture(autouse=True)
def no_blocking_message_boxes():
    """Keep modal dialogs from hanging a headless test run.

    pytest-qt closes widgets registered with qtbot in its own teardown hook,
    which runs before fixture teardown resumes. If the document still has
    unsaved changes, closeEvent reaches get_confirmation_unsaved_changes(),
    which would open a modal QMessageBox and block the run forever with no
    user to answer it. Tests that assert on that dialog patch
    QMessageBox.question themselves; their inner patch takes precedence over
    this one.

    The same reasoning covers ``PrismGraphicsView._ask_export_mode()``: it
    opens a modal box with custom buttons, which patching
    QMessageBox.question does *not* cover, so a test run would sit there
    forever waiting for a click that never comes.  Answering it with the
    adjusted-preview choice matches what the suite did before that dialog
    existed.
    """
    with patch('PyQt6.QtWidgets.QMessageBox.question',
               return_value=QtWidgets.QMessageBox.StandardButton.Yes), \
         patch('prism.view.PrismGraphicsView._ask_export_mode',
               return_value='adjusted'):
        yield


@pytest.fixture
def main_window(qtbot):
    from prism.__main__ import PrismMainWindow
    app = QtWidgets.QApplication.instance()
    main = PrismMainWindow(app)
    qtbot.addWidget(main)
    yield main
    main.view.undo_stack.setClean()
    main.browser_capture.stop()


@pytest.fixture(autouse=True)
def _settle_deferred_deletes():
    """每个测试结束后把排队的 deleteLater 真正执行掉。

    文档/脑图面板一旦显示就会建 QWebEnginePage。pytest-qt 只把窗口
    `deleteLater()`，不会跑事件循环，于是这些 page 一直攒到会话结束才一起
    析构 —— Qt 打印 "Release of profile requested but WebEnginePage still
    not deleted" 并以 0xC0000005 收场（交接单 §3.7）。

    这个 fixture 不依赖任何东西，所以在 teardown 顺序里排在 `qtbot` 之后
    （fixture 按建立顺序的逆序销毁），正好补上 qtbot 缺的那一步。
    """
    yield
    from PyQt6 import QtCore, QtWidgets
    app = QtWidgets.QApplication.instance()
    if app is None:
        return
    for _ in range(3):
        QtCore.QCoreApplication.sendPostedEvents(
            None, QtCore.QEvent.Type.DeferredDelete)
        app.processEvents()


@pytest.fixture
def view(main_window):
    yield main_window.view


@pytest.fixture
def imgfilename3x3():
    root = os.path.dirname(__file__)
    yield os.path.join(root, 'assets', 'test3x3.png')


@pytest.fixture
def imgdata3x3(imgfilename3x3):
    with open(imgfilename3x3, 'rb') as f:
        imgdata3x3 = f.read()
    yield imgdata3x3


@pytest.fixture
def tmpfile(tmpdir):
    yield os.path.join(tmpdir, str(uuid.uuid4()))


@pytest.fixture
def item():
    from prism.items import PrismPixmapItem
    yield PrismPixmapItem(QtGui.QImage(10, 10, QtGui.QImage.Format.Format_RGB32))


@pytest.fixture(scope="session")
def qapp():
    """整个会话共用的 QApplication。

    **`argv` 不能是空列表。** `QApplication(argv)` 要求至少有一个元素：
    Qt 会保留 `argv[0]` 当程序名，空列表等于让后面用它的东西踩空指针。

    在这里表现为：用 `PrismApplication([])` 的话，建 `QWebEnginePage`
    会 **0xC0000409**（进程被立即杀掉，连 faulthandler 都来不及输出）。
    换成 `PrismApplication([sys.argv[0]])` 就正常 —— 三组对照实验：

        QApplication([sys.argv[0]])        -> 通过
        PrismApplication([sys.argv[0]])    -> 通过
        PrismApplication([])               -> 崩

    `or 'prism'` 是防 `sys.argv` 为空的情况（某些嵌入场景）。
    """
    from prism.__main__ import PrismApplication
    yield PrismApplication([sys.argv[0] or 'prism'])
