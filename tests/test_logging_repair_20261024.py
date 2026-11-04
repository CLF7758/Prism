import json
import time
from types import SimpleNamespace
from unittest.mock import Mock
from prism import performance_trace as trace
from tools import analyze_prism_trace as analyzer


def test_gc_records_trigger_context_and_exact_interval(monkeypatch):
    clock = iter((10.0, 10.125))
    monkeypatch.setattr(trace.time, 'perf_counter', lambda: next(clock))
    contexts = []
    sink = Mock(side_effect=lambda *args, **kwargs: contexts.append(trace.capture_context()))
    target = SimpleNamespace(_gc_started={}, gui_thread=trace.threading.get_ident(), record=sink)
    started = trace.TraceContext(trace_id='origin', span_id='allocation-trigger')
    with trace.restore_context(started):
        trace.PerformanceTrace._gc_callback(target, 'start', {'generation': 2})
    with trace.restore_context(trace.TraceContext(trace_id='later', span_id='other')):
        trace.PerformanceTrace._gc_callback(target, 'stop', {'generation': 2, 'collected': 0, 'uncollectable': 0})
    fields = sink.call_args.kwargs
    assert fields['gc_start_monotonic'] == 10
    assert fields['gc_end_monotonic'] == 10.125
    assert fields['duration_ms'] == 125
    assert fields['gc_trigger_span_id'] == 'allocation-trigger'
    assert contexts[-1].trace_id == 'origin'
    assert contexts[-1].span_id == 'allocation-trigger'
    assert fields['gc_on_gui_thread'] is True
    assert fields['gc_generation'] == 2
    assert 'generation' not in fields


def analyse(tmp_path, events):
    path = tmp_path/'frozen.jsonl'
    path.write_text(''.join(json.dumps(dict(session='isolated',sequence=i+1,**e))+'\n'
                            for i,e in enumerate(events)),encoding='utf-8')
    reports,_ = analyzer.analyse([str(path)])
    return reports[0]['diagnostic_work']


def test_analyzer_separates_install_inheritance_and_gc_project_generation(tmp_path):
    work = analyse(tmp_path,[
        dict(event='gc',duration_ms=200,generation=77),
        dict(event='gc',duration_ms=300,gc_generation=2,generation=77,gc_min_duration_ms=10),
        dict(event='catalog_install_work',inherited_rows=100000,evaluated_rows=0,published_rows=37),
        dict(event='catalog_install_slice_overrun',elapsed_ms=123,phase_at_yield='model_publication'),
        dict(event='asset_model_delta_declined',reason='receipt_chain_unproven'),
        dict(event='asset_model_delta_published',changed_rows=1,retained_model_rows=100000),
    ])
    assert work['gc']['generations'] == {'unknown':1,'2':1}
    assert work['gc']['threshold_unknown_events'] == 1
    assert work['gc']['p95_ms'] == 300
    install=work['installation']
    assert install['phases']=={'model_publication':1}
    assert install['work'][0]['entry']['evaluated_rows']==0
    assert work['save_projection']['decline_reasons']=={'receipt_chain_unproven':1}


def test_process_io_requires_compatible_monotonic_counters_and_preserves_big_integers(tmp_path):
    def sample(at, read, write, **extra):
        return dict(event='process',pid=1,monotonic=at,
                    io=dict(source='GetProcessIoCounters',scope='current_process_lifetime',
                            read_bytes=read,write_bytes=write,includes_child_processes=False,**extra))
    big=2**55
    work=analyse(tmp_path,[sample(1,big,50),sample(2,big+3,54),sample(3,1,2),
                           dict(event='process',pid=1,monotonic=4,io='unknown')])
    intervals=work['process_io']['intervals']
    assert intervals[0]['delta'] is None
    assert intervals[1]['delta']==dict(read_bytes=3,write_bytes=4,interval_seconds=1)
    assert intervals[2]['delta'] is None
    assert intervals[3]['delta'] is None
    assert work['process_io']['physical_disk_bytes'] is False


def test_gc_span_uses_collection_end_not_later_log_emission():
    session=analyzer.Session('isolated')
    session.add_span(dict(event='gc',monotonic=10.130,duration_ms=125,
                         gc_start_monotonic=10,gc_end_monotonic=10.125),'fixture',1)
    assert session.spans[-1]['start_mono']==10
    assert session.spans[-1]['end_mono']==10.125
    session.add_span(dict(event='gc',monotonic=11,duration_ms=125,
                         gc_start_monotonic=12,gc_end_monotonic=10),'fixture',2)
    assert session.spans[-1]['end_mono']==11
    assert session.anomalies['invalid_gc_interval']==1
