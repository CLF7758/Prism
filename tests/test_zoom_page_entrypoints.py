import pytest
from PyQt6 import QtCore,QtGui,QtWidgets


def send_wheel(widget):
    point=QtCore.QPoint(10,10)
    event=QtGui.QWheelEvent(QtCore.QPointF(point),QtCore.QPointF(widget.mapToGlobal(point)),
        QtCore.QPoint(),QtCore.QPoint(0,120),QtCore.Qt.MouseButton.NoButton,
        QtCore.Qt.KeyboardModifier.ControlModifier,QtCore.Qt.ScrollPhase.NoScrollPhase,False)
    QtWidgets.QApplication.sendEvent(widget,event)


@pytest.mark.parametrize('mode',['all','unclassified','untagged','recent'])
def test_real_scope_has_visible_page_footer_and_viewport_wheel(main_window,qtbot,mode):
    window=main_window;window.resize(1200,800);window.show()
    browser=window.asset_browser
    browser.open(mode)
    qtbot.waitUntil(lambda:window.tabs.currentWidget() is browser)
    assert browser.zoom_bar.parentWidget() is window.canvas_toolbar
    header=window.canvas_toolbar.layout()
    assert header.indexOf(browser.zoom_bar)+1==header.indexOf(window.canvas_toolbar.filter_button)
    assert browser.zoom_slider.isVisibleTo(window)
    assert browser.zoom_bar.smaller.isVisibleTo(window)
    assert browser.zoom_bar.larger.isVisibleTo(window)
    before=browser.zoom_slider.value()
    browser.zoom_bar.larger.click()
    assert browser.zoom_slider.value()==before+12
    browser.zoom_bar.smaller.click()
    assert browser.zoom_slider.value()==before
    # Actual viewport dispatch, not a direct Python wheelEvent call.
    send_wheel(browser.grid_list.viewport())
    assert browser.zoom_slider.value()==before+12
    send_wheel(browser.empty)
    assert browser.zoom_slider.value()==before+24


def test_real_trash_entry_has_footer_and_ctrl_wheel(main_window,qtbot):
    window=main_window;window.resize(1200,800);window.show()
    window._open_trash_panel()
    qtbot.waitUntil(lambda:getattr(window,'_trash_library_page',None) is not None)
    page=window._trash_library_page
    qtbot.waitUntil(lambda:window.tabs.currentWidget() is page)
    assert page.zoom_slider.isVisibleTo(window)
    assert page.zoom_bar.smaller.isVisibleTo(window)
    before=page.zoom_slider.value()
    send_wheel(page.asset_list.viewport())
    assert page.zoom_slider.value()==before+12
    page.zoom_bar.smaller.click()
    assert page.zoom_slider.value()==before
