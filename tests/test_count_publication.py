"""T12: one synchronous notification shares one validated global snapshot."""
from unittest.mock import patch


def test_navigation_and_status_share_one_count(main_window):
    panel = main_window.view.category_panel
    main_window._status_count_snapshot = None
    with patch.object(panel.catalog, 'count_snapshot',
                      wraps=panel.catalog.count_snapshot) as count:
        panel.update_counts()
    assert count.call_count == 1
    assert getattr(panel, '_count_publication', None) is None


def test_publication_is_not_a_persistent_stale_cache(main_window):
    panel = main_window.view.category_panel
    with patch.object(panel.catalog, 'count_snapshot',
                      wraps=panel.catalog.count_snapshot) as count:
        panel.update_counts()
        panel.update_counts()
        assert count.call_count == 2
        panel.count_snapshot_for_display()
        assert count.call_count == 3


def test_reentrant_revision_change_invalidates_shared_value(main_window):
    panel = main_window.view.category_panel
    snapshots = []

    def edit_during_notification(_counts):
        panel.scene._asset_revision += 1
        snapshots.append(panel.count_snapshot_for_display())

    panel.counts_changed.connect(edit_during_notification)
    try:
        with patch.object(panel.catalog, 'count_snapshot',
                          wraps=panel.catalog.count_snapshot) as count:
            panel.update_counts()
        assert count.call_count == 2
        assert len(snapshots) == 1
        assert getattr(panel, '_count_publication', None) is None
    finally:
        panel.counts_changed.disconnect(edit_during_notification)
