"""Shared log integration, not a substitute for a real material-edit journey."""
from prism.model_library import events


def test_preview_event_diagnostic_does_not_break_model_binding(monkeypatch):
    from prism.model_library.service import ModelLibraryService
    service = ModelLibraryService.__new__(ModelLibraryService)
    service._closed = False
    recorded = []
    monkeypatch.setattr('prism.performance_trace.record',
        lambda name, **fields: recorded.append((name, fields)))
    service.preview_event('material.first_frame', {'identity': {'model_id': 'model-a'}})
    assert recorded[0][0] == 'model_material_preview.event'
    assert recorded[0][1]['event_name'] == 'material.first_frame'
    assert recorded[0][1]['identity_model'] == 'model-a'


def test_material_identity_reaches_trace_and_late_result_is_identifiable(monkeypatch):
    recorded = []
    monkeypatch.setattr('prism.performance_trace.record',
        lambda name, **fields: recorded.append((name, fields)))
    with events.model_identity(project_session='session-a', open_epoch=2,
            model_id='model-a', request_id='request-old', source_revision=3,
            material_id='working-a', binding_id='binding-a', catalog_revision=4,
            state_revision=5, draft_generation=6, gesture_id='gesture-a'):
        events.record_model_event('material.draft.updated')
        events.record_model_event('material.install.stale',
                                  expected_draft_generation=7, reason='superseded')
    assert len(recorded) == 2
    for name, fields in recorded:
        for key in events.MATERIAL_IDENTITY_FIELDS:
            assert fields[key] is not None
        assert fields.get('schema_ok', True)
        assert 'schema_missing_identity' not in fields
    assert recorded[1][1]['draft_generation'] == 6
    assert recorded[1][1]['expected_draft_generation'] == 7
    from prism.performance_trace import PRIORITY_TERMINAL
    assert recorded[1][1]['priority'] == PRIORITY_TERMINAL


def test_whole_model_material_command_does_not_require_single_binding():
    fields = events._prepare('material.commit.completed', dict(
        project_session='session-a', open_epoch=1, model_id='model-a',
        request_id='request-a', source_revision=2, catalog_revision=3,
        state_revision=4, gesture_id='gesture-a', operation='restore_model'))
    assert fields.get('schema_ok', True)
    assert 'schema_missing_identity' not in fields
    assert 'binding_id' not in fields
    assert 'material_id' not in fields


def test_material_install_reports_missing_generation_without_breaking_business():
    fields = events._prepare('material.install.stale', dict(
        project_session='session-a', open_epoch=1, model_id='model-a',
        request_id='request-a', source_revision=2, catalog_revision=3,
        state_revision=4))
    assert fields['schema_ok'] is False
    assert 'draft_generation' in fields['schema_missing_required']
