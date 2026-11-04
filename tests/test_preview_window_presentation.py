"""Qt presentation lifecycle on offscreen QPA; never maps a user desktop window."""
from PyQt6 import QtCore, QtWidgets

from prism.qt_window_owner import present_owned_preview


def test_prewarmed_window_is_exposed_and_can_reopen(qapp, qtbot):
    window = QtWidgets.QDialog()
    qtbot.addWidget(window)
    hidden = QtCore.Qt.WidgetAttribute.WA_DontShowOnScreen
    window.setAttribute(hidden, True)
    window.show()
    qapp.processEvents()
    assert window.isVisible()
    assert not window.windowHandle().isExposed()
    owner, facts = present_owned_preview(window, 'model preview')
    assert owner is None and facts['qt_visible']
    assert not window.testAttribute(hidden)
    qtbot.waitUntil(lambda:window.windowHandle().isExposed())
    assert window.windowTitle() == 'model preview'
    window.hide()
    qapp.processEvents()
    assert not window.windowHandle().isExposed()
    present_owned_preview(window, 'second model')
    qtbot.waitUntil(lambda:window.windowHandle().isExposed())
    assert window.windowTitle() == 'second model'
