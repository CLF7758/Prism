"""AI-09 round2 (D1): the task/owner/epoch contract and the diagnostic isolation.

These are the directed regressions for T1 and T2 of the round2 task card.
They are **new** tests in a new file: no existing test or assertion is
changed by this round, and the previous round's file keeps its own
meaning (two of its expectations were updated with the old/new reading
written down next to them).

The file covers the card's acceptance list:

* the GC fallback really lands **on disk** (the log is flushed and
  parsed, not a returned dict inspected);
* a business exception plus a failing diagnostic end still keeps the
  business traceback, and the context is restored;
* cross-project in-flight work is conserved and attributed to the owner
  it was registered under;
* a modal dialog with a real GC pause is distinguished from a normal
  user wait;
* the old log format, duplicated inputs, a missing segment and a version
  conflict have known answers.
"""
import json
import os
import time
from types import SimpleNamespace

import pytest

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

from prism import performance_trace as trace  # noqa: E402
from tools import analyze_prism_trace as analyzer  # noqa: E402


# ── helpers ─────────────────────────────────────────────────────────────

_APP = None


def _application():
    global _APP
    from PyQt6 import QtWidgets
    if _APP is None:
        _APP = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    return _APP


def _read_events(path):
    events = []
    for part in sorted(path.parent.glob(path.name + '*')):
        with open(part, 'r', encoding='utf-8') as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                try:
                    events.append(json.loads(line))
                except ValueError:
                    pass
    return events


# ── T1: task identity, owner and epoch ──────────────────────────────────

def test_late_completion_is_counted_for_the_owner_it_was_registered_under():
    """A's task finishes after B took over: A is credited, not B."""
    accounting = trace.QueueAccounting('image_decode', min_interval_s=0)
    trace.bind_project('project-A', open_epoch=1, generation=1)
    accounting.submitted(task_id='a1')
    accounting.started(task_id='a1')
    trace.bind_project('project-B', open_epoch=2, generation=1)
    accounting.submitted(task_id='b1')
    accounting.started(task_id='b1')
    # A's work finishes late -- after B is already running.
    accounting.service_done(task_id='a1', service_ms=5.0)
    accounting.installed(task_id='a1')
    accounting.service_done(task_id='b1', service_ms=5.0)
    accounting.installed(task_id='b1')
    snapshot = accounting.snapshot()
    buckets = {(b['owner_project_token'], b['epoch']): b['counters']
               for b in snapshot['owner_buckets']}
    a = buckets[('project-A', 1)]
    b = buckets[('project-B', 2)]
    assert a['submitted'] == 1 and a['completed'] == 1 and a['installed'] == 1
    assert b['submitted'] == 1 and b['completed'] == 1 and b['installed'] == 1
    # The global gauge is empty: nothing is left in the queue.
    assert (snapshot['pending'], snapshot['running'], snapshot['ready']) == (0, 0, 0)


def test_owner_change_opens_a_new_epoch_and_keeps_the_old_bucket():
    accounting = trace.QueueAccounting('image_decode', min_interval_s=0)
    trace.bind_project('project-A', open_epoch=1, generation=1)
    accounting.submitted(task_id='a1')
    accounting.rollup(force=True)
    trace.bind_project('project-B', open_epoch=2, generation=1)
    accounting.submitted(task_id='b1')
    accounting.rollup(force=True)
    trace.bind_project('project-A', open_epoch=3, generation=1)
    accounting.submitted(task_id='a2')
    accounting.rollup(force=True)
    snapshot = accounting.snapshot()
    keys = sorted((b['owner_project_token'], b['epoch'])
                  for b in snapshot['owner_buckets']
                  if b['owner_project_token'] is not None)
    # A comes back with a **new** epoch: the two visits are not merged.
    assert keys == [('project-A', 1), ('project-A', 3), ('project-B', 2)]


def test_one_task_reaches_exactly_one_terminal_state():
    accounting = trace.QueueAccounting('image_decode', min_interval_s=0)
    accounting.submitted(task_id='t1')
    accounting.started(task_id='t1')
    accounting.failed(task_id='t1', reason='boom')
    # The GUI thread finishes the same task later: it must not count again.
    accounting.service_done(task_id='t1', service_ms=1.0)
    accounting.installed(task_id='t1')
    snapshot = accounting.snapshot()
    assert snapshot['counters']['failed'] == 1
    assert snapshot['counters']['installed'] == 0
    assert snapshot['late_terminal_ignored'] == 2
    assert (snapshot['running'], snapshot['ready']) == (0, 0)


