"""Async asset jumps keep their target stable across list refreshes."""
from types import SimpleNamespace
from unittest.mock import Mock

from PyQt6 import QtGui

from prism.items import PrismPixmapItem


def test_locate_waits_and_captures_asset_instead_of_model_index(main_window, monkeypatch):
    window = main_window
    first = PrismPixmapItem(QtGui.QImage(3, 3, QtGui.QImage.Format.Format_RGB32))
    second = PrismPixmapItem(QtGui.QImage(3, 3, QtGui.QImage.Format.Format_RGB32))
    window.view.scene.addItem(first)
    window.view.scene.addItem(second)
    current = [first]
    index = SimpleNamespace(data=lambda _role: current[0])
    window.open_workspace_page('document', 'default-document')
    replies = []
    monkeypatch.setattr(window.document_panel, 'store_to_scene_async', replies.append)
    monkeypatch.setattr(window.asset_browser.catalog, 'locate', lambda _item: ('canvas', 'default-canvas'))
    center = Mock()
    monkeypatch.setattr(window.view, 'centerOn', center)
    window.asset_browser.locate(index)
    assert window.tabs.currentWidget() is window.document_panel
    center.assert_not_called()
    current[0] = second
    replies[0](True)
    assert window.tabs.currentWidget() is window.canvas_page
    assert center.call_args.args == (first,)
    assert first.isSelected() and not second.isSelected()


def test_locate_ignores_asset_removed_during_commit(main_window, monkeypatch):
    window = main_window
    item = PrismPixmapItem(QtGui.QImage(3, 3, QtGui.QImage.Format.Format_RGB32))
    window.view.scene.addItem(item)
    window.open_workspace_page('document', 'default-document')
    replies = []
    monkeypatch.setattr(window.document_panel, 'store_to_scene_async', replies.append)
    window.asset_browser.locate(SimpleNamespace(data=lambda _role: item))
    window.view.scene.removeItem(item)
    replies[0](True)
    assert window.tabs.currentWidget() is window.document_panel
