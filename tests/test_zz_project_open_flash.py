"""A project open must install its landing state exactly once.

Opening a project used to install the landing state twice: once while the
scene was still empty (the project switch) and once when the open finished.
The first pass installed an empty resource service -- the sidebar tree went
blank -- and swapped the central widget away and back, which is the flash
the user sees: the sidebar blinks, the interface drops out, and then the
project comes back.
"""
from types import SimpleNamespace
import os
import tempfile

from prism.window_workspace import WindowWorkspaceMixin


class _Tabs:
    def __init__(self):
        self.calls = []

    def setCurrentIndex(self, index):
        self.calls.append(('setCurrentIndex', index))

    def setCurrentWidget(self, widget):
        self.calls.append(('setCurrentWidget', widget))

    def currentWidget(self):
        return None


class _Window(WindowWorkspaceMixin):
    """A window stub that records the visible work a workspace reload does."""

    def __init__(self):
        self.tabs = _Tabs()
        self._page_panels = {}
        self.canvas_page = object()
        self.view = SimpleNamespace(
            current_canvas_id='stale-canvas',
            scene=SimpleNamespace(workspace_pages=[]),
            category_panel=SimpleNamespace(
                rebuild_workspace_tree=self._rebuild_tree,
                _apply_filter=lambda: None),
            _overview_renderer=SimpleNamespace(
                tiles=SimpleNamespace(switch_context=lambda canvas_id: None)),
        )
        self.asset_browser = SimpleNamespace(history=['old'], history_index=4)
        self.current_page_kind = 'mindmap'
        self.current_page_id = 'stale-page'
        self.rebuilt = 0
        self.synced = 0

    def _rebuild_tree(self):
        self.rebuilt += 1

    def _sync_page_panels(self):
        self.synced += 1

    def _first_canvas_id(self):
        return None

    def _set_widget_after_page_store(self, widget, stored_page):
        self.tabs.setCurrentWidget(widget)


def test_project_switch_does_not_install_the_landing_state():
    window = _Window()
    window._reload_workspace_pages(landing=False)
    # The closed project loses its panels ...
    assert window.synced == 1
    # ... but the tree and the central widget belong to the project that is
    # still loading.  Rebuilding them here is the flash the user sees.
    assert window.rebuilt == 0
    assert window.tabs.calls == []
    assert window.current_page_kind == 'canvas'
    assert window.current_page_id is None
    assert window.view.current_canvas_id is None
    assert window.asset_browser.history == []
    assert window.asset_browser.history_index == -1


def test_the_landing_state_is_installed_once():
    window = _Window()
    window._reload_workspace_pages()
    assert window.synced == 1
    assert window.rebuilt == 1
    assert ('setCurrentIndex', 0) in window.tabs.calls
    assert any(call[0] == 'setCurrentWidget' for call in window.tabs.calls)


def test_clear_scene_switches_the_project_without_installing_the_landing_state(view, monkeypatch):
    calls = []
    monkeypatch.setattr(view.parent, '_reload_workspace_pages',
                        lambda *args, **kwargs: calls.append(kwargs))
    view.clear_scene()
    assert calls == [{'landing': False}]


def test_opening_the_project_that_is_already_open_does_not_clear_the_scene(view, monkeypatch):
    """Re-opening the open project must not wipe the canvas.

    Opening it again clears the scene -- the canvas goes blank and the whole
    interface flashes -- replaces the resource tree, which loses the
    expansion it had, and loads every asset a second time.  The user reads
    that as "the application crashed and started again".
    """
    root = os.path.join(tempfile.gettempdir(), 'prism-open-project')
    view.filename = os.path.join(root, 'demo.prism.data', 'metadata.sqlite')
    cleared = []
    monkeypatch.setattr(view, 'clear_scene', lambda: cleared.append(True))
    assert view.open_from_file(os.path.join(root, 'demo.prism.data')) is False
    assert cleared == []


def test_only_the_open_project_counts_as_already_open(view):
    root = os.path.join(tempfile.gettempdir(), 'prism-open-project')
    view.filename = os.path.join(root, 'a.prism.data', 'metadata.sqlite')
    # The same project, named by its folder or by its database.
    assert view._already_open_project(os.path.join(root, 'a.prism.data')) is True
    assert view._already_open_project(
        os.path.join(root, 'a.prism.data', 'metadata.sqlite')) is True
    # Another project is not blocked.
    assert view._already_open_project(os.path.join(root, 'b.prism.data')) is False
    assert view._already_open_project(os.path.join(root, 'a.prism.data', 'other')) is False


def test_open_replacement_does_not_publish_empty_tree_or_welcome_screen(view, monkeypatch):
    from unittest.mock import Mock
    rebuild = Mock()
    monkeypatch.setattr(view.category_panel, 'force_rebuild', rebuild)
    view._replacing_project = True
    try:
        view.clear_scene()
        assert view._project_opening
        rebuild.assert_not_called()
        view.on_scene_changed([])
        assert view.welcome_overlay.isHidden()
    finally:
        view._replacing_project = False
        view._project_opening = False
    view.clear_scene()
    rebuild.assert_called_once()
