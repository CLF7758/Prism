"""Black-box CLI checks using small, externally specified trace scenarios."""
import hashlib
import json
from pathlib import Path
import subprocess
import sys

import pytest

from tools import analyze_prism_trace as analyzer


ROOT = Path(__file__).resolve().parents[1]
ARTIFACTS = ROOT / 'artifacts/logging-upgrade-20261023/closeout-schema2'


def scenario(session='v2'):
    events = []

    def emit(kind, at, **fields):
        event = dict(event=kind, session=session, sequence=len(events) + 1,
                     monotonic=at, time=1700000000 + at, schema_version=2,
                     pid=101, thread_id=1, thread_name='MainThread',
                     project_token='fixture', open_epoch=1, generation=1,
                     source_revision='fixture-r1', origin_action_id='open-click',
                     trace_id='open-trace', priority=3)
        event.update(fields)
        events.append(event)
        return event

    return events, emit


def run_cli(name, events, suffix=b''):
    ARTIFACTS.mkdir(parents=True, exist_ok=True)
    source = ARTIFACTS / (name + '.jsonl')
    source.write_bytes(b''.join((json.dumps(event) + '\n').encode()
                                for event in events) + suffix)
    before = hashlib.sha256(source.read_bytes()).hexdigest()
    result = subprocess.run(
        [sys.executable, str(ROOT / 'tools/analyze_prism_trace.py'), str(source),
         '--out-dir', str(ARTIFACTS), '--label', name],
        capture_output=True, timeout=20)
    (ARTIFACTS / (name + '.cli.log')).write_bytes(
        ('exit_code=%d\n' % result.returncode).encode() + result.stdout + result.stderr)
    assert result.returncode == 0, result.stderr.decode('utf-8', errors='replace')
    assert hashlib.sha256(source.read_bytes()).hexdigest() == before
    markdown = (ARTIFACTS / (name + '.md')).read_text(encoding='utf-8')
    assert 'Traceback' not in markdown
    return json.loads((ARTIFACTS / (name + '.json')).read_text(encoding='utf-8')), markdown


def test_cli_parent_parallel_and_queued_install_context():
    events, emit = scenario()
    emit('session_start', 0)
    emit('operation_start', 1, name='open', span_id='root', status='start')
    emit('operation_start', 1.1, name='worker-a', span_id='a', parent_span_id='root',
         status='start', thread_name='Worker-1')
    emit('operation_start', 1.2, name='worker-b', span_id='b', parent_span_id='root',
         status='start', thread_name='Worker-2')
    emit('operation', 1.6, name='worker-a', span_id='a', parent_span_id='root',
         status='end', duration_ms=500, thread_name='Worker-1')
    emit('operation', 1.8, name='worker-b', span_id='b', parent_span_id='root',
         status='error', duration_ms=600, thread_name='Worker-2')
    emit('operation_start', 1.85, name='queued-install', span_id='install',
         parent_span_id='a', status='start')
    emit('operation', 1.9, name='queued-install', span_id='install',
         parent_span_id='a', status='stale', duration_ms=50)
    emit('operation', 2, name='open', span_id='root', status='end', duration_ms=1000)
    emit('session_end', 2.1)
    payload, markdown = run_cli('parent-parallel-context', events)
    report = payload['reports'][0]
    chain = report['span_chain']
    attribution = chain['attribution'][0]
    assert attribution['interval_union_ms'] == 1000
    assert attribution['naive_sum_ms'] == 2150
    assert all(link['parent_found'] for link in chain['links'])
    assert {link['parent_span_id'] for link in chain['links']} == {'root', 'a'}
    assert chain['unattributed_spans'] == 0
    assert chain['terminal_status_counts'] == {'end': 2, 'error': 1, 'stale': 1}
    assert not report['completeness']['incomplete']
    assert '1000' in markdown


def test_cli_queue_resource_unknown_and_cancellation():
    events, emit = scenario()
    emit('session_start', 0)
    counters = dict(submitted=8, started=6, completed=5, installed=3,
                    cancelled=2, cancel_after_start=1, stale_discarded=1,
                    rejected=1, retried=1, coalesced=2, failed=1)
    emit('queue_rollup', 1, category='image_decode', counters=counters,
         pending=1, running=1, ready=2, limit=4, peak_pending=4,
         peak_running=2, wait_ms_total=100, service_ms_total=400,
         atomic_in_progress=1, cancelled_remaining_work=5, shared_tasks=2)
    emit('queue_rollup', 1.1, category='catalog', counters={})
    emit('resource_rollup', 1.2, classes={
        'cpu_decoded': dict(source='measured', current=512, peak=1024, evicted=256),
        'gpu': dict(source='unknown', current=None, reason='no-driver-query'),
        'temp_disk': dict(source='not_accounted', reason='not-instrumented')},
        pin_over_budget=128, budget_is_rss_cap=False)
    emit('session_end', 2)
    payload, markdown = run_cli('queue-resource-unknown', events)
    report = payload['reports'][0]
    queues = {row['category']: row for row in report['queue_rollups']['categories']}
    queue = queues['image_decode']
    assert queue['counters'] == counters
    assert queue['wait_vs_service']['wait_share_pct'] == 20
    assert queue['saturation']['atomic_in_progress'] == 1
    assert queue['saturation']['cancelled_remaining_work'] == 5
    assert queue['saturation']['shared_tasks'] == 2
    assert queues['catalog']['discarded_total'] is None
    resources = {row['class']: row for row in report['resource_rollups']['classes']}
    assert resources['cpu_decoded']['current'] == 512
    assert resources['gpu']['current'] is None
    assert resources['temp_disk']['current'] is None
    assert resources['dirty_records']['current'] is None
    assert report['resource_rollups']['shared_pin_over_budget'] == 128
    assert 'no-driver-query' in markdown
    assert 'atomic_in_progress=1' in markdown


