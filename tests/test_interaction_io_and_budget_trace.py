import json
import time
from unittest.mock import patch

from prism import performance_trace as trace_module


def test_zoom_signal_reuses_count_without_archive_io(main_window):
    panel = main_window.view.category_panel
    main_window._update_status({'refresh': True})
    before = main_window._status_count_snapshot
    with patch.object(main_window, '_open_scope_key', side_effect=AssertionError('archive stat')):
        with patch.object(panel.catalog, 'count_snapshot', side_effect=AssertionError('SQLite count')):
            for _ in range(20):
                main_window.view.zoom_changed.emit(120)
    assert main_window._status_count_snapshot is before


def test_zoom_without_snapshot_schedules_recount_without_querying(main_window):
    main_window._status_count_snapshot = None
    with patch.object(main_window, '_open_scope_key', side_effect=AssertionError('archive stat')):
        with patch.object(main_window.view.category_panel.catalog, 'count_snapshot',
                          side_effect=AssertionError('SQLite count')):
            main_window.view.zoom_changed.emit(120)
    assert main_window._recount_timer.isActive()
    assert main_window._status_count_snapshot is None


def test_periodic_budget_snapshot_exists_without_eviction(main_window, qapp, tmp_path):
    trace = trace_module.install(qapp, main_window, tmp_path)
    try:
        trace._heartbeat()
        trace._heartbeat()
    finally:
        trace.close()
    rows = [json.loads(line) for line in trace.path.read_text(encoding='utf-8').splitlines()]
    snapshots = [row for row in rows if row['event'] == 'cache_budget_snapshot'
                 and row.get('reason') == 'periodic']
    assert len(snapshots) == 1
    assert 'categories' in snapshots[0]
    assert 'limit' in snapshots[0]


def test_legacy_paint_latency_does_not_claim_target_display(main_window, qapp, tmp_path):
    trace = trace_module.install(qapp, main_window, tmp_path)
    try:
        trace.input_marker = (time.perf_counter() - .4, 'canvas', 'Wheel')
        trace._consume_input_marker(main_window._status_text)
    finally:
        trace.close()
    rows = [json.loads(line) for line in trace.path.read_text(encoding='utf-8').splitlines()]
    sample = next(row for row in rows if row['event'] == 'slow_input_to_paint')
    assert sample['measurement_basis'] == 'latest_input_to_first_any_widget_paint'
    assert sample['marker_policy'] == 'latest_input_overwrites'
    assert sample['target_content_confirmed'] is False
    assert sample['display_confirmed'] is False