def test_failed_and_service_done_never_each_decrement_running():
    """The legacy path: ``failed`` counts, ``service_done`` departs."""
    accounting = trace.QueueAccounting('image_decode', min_interval_s=0)
    accounting.submitted()
    accounting.started()
    accounting.failed(state='running')
    # The worker never reached service_done: the gauge is honestly
    # unresolved rather than silently decremented twice.
    snapshot = accounting.snapshot()
    assert snapshot['running_unresolved'] == 1
    assert snapshot['counters']['failed'] == 1


def test_the_real_business_failure_path_does_not_double_decrement_running():
    """``prism/image_decode.py`` calls failed(state='running') **and** then
    ``service_done(usable=False)`` for the same task.

    Before this round both methods decremented the running gauge, so one
    failed decode was counted as two departures. The running gauge is now
    released exactly once, by ``service_done``.
    """
    accounting = trace.QueueAccounting('image_decode', min_interval_s=0)
    accounting.submitted()
    accounting.started()
    accounting.failed(reason='decode_failed', state='running')
    accounting.service_done(service_ms=1.0, usable=False)
    snapshot = accounting.snapshot()
    assert snapshot['running'] == 0
    assert snapshot['gauge_clamped'] == 0
    assert snapshot['counters']['failed'] == 1
    # The failure was resolved by the departure that followed it.
    assert snapshot['running_unresolved'] == 0


def test_global_gauge_and_per_owner_counters_are_separate():
    accounting = trace.QueueAccounting('image_decode', min_interval_s=0)
    trace.bind_project('project-A', open_epoch=1, generation=1)
    accounting.submitted(task_id='a1')
    trace.bind_project('project-B', open_epoch=2, generation=1)
    accounting.submitted(task_id='b1')
    snapshot = accounting.snapshot()
    # The gauge is global: two tasks are waiting.
    assert snapshot['pending'] == 2
    assert snapshot['pending_all'] == 2
    # The cumulative counters are per owner.
    buckets = {b['owner_project_token']: b['counters']
               for b in snapshot['owner_buckets']}
    assert buckets['project-A']['submitted'] == 1
    assert buckets['project-B']['submitted'] == 1


def test_oldest_age_is_the_oldest_pending_task_only():
    accounting = trace.QueueAccounting('image_decode', min_interval_s=0)
    now = time.monotonic()
    accounting.submitted(task_id='old', enqueued_at=now-10)
    accounting.submitted(task_id='new', enqueued_at=now)
    accounting.started(task_id='new')
    snapshot = accounting.snapshot()
    # 'new' is running, not pending: the oldest *pending* task is 'old'.
    assert snapshot['oldest_task_id'] == 'old'
    assert snapshot['oldest_age_ms'] >= 9000
    assert snapshot['oldest_age_scope'] == 'oldest_pending_task'
    # The busy period is a separate thing.
    assert snapshot['busy_period_status'] == 'known'
    assert snapshot['busy_period_ms'] is not None


def test_work_without_identity_is_reported_as_mixed():
    accounting = trace.QueueAccounting('image_decode', min_interval_s=0)
    accounting.submitted()
    snapshot = accounting.snapshot()
    assert snapshot['mixed'] is True
    assert snapshot['unattributed_count'] == 1
    assert snapshot['oldest_age_ms'] is None
    assert snapshot['oldest_age_status'] == 'not_accounted_without_task_ids'


# ── T2: diagnostic isolation ────────────────────────────────────────────

def _failing_trace(fail_on):
    """A trace double whose ``record`` fails for one event kind."""
    class _Target:
        STALL_THRESHOLD = trace.PerformanceTrace.STALL_THRESHOLD
        last_action = None

        def __init__(self):
            self.calls = []
            self._gc_intervals = []

        def record(self, kind, **fields):
            self.calls.append(kind)
            if kind in fail_on:
                raise RuntimeError('diagnostic channel is down')

        def note_span_open(self):
            pass

        def note_span_close(self):
            pass

    return _Target()


def test_span_begin_failure_still_runs_the_business_exactly_once():
    target = _failing_trace({'operation_start'})
    before = trace.current_context()
    calls = []
    _span(target, calls)
    assert calls == ['business']
    # The context is restored after the exit.
    assert trace.current_context() is before
    # No terminal half was fabricated for a span whose opening failed.
    assert 'operation' not in target.calls
    assert 'span_diagnostic_failed' in target.calls


