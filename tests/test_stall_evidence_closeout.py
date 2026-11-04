"""Window-free closeout for install/detail regressions and stall evidence loss."""
import importlib.util
import json
from pathlib import Path
from types import MethodType, SimpleNamespace
from unittest.mock import Mock
import threading
import pytest
from prism import catalog_install
from prism import performance_trace as pt
from tools import analyze_prism_trace as analyzer
from prism.widgets.model_details import ModelDetails

ROOT=Path('C:/Users/admin/Desktop/Prism-main/Prism-main')


def trace_helpers():
    spec=importlib.util.spec_from_file_location('trace_test_helpers',ROOT/'tests/test_trace_analyzer_schema2_closeout.py')
    module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
    return module


def test_shared_checkpoint_and_resume_do_not_inherit_live_timer():
    pending={'token':'old','offset':3,'items':['prefix'],'install_queued':True,
             'overlay':{},'new':(), 'requested_digest':'q'}
    checkpoint=catalog_install.checkpoint_install(pending)
    assert 'install_queued' not in checkpoint
    resumed=catalog_install.resume_install(dict(checkpoint,install_queued=True),token='new')
    assert not resumed.get('install_queued',False)
    assert resumed['items']==['prefix'] and resumed['items'] is not pending['items']
    assert pending['install_queued'] is True


def test_restored_checkpoint_schedules_again_without_mutating_old_pending(monkeypatch):
    from prism.widgets.asset_browser import AssetBrowser
    from PyQt6 import QtCore
    callbacks=[]
    monkeypatch.setattr(QtCore.QTimer,'singleShot',lambda delay,callback:callbacks.append(callback))
    old={'offset':1,'items':['prefix'],'install_queued':True}
    resumed=catalog_install.resume_install(old,token='new')
    advanced=[]
    sink=SimpleNamespace(_catalog_install_valid=lambda p:p is resumed,
         _maybe_publish_grid_preview=Mock(),_catalog_install_progress=Mock(),
         _install_catalog_rows=lambda p:advanced.append(p['offset']))
    AssetBrowser._queue_catalog_install(sink,resumed)
    assert len(callbacks)==1 and old['install_queued'] is True
    callbacks[0]()
    assert advanced==[1] and resumed['install_queued'] is False


def detail_sink():
    from prism.model_library.contracts import ModelCapabilities
    from prism.widgets import model_details as md
    values={key:Mock() for key in ('title','tags','notes','files','preview','preview_status',
                                 'apply_button','slot_notice','_metadata_timer')}
    values.update(_record=None,_multimaterial_owned=False,slots={},
        material_panel=SimpleNamespace(model_id=None,finish_edit=Mock(),reset=Mock()),
        _capabilities=ModelCapabilities(edit_metadata=True),_slot_policy=None,
        _pending_tags='old',_pending_notes='old',info={key:Mock() for key in
            ('format','size','triangles','vertices','basis','revision','dependencies')},
        _flush_metadata=Mock(),_load_slots=Mock(),_show_cover=Mock(),
        _dependency_text=lambda record:'none')
    sink=SimpleNamespace(**values)
    sink.show_summary=MethodType(ModelDetails.show_summary,sink)
    sink.set_capabilities=MethodType(ModelDetails.set_capabilities,sink)
    sink.show_record=MethodType(ModelDetails.show_record,sink)
    return sink


def test_summary_is_readonly_and_record_restores_cached_capabilities():
    from prism.model_library.contracts import ModelRecord
    sink=detail_sink()
    sink.show_summary()
    sink.tags.setReadOnly.assert_called_with(True)
    sink.notes.setReadOnly.assert_called_with(True)
    sink.title.setReadOnly.assert_called_with(True)
    record=SimpleNamespace(model_id='A',name='model',tags=(),notes='note',format='glb',
        source_revision=1,files=(),main_file_id=None,statistics=SimpleNamespace(
            triangles=None,vertices=None,basis='unknown'),appearance=None)
    # Existing callers may omit capabilities; the cached policy must recover.
    sink.show_record(record)
    sink.title.setReadOnly.assert_called_with(False)
    sink.tags.setReadOnly.assert_called_with(False)
    sink.notes.setReadOnly.assert_called_with(False)


def intervals(count=155):
    return [dict(dispatch_id='d%d'%i,parent_dispatch_id=None,root_dispatch_id='d%d'%i,
        depth=0,target='QObject',event_type=1,start_monotonic=1+i*.001,
        end_monotonic=1+(i+1)*.001) for i in range(count)]


