"""UI actions must never silently export only a loaded subset of a project."""
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from prism.actions.export_workflow import export_images, export_resource_subtree


def test_partial_whole_project_image_export_is_not_a_silent_subset(monkeypatch):
    from prism.actions import export_workflow
    notify = Mock()
    monkeypatch.setattr(export_workflow.widgets, 'PrismNotification', notify)
    service = SimpleNamespace(items_for=Mock())
    view = SimpleNamespace(scene=SimpleNamespace(_partial_project_source='fixture.prism'),
                           export_service=service)
    export_images(view)
    notify.assert_called_once()
    service.items_for.assert_not_called()


@pytest.mark.parametrize('section', ['canvas', 'document', 'mindmap'])
def test_partial_subtree_never_omits_unloaded_assets_or_page_bodies(monkeypatch, section):
    from prism.actions import export_workflow
    notify = Mock()
    monkeypatch.setattr(export_workflow.widgets, 'PrismNotification', notify)
    view = SimpleNamespace(scene=SimpleNamespace(_partial_project_source='fixture.prism'))
    export_resource_subtree(view, section)
    notify.assert_called_once()


def test_partial_mixed_unloaded_selection_is_not_reduced_to_live_items(monkeypatch):
    from prism.actions import export_workflow
    notify = Mock()
    monkeypatch.setattr(export_workflow.widgets, 'PrismNotification', notify)
    proxy = SimpleNamespace(_catalog_proxy=True, save_id=42)
    browser = SimpleNamespace(model=SimpleNamespace(items=[proxy]),
        list=SimpleNamespace(selectedIndexes=lambda: [SimpleNamespace(row=lambda: 0)]))
    window = SimpleNamespace(asset_browser=browser,
                             tabs=SimpleNamespace(currentWidget=lambda: browser))
    service = SimpleNamespace(items_for=lambda **_: [])
    view = SimpleNamespace(scene=SimpleNamespace(_partial_project_source='fixture.prism'),
                           export_service=service, window=lambda: window)
    export_images(view, selected_only=True)
    notify.assert_called_once()
