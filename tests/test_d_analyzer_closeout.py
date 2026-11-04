"""D9+10 independent black-box regressions; all logs are isolated and read-only."""
import copy
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

# Set before pytest-qt or Prism can create an application. Child CLI is explicit too.
os.environ['QT_QPA_PLATFORM'] = 'offscreen'
ROOT = Path(__file__).resolve().parents[1]
if not (ROOT / 'tools/analyze_prism_trace.py').exists():
    ROOT = Path(r'C:\Users\admin\Desktop\Prism-main\Prism-main')
ANALYZER = ROOT / 'tools/analyze_prism_trace.py'


def scenario(session='transport', query='q'):
    facts = []

    def emit(event, **fields):
        fact = dict(event=event, session=session, session_id='frozen-protocol',
                    trace_session_id=session, sequence=len(facts) + 1,
                    monotonic=len(facts) / 10, schema_version=2,
                    presentation_schema_version=1, query_id=query,
                    scope={'filter': 'all'}, source_revision='current',
                    read_id='read-1', source_query_id=query, read_source_revision='current',
                    complete=True, final=True, cancelled=False, model_rows=6)
        fact.update(fields)
        facts.append(fact)
        return fact

    return facts, emit


def upstream(emit):
    emit('catalog_query_started', final=False)
    emit('catalog_rows_read_evidence', rows_read=10, logical_rows=10,
         unique_rows=10, unique_rows_complete=True)
    emit('catalog_rows_delivered', stage='consumer_consumed', logical_rows=10,
         unique_rows=10, unique_rows_complete=True)
    emit('asset_model_published', model_unique_ids=6, model_unique_ids_complete=True)


def paint(emit, frame='f1', view='v1', **fields):
    values = dict(frame_id=frame, view_id=view, viewport_drawn=3,
                  current_revision_coverage=3, content_ready=3,
                  count_units='content_draw_operations', coverage_scope='paint_clip',
                  counts_are_lower_bounds=True, incomplete=False, unknown=0)
    values.update(fields)
    return emit('asset_paint_frame', **values)


def visibility(emit, frame='f1', view='v1', **fields):
    values = dict(frame_id=frame, view_id=view, viewport_drawn=2,
                  current_revision_coverage=1, content_ready=0,
                  count_units='unique_model_rows', coverage_scope='current_viewport',
                  counts_are_lower_bounds=False, current_viewport_exact=True,
                  incomplete=False, unknown=0)
    values.update(fields)
    return emit('asset_visibility_snapshot', **values)


def chain():
    facts, emit = scenario()
    emit('session_start')
    upstream(emit)
    paint(emit)
    visibility(emit)
    emit('session_end')
    return facts


def renumber(facts):
    for index, fact in enumerate(facts, 1):
        fact['sequence'] = index
        fact['monotonic'] = index / 10


def named(facts, name):
    return next(fact for fact in facts if fact['event'] == name)


def run(tmp_path, facts=None, payload=None, sources=None):
    if sources is None:
        source = tmp_path / 'input.jsonl'
        if payload is None:
            payload = b''.join((json.dumps(fact) + '\n').encode() for fact in facts)
        source.write_bytes(payload)
        sources = [source]
    before = [source.read_bytes() for source in sources]
    result = subprocess.run(
        [sys.executable, '-B', str(ANALYZER), *map(str, sources),
         '--out-dir', str(tmp_path), '--label', 'd-closeout'],
        env=dict(os.environ, QT_QPA_PLATFORM='offscreen', PYTHONDONTWRITEBYTECODE='1'),
        capture_output=True, timeout=30)
    assert result.returncode == 0, result.stderr.decode(errors='replace')
    assert [source.read_bytes() for source in sources] == before
    return (json.loads((tmp_path / 'd-closeout.json').read_text(encoding='utf-8')),
            (tmp_path / 'd-closeout.md').read_text(encoding='utf-8'))


def row_of(payload):
    return payload['reports'][0]['presentation_evidence']['queries'][0]


