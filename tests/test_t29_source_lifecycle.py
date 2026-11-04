from unittest.mock import Mock
import pytest
from PyQt6 import QtGui
from prism import resource_budget as resources
from prism import performance_trace as trace


@pytest.fixture
def project_view(view, monkeypatch):
    monkeypatch.setattr(resources, '_shared', resources.CacheBudget())
    monkeypatch.setattr(resources, '_current_scope', None)
    monkeypatch.setattr(resources, '_scopes', {})
    monkeypatch.setattr(view, '_paged_open_enabled', lambda: False)
    monkeypatch.setattr(view, '_catalog_scope_instance', Mock())
    monkeypatch.setattr(view, '_start_project_reader', Mock())
    monkeypatch.setattr(view, '_allow_project_replacement', lambda: True)
    yield view
    view._release_project_resource_scope()
    view._opening_operation = None


def test_accepted_open_activates_scope_and_clear_releases_after_reader_stops(project_view, tmp_path):
    view = project_view
    filename = tmp_path / 'synthetic.prism'
    view.open_from_file(str(filename))
    scope = resources.current_project_scope()
    assert scope.key == resources.project_scope_key(filename)
    assert resources.shared_cache_budget().active_scope == scope.key
    released = Mock()
    scope.lease('synthetic', 'temporary-test-only', released)
    operation = view._opening_operation
    view._cancel_open = Mock()
    assert view.clear_scene() is False
    assert not scope.is_cancelled() and not released.called
    operation.terminal = True  # Simulate actual reader completion, not a timeout.
    view.clear_scene()
    assert scope.is_cancelled() and released.call_count == 1
    assert resources.current_project_scope() is None
    assert resources.shared_cache_budget().active_scope is None
    view._release_project_resource_scope()
    assert released.call_count == 1


def test_rejected_replacement_keeps_current_resources(project_view, tmp_path, monkeypatch):
    view = project_view
    view.open_from_file(str(tmp_path / 'current.prism'))
    scope = resources.current_project_scope()
    monkeypatch.setattr(view, '_allow_project_replacement', lambda: False)
    assert view.open_from_file(str(tmp_path / 'rejected.prism')) is False
    assert resources.current_project_scope() is scope and not scope.is_cancelled()


def test_save_as_releases_both_source_and_current_filename(project_view, tmp_path, monkeypatch):
    view = project_view
    old, new = str(tmp_path/'old.prism'), str(tmp_path/'new.prism')
    view.open_from_file(old)
    view.filename = new
    cancelled = Mock(return_value=0)
    monkeypatch.setattr(resources, '_cancel_project_decodes', cancelled)
    view._release_project_resource_scope()
    assert {call.args[0] for call in cancelled.call_args_list} == {
        resources.project_scope_key(old), resources.project_scope_key(new)}


def test_close_rejection_does_not_release_resources(main_window, monkeypatch):
    view = main_window.view
    released = Mock()
    monkeypatch.setattr(view, '_release_project_resource_scope', released)
    monkeypatch.setattr(main_window, '_store_current_page', lambda: True)
    monkeypatch.setattr(view, 'get_confirmation_unsaved_changes', lambda *args: False)
    event = QtGui.QCloseEvent()
    main_window.closeEvent(event)
    assert not event.isAccepted()
    released.assert_not_called()
    # Restore before fixture teardown, whose normal close is allowed.


def test_process_io_is_measured_or_explicitly_unknown():
    snapshot = trace.process_io_bytes()
    if snapshot['source'] == 'unknown':
        assert snapshot['read_bytes'] is snapshot['write_bytes'] is None
    else:
        assert snapshot['read_bytes'] >= 0 and snapshot['write_bytes'] >= 0
        assert snapshot['physical_disk_bytes'] is False
        assert snapshot['per_operation_attribution'] is False
        assert snapshot['includes_child_processes'] is False


def test_accepted_close_releases_scope(main_window, tmp_path, monkeypatch):
    monkeypatch.setattr(resources, '_shared', resources.CacheBudget())
    monkeypatch.setattr(resources, '_current_scope', None)
    monkeypatch.setattr(resources, '_scopes', {})
    scope = resources.open_project_resources(str(tmp_path / 'not-created.prism'))
    main_window.view._resource_project_key = scope.key
    monkeypatch.setattr(main_window, '_store_current_page', lambda: True)
    monkeypatch.setattr(main_window.view, 'get_confirmation_unsaved_changes', lambda *args: True)
    event = QtGui.QCloseEvent()
    main_window.closeEvent(event)
    assert event.isAccepted() and scope.is_cancelled()
    assert resources.current_project_scope() is None


def test_unmeasured_children_are_never_zero():
    assert any(name == 'webengine_children' and status == 'unknown'
               for name, status, reason in resources.CacheBudget.UNMEASURED_CLASSES)
