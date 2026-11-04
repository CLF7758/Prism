"""Tab in editable fields must not invoke the sidebar visibility shortcut."""
from PyQt6 import QtCore, QtWidgets


def test_tab_finishes_material_field_without_hiding_sidebars(main_window, qtbot):
    window=main_window
    window.resize(1440,900)
    window.show()
    window._category_action.setChecked(True)
    window._inspector_action.setChecked(True)
    editor=QtWidgets.QDoubleSpinBox(window._right_column)
    editor.move(20,80)
    editor.show()
    editor.setFocus()
    qtbot.waitUntil(lambda:editor.hasFocus() or editor.lineEdit().hasFocus())
    editor.setValue(.5)
    qtbot.keyClick(editor,QtCore.Qt.Key.Key_Tab)
    assert window._category_action.isChecked()
    assert window._inspector_action.isChecked()
    assert not window._right_column.isHidden()