def test_valid_filtered_model_and_visibility_not_paint_clip(tmp_path):
    payload, markdown = run(tmp_path, chain())
    row = row_of(payload)
    assert row['session_id'] == 'frozen-protocol'
    assert row['trace_session_id'] == 'transport'
    assert row['model_rows'] == 6  # Read/consumer are 10; filtering is legal.
    assert all(row['layers_observed'].values())
    assert row['evidence_valid'] and row['unique_count_complete']
    assert row['all_layers_observed']
    assert (row['viewport_drawn'], row['current_revision_coverage'], row['content_ready']) == (2, 1, 0)
    assert row['paint_clip']['content_ready'] == 3
    assert row['current_viewport']['count_units'] == 'unique_model_rows'
    assert row['paint_clip']['coverage_scope'] == 'paint_clip'
    assert row['verdict'] == 'layers_observed_not_full_presentation'
    assert row['display_confirmed'] is None
    assert 'paint_clip' in markdown and 'content_draw_operations' in markdown
    assert 'current_viewport' in markdown and '显示器最终呈现未确认' in markdown


@pytest.mark.parametrize('event,field,value', [
    ('catalog_rows_read_evidence', 'rows_read', None),
    ('catalog_rows_read_evidence', 'logical_rows', -1),
    ('catalog_rows_read_evidence', 'unique_rows', -1),
    ('catalog_rows_read_evidence', 'unique_rows', None),
    ('catalog_rows_read_evidence', 'complete', False),
    ('catalog_rows_read_evidence', 'final', False),
    ('catalog_rows_delivered', 'logical_rows', 'bad'),
    ('catalog_rows_delivered', 'logical_rows', True),
    ('catalog_rows_delivered', 'logical_rows', 10.0),
    ('catalog_rows_delivered', 'unique_rows', -1),
    ('catalog_rows_delivered', 'unique_rows', 11),
    ('catalog_rows_delivered', 'complete', False),
    ('catalog_rows_delivered', 'final', False),
    ('asset_model_published', 'model_rows', 6.0),
    ('asset_model_published', 'model_unique_ids', -1),
    ('asset_model_published', 'model_unique_ids', 7),
    ('asset_model_published', 'complete', False),
    ('asset_paint_frame', 'viewport_drawn', True),
    ('asset_visibility_snapshot', 'content_ready', -1),
])
def test_terminal_and_strict_nonnegative_integer_counts(tmp_path, event, field, value):
    facts = chain()
    named(facts, event)[field] = value
    payload, _ = run(tmp_path, facts)
    row = row_of(payload)
    assert not row['evidence_valid']
    assert not row['all_layers_observed']
    assert row['missing_or_invalid']
    assert row['verdict'] == 'evidence_insufficient'


@pytest.mark.parametrize('event,field', [
    (event, field)
    for event in ('catalog_rows_read_evidence', 'catalog_rows_delivered',
                  'asset_model_published', 'asset_paint_frame', 'asset_visibility_snapshot')
    for field in ('read_id', 'source_query_id', 'read_source_revision')
])
def test_provenance_is_validated_in_every_layer(tmp_path, event, field):
    facts = chain()
    named(facts, event)[field] = 'foreign'
    payload, _ = run(tmp_path, facts)
    row = row_of(payload)
    assert not row['evidence_valid']
    assert row['identity_conflicts']


@pytest.mark.parametrize('field', ['session_id', 'trace_session_id', 'read_id'])
def test_identity_missing_or_mutated_never_rebases_query(tmp_path, field):
    facts = chain()
    named(facts, 'asset_visibility_snapshot')[field] = 'different'
    payload, _ = run(tmp_path, facts)
    row = row_of(payload)
    assert not row['evidence_valid']
    assert row['session_id'] == 'frozen-protocol'
    assert row['latest_started_frame']['visibility'] is None


def test_reused_read_provenance_can_differ_from_current_query(tmp_path):
    facts = chain()
    for fact in facts:
        fact.update(read_reused=True, source_query_id='old-query', read_source_revision='old-revision')
    payload, _ = run(tmp_path, facts)
    assert row_of(payload)['evidence_valid']


