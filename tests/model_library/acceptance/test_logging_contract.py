"""D3: the model-library log is stable, private, bounded and diagnosable.

The contract these tests enforce is the one the storage, native and UI owners
have to code against:

* one entry point with an unchanged signature
* a fixed vocabulary with a fixed identity per task
* no model bytes, no texture Base64, no note bodies, no credentials
* masked paths, so the operations log and the diagnostic bundle agree
* repeated native lines merged into one event that keeps count and times
* terminal evidence protected from a full queue
* a log failure never breaks the business it observes
"""
from __future__ import annotations

import builtins
import sys

import pytest

from prism.model_library import events


@pytest.fixture(autouse=True)
def drain_native_groups():
    """Native-log groups are module state; start every test from a clean one."""
    events.flush_model_events()
    yield
    events.flush_model_events()


@pytest.fixture
def recorded(monkeypatch):
    """Capture what reaches the trace pipeline, without installing a trace."""
    captured = []

    def fake_record(kind, /, *, priority=None, **fields):
        captured.append({'kind': kind, 'priority': priority, 'fields': fields})

    monkeypatch.setattr('prism.performance_trace.record', fake_record)
    return captured


class FakeClock:
    """A monotonic clock the test drives."""

    def __init__(self, value=1000.0):
        self.value = value

    def monotonic(self):
        return self.value

    def advance(self, seconds):
        self.value += seconds


def test_the_entry_point_keeps_its_historic_signature(recorded):
    """The calls that already exist must keep working untouched."""
    events.record_model_event('shell_state', state='unavailable')
    events.record_model_event('feature_unavailable', feature='preview')
    events.record_model_event('diagnostic_folder_opened', directory='C:/tmp/logs')
    assert [item['kind'] for item in recorded] == [
        'model_shell_state', 'model_feature_unavailable', 'model_diagnostic_folder_opened']
    assert recorded[0]['fields']['state'] == 'unavailable'


def test_lifecycle_events_carry_the_full_identity(recorded):
    """Two events belong to the same task only when the identity agrees."""
    with events.model_identity(project_session='sess-1', open_epoch=3, model_id='m-9',
                               request_id='req-4', job_id='job-7', source_revision=2,
                               appearance_revision=5, camera_revision=1,
                               backend_version='f3d-0.0'):
        events.record_model_event(events.EVENT_QUEUED)
        events.record_model_event(events.EVENT_COMPLETED)
    for item in recorded:
        fields = item['fields']
        for name in events.LEGACY_IDENTITY_FIELDS:
            assert fields.get(name) is not None, '%s 缺少 %s' % (item['kind'], name)
        assert 'material_id' not in fields  # Old model events need no invented material.
        assert not any(key in fields.get('schema_missing_identity', '')
                       for key in events.MATERIAL_IDENTITY_FIELDS)
        assert fields['project_session'] == 'sess-1'
        assert fields['model_id'] == 'm-9'


def test_an_explicit_field_always_beats_the_context(recorded):
    with events.model_identity(model_id='m-1'):
        events.record_model_event(events.EVENT_STARTED, model_id='m-2')
    assert recorded[0]['fields']['model_id'] == 'm-2'


def test_missing_required_fields_are_reported_and_never_raised(recorded):
    events.record_model_event(events.EVENT_FAILED, job_id='job-1')
    fields = recorded[0]['fields']
    assert fields['schema_ok'] is False
    assert 'error_code' in fields['schema_missing_required']
    assert fields.get('error_code') is None


def test_private_payloads_never_reach_the_log(recorded):
    """Model bytes, texture Base64, note bodies and credentials are omitted."""
    events.record_model_event(
        events.EVENT_SOURCE_PREPARED,
        notes='用户写的私人备注',
        thumbnail_data_url='data:image/png;base64,AAAABBBB',
        payload=b'\x00\x01\x02',
        api_key='sk-secret',
        bytes=4096,
        normal_field='visible')
    fields = recorded[0]['fields']
    assert fields['notes'] == '<omitted:notes>'
    assert fields['thumbnail_data_url'] == '<omitted:thumbnail_data_url>'
    assert fields['payload'] == '<omitted:payload>'
    assert fields['api_key'] == '<omitted:api_key>'
    assert fields['bytes'] == 4096  # A numeric size is telemetry, not raw bytes.
    assert fields['normal_field'] == 'visible'
    serialized = repr(fields)
    assert 'sk-secret' not in serialized
    assert '用户写的私人备注' not in serialized


def test_a_data_url_value_is_omitted_even_under_a_harmless_name(recorded):
    events.record_model_event(events.EVENT_RENDERED, preview='data:image/png;base64,QUJD')
    assert recorded[0]['fields']['preview'] == '<omitted:data_url>'


def test_paths_are_masked_the_same_way_the_bundle_masks_them(recorded):
    events.record_model_event(events.EVENT_OUTPUT_WRITTEN,
                              directory='C:\\Users\\someone\\Prism\\logs')
    value = recorded[0]['fields']['directory']
    assert value.startswith('<path:')
    assert 'someone' not in value
    from prism.diagnostics import Redactor
    assert Redactor().string('C:\\Users\\someone\\Prism\\logs') == value


