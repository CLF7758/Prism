"""D6: the logging contract the multimaterial work has to satisfy.

The brief requires one user action to be traceable through parse, draft,
commit and install, with identity, revision, material and duration, and it
forbids putting model bytes or textures into the diagnostics.  ``model_library
.events`` already enforces the privacy half (masking, payload removal).  This
module measures both halves against the code that exists today, so the gap is
a written fact instead of an assumption.
"""
from __future__ import annotations

import json
import time

import pytest

from .conftest import D6_DIR

#: Identity fields a material edit needs so one action can be reassembled from
#: the log: which material, which binding, at which source/catalog/state
#: revision, and which draft generation (so a late result can be refused).
REQUIRED_IDENTITY = ('material_id', 'binding_id', 'catalog_revision',
                     'state_revision', 'draft_generation')


def _events_module():
    from prism.model_library import events
    return events


def _material_flags():
    from prism.model_library.multimaterial.service import MultiMaterialService
    report = MultiMaterialService().capabilities()
    return {name: value for name, value in vars(report).items()
            if isinstance(value, bool)}


def test_def1a_identity_fields_are_declared(write_report):
    """DEF-1a: the shared schema declares material-scope identity.

    This half of DEF-1 is about the contract, not about behaviour: a log cannot
    mention *which material at which generation* unless the schema has a place
    to put it.  It is deliberately checked on the contract surface
    (``IDENTITY_FIELDS`` and the exported ``model_event_contract``), because
    that is what every call site and the support bundle read.
    """
    events = _events_module()
    contract = events.model_event_contract()
    declared = list(events.IDENTITY_FIELDS)
    material_fields = list(contract.get('material_identity_fields', []))
    missing = [name for name in REQUIRED_IDENTITY if name not in declared]
    missing_from_contract = [name for name in REQUIRED_IDENTITY
                             if name not in material_fields]
    stages = sorted(contract.get('material_events', []))
    required = {name: events.REQUIRED_FIELDS.get(name, ())
                for name in stages}
    write_report(D6_DIR, 'logging-contract.json',
                 {'identity_fields': declared,
                  'material_identity_fields': material_fields,
                  'material_event_schema': contract.get('material_event_schema'),
                  'material_events': stages,
                  'required_fields': required,
                  'missing_identity': missing,
                  'missing_from_contract': missing_from_contract,
                  'legacy_identity_fields': contract.get('legacy_identity_fields'),
                  'verdict': ('材质身份字段已声明' if not missing
                              and not missing_from_contract
                              else '材质身份字段仍缺失')})
    assert missing == [], ('IDENTITY_FIELDS 缺少材质身份字段 %r（现有 %r）'
                           % (missing, declared))
    assert missing_from_contract == [], (
        '导出的契约里 material_identity_fields 缺少 %r，调用方读不到'
        % missing_from_contract)
    assert contract.get('material_event_schema'), '没有材质事件 schema 版本'
    assert stages, '没有声明任何 material.* 阶段事件'
    for name in stages:
        fields = events.REQUIRED_FIELDS.get(name)
        assert fields, '材料阶段 %s 没有声明必需字段' % name
        for root in ('project_session', 'open_epoch', 'model_id', 'request_id',
                     'source_revision'):
            assert root in fields, '%s 缺少身份根字段 %s' % (name, root)


