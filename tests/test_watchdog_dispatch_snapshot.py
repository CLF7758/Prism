"""The GUI may finish dispatches while the watchdog analyses a stalled window."""
from collections import deque
from types import SimpleNamespace
import threading

from prism.performance_trace import PerformanceTrace


def test_gui_append_during_stall_analysis_does_not_kill_watchdog():
    analysing=threading.Event()
    appended=threading.Event()
    class Interval(dict):
        def __getitem__(self,key):
            if key=='end_monotonic':
                analysing.set()
                assert appended.wait(timeout=5), 'Analysis held the history lock'
            return super().__getitem__(key)
    first=Interval(dispatch_id='first',start_monotonic=1.,end_monotonic=2.,depth=0)
    trace=SimpleNamespace(_dispatch_history=deque([first],maxlen=512),
        _dispatch_history_lock=threading.Lock(),current_dispatch=[])
    def complete_on_gui():
        assert analysing.wait(timeout=5)
        with trace._dispatch_history_lock:
            trace._dispatch_history.append(dict(dispatch_id='later',start_monotonic=2.,end_monotonic=3.,depth=0))
        appended.set()
    writer=threading.Thread(target=complete_on_gui)
    writer.start()
    try:
        intervals=PerformanceTrace._stall_dispatch_intervals(trace,{'window_start_monotonic':0.},4.)
    finally:
        writer.join(timeout=6)
    assert not writer.is_alive()
    assert [item['dispatch_id'] for item in intervals]==['first']
    assert len(trace._dispatch_history)==2