def test_repeated_native_lines_are_merged_with_count_and_times(recorded, monkeypatch):
    """The merged event keeps how many and when -- first and last."""
    clock = FakeClock()
    monkeypatch.setattr(events, 'time', clock)
    for _ in range(5):
        events.forward_native_log('error', 'VTK reader failed', job_id='job-1')
    events.flush_model_events()
    merged = [item for item in recorded if item['kind'] == 'model_native_log_merged']
    assert len(merged) == 1, '重复原生日志没有被合并成一条'
    fields = merged[0]['fields']
    assert fields['occurrences_total'] == 5
    assert fields['occurrences_merged'] == 4
    assert fields['first_mono'] == 1000.0
    assert fields['last_mono'] == 1000.0
    assert fields['job_id'] == 'job-1'


def test_a_new_window_produces_a_new_group(recorded, monkeypatch):
    clock = FakeClock()
    monkeypatch.setattr(events, 'time', clock)
    events.forward_native_log('warn', 'same line')
    clock.advance(5.0)
    events.forward_native_log('warn', 'same line')
    events.flush_model_events()
    individuals = [item for item in recorded if item['kind'] == 'model_native_log']
    merged = [item for item in recorded if item['kind'] == 'model_native_log_merged']
    assert len(individuals) == 2, '窗口外的两次出现必须各记一条'
    assert not merged, '单独一次出现不构成合并事件'


def test_an_error_storm_stays_bounded(recorded):
    """Ten thousand identical native errors may not become ten thousand events."""
    for _ in range(10000):
        events.forward_native_log('error', 'F3D crashed')
    events.flush_model_events()
    total = len(recorded)
    assert total <= 40, '错误风暴产生了 %d 条事件，没有被合并' % total
    merged = [item for item in recorded if item['kind'] == 'model_native_log_merged']
    assert merged, '错误风暴没有被记成合并事件'
    assert max(item['fields']['occurrences_total'] for item in merged) >= 1000


def test_a_native_callback_does_not_touch_the_disk_or_the_ui(recorded, monkeypatch):
    """The F3D callback may only enqueue: no file, no Qt, no widget."""
    opened = []
    real_open = builtins.open

    def spy_open(file, *args, **kwargs):
        opened.append(str(file))
        return real_open(file, *args, **kwargs)

    monkeypatch.setattr(builtins, 'open', spy_open)
    before = set(sys.modules)
    events.forward_native_log('info', 'rendered frame 1')
    after = set(sys.modules)
    assert not opened, '原生回调打开了文件：%s' % opened
    assert not [name for name in after - before if name.startswith('PyQt6')]


def test_terminal_events_use_a_protected_priority(recorded):
    events.record_model_event(events.EVENT_COMPLETED, job_id='job-1')
    events.record_model_event(events.EVENT_FAILED, job_id='job-1', error_code='X')
    events.record_model_event(events.EVENT_RENDERED, job_id='job-1')
    priorities = {item['kind']: item['priority'] for item in recorded}
    from prism.performance_trace import PRIORITY_ERROR, PRIORITY_SAMPLE, PRIORITY_TERMINAL
    assert priorities['model_completed'] == PRIORITY_TERMINAL
    assert priorities['model_failed'] == PRIORITY_ERROR
    assert priorities['model_rendered'] == PRIORITY_SAMPLE


def test_a_broken_pipeline_never_breaks_the_business(monkeypatch):
    """A failing log channel is counted, not raised into the caller."""
    def boom(*args, **kwargs):
        raise RuntimeError('pipeline down')

    monkeypatch.setattr('prism.performance_trace.record', boom)
    events.record_model_event(events.EVENT_QUEUED, job_id='job-1')  # must not raise
    state = events.model_event_state()
    assert state['failures'] >= 1


def test_the_published_contract_lists_what_the_other_owners_must_record():
    contract = events.model_event_contract()
    assert contract['schema'] == events.EVENT_SCHEMA
    assert contract['entry_point'].endswith('record_model_event(event, **fields)')
    for name in ('queued', 'started', 'source_prepared', 'native_loaded', 'rendered',
                 'output_written', 'installed', 'completed', 'failed', 'cancelled', 'stale'):
        assert name in contract['lifecycle_events'], '词表缺少 %s' % name
    for name in ('project_session', 'open_epoch', 'model_id', 'request_id', 'job_id',
                 'source_revision', 'appearance_revision', 'camera_revision',
                 'backend_version'):
        assert name in contract['identity_fields'], '身份字段缺少 %s' % name
    assert contract['import_events'] and contract['export_events']
    assert contract['appearance_events'] and contract['project_events']
    assert 'notes' in contract['forbidden_fields']


def test_the_log_never_claims_to_be_complete_when_it_is_not():
    completeness = events.model_log_completeness()
    assert completeness['complete'] is False, '没有安装追踪管线时不得声称日志完整'
    assert 'bounded queues can drop' in completeness['basis']
    assert 'diagnostic_failures' in completeness
