"""Folders browse existing grids; editor pages remain explicit navigation."""
from unittest.mock import Mock
import pytest
from prism.widgets.resource_tree_model import ResourceTreeModel
from tests.test_asset_browser import photo, assets_only


def folder_with_page(window, kind):
    panel = window.view.category_panel
    page = window.workspace_service.add(kind, 'Inside page')
    window.workspace_service.add(kind, 'Outside page')
    panel.rebuild_workspace_tree()
    service = panel.resource_service
    folder = service.createFolder(kind, 'Root folder')
    inner = service.createFolder(kind, 'Nested folder', folder['id'])
    node = next(n for n in service.nodes if n.get('pageId') == page['id'])
    service.moveBatch([node['id']], inner['id'], kind)
    return folder, inner, page


def test_canvas_folder_uses_asset_grid_without_building_an_infinite_canvas(main_window, monkeypatch):
    window = main_window
    folder, inner, page = folder_with_page(window, 'canvas')
    image = photo(window, page['id'], 'Inside image')
    photo(window, 'default-canvas', 'Outside image')
    browser = window.asset_browser
    monkeypatch.setattr(browser.canvas_list, 'rebuild', Mock(side_effect=AssertionError('infinite canvas')))
    opened = Mock()
    monkeypatch.setattr(window, 'open_workspace_page', opened)
    tree = window.view.category_panel.resource_tree
    tree._on_clicked(tree.model().index_for_id(folder['id']))
    assert window.tabs.currentWidget() is browser
    assert browser.list is browser.grid_list
    assert assets_only(browser.model.items) == [image]
    opened.assert_not_called()


@pytest.mark.parametrize('kind', ['document', 'mindmap'])
def test_page_folders_show_page_cards_without_asset_scan_or_editor(main_window, monkeypatch, kind):
    window = main_window
    folder, inner, page = folder_with_page(window, kind)
    browser = window.asset_browser
    monkeypatch.setattr(browser.catalog, 'query', Mock(side_effect=AssertionError('image scan')))
    opened = Mock()
    monkeypatch.setattr(window, 'open_workspace_page', opened)
    tree = window.view.category_panel.resource_tree
    tree._on_clicked(tree.model().index_for_id(folder['id']))
    assert browser.list is browser.grid_list
    assert [item['page_id'] for item in browser.model.items] == [page['id']]
    opened.assert_not_called()
    browser.locate(browser.model.index(0))
    opened.assert_called_once_with(kind, page['id'])


def test_model_refresh_preserves_user_expansion_but_new_open_collapses(main_window):
    window = main_window
    folder, inner, page = folder_with_page(window, 'document')
    panel = window.view.category_panel
    tree = panel.resource_tree
    tree.expand(tree.model().index_for_id(folder['id']))
    tree.expand(tree.model().index_for_id(inner['id']))
    tree.replace_model(ResourceTreeModel(panel.resource_service, tree))
    assert tree.isExpanded(tree.model().index_for_id(folder['id']))
    captured = panel._capture_tree_state()
    window.view._trace_open_epoch = getattr(window.view, '_trace_open_epoch', 0) + 1
    tree.replace_model(ResourceTreeModel(panel.resource_service, tree))
    panel._restore_tree_state(captured)
    assert not tree.isExpanded(tree.model().index_for_id(folder['id']))
    assert not tree.isExpanded(tree.model().index_for_id(inner['id']))
