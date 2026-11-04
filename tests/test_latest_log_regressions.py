"""Regressions confirmed while reviewing the 2026-11-04 user session."""
from types import SimpleNamespace
from unittest.mock import Mock

from prism.model_library.events import record_model_event
from prism.model_library.service import ModelLibraryService
from prism import performance_trace as trace


def test_worker_priority_cannot_drop_model_start_event(monkeypatch):
    captured = []
    def record(kind, /, *, priority=None, **fields):
        captured.append((kind, priority, fields))
    monkeypatch.setattr(trace, 'record', record)
    before = trace.diagnostic_failures()['total']
    record_model_event('started', priority=2, job_id='current', model_id='model')
    assert trace.diagnostic_failures()['total'] == before
    assert len(captured) == 1
    assert captured[0][0] == 'model_started'
    assert captured[0][2]['job_priority'] == 2
    assert captured[0][1] != 2  # The pipeline owns its own priority vocabulary.


def test_shared_preview_does_not_wait_for_legacy_probe():
    service = ModelLibraryService()
    try:
        assert not service.capabilities.preview
        open_model = Mock(return_value='preview-request')
        service.multimaterial = SimpleNamespace(open_model=open_model)
        record = SimpleNamespace(model_id='chosen')
        service.get = Mock(return_value=record)
        assert service.capabilities.preview
        assert service.open_preview('chosen') == 'preview-request'
        open_model.assert_called_once_with(record)
        service._closed = True
        assert not service.capabilities.preview
        import pytest
        from prism.model_library.contracts import ModelFeatureUnavailable
        with pytest.raises(ModelFeatureUnavailable):
            service.open_preview('chosen')
    finally:
        service._pool.shutdown(wait=True)


def test_capture_lifecycle_and_rejections_survive_rotation():
    for event in ('capture.helper.start', 'capture.helper.startup',
                  'capture.helper.ready', 'capture.helper.failed',
                  'capture.helper.exit', 'capture.captured',
                  'model_feature_unavailable', 'library.ui_commit_receipt.rejected'):
        assert trace._diagnostic_archive_candidate(dict(event=event))
    assert not trace._diagnostic_archive_candidate(dict(event='ui_repaint_counters'))


def test_native_card_prefix_check_yields_and_rejects_reordered_rows(qtbot):
    from prism.widgets.asset_grid import AssetGrid
    grid = AssetGrid()
    qtbot.addWidget(grid)
    grid._native_timer.stop()
    previous = [dict(result_id=str(i)) for i in range(4096)]
    items = [dict(result_id=str(i)) for i in range(4096)]
    grid._native_count = len(previous)
    calls = []
    actual = grid._resource_id
    grid._resource_id = lambda item: (calls.append(item['result_id']), actual(item))[1]
    assert grid._native_prefix_matches(previous, items, 'query') is None
    assert len(calls) <= 1024
    while grid._native_prefix_matches(previous, items, 'query') is None:
        pass
    grid._native_timer.stop()
    assert len(calls) == 2*len(items)
    swapped = list(items)
    swapped[0], swapped[1] = swapped[1], swapped[0]
    assert grid._native_prefix_matches(previous, swapped, 'query') is False
    grid._native_prefix_check = None
    grid._native_count = len(items)
    assert grid._native_prefix_matches(items, items, 'query') is True


def test_startup_filter_ui_is_lazy_and_first_open_uses_current_state(main_window):
    popup = main_window.filter_popover
    assert not popup._ui_ready
    panel = popup.panel
    panel._min_rating = 3
    panel._untagged_only = True
    assert any(field == 'rating' for field, _ in popup.active_summary())
    popup.refresh()
    assert popup._ui_ready
    assert popup.rating_combo.currentIndex() == 3
    assert popup.untagged_box.isChecked()
    popup.clear_all()
    assert panel._min_rating == 0 and not panel._untagged_only
