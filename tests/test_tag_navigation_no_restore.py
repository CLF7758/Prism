"""Selecting a tag must not restore a stale search-origin canvas first."""
from unittest.mock import patch

from tests.test_asset_browser import photo
from tests.test_search_controller import submit
from tests.test_unified_project_search import page
from prism.project_search import item_key


def test_asset_tag_from_located_search_keeps_current_canvas(main_window, qtbot):
    window = main_window
    origin = page(window, 'canvas', 'Origin')
    target = page(window, 'canvas', 'Target')
    photo(window, origin['id'], 'Other image', ['other'])
    item = photo(window, target['id'], 'Located needle', ['needle'])
    window.open_workspace_page('canvas', origin['id'])
    controller = submit(window, 'Located needle')
    hit = next(result for result in controller.results if result.get('item_id') == item_key(item))
    controller.open_result(hit)
    assert window.view.current_canvas_id == target['id']
    with patch.object(window, 'open_workspace_page', wraps=window.open_workspace_page) as opened:
        window.view.category_panel._browse_tag('other')
        qtbot.wait(50)
        assert window.tabs.currentWidget() is window.asset_browser
        assert window.view.current_canvas_id == target['id']
        opened.assert_not_called()
    assert not controller.active
    assert window.asset_browser.saved_rule == {'tag_expression': '"other"'}
