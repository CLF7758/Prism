"""What the resource rollup says about the classes it could not measure.

Four classes used to be reported as "unknown" or "not accounted for":
``gpu``, ``webengine_children``, ``temp_disk`` and ``dirty_records``.  This
pins what each of them reports now:

* which ones are measured, and with which numbers,
* which ones are not, and the exact reason why,
* that the thread that writes the rollup never enumerates the process list,
* that the resident memory no cache claims is an explicit number of its own.

A measured class must not silently become an unknown one again, and an
unknown one must never become a zero: a zero reads like "nothing is used".
"""
import threading
import time

import pytest


class _RollupWriter:
    """Writes resource rollups and keeps what they said."""

    def __init__(self, budget, captured):
        self.budget = budget
        self._captured = captured

    def write(self, reason='test'):
        self._captured.clear()
        self.budget.record_resource_rollup(reason=reason, force=True)
        events = [fields for kind, fields in self._captured
                  if kind == 'resource_rollup']
        assert events, 'the budget wrote no resource_rollup event'
        return events[-1]


@pytest.fixture
def rollup(monkeypatch):
    """A budget of its own, and the rollups it writes."""
    from prism import performance_trace, resource_budget

    captured = []

    def _record(kind, /, **fields):
        captured.append((kind, fields))

    monkeypatch.setattr(performance_trace, 'record', _record)
    budget = resource_budget.CacheBudget(1024 * 1024)
    monkeypatch.setattr(resource_budget, '_shared', budget)
    # The temporary-file leases are module state: keep this test's leases
    # out of the other tests, and theirs out of this test.
    monkeypatch.setattr(resource_budget, '_temp_leases', {})
    monkeypatch.setattr(resource_budget, '_temp_lease_counts', {})
    monkeypatch.setattr(resource_budget, '_scopes', {})
    monkeypatch.setattr(resource_budget, '_current_scope', None)
    return _RollupWriter(budget, captured)


def test_the_rollup_measures_the_classes_it_can(rollup):
    """Two of the four classes carry numbers now, two carry a reason."""
    from prism import performance_trace
    performance_trace.child_process_sample(refresh=True)
    payload = rollup.write()
    classes = payload['classes']

    children = classes['webengine_children']
    assert children['source'] == 'measured'
    assert isinstance(children['current'], int) and children['current'] >= 0
    assert isinstance(children['child_count'], int)
    assert children['child_count'] >= len(children['children'])
    assert children['sampled_at'] > 0
    assert children['sample_age_seconds'] >= 0
    assert children['sample_source']
    # The enumeration is far too expensive for the GUI thread, so it ran
    # somewhere else.
    assert children['sample_thread'] != threading.current_thread().name

    temp = classes['temp_disk']
    assert temp['source'] == 'measured'
    assert isinstance(temp['current'], int)
    assert temp['bytes_complete'] is True
    assert temp['leases_without_size'] == 0

    assert classes['gpu']['source'] == 'unknown'
    assert classes['gpu']['current'] is None
    assert classes['gpu']['reason']

    assert classes['dirty_records']['source'] == 'not_accounted'
    assert classes['dirty_records']['current'] is None
    assert 'partial_project.py' in classes['dirty_records']['reason']


def test_a_class_without_a_source_never_reports_zero(rollup):
    """An unknown value is unknown, never a zero that reads like "unused"."""
    from prism import performance_trace
    performance_trace.child_process_sample(refresh=True)
    classes = rollup.write()['classes']
    for name in ('gpu', 'dirty_records'):
        assert classes[name]['current'] is None, name
        assert classes[name]['peak'] is None, name


def test_temp_disk_follows_the_live_leases(rollup):
    """The bytes of a lease are counted while it lives, and gone after."""
    from prism.resource_budget import ProjectScope
    scope = ProjectScope('test-temp-disk')
    lease = scope.lease('decode_temp', 'C:/tmp/decode-image',
                        lambda released: None, size_bytes=4096)

    temp = rollup.write()['classes']['temp_disk']
    assert temp['source'] == 'measured'
    assert temp['current'] == 4096
    assert temp['lease_count'] == 1
    assert temp['bytes_by_kind'] == {'decode_temp': 4096}

    lease.release()

    temp = rollup.write()['classes']['temp_disk']
    # A real zero: the class is still measured, and it says so.
    assert temp['source'] == 'measured'
    assert temp['current'] == 0
    assert temp['lease_count'] == 0
    assert temp['bytes_by_kind'] == {}