def test_cli_ready_does_not_mean_painted_and_pending_clears_old_paint():
    events, emit = scenario()
    emit('session_start', 0)
    scope = dict(project_id='fixture', generation=1,
                 scope_pages_fingerprint='all', scope_mode='all')
    emit('assets.current_scope_interactive', 1, **scope)
    emit('assets.focus_quality_ready', 1.1, **scope, painted=False,
         quality_covered=10, quality_total=10)
    emit('assets.viewport_quality_unknown', 1.2, **scope, reason='unmeasured')
    emit('session_end', 2)
    payload, _ = run_cli('ready-not-painted', events)
    quality = payload['reports'][0]['presentation_markers']['scopes'][0]
    assert quality['focus_quality']['state'] == 'ready'
    assert not quality['focus_quality']['painted_marker_seen']
    assert quality['viewport_quality']['state'] == 'unknown'
    events.insert(-1, dict(events[-2], event='assets.focus_quality_painted',
                           sequence=6, monotonic=1.3))
    events.insert(-1, dict(events[-2], event='assets.focus_quality_pending',
                           sequence=7, monotonic=1.4, reason='new-zoom-target'))
    events[-1]['sequence'] = 8
    payload, _ = run_cli('new-pending-invalidates-paint', events)
    focus = payload['reports'][0]['presentation_markers']['scopes'][0]['focus_quality']
    assert focus['state'] == 'pending'
    assert not focus['painted_marker_seen']


@pytest.mark.parametrize('fields', [dict(trace_incomplete=True),
                                   dict(seq_gap_count=3),
                                   dict(dropped_by_kind={'operation': 2})])
def test_cli_integrity_flags_are_not_reported_complete(fields):
    events, emit = scenario()
    emit('session_start', 0)
    emit('trace_overflow', 1, **fields)
    emit('session_end', 2)
    payload, markdown = run_cli('integrity-' + next(iter(fields)), events)
    assert payload['reports'][0]['completeness']['incomplete']
    assert 'INCOMPLETE' in markdown


def test_cli_mixed_sessions_and_truncated_last_line():
    events, emit = scenario()
    emit('session_start', 0)
    emit('operation', 1, name='cancelled-work', status='cancelled',
         duration_ms=20, span_id='cancel')
    emit('session_end', 2)
    events.extend([dict(event='session_start', session='legacy', sequence=1,
                        monotonic=0),
                   dict(event='operation', session='legacy', sequence=2,
                        monotonic=1, duration_ms=30, name='legacy-work'),
                   dict(event='session_end', session='legacy', sequence=3,
                        monotonic=2)])
    payload, _ = run_cli('mixed-truncated', events, b'{"session":"v2",')
    reports = {report['session']: report for report in payload['reports']}
    assert set(reports) == {'v2', 'legacy'}
    assert reports['legacy']['schema_coverage']['is_legacy_log']
    assert not reports['legacy']['span_chain']['has_real_chain']
    assert reports['v2']['span_chain']['traces'][0]['interval_union_ms'] == 20
    assert reports['v2']['span_chain']['terminal_status_counts'] == {'cancelled': 1}
    assert reports['v2']['completeness']['incomplete']
    assert len(payload['io']['malformed']) == 1


def test_cli_missing_end_is_not_success_and_missing_parent_is_visible():
    events, emit = scenario()
    emit('session_start', 0)
    emit('operation_start', 1, name='never-finished', span_id='orphan', status='start')
    emit('operation', 1.2, name='child', span_id='child', parent_span_id='absent',
         status='error', duration_ms=50)
    emit('session_end', 2)
    payload, markdown = run_cli('orphan-and-parent-missing', events)
    report = payload['reports'][0]
    assert report['completeness']['incomplete']
    assert report['completeness']['unterminated_operation_starts'] == [2]
    assert report['span_chain']['parent_refs_missing'][0]['parent_span_id'] == 'absent'
    assert 'INCOMPLETE' in markdown


def test_cli_different_span_ids_cannot_pair_by_name():
    events, emit = scenario()
    emit('session_start', 0)
    emit('operation_start', 1, name='same-name', span_id='missing-end', status='start')
    emit('operation', 1.5, name='same-name', span_id='different-span',
         status='end', duration_ms=100)
    emit('session_end', 2)
    payload, _ = run_cli('different-span-identities', events)
    report = payload['reports'][0]
    assert report['completeness']['unterminated_operation_starts'] == [2]
    assert report['span_chain']['traces'][0]['interval_union_ms'] == 100


def test_cli_priority_lane_reordering_is_not_loss_or_old_state():
    events, emit = scenario()
    emit('session_start', 0)
    emit('queue_rollup', 1, category='catalog', counters={'submitted': 2}, pending=2)
    emit('queue_rollup', 2, category='catalog', counters={'submitted': 3}, pending=0)
    scope = dict(project_id='fixture', generation=1, scope_pages_fingerprint='all')
    emit('assets.focus_quality_pending', 3, **scope)
    emit('assets.focus_quality_ready', 4, **scope)
    emit('assets.focus_quality_painted', 5, **scope)
    emit('trace_stats', 6, seq_gap_count=0, trace_incomplete=False,
         seq_gap_basis='known_dropped_events', sequence_order_guaranteed=False)
    emit('session_end', 7)
    # Simulate two priority lanes delivering newer critical state first.
    events = [events[index] for index in (0, 7, 2, 5, 6, 1, 4, 3)]
    payload, _ = run_cli('priority-lane-reorder', events)
    report = payload['reports'][0]
    assert not report['completeness']['incomplete']
    assert report['completeness']['seq_gap_basis'] == 'known_dropped_events'
    assert report['completeness']['sequence_order_guaranteed'] is False
    assert report['queue_rollups']['categories'][0]['depth']['pending'] == 0
    quality = report['presentation_markers']['scopes'][0]['focus_quality']
    assert quality['state'] == 'ready'
    assert quality['painted_marker_seen']


def _write_events(path, events, newline=b'\n'):
    path.write_bytes(newline.join(
        json.dumps(event, ensure_ascii=False).encode('utf-8') for event in events
    ) + newline)


def test_directory_input_includes_only_numeric_rotations(tmp_path):
    for name in ('trace.jsonl', 'trace.jsonl.1', 'trace.jsonl.2',
                 'trace.jsonl.bak', 'trace.jsonl.tmp'):
        (tmp_path / name).write_text('{}\n', encoding='utf-8')
    resolved = {Path(value).name for value in analyzer.resolve_inputs([str(tmp_path)])}
    assert resolved == {'trace.jsonl', 'trace.jsonl.1', 'trace.jsonl.2'}


def test_dedup_conflicts_are_deterministic_and_precede_statistics(tmp_path):
    shared = dict(event='session_start', session='s', sequence=1, monotonic=0)
    left = tmp_path / 'left.jsonl'
    right = tmp_path / 'right.jsonl'
    _write_events(left, [shared,
                         dict(event='z-event', session='s', sequence=2,
                              monotonic=2)])
    _write_events(right, [shared,
                          dict(event='a-event', session='s', sequence=2,
                               monotonic=2)])
    reports_a, io_a = analyzer.analyse([str(left), str(right)])
    reports_b, io_b = analyzer.analyse([str(right), str(left)])
    assert io_a['physical_records'] == 4
    assert io_a['unique_events'] == 2
    assert io_a['identical_duplicates'] == 1
    assert io_a['conflict_keys'] == 1
    assert io_a['conflicts'] == io_b['conflicts']
    counts_a = reports_a[0]['session_scope']['event_counts']
    counts_b = reports_b[0]['session_scope']['event_counts']
    assert counts_a == counts_b
    assert sum(counts_a.values()) == 2