@pytest.mark.parametrize('unknown', [False, True])
def test_budget_and_unknown_are_explicitly_degraded(tmp_path, unknown):
    facts = chain()
    for event in ('catalog_rows_read_evidence', 'catalog_rows_delivered'):
        fact = named(facts, event)
        fact.update(unique_rows=None if unknown else 2, unique_rows_complete=False,
                    unique_rows_basis='not_collected' if unknown else 'lower_bound',
                    id_budget_exhausted=not unknown)
    named(facts, 'asset_model_published').update(model_unique_ids=2, model_unique_ids_complete=False)
    for event in ('asset_paint_frame', 'asset_visibility_snapshot'):
        named(facts, event).update(incomplete=True, unknown=None if unknown else 1)
    payload, markdown = run(tmp_path, facts)
    row = row_of(payload)
    assert all(row['layers_observed'].values())
    assert not row['unique_count_complete']
    assert row['warnings']
    assert 'model_unique_count_incomplete' in row['warnings']
    assert row['content_ready'] == 0 and row['paint_clip']['content_ready'] == 3
    assert row['latest_started_frame_closed']
    assert not row['latest_started_frame']['evidence_complete']
    assert row['most_recent_complete_frame'] is None
    assert row['verdict'] == 'layers_observed_evidence_degraded'
    assert '证据降级警告' in markdown and 'unique_count_complete=False' in markdown


@pytest.mark.parametrize('damage', ['middle_json', 'tail_json', 'utf8', 'nonobject', 'gap', 'conflict'])
def test_input_gap_is_not_queue_loss_and_invalidates_d_chain(tmp_path, damage):
    facts = chain()
    lines = [(json.dumps(fact) + '\n').encode() for fact in facts]
    if damage == 'gap':
        for fact in facts[3:]:
            fact['sequence'] += 1
        lines = [(json.dumps(fact) + '\n').encode() for fact in facts]
    elif damage == 'conflict':
        conflict = dict(facts[5], content_ready=1000)
        lines.insert(6, (json.dumps(conflict) + '\n').encode())
    elif damage == 'tail_json':
        lines.append(b'{broken')
    else:
        lines.insert(3, {'middle_json': b'{broken}\n', 'utf8': b'\xff\n', 'nonobject': b'[]\n'}[damage])
    payload, markdown = run(tmp_path, payload=b''.join(lines))
    report, row = payload['reports'][0], row_of(payload)
    assert report['completeness']['incomplete']
    assert report['completeness']['input_evidence_gap']
    assert row['input_evidence_gap'] and not row['evidence_valid']
    assert 'input_evidence_gap' in row['missing_or_invalid']
    assert payload['io']['input_evidence_gap']
    assert report['completeness']['dropped_events'] == 0
    assert report['completeness']['seq_gap_count'] == 0
    assert '不能' in markdown and '队列丢失数量' in markdown
    if damage == 'gap':
        gap = next(item for item in payload['io']['input_evidence_gaps'] if item['kind'] == 'sequence_coverage_gap')
        assert gap['missing_sequence_ranges'] == [[4, 4]]


def test_reordered_priority_lanes_and_identical_duplicates_are_not_gaps(tmp_path):
    facts = chain()
    payload, _ = run(tmp_path, list(reversed(facts)) + [copy.deepcopy(facts[4])])
    assert not payload['io']['input_evidence_gap']
    assert payload['io']['identical_duplicates'] == 1
    assert row_of(payload)['evidence_valid']


def test_gap_affects_only_physical_sessions_in_damaged_files(tmp_path):
    bad = tmp_path / 'bad.jsonl'
    good = tmp_path / 'good.jsonl'
    bad.write_bytes(b''.join((json.dumps(fact) + '\n').encode() for fact in chain()) + b'{broken')
    good_facts = chain()
    for fact in good_facts:
        fact.update(session='other-transport', trace_session_id='other-transport')
    good.write_bytes(b''.join((json.dumps(fact) + '\n').encode() for fact in good_facts))
    payload, _ = run(tmp_path, sources=[bad, good])
    reports = {report['session']: report for report in payload['reports']}
    assert reports['transport']['presentation_evidence']['queries'][0]['input_evidence_gap']
    assert not reports['other-transport']['completeness']['incomplete']
    assert reports['other-transport']['presentation_evidence']['queries'][0]['evidence_valid']