def test_a_lease_without_a_size_is_unknown_and_not_zero(rollup):
    """A lease whose size nobody knew cannot make the total a number."""
    from prism.resource_budget import ProjectScope
    scope = ProjectScope('test-unsized-lease')
    scope.lease('decode_temp', 'C:/tmp/unsized', lambda released: None)
    temp = rollup.write()['classes']['temp_disk']
    assert temp['source'] == 'not_accounted'
    assert temp['current'] is None
    assert temp['reason']


def test_the_rollup_never_enumerates_the_process_list(monkeypatch, rollup):
    """The enumeration costs ~20 ms, so it never runs where the rollup runs."""
    from prism import performance_trace
    calls = []
    real = performance_trace._toolhelp_children

    def _counted(parent_pid):
        calls.append(parent_pid)
        return real(parent_pid)

    monkeypatch.setattr(performance_trace, '_toolhelp_children', _counted)
    performance_trace.child_process_sample(refresh=True)
    assert calls, 'the background sampler did not enumerate at all'

    calls.clear()
    payload = rollup.write()
    assert calls == [], 'the rollup enumerated the process list itself'
    assert payload['classes']['webengine_children']['source'] == 'measured'


def test_the_resident_memory_no_cache_claims_is_an_explicit_number(monkeypatch, rollup):
    """The gap between the process and the caches is stated, not implied."""
    from prism import performance_trace

    monkeypatch.setattr(performance_trace, 'child_process_sample', lambda **kwargs: {
        'bytes': 0, 'count': 0, 'children': [], 'children_truncated': False,
        'children_unreadable': 0, 'source': 'test',
        'rss_bytes': 5_000_000, 'monotonic': time.perf_counter(),
        'sampled_at': time.time(), 'duration_ms': 1.0, 'thread': 'test'})

    class Cache:
        bytes = 1000

        def _trim_cache_to(self, target):
            self.bytes = target

    # The budget holds the cache weakly, so the owner has to stay alive.
    cache = Cache()
    rollup.budget.register(cache, category='image_display', usage='bytes')
    payload = rollup.write()

    assert payload['resident_bytes'] == 1000
    assert payload['process_rss_bytes'] == 5_000_000
    assert payload['resident_unaccounted_bytes'] == 5_000_000 - 1000
    assert payload['resident_unaccounted_cache_bytes'] == 1000
    assert payload['resident_unaccounted_status'] == 'measured'
    # It says what it is: not a leak verdict, and not a number to add up.
    assert 'not a leak verdict' in payload['resident_unaccounted_note']
    assert 'must not be added' in payload['resident_unaccounted_note']
    # It is a field of its own, never folded into one of the classes.
    for name, entry in payload['classes'].items():
        assert 'resident_unaccounted_bytes' not in entry, name


def test_the_resident_gap_is_unknown_before_the_first_sample(monkeypatch, rollup):
    """Without a sample the gap is unknown, not a zero."""
    from prism import performance_trace
    monkeypatch.setattr(performance_trace, 'child_process_sample',
                        lambda **kwargs: None)
    payload = rollup.write()
    assert payload['resident_unaccounted_bytes'] is None
    assert payload['resident_unaccounted_status'] == 'unknown'
    assert payload['process_rss_bytes'] is None
    assert payload['classes']['webengine_children']['source'] == 'unknown'
    assert payload['classes']['webengine_children']['current'] is None


def test_the_decode_lease_carries_the_bytes_the_writer_knew(qapp, monkeypatch):
    """The image decode leases the bytes it wrote, so nobody has to stat it."""
    from prism import image_decode, resource_budget
    monkeypatch.setattr(resource_budget, '_temp_leases', {})
    monkeypatch.setattr(resource_budget, '_temp_lease_counts', {})
    scope = resource_budget.ProjectScope('test-decode')
    payload = b'x' * 321
    with image_decode.native_reader(blob=payload, scope=scope):
        usage = resource_budget.temp_disk_usage()
        assert usage['bytes'] == len(payload)
        assert usage['bytes_by_kind'] == {'decode_temp': len(payload)}
        assert usage['lease_count'] == 1
    assert resource_budget.temp_disk_usage()['bytes'] == 0