def test_phase_double_write_and_reverse_span_are_not_misclassified(tmp_path):
    events = [
        dict(event='operation', session='s', sequence=1, monotonic=1,
             schema_version=2, trace_id='t', span_id='x', name='work',
             role='end', status='end', duration_ms=10, thread_name='MainThread'),
        dict(event='operation_start', session='s', sequence=2, monotonic=2,
             schema_version=2, trace_id='t', span_id='x', name='work',
             role='start', status='start', thread_name='MainThread'),
        dict(event='paged_catalog_build_progress', session='s', sequence=3,
             monotonic=3, build_id='b', processed=10, processed_this_run=None,
             total_records=None, total_known=False, final=False),
        dict(event='paged_prepare_processed', session='s', sequence=4,
             monotonic=4, phase='catalog_build', legacy=True, build_id='b',
             processed=10, processed_this_run=None, total_records=None,
             total_known=False, final=False),
        dict(event='paged_prepare_processed', session='s', sequence=5,
             monotonic=5, processed=100),
    ]
    source = tmp_path / 'phase.jsonl'
    _write_events(source, events)
    reports, _io = analyzer.analyse([str(source)])
    report = reports[0]
    assert len(report['paged_progress']['catalog_build']) == 1
    assert len(report['paged_progress']['ambiguous']) == 1
    assert report['completeness']['anomalous_line_counts']['span_pair_rejected'] == 1
    assert report['completeness']['unterminated_operation_starts'] == [2]


def test_utf8_crlf_byte_accounting_and_stall_identity(tmp_path):
    events = [
        dict(event='gui_stall', session='s', sequence=1, monotonic=1,
             phase='ongoing', role='attachment', stall_id='stall-1',
             duration_ms=250, note='中文'),
        dict(event='gui_stall', session='s', sequence=2, monotonic=2,
             phase='ongoing', role='attachment', stall_id='stall-1',
             duration_ms=500),
        dict(event='gui_stall', session='s', sequence=3, monotonic=3,
             phase='end', role='end', stall_id='stall-1', duration_ms=700,
             classification='unclassified'),
    ]
    source = tmp_path / 'bytes.jsonl'
    _write_events(source, events, newline=b'\r\n')
    reports, io = analyzer.analyse([str(source)])
    accounting = io['byte_accounting']
    physical = accounting['physical']
    assert accounting['classification_version'] == 2
    assert physical['physical_bytes'] == source.stat().st_size
    assert physical['newline_bytes'] == len(events) * 2
    assert physical['event_line_bytes'] + physical['newline_bytes'] == source.stat().st_size
    assert physical['dimensions']['diagnostic_purpose']['stall_diagnosis'] == physical['event_line_bytes']
    assert physical['dimensions']['sampling_semantics']['event_driven'] == physical['event_line_bytes']
    assert accounting['value_judgement'] is None
    stalls = reports[0]['gui_stalls']
    assert stalls['complete_stalls'] == 1
    assert stalls['ongoing_repeats_ignored'] == 2
    assert stalls['unterminated_stalls_no_end'] == 0
    assert stalls['top_stalls'][0]['stall_id'] == 'stall-1'
    assert stalls['top_stalls'][0]['association_basis'] == 'session+stall_id'


def test_roleless_sql_attachment_does_not_close_enclosing_span(tmp_path):
    events, emit = scenario('s')
    emit('operation_start', 1, name='reader', span_id='r', status='start')
    emit('sql.execute', 1.05, query='fixture', span_id='r', duration_ms=20)
    emit('operation', 1.1, name='reader', span_id='r', status='end', duration_ms=100)
    source = tmp_path / 'attachment.jsonl'
    _write_events(source, events)
    reports, _io = analyzer.analyse([str(source)])
    report = reports[0]
    assert report['completeness']['unterminated_operation_starts'] == []
    assert 'span_identity_conflict' not in report['completeness']['anomalous_line_counts']
    assert report['span_chain']['traces'][0]['interval_union_ms'] == 100
    assert report['span_chain']['terminal_status_counts'] == {'end': 1}


def test_unclosed_stall_has_one_window_not_one_per_ongoing(tmp_path):
    events = [dict(event='gui_stall', session='s', sequence=index,
                   monotonic=index, phase='ongoing', role='attachment',
                   stall_id='one-window', duration_ms=index * 1000)
              for index in (1, 2, 3)]
    source = tmp_path / 'unclosed.jsonl'
    _write_events(source, events)
    reports, _io = analyzer.analyse([str(source)])
    stalls = reports[0]['gui_stalls']
    assert stalls['complete_stalls'] == 0
    assert stalls['unterminated_stalls_no_end'] == 1
    assert stalls['unterminated_detail'][0]['duration_ms_so_far'] == 3000


def test_same_thread_name_cannot_hide_cross_thread_span_completion(tmp_path):
    events, emit = scenario('s')
    emit('operation_start', 1, name='work', span_id='r', role='start',
         status='start', thread_id=10, thread_name='worker')
    emit('operation', 2, name='work', span_id='r', role='end',
         status='end', duration_ms=1000, thread_id=20, thread_name='worker')
    source = tmp_path / 'threads.jsonl'
    _write_events(source, events)
    reports, _io = analyzer.analyse([str(source)])
    assert reports[0]['completeness']['unterminated_operation_starts'] == [1]
    assert reports[0]['completeness']['anomalous_line_counts']['span_pair_rejected'] == 1


def test_legacy_overlapping_starts_do_not_crash_pairing(tmp_path):
    events = [dict(event=kind, session='legacy', sequence=index,
                   monotonic=index, name='work', thread='MainThread', **fields)
              for index, kind, fields in (
                  (1, 'operation_start', {}),
                  (2, 'operation_start', {}),
                  (3, 'operation', {'duration_ms': 1000}))]
    source = tmp_path / 'legacy-overlap.jsonl'
    _write_events(source, events)
    reports, _io = analyzer.analyse([str(source)])
    assert reports[0]['completeness']['unterminated_operation_starts'] == [1]


