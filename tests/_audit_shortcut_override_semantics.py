"""What a ShortcutOverride decides, and what cannot be measured here.

The card surface accepts the ShortcutOverride of F2 and Delete so that the
canvas delete action - which is window scoped - does not claim those keys
while the card page has the focus.

Qt states the rule for QEvent::ShortcutOverride:

    "When a shortcut is about to trigger, ShortcutOverride is sent to the
     active window.  This allows clients (e.g. widgets) to signal that they
     will handle the shortcut themselves, by accepting the event.  **If the
     shortcut override is accepted, the event is delivered as a normal key
     press to the focus widget.  Otherwise, it triggers the shortcut action,
     if one exists.**"
    (https://doc.qt.io/qt-6/qevent.html#ShortcutOverride)

So accepting is what gives the key to the card page, and ignoring it is what
lets the canvas action claim the key.  The project's own code says the same
in ``prism/widgets/model_material_panel.py``: "Accept the override so no
canvas/model undo action claims it."

What cannot be measured in this environment: the shortcut action is matched
by ``QApplication::notify``, and only for *spontaneous* events - events that
came from the window system.  A programmatic ``sendEvent`` is not
spontaneous, so no shortcut is matched and no key press is delivered by Qt
itself.  The end to end behaviour of a real key press therefore needs a real
desktop, and this probe only pins the part that is observable here: the card
surface accepts the override.
"""
from __future__ import annotations

from PyQt6 import QtCore, QtGui, QtWidgets


def test_the_card_surface_accepts_the_override_of_its_own_keys(main_window, qtbot):
    from tests.test_material_cards import show_cards
    page, _records = show_cards(main_window, qtbot)
    grid = page.materials
    app = QtWidgets.QApplication.instance()
    observed = {}
    for name, key in (('F2', QtCore.Qt.Key.Key_F2),
                      ('Delete', QtCore.Qt.Key.Key_Delete)):
        override = QtGui.QKeyEvent(QtCore.QEvent.Type.ShortcutOverride, key,
                                   QtCore.Qt.KeyboardModifier.NoModifier)
        app.sendEvent(grid.web, override)
        observed[name] = override.isAccepted()
    print('\nAUDIT: 卡片面接受的 ShortcutOverride -> %r' % (observed,), flush=True)
    assert observed == {'F2': True, 'Delete': True}
