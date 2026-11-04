import time
from unittest.mock import Mock, patch

from PyQt6 import QtWidgets
from prism import performance_trace as trace_module
from prism.project_catalog_index import build_stall_context


def test_explicit_tag_closes_both_visual_and_text_state(main_window):
    controller = main_window.search_controller
    visual = controller.visual_search
    visual.start('unused-query.png')
    visual.timer.stop()
    controller.active = True
    controller._capture_origin()
    main_window.asset_browser.saved_rule = {'tag_expression': '"old"'}
    with patch.object(main_window, 'open_workspace_page') as opened:
        main_window.view.category_panel._browse_tag('new')
        opened.assert_not_called()
    assert not visual.active and not controller.active
    assert controller.origin is None
    assert main_window.asset_browser.saved_rule == {'tag_expression': '"new"'}
    assert main_window.tabs.currentWidget() is main_window.asset_browser
    controller.exit()
    assert controller.origin is None


def test_normal_visual_exit_still_restores_previous_tab(main_window):
    controller = main_window.search_controller.visual_search
    previous = main_window.tabs.currentWidget()
    controller.start('unused-query.png')
    controller.timer.stop()
    controller.exit()
    assert not controller.active
    assert main_window.tabs.currentWidget() is previous


def test_material_tag_clears_old_asset_rule_and_runs_during_ime(main_window):
    panel = main_window.view.category_panel
    material = panel.material_service.create(name='Wood')
    panel.material_service.configure(material['id'], tags=['wood'])
    controller = main_window.search_controller
    main_window.asset_browser.saved_rule = {'tag_expression': '"old"'}
    controller.input.composing = True
    panel._browse_tag('wood')
    assert not controller.input.composing
    assert main_window.asset_browser.saved_rule is None
    assert main_window.tabs.currentWidget() is controller.page
    assert any(hit.get('material_id') == material['id'] for hit in controller.results)


def test_programmatic_query_survives_expired_wait_and_resubmits(main_window, monkeypatch):
    controller = main_window.search_controller
    current = QtWidgets.QWidget()
    callbacks, scheduled = [], []
    current.search_snapshot = callbacks.append
    monkeypatch.setattr(main_window.tabs, 'currentWidget', lambda: current)
    monkeypatch.setattr(controller, '_run_query', Mock())
    controller.input.setText('tag:"wood"')
    controller.submit(programmatic=True)
    controller.wait_until = time.monotonic() - 1
    with patch('prism.search_controller.QtCore.QTimer.singleShot',
               side_effect=lambda delay, callback: scheduled.append(callback)):
        callbacks.pop()(False)
    assert len(scheduled) == 1
    # A page-content signal may submit again through the regular entry.
    controller.submit()
    controller.wait_until = time.monotonic() - 1
    callbacks.pop()(True)
    controller._run_query.assert_called_once()
    assert controller._programmatic_query is None
    controller.exit(restore_origin=False)
    scheduled[0]()  # The obsolete pending retry is invalidated by exit.
    controller._run_query.assert_called_once()


def test_near_coverage_is_distinct_from_exact_and_not_root_cause():
    result = build_stall_context(duration_ms=1000, stage='test',
        window_start_monotonic=10, window_end_monotonic=11,
        heartbeat_delay_ms=1000,
        dispatch_intervals=[{'dispatch_id': 'one', 'start_monotonic': 10.049,
                             'end_monotonic': 11}])
    assert result['classification'] == 'heartbeat_starved'
    assert result['covering_dispatches'] == []
    assert len(result['dominant_dispatches']) == 1
    assert result['dispatch_intervals'][0]['covers_window'] is False
    assert result['classification_is_root_cause'] is False
    assert result['nested_loop'] == 'unknown'


def test_large_gap_or_insufficient_overlap_remains_unknown():
    for start in (10.051, 10.5):
        result = build_stall_context(duration_ms=1000, stage='test',
            window_start_monotonic=10, window_end_monotonic=11,
            heartbeat_delay_ms=1000,
            dispatch_intervals=[{'dispatch_id': 'one', 'start_monotonic': start,
                                 'end_monotonic': 11}])
        assert result['classification'] == 'unclassified'
        assert result['dominant_dispatches'] == []


def test_parent_sampling_survives_child_transitions(main_window, qapp, tmp_path):
    trace = trace_module.install(qapp, main_window, tmp_path)
    try:
        now = time.perf_counter()
        trace.current_dispatch = [dict(dispatch_id='parent', start_monotonic=now-12),
                                  dict(dispatch_id='child-1', start_monotonic=now-1)]
        trace._sample_active_dispatches(now)
        trace.current_dispatch[-1] = dict(dispatch_id='child-2', start_monotonic=now-1)
        trace._sample_active_dispatches(now)
        assert trace._dispatch_samples['parent']['count'] == 2
        assert trace._dispatch_samples['parent']['descendant_samples'] == 2
        assert trace._dispatch_samples['child-1']['count'] == 1
        assert trace._dispatch_samples['child-2']['count'] == 1
        trace.current_dispatch.pop()
        trace._sample_active_dispatches(now)
        assert trace._dispatch_samples['parent']['count'] == 3
        assert trace._dispatch_samples['parent']['descendant_samples'] == 2
    finally:
        trace.current_dispatch.clear()
        trace.close()
