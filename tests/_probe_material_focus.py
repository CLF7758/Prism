from PyQt6 import QtWidgets, QtCore
from tests.test_material_cards import show_cards, javascript


def test_probe(main_window, qtbot):
    page, _ = show_cards(main_window, qtbot)
    grid = page.materials
    def describe(widget):
        return None if widget is None else (type(widget).__name__, widget.objectName(), widget.isVisible(), widget.isEnabled(), widget.hasFocus())
    def status(label):
        print(label, 'Qt focus', describe(QtWidgets.QApplication.focusWidget()), 'search', describe(page.search), 'active', describe(QtWidgets.QApplication.activeWindow()), 'web', describe(grid.web), 'proxy', describe(grid.web.focusProxy()), 'dom', javascript(grid, '[document.hasFocus(),document.activeElement.tagName]', qtbot), flush=True)
    status('initial')
    grid.web.setFocus()
    javascript(grid, "(()=>{const c=document.querySelector('.asset-card');c.focus();c.dispatchEvent(new MouseEvent('contextmenu',{bubbles:true,cancelable:true,clientX:60,clientY:60}));return true;})()",qtbot)
    status('menu')
    page.search.setFocus()
    qtbot.wait(100)
    status('search')
    main_window.activateWindow()
    page.search.setFocus()
    qtbot.wait(100)
    status('activated')