def test_def1a_identity_reaches_the_payload(write_report):
    """DEF-1a: declared fields must actually arrive in the event payload.

    Declaration without carriage is the failure mode this catches: a field can
    exist in the schema and still never be filled, in which case every material
    event looks alike and no action can be reassembled.
    """
    events = _events_module()
    identity = {
        'project_session': 'session-1', 'open_epoch': 3, 'model_id': 'model-7',
        'request_id': 'req-9', 'job_id': 'job-2', 'source_revision': 4,
        'catalog_revision': 5, 'state_revision': 6, 'material_id': 'gltf:material:2',
        'binding_id': 'gltf|scene:0|node:3|mesh:1|primitive:0', 'gesture_id': 'g-1',
    }
    with events.model_identity(**identity):
        payload = events._prepare('material.commit.completed',
                                  {'draft_generation': 11, 'duration_ms': 12.5})
    missing = [name for name in REQUIRED_IDENTITY + ('gesture_id',)
               if not payload.get(name)]
    write_report(D6_DIR, 'logging-identity-carriage.json',
                 {'payload': payload, 'identity_supplied': identity,
                  'missing_in_payload': missing,
                  'schema_ok': payload.get('schema_ok'),
                  'schema_missing_required': payload.get('schema_missing_required')})
    assert missing == [], '这些字段没有进入事件载荷：%r（载荷是 %r）' % (missing, payload)
    assert payload.get('draft_generation') == 11
    assert payload.get('event_schema') == events.EVENT_SCHEMA
    assert payload.get('schema_missing_required') is None, (
        '身份字段齐备却仍报缺字段：%r' % payload.get('schema_missing_required'))


def test_def1a_recording_counter_moves(write_report):
    """DEF-1a: the event reaches the recording path, not only the formatter."""
    events = _events_module()
    before = events.model_event_state()
    events.record_model_event('material.parse.completed',
                              model_id='model-1', request_id='req-1',
                              source_revision=1, material_id='gltf:material:0')
    after = events.model_event_state()
    write_report(D6_DIR, 'logging-counter.json',
                 {'before': before, 'after': after})
    assert after['recorded'] == before['recorded'] + 1, '事件没有进入记录路径'
    assert after['failures'] == before['failures'], '记录路径出现失败'


def test_def1b_edit_chain_correlation_is_not_testable_yet(write_report):
    """DEF-1b: the real edit chain (parse→draft→commit→install) stays open.

    This half of DEF-1 cannot be closed by a schema test.  It needs one user
    action to produce a correlated set of material events, which needs a commit
    and an install to actually happen.  While the service reports no capability
    that is recorded as blocked -- never as passed.
    """
    flags = _material_flags()
    active = any(flags.values())
    write_report(D6_DIR, 'def1b-status.json',
                 {'capabilities': flags,
                  'state': 'open' if active else 'blocked',
                  'required_evidence': [
                      '一次编辑产生 material.parse/…/material.commit.completed 的关联事件',
                      '旧结果被拒时能指出 material_id + draft_generation',
                      '事件真的落盘（需要活动 trace writer）',
                  ],
                  'reason': ('服务能力全关，没有提交与安装，无法产生可关联的事件链'
                             if not active else
                             '服务已报告能力，需要真的跑一次编辑并核对事件链')})
    if not active:
        pytest.skip('DEF-1b 未测：服务能力全关（%r）' % flags)
    pytest.fail('服务已报告能力 %r，但还没有跑过真实编辑链的日志关联核对' % flags)


def test_private_content_cannot_enter_an_event():
    """Model bytes, note bodies and data URLs must not be written to the log."""
    events = _events_module()
    payload = events._prepare('material.parse', {
        'bytes': b'\x00\x01' * 32,
        'note': '用户备注正文',
        'texture_data_url': 'data:image/png;base64,' + 'A' * 700,
        'thumbnail_data_url': 'data:image/png;base64,' + 'B' * 700,
        'model_id': 'model-1',
    })
    assert payload['bytes'].startswith('<'), '字节内容被写进了事件'
    assert payload['note'] == '<omitted:note>'
    assert payload['texture_data_url'].startswith('<omitted')
    assert payload['thumbnail_data_url'].startswith('<omitted')
    assert payload['model_id'] == 'model-1'


def test_texture_paths_are_masked():
    """A texture path is a user path: the log keeps a token, not the path."""
    events = _events_module()
    payload = events._prepare('material.parse', {
        'source': r'C:\Users\someone\Desktop\保密项目\textures\wood_diffuse.png',
        'model_id': 'model-1',
    })
    assert 'someone' not in payload['source']
    assert '<path:' in payload['source']


