"""AI-09: regression tests for the P19-P22 logging contract.

They cover the four contracts this round changed:

* **P20** -- overlapping GC intervals are merged (union), never summed, and
  a modal user operation is not turned into a GC stall.
* **P19** -- the fast-path counters are cumulative, belong to an owner, are
  reset when the owner changes and are flushed when the recording ends.
* **P21** -- the queue reports the current depth, the real oldest-task age
  (unknown without a task identity), the busy period and the history peak as
  four separate things.
* **P22** -- the session publishes a manifest with the recording contract
  and the retention policy.
"""
import json
import os
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

from prism import performance_trace as trace  # noqa: E402
from tools import analyze_prism_trace as analyzer  # noqa: E402


# ── P20 ────────────────────────────────────────────────────────────────

def test_union_intervals_merges_overlaps_and_keeps_gaps():
    merged = trace.union_intervals([(0.0, 0.2), (0.1, 0.3), (0.5, 0.6)])
    assert merged == [(0.0, 0.3), (0.5, 0.6)]


def test_union_intervals_ignores_empty_spans():
    assert trace.union_intervals([(1.0, 1.0), (2.0, 1.0)]) == []


def _stall_fields(trace_obj, *, modal):
    trace_obj._gc_intervals.append((100.0, 100.2, True))
    trace_obj._gc_intervals.append((100.1, 100.3, True))
    stall = dict(stall_id='s1', window_start_monotonic=100.0,
                 heartbeat_max=250, count=3, dispatches_completed=1,
                 background_observed=False, dispatch_intervals=[],
                 context_snapshot={'modal': modal, 'project_token': 'p'})
    # The double is not a PerformanceTrace, so the method is called unbound.
    return trace.PerformanceTrace._record_stall_context(trace_obj, stall, 300.0, 100.3)


def _double():
    return SimpleNamespace(_gc_started={}, _gc_intervals=[],
                           gui_thread=trace.threading.get_ident(),
                           record=Mock())


def test_gc_overlap_is_unioned_not_summed():
    target = _double()
    fields = _stall_fields(target, modal=False)
    # The two collections cover 300 ms of wall clock, not 400 ms.
    assert fields['observed_gui_gc_ms'] == pytest.approx(300.0, abs=0.01)
    assert fields['observed_gui_gc_naive_sum_ms'] == pytest.approx(400.0, abs=0.01)
    assert fields['observed_gui_gc_merged_interval_count'] == 1
    assert fields['observed_gui_gc_interval_count'] == 2
    assert fields['observed_gui_gc_fraction'] <= 1.0


def test_gc_dominating_window_is_classified_as_gc_pause():
    target = _double()
    fields = _stall_fields(target, modal=False)
    assert fields['classification'] == 'gc_pause'
    assert fields['gc_classification_applied'] is True


def test_modal_user_operation_keeps_real_blocking_evidence():
    """A modal dialog does not cancel real blocking evidence.

    round2 (T2): the previous rule suppressed ``gc_pause`` whenever a
    modal was open. That conflated "a dialog is open" with "the event
    loop is running, waiting for the user". Inside a dialog the GUI can
    still be blocked by the collector, so the GC evidence now survives and
    the modal context is published as evidence instead.
    """
    target = _double()
    fields = _stall_fields(target, modal=True)
    assert fields['classification'] == 'gc_pause'
    assert fields['gc_classification_applied'] is True
    # The measurement itself is untouched.
    assert fields['observed_gui_gc_ms'] == pytest.approx(300.0, abs=0.01)
    assert fields['modal_context'] is True


def test_gc_callback_survives_a_minimal_test_double():
    """A double without the interval store must not swallow the event."""
    target = SimpleNamespace(_gc_started={}, gui_thread=trace.threading.get_ident(),
                             record=Mock())
    started = trace.TraceContext(trace_id='t', span_id='s')
    clock = iter((10.0, 10.125))
    original = trace.time.perf_counter
    trace.time.perf_counter = lambda: next(clock)
    try:
        with trace.restore_context(started):
            trace.PerformanceTrace._gc_callback(target, 'start', {'generation': 1})
        with trace.restore_context(started):
            trace.PerformanceTrace._gc_callback(
                target, 'stop', {'generation': 1, 'collected': 0, 'uncollectable': 0})
    finally:
        trace.time.perf_counter = original
    assert target.record.called
    assert target.record.call_args.kwargs['duration_ms'] == pytest.approx(125.0)


# ── P21 ────────────────────────────────────────────────────────────────