@pytest.mark.parametrize('order', ['paint_before_publish', 'visibility_before_publish', 'visibility_before_paint', 'monotonic_reversal'])
def test_true_publish_and_frame_order_required(tmp_path, order):
    facts = chain()
    if order == 'paint_before_publish':
        facts[4], facts[5] = facts[5], facts[4]
    elif order == 'visibility_before_publish':
        publication = facts.pop(4)
        facts.insert(6, publication)
    elif order == 'visibility_before_paint':
        facts[5], facts[6] = facts[6], facts[5]
    renumber(facts)
    if order == 'monotonic_reversal':
        named(facts, 'asset_paint_frame')['monotonic'] = 0.01
    payload, _ = run(tmp_path, facts)
    assert not row_of(payload)['evidence_valid']


@pytest.mark.parametrize('dual_view', [False, True])
def test_interleaved_complete_frames_pair_stably(tmp_path, dual_view):
    facts, emit = scenario()
    emit('session_start')
    upstream(emit)
    paint(emit, 'f1', 'v1')
    paint(emit, 'f2' if not dual_view else 'f1', 'v2' if dual_view else 'v1')
    visibility(emit, 'f2' if not dual_view else 'f1', 'v2' if dual_view else 'v1', content_ready=1)
    visibility(emit, 'f1', 'v1', content_ready=0)
    emit('session_end')
    payload, _ = run(tmp_path, facts)
    row = row_of(payload)
    assert row['evidence_valid'] and row['latest_started_frame_closed']
    assert len(row['frames']) == 2 and all(frame['closed'] for frame in row['frames'])
    assert row['content_ready'] == 1  # Latest start, not last visibility completion.
    assert row['latest_started_frame']['view_id'] == ('v2' if dual_view else 'v1')
    assert row['most_recent_complete_frame']['paint']['sequence'] == 7


@pytest.mark.parametrize('dual_view', [False, True])
def test_latest_pending_is_not_silently_replaced_by_old_complete_frame(tmp_path, dual_view):
    facts = chain()
    facts.pop()
    pending = dict(named(facts, 'asset_paint_frame'), frame_id='f2', view_id='v2' if dual_view else 'v1')
    facts.append(pending)
    facts.append(dict(facts[0], event='session_end'))
    renumber(facts)
    payload, markdown = run(tmp_path, facts)
    row = row_of(payload)
    assert not row['latest_started_frame_closed'] and not row['evidence_valid']
    assert row['latest_started_frame']['frame_id'] == 'f2'
    assert row['most_recent_complete_frame']['frame_id'] == 'f1'
    assert row['viewport_drawn'] is None and row['content_ready'] is None
    assert 'latest_frame_pending' in markdown


def test_missing_historical_view_id_uses_defaultview_but_missing_visibility_counts_stay_unknown(tmp_path):
    facts = chain()
    for fact in facts:
        fact.pop('view_id', None)
    snapshot = named(facts, 'asset_visibility_snapshot')
    for field in ('viewport_drawn', 'current_revision_coverage', 'content_ready'):
        snapshot.pop(field)
    payload, _ = run(tmp_path, facts)
    row = row_of(payload)
    assert row['latest_started_frame']['view_id'] == 'defaultview'
    assert row['latest_started_frame_closed']
    assert row['content_ready'] is None
    assert row['paint_clip']['content_ready'] == 3
    assert 'visibility_current_counts_unknown' in row['warnings']


@pytest.mark.parametrize('damage', ['missing_model_unique', 'model_budget', 'visibility_incomplete',
                                   'visibility_unknown', 'missing_provenance', 'model_alias_conflict'])