def test_stall_interval_array_survives_bounded_recording(monkeypatch, tmp_path):
    recorder=trace_helpers()._real_record_sink()
    monkeypatch.setattr(pt, '_active', recorder)
    stall={'stall_id':'stall','window_start_monotonic':1.,'context_snapshot':{},
           'dispatch_intervals':intervals(),'count':1,'heartbeat_max':155.,
           'dispatches_completed':155}
    recorder.gui_thread=threading.get_ident()
    recorder._gc_lock=threading.Lock();recorder._gc_intervals=[]
    recorder._record_stall_context=MethodType(pt.PerformanceTrace._record_stall_context,recorder)
    recorder._record_stall_context(stall,155.,1.155)
    rows=list(recorder.recent)
    batches=[row for row in rows if row['event']=='ui.stall_dispatch_batch']
    assert len(batches)==7
    assert all(len(row['dispatch_intervals'])<=pt.MAX_SEQUENCE for row in batches)
    assert sum(len(row['dispatch_intervals']) for row in batches)==155
    assert all(isinstance(item,dict) for row in batches for item in row['dispatch_intervals'])
    assert max(len(json.dumps(row).encode()) for row in batches)<pt.PerformanceTrace.MAX_EVENT_BYTES
    contexts=[row for row in rows if row['event']=='ui.stall_context']
    assert len(contexts)==1 and contexts[0]['dispatch_interval_count']==155
    path=tmp_path/'actual-recorded.jsonl'
    path.write_text(''.join(json.dumps(row)+'\n' for row in rows),encoding='utf-8')
    reports,_=analyzer.analyse([str(path)])
    recovered=reports[0]['stall_contexts']['events'][0]
    assert recovered['dispatch_evidence']['complete']
    assert len(recovered['dispatch_intervals'])==155


def emit_batches(count=155):
    helper=getattr(pt,'_stall_dispatch_payloads',None)
    assert helper is not None
    return helper(intervals(count))


def analyse_batches(tmp_path, *, damage=None):
    prefix,batches,metadata=emit_batches()
    common=dict(session='s',stall_id='stall',window_start_monotonic=1.,window_end_monotonic=1.155)
    rows=[dict(event='session_start',session='s')]
    rows.append(dict(common,event='ui.stall_context',dispatch_intervals=prefix,**metadata))
    for index,batch in enumerate(batches):
        rows.append(dict(common,event='ui.stall_dispatch_batch',dispatch_batch_version=1,
            batch_index=index,batch_count=len(batches),dispatch_interval_count=155,
            dispatch_intervals=batch))
    rows.append(dict(common,event='gui_stall',phase='end',role='end',duration_ms=155.))
    rows.append(dict(event='session_end',session='s'))
    if damage=='missing':del rows[3]
    if damage=='duplicate':rows.insert(3,dict(rows[2]))
    if damage=='window':rows[2]['window_end_monotonic']=9.
    if damage=='identity':rows[2]['dispatch_intervals'][0]['dispatch_id']='d1'
    if damage=='prefix':rows[1]['dispatch_intervals'][0]=dict(rows[1]['dispatch_intervals'][0],target='different')
    if damage=='bool':rows[2]['batch_index']=False
    if damage=='outside':
        rows[2]['dispatch_intervals'][0]=dict(rows[2]['dispatch_intervals'][0],start_monotonic=8.,end_monotonic=9.)
    for index,row in enumerate(rows):
        row.update(sequence=index+1,monotonic=1+index*.001)
    path=tmp_path/'batches.jsonl'
    path.write_text(''.join(json.dumps(row)+'\n' for row in rows),encoding='utf-8')
    reports,_=analyzer.analyse([str(path)])
    return reports[0]['stall_contexts']['events'][0]


def test_parser_joins_only_valid_same_window_stall_batches(tmp_path):
    row=analyse_batches(tmp_path)
    assert row['dispatch_evidence']['complete'] is True
    assert len(row['dispatch_intervals'])==155
    assert len(row['dispatch_evidence']['batch_evidence'])==7


@pytest.mark.parametrize('damage',['missing','duplicate','window','identity','prefix','bool','outside'])
def test_incomplete_or_conflicting_batch_never_claims_full_coverage(tmp_path,damage):
    row=analyse_batches(tmp_path,damage=damage)
    assert row['dispatch_evidence']['complete'] is False
    assert row['dispatch_evidence']['reason']!='complete_batches'


def test_legacy_capped_dispatch_evidence_remains_unknown(tmp_path):
    path=tmp_path/'legacy.jsonl'
    path.write_text(json.dumps(dict(event='ui.stall_context',session='s',sequence=1,
        stall_id='old',dispatch_intervals=['…[capped 155 items]']))+'\n',encoding='utf-8')
    reports,_=analyzer.analyse([str(path)])
    row=reports[0]['stall_contexts']['events'][0]
    assert row['dispatch_evidence']['complete'] is False
    assert row['dispatch_evidence']['reason']=='legacy_capped_or_invalid'
    assert row['dispatch_intervals']==['…[capped 155 items]']


def test_interval_retention_ceiling_explicitly_reports_omitted():
    prefix,batches,metadata=emit_batches(600)
    assert metadata['dispatch_interval_count']==600
    assert metadata['dispatch_intervals_retained']==512
    assert metadata['dispatch_intervals_omitted']==88
    assert len(batches)==22 and all(len(batch)<=24 for batch in batches)
    assert len(prefix)<=24