def test_queue_oldest_age_is_unknown_without_task_identity():
    accounting = trace.QueueAccounting('image_decode')
    accounting.submitted()
    snapshot = accounting.snapshot()
    assert snapshot['oldest_age_ms'] is None
    assert snapshot['oldest_age_status'] == 'not_accounted_without_task_ids'


def test_queue_reports_the_real_oldest_age_with_task_identity():
    accounting = trace.QueueAccounting('image_decode')
    accounting.submitted(task_id='t1')
    accounting.submitted(task_id='t2', enqueued_at=1.0)
    snapshot = accounting.snapshot()
    assert snapshot['oldest_task_id'] == 't2'
    assert snapshot['oldest_age_ms'] is not None
    assert snapshot['oldest_age_status'] == 'known_from_task_identity'


def test_queue_busy_period_is_not_the_oldest_task_age():
    accounting = trace.QueueAccounting('image_decode')
    accounting.submitted()
    snapshot = accounting.snapshot()
    assert snapshot['busy_period_status'] == 'known'
    assert snapshot['busy_period_ms'] is not None
    assert snapshot['oldest_age_ms'] is None
    # The legacy alias keeps working and states what it means.
    assert snapshot['pending_busy_ms'] == snapshot['busy_period_ms']
    assert 'busy_period' in snapshot['pending_busy_ms_semantics']


def test_queue_depth_is_zero_after_the_queue_drains():
    accounting = trace.QueueAccounting('image_decode')
    accounting.submitted()
    accounting.started()
    accounting.service_done()
    accounting.installed()
    snapshot = accounting.snapshot()
    assert (snapshot['pending'], snapshot['running'], snapshot['ready']) == (0, 0, 0)
    assert snapshot['busy_period_status'] == 'idle'
    assert snapshot['oldest_age_ms'] is None


def test_queue_peak_is_reported_separately_from_the_current_depth():
    accounting = trace.QueueAccounting('image_decode')
    for _ in range(3):
        accounting.submitted()
    for _ in range(3):
        accounting.started()
        accounting.service_done()
        accounting.installed()
    snapshot = accounting.snapshot()
    assert snapshot['pending'] == 0
    assert snapshot['peak_pending'] == 3
    # round2 (T1): the peak is no longer reset when the owner changes --
    # per-owner cumulative counters live in their own buckets now. The old
    # meaning was "since the last reset"; the new one is "since the
    # session started".
    assert snapshot['peak_scope'] == 'since_session_start'


def test_queue_counters_reset_when_the_owner_changes():
    accounting = trace.QueueAccounting('image_decode', min_interval_s=0)
    trace.bind_project('project-A', open_epoch=1, generation=1)
    accounting.submitted()
    accounting.started()
    accounting.service_done()
    trace.bind_project('project-B', open_epoch=2, generation=1)
    accounting.submitted()
    snapshot = accounting.snapshot()
    assert snapshot['submitted'] == 1


# ── P19 ────────────────────────────────────────────────────────────────

def test_hotpath_rollup_is_cumulative_and_names_its_owner():
    accounting = trace._HotpathRollup('assets.queue_visible_details', 16,
                                      ('project-A', 1, 1))
    accounting.add(1.0, False)
    accounting.add(2.0, True)
    payload = trace._hotpath_payload(accounting, reset_reason='initial')
    assert payload['calls'] == 2
    assert payload['total_ms'] == pytest.approx(3.0)
    assert payload['max_ms'] == pytest.approx(2.0)
    assert payload['counters_cumulative'] is True
    assert payload['cumulative_scope'] == 'since_last_reset'
    assert payload['owner_project_token'] == 'project-A'
    assert payload['sampling_strategy'] == 'threshold_rollup'
    assert payload['window_calls'] == 2


def test_hotpath_rollup_resets_when_the_owner_changes():
    state = trace._HotpathRollup('x', 16, ('project-A', 1, 1))
    state.add(5.0, False)
    state.reset_all('owner_changed')
    assert state.calls == 0
    assert state.reset_reason == 'owner_changed'


# ── analyzer ───────────────────────────────────────────────────────────

def _analyse(tmp_path, events):
    path = tmp_path/'fixture.jsonl'
    path.write_text(''.join(json.dumps(dict(session='s', sequence=i+1, **event))+'\n'
                             for i, event in enumerate(events)), encoding='utf-8')
    reports, io_info = analyzer.analyse([str(path)])
    return reports[0], io_info