def test_unknown_and_budget_states_are_never_unique_complete(tmp_path, damage):
    facts = chain()
    model = named(facts, 'asset_model_published')
    snapshot = named(facts, 'asset_visibility_snapshot')
    if damage == 'missing_model_unique':
        model.pop('model_unique_ids')
        model.pop('model_unique_ids_complete')
    elif damage == 'model_budget':
        model['id_budget_exhausted'] = True
    elif damage == 'visibility_incomplete':
        snapshot['incomplete'] = True
    elif damage == 'visibility_unknown':
        snapshot.update(content_ready=None, current_viewport_exact=False, unknown=None)
    elif damage == 'missing_provenance':
        snapshot.pop('read_id')
    else:
        model.update(model_unique_rows=5, model_unique_rows_complete=True)
    payload, _ = run(tmp_path, facts)
    row = row_of(payload)
    assert not row['unique_count_complete']
    assert row['warnings'] or row['missing_or_invalid']
    assert row['verdict'] != 'layers_observed_not_full_presentation'


def test_absent_optional_transport_identity_does_not_require_protocol_session_equality(tmp_path):
    facts = chain()
    for fact in facts:
        fact.pop('trace_session_id')
    payload, _ = run(tmp_path, facts)
    assert row_of(payload)['evidence_valid']


def test_latest_invalid_read_snapshot_does_not_fall_back_to_prior_terminal(tmp_path):
    facts = chain()
    facts.insert(-1, dict(named(facts, 'catalog_rows_read_evidence'), complete=False, final=False))
    renumber(facts)
    payload, _ = run(tmp_path, facts)
    row = row_of(payload)
    assert not row['evidence_valid']
    assert row['layers']['read'][0]['final'] is False


def test_explicit_parent_trace_baseline_is_linked_not_a_new_read(tmp_path):
    facts, emit = scenario()
    emit('session_start')
    emit('catalog_query_baseline', baseline_only=True, baseline_rows_are_new_work=False,
         baseline_layers=['read', 'consumer', 'model'], final=False)
    emit('catalog_rows_read_evidence', baseline_only=True, baseline_rows_are_new_work=False,
         state_origin_trace_session_id='previous', rows_read=10, logical_rows=10,
         unique_rows=10, unique_rows_complete=True)
    emit('catalog_rows_delivered', baseline_only=True, baseline_rows_are_new_work=False,
         state_origin_trace_session_id='previous', stage='consumer_consumed', logical_rows=10,
         unique_rows=10, unique_rows_complete=True)
    emit('asset_model_published', baseline_only=True, baseline_rows_are_new_work=False,
         state_origin_trace_session_id='previous')
    paint(emit)
    visibility(emit)
    emit('session_end')
    payload, _ = run(tmp_path, facts)
    row = row_of(payload)
    assert row['baseline_linked']
    assert not row['layers_observed']['read'] and not row['layers_observed']['model']
    assert row['layers']['read'] == [None]
    assert row['layers']['model'] is None
    assert row['latest_started_frame']['evidence_valid']
    assert not row['all_layers_observed'] and row['verdict'] == 'evidence_insufficient'
    assert 'trace_baseline_linked_not_actual_read' in row['warnings']


@pytest.mark.parametrize('damage', ['io', 'unstable'])
def test_io_and_read_instability_propagate_conservatively(tmp_path, monkeypatch, damage):
    spec = importlib.util.spec_from_file_location('d_closeout_analyzer', ANALYZER)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    source = tmp_path / 'stable.jsonl'
    source.write_bytes(b''.join((json.dumps(fact) + '\n').encode() for fact in chain()))
    if damage == 'io':
        paths = [str(source), str(tmp_path / 'unreadable.jsonl')]
    else:
        paths = [str(source)]
        stat = module._stat_identity
        calls = []

        def changing(path):
            result = stat(path)
            calls.append(path)
            result['mtime_ns'] += len(calls)
            return result

        monkeypatch.setattr(module, '_stat_identity', changing)
    reports, io = module.analyse(paths)
    assert io['input_evidence_gap']
    assert reports[0]['completeness']['incomplete']
    assert reports[0]['presentation_evidence']['queries'][0]['input_evidence_gap']
    assert not reports[0]['presentation_evidence']['queries'][0]['evidence_valid']
