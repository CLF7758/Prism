"""The analyzer must not claim more than the log says.

Three notes in the queue report were written for one case and applied to all
of them:

* a ``cancel_after_start`` counter of zero still said "this work had already
  started, the cancel cannot stop it",
* a queue that reached its limit once was called "saturated",
* a category that never appeared was called "no such queue".

None of those is a fact the log supports.  These tests pin the wording to the
count it describes, so a reader cannot be sent after a problem that the
evidence does not name.
"""
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace

ANALYZER = Path(__file__).resolve().parents[1] / 'tools' / 'analyze_prism_trace.py'


def test_application_errors_are_reported_without_counting_empty_debug_fields(tmp_path):
    analyzer = _analyzer()
    path = tmp_path/'operations.jsonl'
    rows = [dict(session='s', sequence=i, event=event, error=error, time=i)
        for i,event,error in [(1,'model_composition_failed','duplicate event argument'),
            (2,'model_composition_failed','duplicate event argument'),
            (3,'project_catalog_build_failed','Source changed during catalog build'),
            (4,'debug',''), (5,'debug',[])]]
    rows.append(dict(session='s',sequence=6,event='model_failed',error_message='SSS RGB rejected',time=6))
    path.write_text('\n'.join(json.dumps(row) for row in rows)+'\n', encoding='utf-8')
    reports, info = analyzer.analyse([str(path)])
    errors = reports[0]['application_errors']
    assert [(row['event'],row['count']) for row in errors['groups']] == [
        ('model_composition_failed',2), ('project_catalog_build_failed',1), ('model_failed',1)]
    assert errors['groups'][0]['first_sequence'] == 1
    assert errors['groups'][0]['last_sequence'] == 2
    assert 'duplicate event argument' in analyzer.render_markdown(reports, info, None)


def _analyzer():
    spec = importlib.util.spec_from_file_location('analyze_prism_trace', ANALYZER)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_cleanup_results_and_archive_budget_are_visible_in_report(tmp_path):
    analyzer = _analyzer()
    path = tmp_path/'operations.jsonl'
    rows = [dict(event='trace_log_cleanup', removed_files=5, failed_files=0, protected_processes=1),
            dict(event='capture.inbox.cleanup', removed_files=3, protected_files=2, over_budget=False),
            dict(event='trace_diagnostic_archive_summary', budget_bytes=100,
                 retained_bytes=99, skipped_by_kind={'gc': 1}, complete=False)]
    for i, row in enumerate(rows):
        row.update(session='s', sequence=i+1, time=i)
    path.write_text('\n'.join(json.dumps(row) for row in rows)+'\n', encoding='utf-8')
    reports, info = analyzer.analyse([str(path)])
    assert len(reports[0]['maintenance']['events']) == 2
    assert reports[0]['diagnostic_archive']['summaries'][0]['entry']['skipped_by_kind'] == {'gc': 1}
    markdown = analyzer.render_markdown(reports, info, None)
    assert 'removed=5' in markdown and 'protected=2' in markdown


def test_crash_sidecar_matches_session_and_does_not_invent_trace_events(tmp_path):
    analyzer = _analyzer()
    path = tmp_path/'Prism-operations-123.jsonl'
    path.write_text(json.dumps(dict(session='current', sequence=1,
        event='session_end', time=1))+'\n', encoding='utf-8')
    sidecar = tmp_path/'Prism-crash-123.json'
    sidecar.write_text(json.dumps(dict(session='old', error='old PID crash')), encoding='utf-8')
    reports, info = analyzer.analyse([str(path)])
    assert reports[0]['crash_reports'] == []
    sidecar.write_text(json.dumps(dict(session='current', error='missing zoom_changed')), encoding='utf-8')
    reports, info = analyzer.analyse([str(path)])
    assert reports[0]['crash_reports'][0]['error'] == 'missing zoom_changed'
    assert reports[0]['session_scope']['total_events'] == 1
    assert reports[0]['application_errors']['groups'] == []
    assert 'missing zoom_changed' in analyzer.render_markdown(reports, info, None)


def test_directory_paging_and_recovery_are_not_reported_as_persistent_failure(tmp_path):
    analyzer = _analyzer()
    rows = [dict(event='catalog_browser_rows_read', rows=80, pages=1),
            dict(event='catalog_scope_miss', reason='not_ready', partial=True),
            dict(event='catalog_scope_miss', reason='not_ready', partial=True),
            dict(event='catalog_scope_hit', partial=True)]
    path = tmp_path / 'operations.jsonl'
    path.write_text('\n'.join(json.dumps(dict(row, session='s', sequence=i,
        monotonic=i, time=i)) for i, row in enumerate(rows, 1)), encoding='utf-8')
    reports, info = analyzer.analyse([str(path)])
    findings = {item['id']: item for item in reports[0]['findings']}
    assert 'catalog_browser_full_scan' not in findings
    assert findings['catalog_browser_read_batches']['rows_total'] == 80
    result = findings['catalog_scope_miss_not_ready']
    assert result['count'] == 2
    assert result['episodes'] == [dict(first_seq=2, count=2, recovered=True,
                                     recovery_seq=4, duration_ms=2000)]
    assert '持续未就绪' not in analyzer.render_markdown(reports, info, None)


def _session(**counters):
    base = {'submitted': 10, 'started': 10, 'completed': 10, 'installed': 10,
            'cancelled': 0, 'cancel_after_start': 0, 'stale_discarded': 0,
            'rejected': 0, 'retried': 0, 'coalesced': 0, 'failed': 0}
    base.update(counters)
    return SimpleNamespace(queue_rollups=[{
        'category': 'image_decode', 'seq': 7,
        'evidence': {'file': 'Prism-operations.jsonl', 'line': 7, 'sequence': 7},
        'counters': base,
        'limit': 8, 'peak_pending': 8, 'pending': 0, 'running': 0, 'ready': 0,
    }])


def test_the_cancel_note_matches_the_counter():
    analyzer = _analyzer()
    none_started = analyzer._cancel_after_start_note(0)
    assert '已经开始跑' not in none_started, \
        'a zero counter must not claim that work had already started'
    assert '没有' in none_started
    some_started = analyzer._cancel_after_start_note(3)
    assert '已经开始跑' in some_started
    not_recorded = analyzer._cancel_after_start_note(None)
    assert '无法判断' in not_recorded
    assert '已经开始跑' not in not_recorded


def test_a_queue_that_reached_its_limit_once_is_not_called_saturated():
    analyzer = _analyzer()
    report = analyzer.queue_rollups_report(_session())
    verdict = report['categories'][0]['saturation']['verdict']
    assert '饱和证据成立' not in verdict, \
        'reaching the limit once does not prove sustained saturation'
    assert '曾触顶' in verdict
    assert '持续饱和' in verdict, 'the verdict has to say what it cannot prove'


def test_a_category_that_never_appeared_is_only_not_observed():
    analyzer = _analyzer()
    report = analyzer.queue_rollups_report(_session())
    assert report['missing_categories'], 'the fixture has to miss categories'
    assert '没有这类队列' not in report['policy'], \
        'a missing category is not proof that the queue does not exist'
    assert '没有观测到' in report['policy']
    assert report['missing_categories_basis'] == 'not_observed_in_this_session'