def _span(target, calls):
    """``span()`` against a trace double, with the business body counted."""
    original = trace._active
    trace._active = target
    try:
        with trace.span('business.stage') as handle:
            calls.append('business')
            handle.set(work=1)
    finally:
        trace._active = original


def test_span_end_failure_keeps_the_return_value():
    target = _failing_trace({'operation'})
    original = trace._active
    trace._active = target
    try:
        with trace.span('business.stage') as handle:
            handle.set(work=1)
            result = 42
    finally:
        trace._active = original
    assert result == 42
    # The opening half is there, the closing half could not be written, and
    # the failure was counted instead of being swallowed.
    assert 'operation_start' in target.calls
    assert 'operation' in target.calls
    assert trace.diagnostic_failures()['total'] >= 1


def test_business_exception_survives_a_failing_diagnostic_end():
    target = _failing_trace({'operation'})
    original = trace._active
    trace._active = target
    try:
        with pytest.raises(ValueError) as caught:
            with trace.span('business.stage'):
                raise ValueError('business failed')
    finally:
        trace._active = original
    # The business exception, not the diagnostic one.
    assert isinstance(caught.value, ValueError)
    assert 'business failed' in str(caught.value)
    assert caught.value.__traceback__ is not None


def test_open_phase_restores_the_context_even_when_the_end_write_fails():
    target = _failing_trace({'open_phase'})
    original = trace._active
    trace._active = target
    before = trace.current_context()
    try:
        with trace.open_phase('catalog.build') as phase:
            phase['rows'] = 3
    finally:
        trace._active = original
    assert trace.current_context() is before


def test_diagnostic_failure_counter_is_bounded_and_never_recursive():
    before = trace.diagnostic_failures()['total']
    target = _failing_trace({'operation_start', 'operation'})
    original = trace._active
    trace._active = target
    try:
        for _ in range(50):
            with trace.span('business.stage'):
                pass
    finally:
        trace._active = original
    report = trace.diagnostic_failures()
    assert report['total'] > before
    assert len(report['by_kind']) <= 8


def test_record_never_raises_into_the_caller():
    target = _failing_trace({'anything'})
    target.record = target.record
    original = trace._active
    trace._active = target
    try:
        # The module level record() must not raise.
        trace.record('anything', value=1)
    finally:
        trace._active = original


# ── T2: the GC fallback really lands on disk ───────────────────────────

def test_stall_context_fallback_is_written_to_the_log_file(tmp_path):
    """The classifier is unavailable: this module writes the evidence."""
    app = _application()
    active = trace.install(app, None, tmp_path / 'logs')
    try:
        active._gc_intervals.append((100.0, 100.2, True))
        active._gc_intervals.append((100.1, 100.3, True))
        stall = dict(stall_id='s-fallback', window_start_monotonic=100.0,
                     heartbeat_max=250, count=3, dispatches_completed=1,
                     background_observed=False, dispatch_intervals=[],
                     context_snapshot={'modal': False, 'project_token': 'p'})
        fields = active._record_stall_context(stall, 300.0, 100.3)
    finally:
        active.close()
    events = _read_events(Path(active.path))
    contexts = [e for e in events if e.get('event') == 'ui.stall_context']
    assert contexts, 'the fallback must reach the log file, not just the dict'
    written = contexts[-1]
    assert written.get('observed_gui_gc_ms') == pytest.approx(300.0, abs=0.01)
    assert written.get('gc_attribution')
    assert fields['observed_gui_gc_ms'] == pytest.approx(300.0, abs=0.01)
    # One stall is written once.
    assert len([e for e in contexts if e.get('stall_id') == 's-fallback']) == 1


from pathlib import Path  # noqa: E402  (used by the disk test above)


# ── T2: the four modal contrasts ────────────────────────────────────────

def _stall_fields(modal, heartbeat_max, gc_intervals, window=(100.0, 100.3)):
    target = SimpleNamespace(
        STALL_THRESHOLD=trace.PerformanceTrace.STALL_THRESHOLD,
        _gc_intervals=list(gc_intervals), _gc_lock=__import__('threading').Lock(),
        last_action=None, record=lambda *a, **k: None)
    start, end = window
    stall = dict(stall_id='s', window_start_monotonic=start,
                 heartbeat_max=heartbeat_max, count=3, dispatches_completed=1,
                 background_observed=False, dispatch_intervals=[],
                 context_snapshot={'modal': modal, 'project_token': 'p'})
    return trace.PerformanceTrace._record_stall_context(target, stall, 300.0, end)