def test_matching_startup_archive_restores_identity_without_other_pid(tmp_path):
    source = tmp_path / 'Prism-operations-101.jsonl'
    archive = tmp_path / 'Prism-startup-101.jsonl'
    _write_events(source, [dict(event='performance', session='s', sequence=20,
                               time=20, monotonic=20, pid=101)])
    _write_events(archive, [dict(event='session_start', session='s', sequence=1,
                                time=1, monotonic=1, pid=101),
                            dict(event='session_start', session='old-pid-reuse',
                                 sequence=1, time=0, monotonic=0, pid=101)])
    _write_events(tmp_path / 'Prism-startup-102.jsonl',
                  [dict(event='session_start', session='other', sequence=1)])
    reports, info = analyzer.analyse([str(source)])
    assert len(reports) == 1 and reports[0]['session'] == 's'
    assert reports[0]['session_scope']['has_session_start']
    assert reports[0]['session_scope']['pid'] == 101
    assert not reports[0]['session_scope']['has_session_end']
    assert reports[0]['completeness']['lifecycle']['state'] == 'end_not_observed'
    assert reports[0]['completeness']['retention']['operation_sequence_range'] == [20, 20]
    assert info['excluded_other_session_archive_records'] == 1
    assert len(info['startup_archives']) == 1


def test_startup_duplicates_are_deduplicated_and_guarded(tmp_path):
    source = tmp_path / 'Prism-operations-101.jsonl'
    archive = tmp_path / 'Prism-startup-101.jsonl'
    row = dict(event='session_start', session='s', sequence=1, monotonic=1)
    _write_events(source, [row])
    _write_events(archive, [row])
    reports, info = analyzer.analyse([str(source)])
    assert reports[0]['session_scope']['total_events'] == 1
    assert info['identical_duplicates'] == 1
    with pytest.raises(SystemExit):
        analyzer.guard_outputs(analyzer.with_startup_archives([str(source)]),
                               [str(archive)])


def test_startup_join_does_not_pull_unselected_rotations(tmp_path):
    source = tmp_path / 'Prism-operations-101.jsonl'
    for name in (source.name, source.name + '.1', 'Prism-startup-101.jsonl'):
        (tmp_path / name).write_text('{}\n', encoding='utf-8')
    assert {Path(p).name for p in analyzer.with_startup_archives([str(source)])} == {
        source.name, 'Prism-startup-101.jsonl'}


def test_missing_half_is_not_identity_conflict_but_duplicate_start_is(tmp_path):
    events, emit = scenario('s')
    emit('session_start', 0)
    emit('operation_start', 1, name='work', span_id='r', role='start', status='start')
    emit('session_end', 3)
    source = tmp_path / 'missing.jsonl'
    _write_events(source, events)
    reports, _ = analyzer.analyse([str(source)])
    comp = reports[0]['completeness']
    assert comp['unterminated_operation_starts'] == [2]
    assert 'span_identity_conflict' not in comp['anomalous_line_counts']
    events.append(dict(events[1], sequence=4))
    _write_events(source, events)
    reports, _ = analyzer.analyse([str(source)])
    anomaly = next(item for item in reports[0]['completeness']['anomalous_line_samples']
                   if item['kind'] == 'span_identity_conflict')
    assert anomaly['sequence'] == 2 and anomaly['line'] == 2
    assert anomaly['file'] == str(source)


def test_parent_reference_is_trace_scoped_and_no_empty_warning(tmp_path):
    events, emit = scenario('s')
    emit('session_start', 0)
    emit('operation_start', 1, name='root', span_id='root', status='start')
    emit('operation_start', 2, name='child', span_id='child',
         parent_span_id='root', status='start')
    emit('operation', 3, name='child', span_id='child', parent_span_id='root',
         status='end', duration_ms=1000)
    emit('operation', 4, name='root', span_id='root', status='end', duration_ms=3000)
    emit('session_end', 5)
    source = tmp_path / 'parent.jsonl'
    _write_events(source, events)
    reports, _ = analyzer.analyse([str(source)])
    assert 'unknown_parent_span' not in reports[0]['completeness']['anomalous_line_counts']
    for entry in events[2:4]:
        entry['trace_id'] = 'different-trace'
    _write_events(source, events)
    reports, _ = analyzer.analyse([str(source)])
    assert reports[0]['completeness']['anomalous_line_counts']['unknown_parent_span'] == 1


def test_new_lifecycle_needs_actual_drain_after_end(tmp_path):
    events, emit = scenario('s')
    emit('session_start', 0)
    emit('session_end', 1, writer_lifecycle_version=1)
    source = tmp_path / 'end.jsonl'
    _write_events(source, events)
    reports, _ = analyzer.analyse([str(source)])
    assert reports[0]['completeness']['lifecycle']['state'] == 'shutdown_drain_unconfirmed'
    assert reports[0]['completeness']['incomplete']
    emit('trace_writer_drained', 2, writer_lifecycle_version=1, pending_unwritten=0)
    _write_events(source, events)
    reports, _ = analyzer.analyse([str(source)])
    assert reports[0]['completeness']['lifecycle']['state'] == 'shutdown_drained'
    assert not reports[0]['completeness']['incomplete']
    assert reports[0]['completeness']['lifecycle']['process_exit'] != 'normal_exit'
    emit('trace_writer_failed', 3, writer_errors=1, trace_incomplete=True)
    _write_events(source, events)
    reports, _ = analyzer.analyse([str(source)])
    assert reports[0]['completeness']['lifecycle']['state'] == 'writer_failed'
    assert reports[0]['completeness']['incomplete']


def test_default_zero_is_not_observed_drop_counter(tmp_path):
    events, emit = scenario('s')
    emit('session_start', 0)
    emit('session_end', 1)
    source = tmp_path / 'counter.jsonl'
    _write_events(source, events)
    reports, info = analyzer.analyse([str(source)])
    assert not reports[0]['completeness']['dropped_events_observed']
    assert '默认 0 不是实测' in analyzer.render_markdown(reports, info, None)
    events[-1]['dropped_events'] = 0
    _write_events(source, events)
    reports, _ = analyzer.analyse([str(source)])
    assert reports[0]['completeness']['dropped_events_observed']


