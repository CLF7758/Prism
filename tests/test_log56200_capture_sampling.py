"""Capture totals stay exact without flooding the recorder's normal queue."""
from types import SimpleNamespace
import json
import pytest
from prism.fileio import save_preparation as preparation
from tests.test_log56008_save_capture import operation


def burst(op, monkeypatch, count=3855):
    clock = [0.0]
    monkeypatch.setattr(preparation, 'time', SimpleNamespace(
        monotonic=lambda: clock[0], perf_counter=lambda: clock[0]))
    op._capture_clock = op._capture_last_end = 0.0
    op.SLICE_STEPS = 1
    def steps():
        for i in range(count-1):
            op._capture_stage = 'phase-' + str(i%3)
            clock[0] += 1.0 if i == count-2 else .0001
            yield
    op._steps = steps()
    while not op.ready.is_set():
        op._drain()
        op.timer.stop()
    return count


@pytest.mark.parametrize('diagnostic,limit', [(False,16),(True,64)])
def test_burst_coalesces_events_but_keeps_all_steps_and_late_worst_slice(operation, monkeypatch, diagnostic, limit):
    operation._capture_sample_mode = preparation.MODE_DIAGNOSTIC if diagnostic else 'normal'
    operation._capture_sample_limit = limit
    events = []
    monkeypatch.setattr(preparation, 'record', lambda name, **fields: events.append((name,fields)))
    total = burst(operation, monkeypatch)
    samples = [r for name,r in events if name=='project_save_capture_slice']
    summaries = [r for name,r in events if name=='project_save_capture']
    assert len(samples) == limit
    assert len(summaries) == 1
    final = summaries[0]
    assert final['slices'] == total and final['steps'] == total-1
    assert final['slice_samples'] == limit
    assert final['slices_coalesced'] == total-limit
    assert final['max_slice_ms'] == pytest.approx(1000)
    assert final['slow_slices'] == 1
    assert final['priority'] == 'terminal'
    assert all(sample['priority']=='sample' for sample in samples)


def test_burst_real_recorder_flush_keeps_summary_and_reports_no_overflow(operation, monkeypatch, qapp, tmp_path):
    from prism import performance_trace
    trace = performance_trace.install(qapp, None, tmp_path/'capture-trace')
    try:
        burst(operation, monkeypatch)
    finally:
        trace.close()
    rows = [json.loads(line) for line in trace.path.read_text(encoding='utf-8').splitlines()]
    assert not trace.dropped
    assert not any(row['event']=='trace_overflow' for row in rows)
    final = [row for row in rows if row['event']=='project_save_capture']
    assert len(final) == 1 and final[0]['slices']==3855
    assert sum(row['event']=='project_save_capture_slice' for row in rows) <= 16
    assert final[0]['priority']=='terminal'
