"""D组新旧协议独立黑盒；只写隔离日志，不启动用户工程。"""
import json
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def scenario():
    facts = []

    def emit(event, query_id='q1', **fields):
        value = dict(event=event, session='d-test', session_id='d-test',
                     sequence=len(facts) + 1, monotonic=len(facts) / 10,
                     schema_version=2, presentation_schema_version=1,
                     query_id=query_id, scope='all', source_revision='r1',
                     frame_id=1, model_rows=100000, complete=True, cancelled=False, final=False)
        value.update(fields)
        facts.append(value)
        return value

    return facts, emit


def run(tmp_path, facts):
    source = tmp_path / 'input.jsonl'
    payload = b''.join((json.dumps(fact) + '\n').encode() for fact in facts)
    source.write_bytes(payload)
    result = subprocess.run([sys.executable, str(ROOT / 'tools/analyze_prism_trace.py'),
                             str(source), '--out-dir', str(tmp_path), '--label', 'd'],
                            capture_output=True, timeout=30)
    assert result.returncode == 0, result.stderr.decode(errors='replace')
    assert source.read_bytes() == payload
    report = json.loads((tmp_path / 'd.json').read_text(encoding='utf-8'))['reports'][0]
    return report, (tmp_path / 'd.md').read_text(encoding='utf-8')


def five_layers(emit, query='q1'):
    emit('catalog_query_started', query)
    emit('catalog_rows_read_evidence', query, rows_read=100000, unique_rows=100000,
         unique_rows_complete=True, complete=True, final=True)
    emit('catalog_rows_delivered', query, layer='consumer_consumed',
         logical_rows=100000, unique_rows=100000, unique_rows_complete=True,
         complete=True, final=True)
    emit('asset_model_published', query, model_rows=100000, model_unique_ids=100000,
         model_unique_ids_complete=True, final=True)
    emit('asset_paint_frame', query, model_rows=100000, viewport_drawn=40,
         current_revision_coverage=35, content_ready=20, placeholder=5, fallback=10,
         count_units='content_draw_operations', coverage_scope='paint_clip')
    # Current-viewport counts are independent facts, not paint-operation aliases.
    emit('asset_visibility_snapshot', query, viewport_candidates=40, viewport_drawn=38,
         current_revision_coverage=30, content_ready=18, count_units='unique_model_rows',
         coverage_scope='current_viewport', current_viewport_exact=True, incomplete=False)


def test_same_query_direct_chain_does_not_claim_all_visible(tmp_path):
    facts, emit = scenario()
    emit('session_start')
    five_layers(emit)
    emit('session_end')
    report, markdown = run(tmp_path, facts)
    row = report['presentation_evidence']['queries'][0]
    assert row['all_layers_observed']
    assert row['model_rows'] == 100000
    assert row['viewport_drawn'] == 40
    assert row['content_ready'] == 20
    assert row['verdict'] == 'layers_observed_not_full_presentation'
    assert row['display_confirmed'] is None
    assert '显示器最终呈现未确认' in markdown


def test_resources_never_become_paint_and_missing_stays_unknown(tmp_path):
    facts, emit = scenario()
    emit('session_start')
    emit('asset_resource_generated', generated=100000)
    emit('asset_cache_installed', cache_installed=100000)
    emit('asset_model_published', model_rows=100000, final=True)
    emit('session_end')
    report, markdown = run(tmp_path, facts)
    row = report['presentation_evidence']['queries'][0]
    assert row['viewport_drawn'] is None
    assert row['content_ready'] is None
    assert 'paint' in row['missing_or_invalid']
    assert row['verdict'] == 'evidence_insufficient'
    assert 'viewport_drawn=未知' in markdown


def test_double_view_revision_conflict_and_old_callback(tmp_path):
    facts, emit = scenario()
    emit('session_start')
    five_layers(emit, 'view1')
    emit('catalog_query_cancelled', 'view1', cancelled=True, final=True)
    emit('asset_paint_frame', 'view1', viewport_drawn=999999)
    five_layers(emit, 'view2')
    emit('asset_paint_frame', 'view2', viewport_drawn=888888, source_revision='old')
    emit('session_end')
    report, _ = run(tmp_path, facts)
    rows = {row['query_id']: row for row in report['presentation_evidence']['queries']}
    assert rows['view1']['cancelled']
    assert rows['view1']['viewport_drawn'] == 40
    assert rows['view1']['post_cancel_events']
    assert rows['view2']['viewport_drawn'] == 40
    assert rows['view2']['identity_conflicts']