def test_rotation_checkpoint_proves_only_pruned_segment_sequences(tmp_path):
    events, emit = scenario('s')
    emit('session_start', 0)
    emit('trace_rotation', 1, rotation_count=1, closed_segment_index=0,
         rotation_backup_count=2, closed_segment_sequence_ranges=[[10, 12], [14, 14]])
    emit('trace_rotation', 2, rotation_count=2, closed_segment_index=1,
         rotation_backup_count=2, closed_segment_sequence_ranges=[[15, 20]])
    emit('trace_rotation', 3, rotation_count=3, closed_segment_index=2,
         rotation_backup_count=2, closed_segment_sequence_ranges=[[21, 30]])
    emit('session_end', 4)
    source = tmp_path / 'rotation.jsonl'
    _write_events(source, events)
    reports, _ = analyzer.analyse([str(source)])
    retention = reports[0]['completeness']['retention']
    assert retention['verified_pruned_sequence_ranges'] == [[10, 12], [14, 14]]
    assert reports[0]['completeness']['incomplete']


def _memory_writer(monkeypatch, events, *, rotate=False, fail=False, idle=False):
    """Exercise the real writer, with every filesystem operation in memory.

    No QApplication, global log files, old-file deletion or real rollover.
    """
    import io
    import itertools
    import queue
    import threading
    from types import MethodType, SimpleNamespace
    from unittest.mock import MagicMock
    from prism import performance_trace as pt

    class Stream(io.StringIO):
        def close(self):
            pass

    class Handler:
        def __init__(self, *args, **kwargs):
            self.stream = Stream()
            self.maxBytes = 1 if rotate else 1000000
            self.segments = []
        def acquire(self):
            pass
        def release(self):
            pass
        def flush(self):
            if fail:
                raise OSError('injected write failure')
        def doRollover(self):
            self.segments.append(self.stream.getvalue())
            self.stream = Stream()
        def close(self):
            pass

    archive = Stream()
    path = MagicMock()
    path.parent.glob.return_value = []  # Never enter product retention deletion.
    path.parent.__truediv__.return_value.open.return_value = archive
    handler = Handler()
    monkeypatch.setattr(pt, 'RotatingFileHandler', lambda *a, **k: handler)
    stopped = threading.Event()
    if not idle:
        stopped.set()

    class IdleQueue(queue.Queue):
        def get(self, block=True, timeout=None):
            if timeout is not None:
                stopped.set()
                raise queue.Empty
            return super().get(block=block, timeout=timeout)

    trace = SimpleNamespace(path=path, stopped=stopped, events=queue.Queue(),
        important=IdleQueue() if idle else queue.Queue(), _overflow_pending=False,
        sequence=itertools.count(max((e['sequence'] for e in events), default=0)+1),
        session='memory', _startup_archive_errors=0, _last_written_sequence=None,
        MAX_EVENT_BYTES=65536, writer_errors=0, writer_reopens=0,
        writer_ms_total=0, writer_ms_max=0, events_written=0, bytes_written=0,
        _written_since_heartbeat=0, _bytes_since_heartbeat=0,
        dropped=0, seq_gaps=0, trace_incomplete=False, writer_failed=MagicMock())
    for event in events:
        trace.important.put(event)
    trace._writer_evidence = MethodType(pt.PerformanceTrace._writer_evidence, trace)
    trace._writer_progress = MethodType(pt.PerformanceTrace._writer_progress, trace)
    trace._archive_write = MethodType(pt.PerformanceTrace._archive_write, trace)
    trace._sequence_ranges = pt.PerformanceTrace._sequence_ranges
    pt.PerformanceTrace._write(trace)
    return trace, handler, [json.loads(line) for line in archive.getvalue().splitlines()]


def test_recovered_pruned_segment_is_not_still_called_missing(tmp_path):
    events, emit = scenario('s')
    emit('session_start', 0)
    for count in (1, 2, 3):
        emit('trace_rotation', count, rotation_count=count,
             closed_segment_index=count-1, rotation_backup_count=2,
             closed_segment_sequence_ranges=[[count, count]])
    emit('session_end', 4)
    source = tmp_path / 'recovered.jsonl'
    _write_events(source, events)
    reports, _ = analyzer.analyse([str(source)])
    comp = reports[0]['completeness']
    assert comp['retention']['verified_pruned_sequence_ranges'] == [[1, 1]]
    assert comp['retention']['verified_missing_sequence_ranges'] == []
    assert not comp['incomplete']


def test_writer_lifecycle_halves_share_fifo_priority():
    import queue
    from types import SimpleNamespace
    from prism import performance_trace as pt
    assert pt._priority_for('operation_start') == pt.PRIORITY_TERMINAL
    assert pt._priority_for('operation', pt.PRIORITY_TERMINAL) == pt.PRIORITY_TERMINAL
    assert pt._priority_for('performance') == pt.PRIORITY_SAMPLE
    trace = SimpleNamespace(events=queue.Queue(), important=queue.Queue(), queue_peak=0)
    start = {'event': 'operation_start', 'sequence': 1}
    end = {'event': 'operation', 'sequence': 2}
    pt.PerformanceTrace._enqueue(trace, start, pt._priority_for('operation_start'))
    pt.PerformanceTrace._enqueue(trace, end, pt.PRIORITY_TERMINAL)
    assert trace.important.get_nowait() == start
    assert trace.important.get_nowait() == end
    assert trace.events.empty()


def test_writer_drains_and_archives_actual_terminal(monkeypatch, tmp_path):
    events = [dict(event='session_start', session='memory', sequence=1),
              dict(event='session_end', session='memory', sequence=2,
                   writer_lifecycle_version=1)]
    trace, handler, archive = _memory_writer(monkeypatch, events)
    assert trace.writer_errors == 0
    assert archive[-1]['event'] == 'trace_writer_drained'
    assert archive[-1]['sequence'] > 2 and archive[-1]['pending_unwritten'] == 0
    assert json.loads(handler.stream.getvalue().splitlines()[-1]) == archive[-1]
    source = tmp_path / 'Prism-operations-101.jsonl'
    startup = tmp_path / 'Prism-startup-101.jsonl'
    source.write_text(handler.stream.getvalue(), encoding='utf-8')
    _write_events(startup, archive)
    reports, info = analyzer.analyse([str(source)])
    assert reports[0]['completeness']['lifecycle']['state'] == 'shutdown_drained'
    assert not reports[0]['completeness']['incomplete']
    assert info['identical_duplicates'] == 3


def test_writer_failure_is_sequenced_not_fake_drain(monkeypatch):
    events = [dict(event='session_start', session='memory', sequence=1)]
    trace, _handler, archive = _memory_writer(monkeypatch, events, fail=True)
    assert trace.writer_errors == 1 and trace.trace_incomplete
    assert archive[-1]['event'] == 'trace_writer_failed'
    assert archive[-1]['sequence'] > max(entry['sequence'] for entry in archive[:-1])
    assert archive[-1]['schema_version'] == 2
    assert not any(row['event'] == 'trace_writer_drained' for row in archive)


