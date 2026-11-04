"""Queue registration identity and legacy pending-failure regressions."""
import pytest

from prism import performance_trace as trace


def _buckets(accounting):
    return {(b['owner_project_token'], b['owner_open_epoch'], b['epoch']): b
            for b in accounting.snapshot()['owner_buckets']}


@pytest.mark.parametrize('explicit_epoch', [None, 77])
def test_explicit_owner_submission_and_completion_share_bucket(
        monkeypatch, explicit_epoch):
    current = ('B', 2, 5)
    frozen = ('A', 1, 3)
    monkeypatch.setattr(trace, '_recording_owner', lambda: current)
    accounting = trace.QueueAccounting('image_decode')
    accounting.submitted(task_id='current')
    accounting.submitted(task_id='frozen', owner=frozen, epoch=explicit_epoch)
    accounting.started(task_id='frozen')
    accounting.service_done(task_id='frozen', usable=True)
    accounting.installed(task_id='frozen')
    buckets = _buckets(accounting)
    epoch = 1 if explicit_epoch is None else explicit_epoch
    frozen_bucket = buckets[('A', 1, epoch)]
    assert frozen_bucket['owner_generation'] == 3
    assert {name: frozen_bucket['counters'][name]
            for name in ('submitted', 'started', 'completed', 'installed')} == {
                'submitted': 1, 'started': 1, 'completed': 1, 'installed': 1}
    assert buckets[('B', 2, 1)]['counters']['submitted'] == 1
    assert buckets[('B', 2, 1)]['counters']['installed'] == 0
    assert accounting.snapshot()['pending'] == 1


def test_explicit_epoch_without_owner_is_used_for_all_task_transitions(monkeypatch):
    monkeypatch.setattr(trace, '_recording_owner', lambda: ('A', 1, 3))
    accounting = trace.QueueAccounting('image_decode')
    accounting.submitted(task_id='failure', epoch=42)
    accounting.started(task_id='failure')
    accounting.failed(task_id='failure')
    accounting.service_done(task_id='failure', usable=False)
    accounting.installed(task_id='failure')
    counters = _buckets(accounting)[('A', 1, 42)]['counters']
    assert counters['submitted'] == counters['started'] == counters['failed'] == 1
    assert counters['completed'] == counters['installed'] == 0
    snapshot = accounting.snapshot()
    assert snapshot['late_terminal_ignored'] == 2
    assert (snapshot['pending'], snapshot['running'], snapshot['ready']) == (0, 0, 0)


def test_legacy_pending_failure_releases_only_queued_work():
    accounting = trace.QueueAccounting('material_batch')
    accounting.submitted(n=3)
    accounting.started()
    accounting.failed(n=2, state='pending', reason='queued_error')
    snapshot = accounting.snapshot()
    assert (snapshot['pending'], snapshot['running'], snapshot['ready']) == (0, 1, 0)
    assert snapshot['counters']['failed'] == 2
    assert snapshot['gauge_clamped'] == 0
    accounting.failed(state='running')
    assert accounting.snapshot()['running'] == 1
    assert accounting.snapshot()['running_unresolved'] == 1
    accounting.service_done(usable=False)
    snapshot = accounting.snapshot()
    assert snapshot['running'] == snapshot['running_unresolved'] == 0
    assert snapshot['gauge_clamped'] == 0


def test_legacy_pending_underflow_is_reported_without_negative_depth():
    accounting = trace.QueueAccounting('material_batch')
    accounting.submitted()
    accounting.failed(n=2, state='pending')
    snapshot = accounting.snapshot()
    assert snapshot['pending'] == 0
    assert snapshot['gauge_clamped'] == 1