def test_missing_identity_and_reported_loss_cannot_pass(tmp_path):
    facts, emit = scenario()
    emit('session_start')
    five_layers(emit)
    value = emit('asset_paint_frame', viewport_drawn=777777)
    value.pop('source_revision')
    emit('session_end', dropped_events=2)
    report, _ = run(tmp_path, facts)
    layered = report['presentation_evidence']
    assert len(layered['unlinked_events']) == 1
    assert layered['queries'][0]['viewport_drawn'] == 40
    assert not layered['queries'][0]['all_layers_observed']
    assert 'trace_loss_reported' in layered['queries'][0]['missing_or_invalid']


def test_legacy_cross_frame_model_sum_is_not_unique_drawn(tmp_path):
    facts = [dict(event='session_start', session='legacy', sequence=1),
             dict(event='assets.paint_frame', session='legacy', sequence=2,
                  cards=100700, frames=8),
             dict(event='session_end', session='legacy', sequence=3)]
    report, markdown = run(tmp_path, facts)
    assert report['presentation_markers']['frames']['totals']['cards'] == 100700
    assert report['presentation_evidence']['protocol'] == 'legacy'
    assert report['presentation_evidence']['verdict'] == 'evidence_insufficient'
    assert '跨帧模型行数之和' in report['presentation_markers']['frames']['note']
    assert '不能证明完整呈现' in markdown


def test_snapshot_counts_are_not_added_and_no_session_end_needed(tmp_path):
    facts, emit = scenario()
    emit('session_start')
    five_layers(emit)
    emit('asset_paint_frame', viewport_drawn=5, current_revision_coverage=5,
         content_ready=3)
    emit('asset_visibility_snapshot', viewport_candidates=5)
    report, _ = run(tmp_path, list(reversed(facts)))
    row = report['presentation_evidence']['queries'][0]
    assert row['viewport_drawn'] == 5
    assert row['content_ready'] == 3
    assert row['all_layers_observed']
    assert report['completeness']['incomplete']


@pytest.mark.parametrize('broken', ['sequence_only', 'incomplete_consumer', 'mismatched_frame', 'unknown_model'])
def test_event_names_alone_cannot_prove_valid_chain(tmp_path, broken):
    facts, emit = scenario()
    emit('session_start')
    five_layers(emit)
    if broken == 'sequence_only':
        next(value for value in facts if value['event'] == 'catalog_rows_delivered')['layer'] = 'sequence_access'
    elif broken == 'incomplete_consumer':
        next(value for value in facts if value['event'] == 'catalog_rows_delivered')['complete'] = False
    elif broken == 'mismatched_frame':
        next(value for value in facts if value['event'] == 'asset_visibility_snapshot')['frame_id'] = 2
    else:
        next(value for value in facts if value['event'] == 'asset_model_published').pop('model_rows')
    emit('session_end')
    report, _ = run(tmp_path, facts)
    row = report['presentation_evidence']['queries'][0]
    assert not row['all_layers_observed']
    assert row['verdict'] == 'evidence_insufficient'


@pytest.mark.parametrize('broken', ['cancelled_paint', 'kind_loss', 'invalid_counter', 'incomplete_model', 'old_model'])
def test_independent_review_regressions(tmp_path, broken):
    facts, emit = scenario()
    emit('session_start')
    five_layers(emit)
    if broken == 'cancelled_paint':
        emit('asset_paint_frame', cancelled=True, viewport_drawn=999999)
    elif broken == 'invalid_counter':
        emit('asset_paint_frame', viewport_drawn='invalid')
    elif broken == 'incomplete_model':
        next(value for value in facts if value['event'] == 'asset_model_published')['complete'] = False
    elif broken == 'old_model':
        emit('asset_paint_frame', model_rows=100, viewport_drawn=10)
    emit('session_end', **({'dropped_by_kind': {'asset_visibility_snapshot': 1}}
                          if broken == 'kind_loss' else {}))
    report, _ = run(tmp_path, facts)
    row = report['presentation_evidence']['queries'][0]
    assert not row['all_layers_observed']
    if broken == 'cancelled_paint':
        assert row['viewport_drawn'] == 40
        assert row['verdict'] == 'cancelled'
    else:
        assert row['verdict'] == 'evidence_insufficient'


def test_unknown_source_revision_is_not_real_identity(tmp_path):
    facts, emit = scenario()
    emit('session_start')
    five_layers(emit)
    for fact in facts:
        fact['source_revision'] = 'unknown'
    emit('session_end')
    report, _ = run(tmp_path, facts)
    layered = report['presentation_evidence']
    assert layered['query_count'] == 0
    assert layered['unlinked_events']