def test_writer_successful_rotations_have_exact_ranges(monkeypatch):
    events = [dict(event='session_start', session='memory', sequence=1),
              dict(event='performance', session='memory', sequence=2),
              dict(event='session_end', session='memory', sequence=3)]
    trace, _handler, archive = _memory_writer(monkeypatch, events, rotate=True)
    rotations = [row for row in archive if row['event'] == 'trace_rotation']
    assert trace.writer_errors == 0
    assert [row['rotation_count'] for row in rotations] == [1, 2, 3]
    assert [row['closed_segment_sequence_ranges'] for row in rotations] == [
        [[1, 1]], [[2, 2]], [[3, 3]]]


@pytest.mark.parametrize('pending', [False, None, -1, 1, '0'])
def test_invalid_drain_counter_never_proves_shutdown(tmp_path, pending):
    events, emit = scenario('s')
    emit('session_start', 0)
    emit('session_end', 1, writer_lifecycle_version=1)
    emit('trace_writer_drained', 2, pending_unwritten=pending)
    source = tmp_path / 'invalid-drain.jsonl'
    _write_events(source, events)
    reports, _ = analyzer.analyse([str(source)])
    assert reports[0]['completeness']['lifecycle']['state'] == 'shutdown_drain_unconfirmed'
    assert reports[0]['completeness']['incomplete']


def test_writer_checkpoints_observe_progress_without_gui(monkeypatch):
    import itertools
    from prism import performance_trace as pt
    monkeypatch.setattr(pt.time, 'monotonic', itertools.count(0, 6).__next__)
    events = [dict(event='session_start', session='memory', sequence=1),
              dict(event='performance', session='memory', sequence=2)]
    _trace, _handler, archive = _memory_writer(monkeypatch, events)
    checkpoints = [row for row in archive if row['event'] == 'trace_writer_checkpoint']
    assert checkpoints
    assert checkpoints[-1]['events_written'] == 2
    assert checkpoints[-1]['writer_errors'] == 0
    assert checkpoints[-1]['pending_unwritten'] == 0


def test_idle_writer_still_emits_health_checkpoint(monkeypatch):
    import itertools
    from prism import performance_trace as pt
    monkeypatch.setattr(pt.time, 'monotonic', itertools.count(0, 6).__next__)
    _trace, _handler, archive = _memory_writer(monkeypatch, [], idle=True)
    checkpoints = [row for row in archive if row['event'] == 'trace_writer_checkpoint']
    assert len(checkpoints) == 1
    assert checkpoints[0]['events_written'] == 0
    assert checkpoints[0]['writer_errors'] == 0
    assert checkpoints[0]['pending_unwritten'] == 0


def test_archive_failure_is_counted_not_silenced():
    from types import SimpleNamespace
    from unittest.mock import MagicMock
    from prism import performance_trace as pt
    stream = MagicMock()
    stream.write.side_effect = OSError('archive full')
    trace = SimpleNamespace(_startup_archive_errors=0, trace_incomplete=False)
    assert pt.PerformanceTrace._archive_write(trace, stream, {'event': 'fixture'}) is None
    assert trace._startup_archive_errors == 1 and trace.trace_incomplete
    stream.close.assert_called_once()


def test_significant_failures_and_stalls_survive_trace_rotation(monkeypatch):
    events = [dict(event='session_start', session='memory', sequence=1),
              dict(event='gui_stall', session='memory', sequence=2, phase='end', duration_ms=220),
              dict(event='gc', session='memory', sequence=3, duration_ms=120),
              dict(event='project_save_failed', session='memory', sequence=4, error='fixture'),
              dict(event='performance', session='memory', sequence=5),
              dict(event='crash', session='memory', sequence=6, error='missing signal')]
    trace, handler, archive = _memory_writer(monkeypatch, events, rotate=True)
    by_kind = {entry['event']: entry for entry in archive}
    assert by_kind['gui_stall']['duration_ms'] == 220
    assert by_kind['gc']['sequence'] == 3
    assert by_kind['project_save_failed']['error'] == 'fixture'
    assert by_kind['crash']['error'] == 'missing signal'
    assert 'performance' not in by_kind
    assert by_kind['trace_diagnostic_archive_summary']['complete']
    assert handler.segments


def test_diagnostic_archive_budget_is_explicit_and_still_keeps_crash(monkeypatch):
    from prism import performance_trace as pt
    monkeypatch.setattr(pt, 'DIAGNOSTIC_ARCHIVE_MAX_BYTES', 1)
    events = [dict(event='gc', session='memory', sequence=1, duration_ms=120),
              dict(event='crash', session='memory', sequence=2, error='fixture')]
    _trace, _handler, archive = _memory_writer(monkeypatch, events)
    summary = next(entry for entry in archive if entry['event']=='trace_diagnostic_archive_summary')
    assert summary['skipped_by_kind'] == {'gc': 1}
    assert not summary['complete']
    assert any(entry['event']=='crash' for entry in archive)


def test_log_cleanup_keeps_live_process_families_and_unrelated_files(tmp_path, monkeypatch):
    import os
    from prism import performance_trace as pt
    monkeypatch.setattr(pt, '_log_process_running', lambda pid: pid == 101)
    for pid in range(101, 107):
        for name in (f'Prism-operations-{pid}.jsonl', f'Prism-startup-{pid}.jsonl',
                     f'Prism-crash-{pid}.json', f'Prism-operations-{pid}.jsonl.1'):
            path = tmp_path/name
            path.write_bytes(b'fixture')
            os.utime(path, (pid, pid))
    unrelated = tmp_path/'Prism-operations-102.jsonl.keep'
    unrelated.write_bytes(b'user file')
    result = pt._prune_trace_logs(tmp_path, current_pid=106, now=107)
    assert result['removed_files'] == 8  # only inactive 102/103 families
    assert result['protected_processes'] == 1
    assert (tmp_path/'Prism-crash-101.json').exists()
    assert (tmp_path/'Prism-startup-106.jsonl').exists()
    assert unrelated.read_bytes() == b'user file'


