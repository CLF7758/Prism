"""Shared material picker anchors at the clicked swatch above topmost preview."""
import pytest
from PyQt6 import QtCore, QtGui, QtWidgets, sip

from prism.widgets.material_color import MaterialColorDialog
from prism.widgets.material_slots import MaterialSlot


@pytest.mark.parametrize('edge', [False, True])
def test_material_picker_is_topmost_and_next_to_swatch(qtbot, qapp, edge):
    button = QtWidgets.QToolButton()
    qtbot.addWidget(button)
    button.resize(48, 26)
    available = qapp.primaryScreen().availableGeometry()
    button.move(available.right()-60 if edge else available.left()+80,
                available.bottom()-50 if edge else available.top()+30)
    button.show()
    dialog = MaterialColorDialog([.2,.5,.8], button, anchor=button)
    qtbot.addWidget(dialog)
    dialog.show()
    qtbot.waitUntil(dialog.isVisible)
    assert dialog.windowFlags() & QtCore.Qt.WindowType.WindowStaysOnTopHint
    anchor = QtCore.QRect(button.mapToGlobal(QtCore.QPoint()), button.size())
    frame = dialog.frameGeometry()
    assert available.contains(frame)
    if edge:
        assert frame.bottom() < anchor.top()
    else:
        assert 0 < frame.top()-anchor.bottom() <= 10
        assert abs(frame.left()-anchor.left()) <= 2
    assert dialog.core.parentWidget() is dialog
    dialog.reject()


@pytest.mark.parametrize('key', ['basecolor','emissive'])
@pytest.mark.parametrize('accepted', [True, False])
def test_slot_opens_at_actual_button_and_keeps_apply_cancel_semantics(
        qtbot, qapp, monkeypatch, key, accepted):
    slot = MaterialSlot(key, key)
    qtbot.addWidget(slot)
    slot.fold.setChecked(True)
    slot.show()
    before = list(slot._color)
    changes, dialogs = [], []
    slot.options_requested.connect(lambda role, patch: changes.append((role,patch)))
    real_exec = MaterialColorDialog.exec
    def execute(dialog):
        dialogs.append((dialog, dialog.core))
        assert dialog._anchor is slot.color
        def choose():
            dialog.set_color(QtGui.QColor('#ff0000'))
            dialog.accept() if accepted else dialog.reject()
        QtCore.QTimer.singleShot(0, choose)
        return real_exec(dialog)
    monkeypatch.setattr(MaterialColorDialog, 'exec', execute)
    slot._choose_color()
    if accepted:
        assert slot._color == [1.,0.,0.]
        assert changes == [(key, {'color':[1.,0.,0.]})]
    else:
        assert slot._color == before
        assert changes == []
    QtCore.QCoreApplication.sendPostedEvents(None, QtCore.QEvent.Type.DeferredDelete)
    assert all(sip.isdeleted(dialog) and sip.isdeleted(core) for dialog,core in dialogs)