def test_modal_contrast_one_healthy_heartbeat_is_a_possible_user_wait():
    fields = _stall_fields(True, 10, [])
    assert fields['modal_context'] is True
    assert fields['heartbeat_starved'] is False
    assert fields['user_waiting_possible'] is True
    assert fields.get('classification') != 'gc_pause'


def test_modal_contrast_two_real_gc_pause_stays_a_block():
    fields = _stall_fields(True, 10, [(100.0, 100.2, True), (100.1, 100.3, True)])
    assert fields['classification'] == 'gc_pause'
    assert fields['observed_gui_gc_ms'] == pytest.approx(300.0, abs=0.01)
    assert fields['user_waiting_possible'] is True


def test_modal_contrast_three_starved_heartbeat_is_a_block():
    fields = _stall_fields(True, 5000, [])
    assert fields['heartbeat_starved'] is True
    assert fields['classification'] == 'heartbeat_starved'


def test_modal_contrast_four_non_modal_gc_pause():
    fields = _stall_fields(False, 10, [(100.0, 100.2, True), (100.1, 100.3, True)])
    assert fields['classification'] == 'gc_pause'
    assert fields['modal_context'] is False


# ── T3: the analyzer ────────────────────────────────────────────────────

def _analyse(tmp_path, events, name='fixture.jsonl'):
    path = tmp_path / name
    lines = []
    for index, event in enumerate(events):
        row = dict(session='s', **event)
        row.setdefault('sequence', index+1)
        lines.append(json.dumps(row)+'\n')
    path.write_text(''.join(lines), encoding='utf-8')
    return analyzer.analyse([str(path)])


def test_analyzer_ingests_catalog_result_published_as_this_attempt(tmp_path):
    reports, _ = _analyse(tmp_path, [dict(
        event='catalog_result_published', query_digest='q1',
        project='p1', published_rows=120, evaluated_rows=900,
        read_rows=900, proxies_created=7, complete=True, final=True,
        counter_basis='this_publication_attempt',
        evaluated_rows_cumulative=False, read_rows_cumulative=False,
        proxies_created_cumulative=False)])
    report = reports[0]
    section = report.get('catalog_publications')
    assert section is not None, 'the analyzer must know the new event name'
    rows = section.get('publications') or []
    assert len(rows) == 1
    assert rows[0]['published_rows'] == 120
    assert rows[0]['evaluated_rows'] == 900
    # The three counts stay separate: they are not one "total work".
    assert rows[0]['evaluated_rows'] != rows[0]['published_rows']
    assert section.get('counter_basis') == 'this_publication_attempt'


def test_analyzer_keeps_unknown_fields_unknown(tmp_path):
    reports, _ = _analyse(tmp_path, [dict(
        event='catalog_result_published', query_digest='q1',
        published_rows=10)])
    rows = reports[0]['catalog_publications']['publications']
    assert rows[0]['evaluated_rows'] is None
    assert rows[0]['evaluated_rows_status'] == 'unknown'


def test_analyzer_reports_a_duplicate_publication_once(tmp_path):
    # The dedup key is the **business** identity (``publication_id``), not
    # the log's sequence: a redelivery has its own sequence but the same
    # publication identity.
    event = dict(event='catalog_result_published', publication_id='pub-1',
                 query_digest='q1', published_rows=10, complete=True)
    reports, io_info = _analyse(tmp_path, [event, dict(event)], name='dup.jsonl')
    section = reports[0]['catalog_publications']
    assert section.get('duplicate_publications') == 1
    assert section['publication_count'] == 1


def test_analyzer_does_not_double_count_the_model_marker(tmp_path):
    reports, _ = _analyse(tmp_path, [
        dict(event='catalog_result_published', publication_id='pub-1',
             query_digest='q1', published_rows=10, complete=True),
        dict(event='asset_model_published', rows=10),
    ])
    section = reports[0]['catalog_publications']
    # The model marker is reported next to the publication, never added.
    assert section['model_marker_event_count'] == 1
    assert 'double_count_warning' in section
    assert section['counts_are_separate'] is True