def test_missing_required_fields_are_annotated_not_dropped():
    """An incomplete lifecycle event is annotated so it can be counted."""
    events = _events_module()
    payload = events._prepare(events.EVENT_STALE, {'model_id': 'model-1'})
    assert payload.get('event_schema') == events.EVENT_SCHEMA
    assert payload['schema_ok'] is False
    assert 'job_id' in payload.get('schema_missing_required', '')
    assert 'schema_missing_identity' in payload


def _measure(mode, count=2000):
    """Cost of one event, and of ``count`` events plus a flush."""
    from prism import performance_trace
    events = _events_module()
    previous = performance_trace.mode()
    performance_trace.set_mode(mode)
    try:
        events.flush_model_events()
        started = time.perf_counter()
        for index in range(count):
            events.record_model_event('material.parse', model_id='model-%d' % index,
                                      source_revision=1, material='gltf:material:0',
                                      stage='parse', duration_ms=1.5)
        single = (time.perf_counter() - started) / count
        started = time.perf_counter()
        events.flush_model_events()
        end_to_end = time.perf_counter() - started
    finally:
        performance_trace.set_mode(previous)
    return {'mode': mode, 'events': count,
            'per_event_us': round(single * 1e6, 2),
            'flush_seconds': round(end_to_end, 4),
            'per_event_including_flush_us': round(
                (single * count + end_to_end) / count * 1e6, 2)}


def test_logging_overhead_off_versus_normal(write_report):
    """Both calibres: one event on its own, and one event end to end.

    The brief forbids quoting whichever number looks better, so both are kept.
    """
    off = _measure('off')
    normal = _measure('normal')
    state = _events_module().model_event_state()
    completeness = _events_module().model_log_completeness()
    writer_active = bool(completeness.get('trace', {}).get('active'))
    payload = {'off': off, 'normal': normal,
               'ratio_per_event': round(normal['per_event_us']
                                        / max(off['per_event_us'], 1e-9), 2),
               'ratio_including_flush':
                   round(normal['per_event_including_flush_us']
                         / max(off['per_event_including_flush_us'], 1e-9), 2),
               'writer_active': writer_active,
               'event_state_after': state,
               'event_completeness': completeness,
               'machine': 'artifacts/model-multimaterial/d0/machine.txt',
               'calibres': {
                   'per_event_us': '一次 record_model_event 调用本身',
                   'per_event_including_flush_us': '同样的调用 + 一次 flush 平摊',
               },
               'verdict': (
                   '没有活动 trace 写入者：两种模式都不落盘，这两个数字只代表'
                   '事件构造开销（清洗 + 身份填充），不代表带写入的端到端开销；'
                   '带写入端的 off/normal 对比记为未测'
                   if not writer_active else
                   '存在活动写入者，两个口径都包含真实写入路径'),
               'note': '单事件与含刷新两个口径都记录，不得只引用较好的一侧'}
    target = write_report(D6_DIR, 'logging-overhead.json', payload)
    stored = json.loads(target.read_text(encoding='utf-8'))
    assert stored['off']['per_event_us'] > 0
    assert stored['normal']['per_event_us'] > 0
    assert stored['verdict'], '必须写明这组数字的适用范围'
    if not stored['writer_active']:
        assert '未测' in stored['verdict'], (
            '没有写入者时，端到端开销必须标为未测，不能当成已测')


def test_event_state_counts_are_exposed():
    """The counters a support bundle needs are readable without a private API."""
    events = _events_module()
    events.record_model_event('material.parse', model_id='model-1')
    state = events.model_event_state()
    for key in ('recorded', 'failures'):
        assert key in state, '缺少计数器 %r' % key
    assert events.model_event_contract(), '没有可导出的日志契约'