def test_log_cleanup_expires_old_runs_and_enforces_bytes(tmp_path, monkeypatch):
    import os
    from prism import performance_trace as pt
    monkeypatch.setattr(pt, '_log_process_running', lambda pid: False)
    monkeypatch.setattr(pt, 'LOG_RETENTION_MAX_BYTES', 15)
    for pid in (101, 102):
        path = tmp_path/f'Prism-operations-{pid}.jsonl'
        path.write_bytes(b'x'*10)
        os.utime(path, (pid, pid))
    result = pt._prune_trace_logs(tmp_path, current_pid=999, now=103)
    assert result['removed_files'] == 1
    assert not result['over_budget']
    result = pt._prune_trace_logs(tmp_path, current_pid=999, now=103+pt.LOG_RETENTION_AGE_S)
    assert result['removed_files'] == 1


def test_idle_writer_runs_periodic_cleanup_without_gui_timer(monkeypatch):
    import itertools
    from prism import performance_trace as pt
    calls = []
    monkeypatch.setattr(pt.time, 'monotonic', itertools.count(0, pt.LOG_CLEANUP_INTERVAL_S+1).__next__)
    def clean(folder):
        calls.append(folder)
        return dict(removed_files=0)
    monkeypatch.setattr(pt, '_prune_trace_logs', clean)
    _trace, _handler, archive = _memory_writer(monkeypatch, [], idle=True)
    assert len(calls) >= 2  # startup plus idle-period tick
    assert sum(entry['event']=='trace_log_cleanup' for entry in archive) == len(calls)


class _SpanSink:
    def __init__(self, reject_begin=False):
        self.last_action = None
        self.rows = []
        self.opened = 0
        self.reject_begin = reject_begin
    def record(self, kind, **fields):
        if kind == 'operation_start' and self.reject_begin:
            return False
        self.rows.append(dict(event=kind, **fields))
        return True
    def note_span_open(self):
        self.opened += 1
    def note_span_close(self):
        self.opened -= 1


def test_rejected_span_begin_does_not_get_a_fake_end(monkeypatch):
    from prism import performance_trace as pt
    sink = _SpanSink(reject_begin=True)
    monkeypatch.setattr(pt, '_active', sink)
    with pt.span('rejected') as handle:
        pass
    assert handle.begin_failed
    assert sink.opened == 0
    assert not any(row['event'] == 'operation' for row in sink.rows)


def test_span_end_stays_with_its_originating_recorder(monkeypatch):
    from prism import performance_trace as pt
    first, second = _SpanSink(), _SpanSink()
    monkeypatch.setattr(pt, '_active', first)
    with pt.span('cross-recorder'):
        monkeypatch.setattr(pt, '_active', second)
    assert second.rows == []
    assert first.opened == 0
    assert [row['event'] for row in first.rows] == ['operation_start', 'operation']


def _real_record_sink():
    import collections
    import itertools
    import queue
    import threading
    from types import MethodType, SimpleNamespace
    from prism import performance_trace as pt
    sink = SimpleNamespace(stopped=threading.Event(), sequence=itertools.count(1),
        session='record-test', recent=collections.deque(maxlen=128),
        important=queue.Queue(maxsize=128), events=queue.Queue(maxsize=384),
        queue_peak=0, self_ms_total=0, self_ms_max=0,
        _record_gate=threading.Lock(), _loss_lock=threading.Lock(),
        dropped=0, dropped_by_kind=collections.Counter(), seq_gaps=0,
        trace_incomplete=False, _overflow_pending=False)
    sink._enqueue = MethodType(pt.PerformanceTrace._enqueue, sink)
    sink._note_drop = MethodType(pt.PerformanceTrace._note_drop, sink)
    sink._record_body = MethodType(pt.PerformanceTrace._record_body, sink)
    sink.record = MethodType(pt.PerformanceTrace.record, sink)
    return sink


def test_recorder_reports_acceptance_and_rejection():
    from prism import performance_trace as pt
    sink = _real_record_sink()
    assert sink.record('operation_start', name='accepted') is True
    for i in range(127):
        sink.record('session_start')
    assert sink.record('operation_start', name='rejected') is False
    assert sink.dropped == 1
    sink.stopped.set()
    before = sink.events.qsize()
    assert sink._enqueue({'event': 'performance'}, pt.PRIORITY_SAMPLE) is False
    assert sink.events.qsize() == before


def test_payload_field_limit_keeps_lifecycle_contract(monkeypatch):
    from prism import performance_trace as pt
    sink = _real_record_sink()
    monkeypatch.setattr(pt, '_active', sink)
    with pt.span('many-fields', **{'payload_%d' % i: i for i in range(150)}):
        pass
    rows = [row for row in sink.recent if row['event'] == 'operation']
    assert len(rows) == 1
    assert rows[0]['name'] == 'many-fields'
    assert rows[0]['role'] == 'end' and rows[0]['status'] == 'end'
    assert isinstance(rows[0]['duration_ms'], (int, float))


def test_turning_off_does_not_drop_already_open_span_end(monkeypatch):
    from prism import performance_trace as pt
    sink = _real_record_sink()
    monkeypatch.setattr(pt, '_active', sink)
    before_mode = pt.mode()
    try:
        with pt.span('mode-switch'):
            pt.set_mode('off')
    finally:
        pt.set_mode(before_mode)
    assert [row['event'] for row in sink.recent if row['event'] in
            ('operation_start', 'operation')] == ['operation_start', 'operation']


def test_oversized_writer_event_keeps_pairing_and_duration(monkeypatch):
    identity = dict(session='memory', trace_id='t', span_id='s',
                    thread_name='MainThread', schema_version=2, name='large', stage='large')
    events = [dict(identity, event='operation_start', sequence=1, monotonic=1,
                   role='start', status='start'),
              dict(identity, event='operation', sequence=2, monotonic=2,
                   role='end', status='end', duration_ms=1000, detail='汉'*40000)]
    _trace, handler, _archive = _memory_writer(monkeypatch, events)
    rows = [json.loads(line) for line in handler.stream.getvalue().splitlines()]
    end = next(row for row in rows if row['event'] == 'operation')
    assert end['truncated']
    assert end['name'] == 'large' and end['stage'] == 'large'
    assert end['role'] == 'end' and end['status'] == 'end'
    assert end['duration_ms'] == 1000 and end['thread_name'] == 'MainThread'


def test_explicit_priority_and_mutable_phase_fields_do_not_drop_end(monkeypatch):
    from prism import performance_trace as pt
    sink = _SpanSink()
    monkeypatch.setattr(pt, '_active', sink)
    with pt.span('priority', priority='sample'):
        pass
    with pt.open_phase('load', priority='sample') as fields:
        fields.update(status='payload-value', failed=True, phase='payload-value',
                      priority='sample', span_id='fake-payload-identity')
    ends = [row for row in sink.rows if row.get('role') == 'end']
    assert len(ends) == 2
    assert ends[0]['priority'] == pt.PRIORITY_TERMINAL
    assert ends[1]['phase'] == 'end' and ends[1]['status'] == 'end'
    assert ends[1]['failed'] is False and ends[1]['span_id'] != 'fake-payload-identity'


