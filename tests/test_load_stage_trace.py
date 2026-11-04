"""Opening completion must include the real finalization work."""
from unittest.mock import Mock, patch

import pytest

from prism import performance_trace


def test_operation_reports_one_stage_with_counts_and_failure():
    trace = Mock(last_action={'action': 'open'})
    with patch.object(performance_trace, '_active', trace):
        with pytest.raises(ValueError):
            with performance_trace.operation('read', query='images') as stage:
                stage['rows'] = 2
                raise ValueError('fixture failure')
    assert trace.record.call_count == 2
    assert trace.record.call_args_list[0].args == ('operation_start',)
    assert trace.record.call_args.args == ('operation',)
    fields = trace.record.call_args.kwargs
    assert fields['failed'] and fields['rows'] == 2 and fields['query'] == 'images'
    assert fields['duration_ms'] >= 0


def test_disabled_stage_does_not_enable_tracing():
    with patch.object(performance_trace, '_active', None):
        with performance_trace.operation('read') as fields:
            fields['rows'] = 2
        assert performance_trace._active is None


def test_finish_event_follows_categories_fit_and_workspace(main_window):
    view = main_window.view
    events = []
    with patch('prism.view.trace_record', side_effect=lambda name, **kw: events.append(name)), \
            patch.object(view.category_panel, 'force_rebuild', side_effect=lambda: events.append('categories')), \
            patch.object(view, 'on_action_fit_scene', side_effect=lambda: events.append('fit')), \
            patch.object(view.parent, '_reload_workspace_pages', side_effect=lambda: events.append('workspace')):
        view._finish_loading('audit.prism', [])
    assert events == ['project_open_finalize_start', 'categories', 'fit', 'workspace',
                      'project_open_finished']


def test_failed_finalize_is_not_reported_successful(main_window):
    view = main_window.view
    with patch('prism.view.trace_record') as record, \
            patch.object(view.category_panel, 'force_rebuild', side_effect=RuntimeError('fixture failure')):
        with pytest.raises(RuntimeError):
            view._finish_loading('audit.prism', [])
    assert record.call_args.args == ('project_open_finished',)
    assert record.call_args.kwargs['failed'] is True
