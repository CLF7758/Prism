"""Does a real F2 key press reach the card page?

The card page renames a card in place: F2 puts an input into the card, the
new name is typed into it and Enter commits it.  The Qt side accepts the
ShortcutOverride of F2 and Delete for the card surface so that the canvas
never swallows them - but an accepted ShortcutOverride also means the key
press is not delivered to the page at all.

This probe sends the events the way Qt does it: the ShortcutOverride first,
and the key press only when the override was ignored.
"""
from __future__ import annotations

import pytest
from PyQt6 import QtCore, QtGui, QtWidgets

from tests.test_material_cards import javascript, show_cards


def _press_f2(app, widget):
    """What Qt does for one key press on the focused widget."""
    override = QtGui.QKeyEvent(QtCore.QEvent.Type.ShortcutOverride,
                                QtCore.Qt.Key.Key_F2, QtCore.Qt.KeyboardModifier.NoModifier)
    app.sendEvent(widget, override)
    accepted = override.isAccepted()
    if not accepted:
        app.sendEvent(widget, QtGui.QKeyEvent(QtCore.QEvent.Type.KeyPress,
                                              QtCore.Qt.Key.Key_F2,
                                              QtCore.Qt.KeyboardModifier.NoModifier))
    return accepted


def test_the_f2_key_reaches_the_card_page(main_window, qtbot):
    page, _records = show_cards(main_window, qtbot)
    grid = page.materials
    app = QtWidgets.QApplication.instance()
    main_window.activateWindow()
    grid.web.setFocus()
    app.processEvents()
    qtbot.wait(200)
    focus = javascript(grid, """(()=>{
        window.__keys=[];
        document.addEventListener('keydown',e=>window.__keys.push(e.key+':'+(e.target.className||e.target.tagName)),true);
        const card=document.querySelector('.asset-card');
        card.focus();
        return document.activeElement.className+'/'+document.activeElement.tagName;})()""", qtbot)
    print(f'\nAUDIT: 页面焦点={focus} web焦点={grid.web.hasFocus()}', flush=True)
    accepted = _press_f2(app, grid.web)
    print(f'AUDIT: ShortcutOverride 被接受={accepted}', flush=True)
    qtbot.wait(200)
    keys = javascript(grid, "JSON.stringify(window.__keys)", qtbot)
    print(f'AUDIT: 页面收到的按键={keys}', flush=True)
    opened = javascript(grid, "Boolean(document.querySelector('.asset-card h3 input'))", qtbot)
    print(f'AUDIT: 卡片里出现重命名输入框={opened}', flush=True)
    assert opened, 'the F2 key press never reached the card page'