def test_analyzer_does_not_double_count_cancelled_work(tmp_path):
    report, _ = _analyse(tmp_path, [dict(
        event='queue_rollup', category='image_decode',
        counters=dict(submitted=10, started=10, completed=10, installed=8,
                      cancelled=2, cancel_after_start=1, stale_discarded=0,
                      rejected=0, retried=0, coalesced=0, failed=0),
        pending=0, running=0, ready=0, limit=8,
        peak_pending=8, peak_running=2, oldest_age_ms=None)])
    row = report['queue_rollups']['categories'][0]
    # cancelled already contains cancel_after_start: the total is 2, not 3.
    assert row['discarded_total'] == 2
    breakdown = row['discarded_breakdown']
    assert breakdown['never_ran']['cancelled_never_ran'] == 1
    assert breakdown['ran_but_result_dropped']['cancel_after_start'] == 1


def test_analyzer_keeps_coalesced_and_retried_out_of_the_drop_total(tmp_path):
    report, _ = _analyse(tmp_path, [dict(
        event='queue_rollup', category='image_decode',
        counters=dict(submitted=10, started=10, completed=10, installed=10,
                      cancelled=0, cancel_after_start=0, stale_discarded=0,
                      rejected=0, retried=3, coalesced=2, failed=0),
        pending=0, running=0, ready=0, limit=8,
        peak_pending=8, peak_running=2, oldest_age_ms=None)])
    row = report['queue_rollups']['categories'][0]
    assert row['discarded_total'] == 0
    assert row['discarded_breakdown']['not_a_drop'] == {'coalesced': 2, 'retried': 3}


def test_analyzer_keeps_the_queue_age_unknown_with_its_reason(tmp_path):
    report, _ = _analyse(tmp_path, [dict(
        event='queue_rollup', category='image_decode',
        counters=dict(submitted=1, started=0, completed=0, installed=0),
        pending=0, running=0, ready=0, limit=8,
        peak_pending=1, peak_running=0, oldest_age_ms=None,
        oldest_age_status='not_accounted_without_task_ids',
        busy_period_ms=12.5, busy_period_status='known')])
    depth = report['queue_rollups']['categories'][0]['depth']
    assert depth['oldest_age_ms'] is None
    assert depth['oldest_age_status'] == 'not_accounted_without_task_ids'
    assert depth['busy_period_ms'] == 12.5


def test_analyzer_does_not_rank_threshold_samples_as_stages(tmp_path):
    report, _ = _analyse(tmp_path, [
        dict(event='hotpath_sample', name='assets.queue_visible_details',
             duration_ms=40.0, observation='threshold_sample_not_complete_span',
             role='attachment', monotonic=1.0, start_mono=0.96),
        dict(event='operation', name='real.stage', duration_ms=5.0,
             status='end', role='end', monotonic=1.5),
    ])
    window = report['traces'][-1]['timeline']
    names = [stage['name'] for stage in window['stage_ranking']]
    assert 'assets.queue_visible_details' not in names
    assert window['threshold_observations']['count'] == 1


def test_analyzer_reports_the_rotation_chain(tmp_path):
    base = tmp_path/'Prism-operations-1.jsonl'
    base.write_text(json.dumps(dict(event='operation', sequence=1, session='s',
                                    monotonic=1.0, duration_ms=1.0))+'\n',
                    encoding='utf-8')
    (tmp_path/'Prism-operations-1.jsonl.1').write_text(
        json.dumps(dict(event='operation', sequence=2, session='s',
                        monotonic=2.0, duration_ms=1.0))+'\n', encoding='utf-8')
    (tmp_path/'Prism-operations-1.jsonl.3').write_text(
        json.dumps(dict(event='operation', sequence=3, session='s',
                        monotonic=3.0, duration_ms=1.0))+'\n', encoding='utf-8')
    paths = analyzer.resolve_inputs([str(tmp_path)])
    _, io_info = analyzer.analyse(paths)
    chain = io_info['rotation_chains'][0]
    assert chain['missing_indices'] == [2]
    assert chain['complete'] is False


def test_analyzer_surfaces_the_gc_evidence_next_to_the_stall(tmp_path):
    report, _ = _analyse(tmp_path, [dict(
        event='ui.stall_context', stall_id='s1', duration_ms=300.0,
        classification='gc_pause', observed_gui_gc_ms=300.0,
        observed_gui_gc_fraction=1.0, gc_attribution='measured')])
    contexts = report['stall_contexts']
    assert any(row.get('observed_gui_gc_ms') == 300.0
               for row in contexts.get('events', []))
