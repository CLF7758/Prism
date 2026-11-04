"""The library pre-warm must not make the window repaint itself.

Opening a project starts a pre-warm of the two library panels.  The panels
are built while the user is looking at the canvas they just opened, so
anything the pre-warm does to the **window** is seen as the whole interface
flashing.

Three things used to do exactly that:

* the page was built with the **window** as its Qt parent, so every widget
  it created was a child of the window and invalidated the window's layout;
* the page was **attached** to the tab's layout during the pre-warm, and
  attaching invalidates the layout of the host -- which sits in the window's
  tab bar;
* the inline preview started **Chromium** during the construction: 236 ms of
  GUI thread for a surface that is hidden by default and never shown.

The pre-warm itself must not be delayed: an existing contract
(``tests/test_library_prewarm_never_blocks.py``) says that deferring it only
makes it more likely that the user clicks into a panel that is not ready.
"""
import os

import pytest
from PyQt6 import QtWidgets


def test_startup_web_surface_is_owned_before_view_and_native_window_creation(
        qapp, monkeypatch, request):
    import prism.__main__ as main_module
    surface = QtWidgets.QWidget()
    monkeypatch.setattr(qapp, '_webengine_warmup', surface, raising=False)
    original = main_module.PrismGraphicsView
    def construct_view(app, parent):
        assert surface.parent() is parent
        assert parent._webengine_surface_anchor is surface
        assert not parent.isVisible()
        return original(app, parent)
    monkeypatch.setattr(main_module, 'PrismGraphicsView', construct_view)
    window = request.getfixturevalue('main_window')
    assert surface.parent() is window
    assert surface.isHidden()


def test_the_library_page_belongs_to_its_tab_not_to_the_window(main_window, qtbot):
    """The page is built under the tab, so its widgets cannot dirty the window."""
    window = main_window
    window.show()
    window.schedule_library_prewarm()
    qtbot.waitUntil(lambda: window.material_library.initialized, timeout=60000)
    page = window.material_library._page
    assert page is not None
    # The window stays the *host* ...
    assert page.window_host is window
    # ... but not the Qt parent: a widget under the window invalidates the
    # window's layout on every addWidget.
    assert page.parent() is window.material_library
    assert page.details.parent() is window.material_library.details


def test_the_prewarm_keeps_the_page_out_of_the_visible_layout(main_window, qtbot):
    """Attaching the page repaints the window; that happens when the tab opens."""
    window = main_window
    window.show()
    window.schedule_library_prewarm()
    qtbot.waitUntil(lambda: window.material_library.initialized, timeout=60000)
    host = window.material_library
    assert host._body.indexOf(host._page) == -1
    assert host.details.body.indexOf(host._page.details) == -1
    # Showing the tab is the moment the page may join the layout.
    host.show()
    assert host._body.indexOf(host._page) >= 0
    assert host.details.body.indexOf(host._page.details) >= 0


def test_the_inline_preview_does_not_start_chromium_during_the_construction(qtbot):
    """The preview is hidden by default; its Chromium surface waits for it."""
    from prism.widgets.material_render import MaterialRender

    viewer = MaterialRender()
    qtbot.addWidget(viewer)
    assert viewer.web is None


def test_the_prewarm_can_be_switched_off_for_a_comparison(monkeypatch, main_window):
    """``PRISM_LIBRARY_PREWARM=0`` measures the window without the pre-warm."""
    monkeypatch.setenv('PRISM_LIBRARY_PREWARM', '0')
    window = main_window
    window.schedule_library_prewarm()
    assert window.material_library._construction is None
    assert window.model_library._construction is None
