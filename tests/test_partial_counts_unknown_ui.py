"""Unknown project counts must not be presented as loaded-only totals."""
from prism.widgets.resource_tree_model import ASSET_COUNT_ROLE


def test_unknown_sidebar_counts_do_not_render_as_zero(view, monkeypatch):
    panel = view.category_panel
    monkeypatch.setattr(panel.catalog, 'count_snapshot', lambda: dict(
        total=None, unclassified=None, untagged=None, trash=None,
        canvases=None, visible=0, complete=False))
    panel._update_navigation()
    for key in ('all', 'untagged', 'unclassified', 'trash'):
        if key in panel._nav_counts:
            assert panel._nav_counts[key].text()
            assert panel._nav_counts[key].text() not in ('0', 'None')


def test_tree_counts_can_become_unknown_then_ready(view):
    panel = view.category_panel
    folder = panel.resource_service.createFolder('canvas', 'Counts')
    page = panel.resource_service.createPage('canvas', 'Board', 'count-board', folder['id'])
    model = panel.resource_tree.model()
    model.set_asset_counts({'count-board': 8})
    assert model.index_for_id(folder['id']).data(ASSET_COUNT_ROLE) == 8
    model.set_asset_counts(None)
    assert model.index_for_id(page['id']).data(ASSET_COUNT_ROLE) is None
    assert model.index_for_id(folder['id']).data(ASSET_COUNT_ROLE) is None
    model.set_asset_counts({'count-board': 0})
    assert model.index_for_id(folder['id']).data(ASSET_COUNT_ROLE) == 0