def test_bad_diagnostic_boolean_does_not_override_business(monkeypatch):
    from prism import performance_trace as pt
    sink = _SpanSink()
    monkeypatch.setattr(pt, '_active', sink)
    class Poison:
        def __bool__(self):
            raise AssertionError('diagnostics invoked arbitrary bool')
    with pt.span('normal-exit') as handle:
        handle.set(failed=Poison())
    expected = ValueError('original business exception')
    with pytest.raises(ValueError) as caught:
        with pt.span('error-exit', status=Poison()) as handle:
            handle.set(failed=Poison())
            raise expected
    assert caught.value is expected
    assert sink.opened == 0
    assert len([r for r in sink.rows if r.get('role') == 'end']) == 2
    assert sink.rows[-1]['status'] == 'error' and sink.rows[-1]['failed'] is True


def test_rejected_or_raising_phase_begin_never_fabricates_end(monkeypatch):
    from prism import performance_trace as pt
    class Reject(_SpanSink):
        def record(self, kind, **fields):
            if kind == 'open_phase' and fields['phase'] == 'start':
                return False
            return super().record(kind, **fields)
    sink = Reject()
    monkeypatch.setattr(pt, '_active', sink)
    business = []
    with pt.open_phase('rejected'):
        business.append('once')
    assert business == ['once'] and sink.rows == []
    class Raise(Reject):
        def record(self, kind, **fields):
            if kind == 'open_phase' and fields['phase'] == 'start':
                raise OSError('injected opening error')
            return super().record(kind, **fields)
    sink = Raise()
    monkeypatch.setattr(pt, '_active', sink)
    with pt.open_phase('raising'):
        business.append('once')
    assert business == ['once', 'once'] and sink.rows == []


def test_close_during_payload_formatting_cannot_enqueue_after_stop():
    from prism import performance_trace as pt
    sink = _real_record_sink()
    class StopsDuringFormat:
        def __str__(self):
            with sink._record_gate:
                sink.stopped.set()
            return 'formatted-after-close'
    before = sink.events.qsize()
    assert sink.record('performance', detail=StopsDuringFormat()) is False
    assert sink.events.qsize() == before


def test_only_explicit_end_is_incomplete_not_a_standalone_observation(tmp_path):
    events, emit = scenario('s')
    emit('session_start', 0)
    emit('operation', 1, name='missing-start', span_id='s', role='end',
         status='end', duration_ms=10)
    emit('session_end', 2, writer_lifecycle_version=1)
    emit('trace_writer_drained', 3, pending_unwritten=0)
    source = tmp_path / 'orphan-end.jsonl'
    _write_events(source, events)
    reports, _ = analyzer.analyse([str(source)])
    comp = reports[0]['completeness']
    assert comp['incomplete'] and comp['unpaired_explicit_operation_ends'] == [2]


def test_hotpath_snapshots_do_not_expire_mode_under_state_lock(monkeypatch):
    from prism import performance_trace as pt
    state = pt._HotpathRollup('snapshot', 10)
    state.add(1, False)
    monkeypatch.setattr(pt, 'mode', lambda: (_ for _ in ()).throw(
        AssertionError('snapshot must not recursively expire/flush mode')))
    with state.lock:
        payload = pt._hotpath_payload(state, reset_reason='test')
    assert payload['calls'] == 1


def test_hotpath_recording_holds_neither_registry_nor_state_lock(monkeypatch):
    from prism import performance_trace as pt
    state = pt._HotpathRollup('lock-check', 10)
    state.add(1, False)
    monkeypatch.setattr(pt, '_HOTPATH_ROLLUPS', {'lock-check': state})
    held = []
    class Sink:
        def record(self, kind, **fields):
            held.append((pt._HOTPATH_LOCK._is_owned(), state.lock._is_owned()))
    pt.flush_hotpath_rollups(trace=Sink())
    assert held == [(False, False)]
    assert state.calls == 0


def test_hotpath_windows_are_deltas_not_cumulative(monkeypatch):
    from prism import performance_trace as pt
    clock = iter((10, 10.001, 13, 13.001))
    monkeypatch.setattr(pt.time, 'perf_counter', lambda: next(clock))
    monkeypatch.setattr(pt, '_HOTPATH_ROLLUPS', {})
    monkeypatch.setattr(pt, '_mode', pt.MODE_NORMAL)
    sink = _SpanSink()
    monkeypatch.setattr(pt, '_active', sink)
    function = pt.measured_hotpath('window-test', minimum_ms=100000)(lambda: None)
    with pt.restore_context(pt.TraceContext()):
        function()
        function()
    rows = [r for r in sink.rows if r['event'] == 'hotpath_rollup']
    assert [r['calls'] for r in rows] == [1, 2]
    assert [r['window_calls'] for r in rows] == [1, 1]


def test_string_subclass_status_cannot_mask_business_exception(monkeypatch):
    from prism import performance_trace as pt
    sink = _SpanSink()
    monkeypatch.setattr(pt, '_active', sink)
    class PoisonString(str):
        def __bool__(self):
            raise AssertionError('status bool hook must never run')
    original = ValueError('business')
    with pytest.raises(ValueError) as caught:
        with pt.span('poison-string', status=PoisonString('end')):
            raise original
    assert caught.value is original
    assert sink.opened == 0
    assert sink.rows[-1]['status'] == 'error'


def test_complete_stall_recovery_is_not_an_orphan_operation_end(tmp_path):
    events, emit = scenario('s')
    emit('session_start', 0)
    emit('gui_stall', 1, stall_id='stall', phase='ongoing', role='attachment',
         duration_ms=100)
    emit('gui_stall', 2, stall_id='stall', phase='end', role='end', duration_ms=150)
    emit('session_end', 3, writer_lifecycle_version=1)
    emit('trace_writer_drained', 4, pending_unwritten=0)
    source = tmp_path / 'stall-complete.jsonl'
    _write_events(source, events)
    reports, _ = analyzer.analyse([str(source)])
    assert not reports[0]['completeness']['incomplete']
    assert reports[0]['completeness']['unpaired_explicit_operation_ends'] == []
    assert reports[0]['gui_stalls']['complete_stalls'] == 1
