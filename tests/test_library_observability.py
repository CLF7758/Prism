"""Check independent failure outcomes in the actual JSONL and diagnostic ZIP."""
import json
import threading
import zipfile
from types import SimpleNamespace

import pytest
from PIL import Image

from prism import library_observability as audit
from prism import performance_trace as trace


def test_material_bindings_failures_and_stale_covers_reach_disk(main_window,qapp,tmp_path):
    from prism.material_textures import prepare_texture
    logger=trace.install(qapp,main_window,tmp_path/'logs')
    service=main_window.view.category_panel.material_service
    try:
        material=service.create(name='do not log my private note')
        image=tmp_path/'map.png';Image.new('RGB',(8,8),'red').save(image)
        service.configure(material['id'],slot='basecolor',prepared=prepare_texture(image),channel='RGB')
        with pytest.raises(ValueError):service.configure(material['id'],slot='basecolor',channel='INVALID')
        with pytest.raises(FileNotFoundError):prepare_texture(tmp_path/'missing.png')
        assert service.save_preview(material['id'],'obsolete-signature',b'not-an-image') is False
    finally:logger.close()
    rows=[json.loads(line) for line in logger.path.read_text(encoding='utf-8').splitlines()]
    bindings=[r for r in rows if r['event']=='library.binding_state.installed']
    assert bindings and bindings[-1]['material_id']==material['id']
    assert bindings[-1]['sha256'] and bindings[-1]['durable'] is False
    failures=[r for r in rows if r['event']=='library.binding.failed']
    assert failures[-1]['error_type']=='ValueError' and failures[-1]['error_site']
    assert any(r['event']=='library.texture_read_decode.failed' for r in rows)
    assert any(r['event']=='library.cover_install.stale' for r in rows)
    assert 'do not log my private note' not in logger.path.read_text(encoding='utf-8')


def test_material_thread_inherits_trace_and_reports_exception(main_window,qapp,tmp_path):
    from prism.widgets.material_task import MaterialTask
    logger=trace.install(qapp,main_window,tmp_path/'logs')
    try:
        with trace.operation('user.import_texture'):
            parent=trace.current_context()
            def fail():raise OSError('simulated read failure')
            task=MaterialTask(fail);task.task_id='controlled-task'
        worker=threading.Thread(target=task.run);worker.start();worker.join(timeout=5)
        assert not worker.is_alive()
    finally:logger.close()
    rows=[json.loads(line) for line in logger.path.read_text(encoding='utf-8').splitlines()]
    failed=next(r for r in rows if r['event']=='library.background_task.failed')
    assert failed['trace_id']==parent.trace_id
    assert failed['job_id']=='controlled-task'


def test_broken_logging_does_not_repeat_or_replace_business_exception(monkeypatch):
    def broken(*args,**kwargs):raise OSError('log disk full')
    monkeypatch.setattr(trace,'record',broken)
    count=[]
    @audit.observed('material','fault_injection')
    def operation():
        count.append(1)
        raise ValueError('original business failure')
    before=audit.snapshot()['diagnostic_failures']
    with pytest.raises(ValueError,match='original business failure'):operation()
    assert count==[1]
    assert audit.snapshot()['diagnostic_failures']>before


def test_snapshot_is_bounded_and_stale_storm_is_explicitly_coalesced(monkeypatch):
    rows=[];monkeypatch.setattr(trace,'record',lambda name,**fields:rows.append((name,fields)))
    for index in range(2000):
        audit.event('material','stale_storm','stale',reason='late',expected_request_id='one')
    data=audit.snapshot()
    assert len(data['recent'])<=128 and len(rows)<=3
    assert data['counts']['material.stale_storm.stale']>=2000
    assert data['counts']['material.stale_storm.coalesced_log_lines']>=1997


def test_bundle_contains_library_evidence_and_redacts_content(tmp_path,monkeypatch):
    from prism.diagnostics import collect_bundle
    audit.event('model','test_failure','failed',reason=r'C:\Users\admin\private\model.fbx',
                payload='PRIVATE-BYTES',notes='PRIVATE-NOTE',image=b'PRIVATE-PIXELS')
    logs=tmp_path/'logs';logs.mkdir()
    target=tmp_path/'diagnostic.zip'
    collect_bundle(logs,target,runtime={})
    with zipfile.ZipFile(target) as bundle:
        body=bundle.read('library-evidence.json')
        data=json.loads(body)
    assert data['schema']=='library_evidence/1'
    assert b'PRIVATE-BYTES' not in body and b'PRIVATE-NOTE' not in body and b'PRIVATE-PIXELS' not in body
    assert b'C:\\\\Users' not in body


def test_project_boundaries_never_scan_lazy_records(monkeypatch):
    class Lazy:
        def __iter__(self):raise AssertionError('No diagnostic scan on GUI thread')
        def __len__(self):raise AssertionError('No lazy count query on GUI thread')
    view=SimpleNamespace(scene=SimpleNamespace(project_id='p',material_records=Lazy(),model_records=Lazy()))
    rows=[];monkeypatch.setattr(trace,'record',lambda name,**fields:rows.append(fields))
    audit.project_boundary(view,'save_outcome',phase='unconfirmed',durable=False)
    assert rows[-1]['scope']=='resident_scene_metadata_only'
    assert 'model_count' not in rows[-1] and 'material_count' not in rows[-1]


def test_saved_material_row_digest_matches_real_reopen(main_window,qapp,tmp_path):
    from prism.fileio.sql import SQLiteIO
    logger=trace.install(qapp,main_window,tmp_path/'logs')
    try:
        service=main_window.view.category_panel.material_service
        service.create(name='Stored material')
        filename=str(tmp_path/'roundtrip.prism')
        writer=SQLiteIO(filename,main_window.view.scene,create_new=True)
        try:writer.write()
        finally:writer._close_connection()
        reader=SQLiteIO(filename,main_window.view.scene,readonly=True)
        try:reader.read()
        finally:reader._close_connection()
    finally:logger.close()
    rows=[json.loads(line) for line in logger.path.read_text(encoding='utf-8').splitlines()]
    writes=[r for r in rows if r['event']=='library.storage_rows.write_prepared' and r['domain']=='material']
    reads=[r for r in rows if r['event']=='library.storage_rows.read_validated' and r['domain']=='material']
    assert writes and reads
    assert writes[-1]['sha256']==reads[-1]['sha256']
    assert writes[-1]['count']==reads[-1]['count']==1
    assert writes[-1]['durable'] is False,'Prepared metadata is not a commit acknowledgement'


def test_model_worker_failure_preserves_request_and_root_trace(main_window,qapp,tmp_path):
    from concurrent.futures import ThreadPoolExecutor
    from prism.model_library.multimaterial.application import ProjectMaterialService
    from prism.model_library.multimaterial.contracts import MaterialTarget
    from prism.model_library.multimaterial.scheduling import MaterialScheduler
    logger=trace.install(qapp,main_window,tmp_path/'logs')
    service=ProjectMaterialService.__new__(ProjectMaterialService)
    service._closed=False;service._pool=ThreadPoolExecutor(max_workers=1)
    # A preview without a request runs on the render lane, exactly like the
    # real service does.
    service._render_pool=MaterialScheduler()
    service._jobs={};service._job_requests={};service._cancelled=set()
    service._listeners={};service.owner=SimpleNamespace(_post=lambda callback:callback(),_publish=lambda value:None)
    target=MaterialTarget('session',1,'model',1,1,1)
    try:
        with trace.operation('user.model_texture'):
            parent=trace.current_context()
            def fail(_job):raise ValueError('fault injection')
            job=service._queue('preview',target,fail,generation=7)
        service._jobs[job].result(timeout=5)
    finally:
        service._pool.shutdown();service._render_pool.shutdown();logger.close()
    rows=[json.loads(line) for line in logger.path.read_text(encoding='utf-8').splitlines()]
    failed=next(r for r in rows if r['event']=='library.job_preview.failed')
    assert failed['trace_id']==parent.trace_id
    assert failed['model_id']=='model' and failed['job_id']==job and failed['draft_generation']==7


def test_the_scheduler_snapshot_carries_the_queue_depth(main_window,qapp,tmp_path):
    """The queue depth must reach the trace, not be filtered out of it."""
    from prism.model_library.multimaterial.application import ProjectMaterialService
    from prism.model_library.multimaterial.contracts import MaterialTarget
    from prism.model_library.multimaterial.scheduling import MaterialScheduler
    logger=trace.install(qapp,main_window,tmp_path/'logs')
    service=ProjectMaterialService.__new__(ProjectMaterialService)
    service._closed=False
    service._render_pool=MaterialScheduler()
    service._jobs={};service._job_requests={};service._cancelled=set()
    service._listeners={};service.owner=SimpleNamespace(_post=lambda callback:callback(),_publish=lambda value:None)
    target=MaterialTarget('session',1,'model',1,1,1)
    try:
        release=__import__('threading').Event()
        service._queue('preview',target,lambda _job:release.wait(5))
    finally:
        service._render_pool.shutdown();logger.close()
    rows=[json.loads(line) for line in logger.path.read_text(encoding='utf-8').splitlines()]
    snapshot=next(r for r in rows if r['event']=='library.scheduler.snapshot')
    assert snapshot['submitted']==1
    assert snapshot['pending']==1
    assert snapshot['replaced']==0 and snapshot['heap_entries']>=1
    # The counters must be numbers, not the nulls the whitelist used to leave.
    assert isinstance(snapshot['active'],int) and snapshot['active'] in (0,1)
    assert isinstance(snapshot['completed'],int)


def test_writer_survives_global_logging_reconfiguration(main_window,qapp,tmp_path,qtbot):
    import logging
    logger=trace.install(qapp,main_window,tmp_path/'logs')
    try:
        trace.record('before_reconfigure',priority=trace.PRIORITY_STATE)
        qtbot.waitUntil(lambda:logger.events_written>=2,timeout=5000)
        # This is what logging.config does to registered handlers. The trace
        # owns its lifetime and must reopen instead of dying on stream=None.
        handlers=[ref() for ref in logging._handlerList]
        for handler in handlers:
            if isinstance(handler,logging.handlers.RotatingFileHandler) and handler.baseFilename==str(logger.path.resolve()):
                handler.close()
        trace.record('after_reconfigure',priority=trace.PRIORITY_STATE)
        qtbot.waitUntil(lambda:logger.writer_reopens>=1,timeout=5000)
    finally:logger.close()
    rows=[json.loads(line) for line in logger.path.read_text(encoding='utf-8').splitlines()]
    assert any(r['event']=='after_reconfigure' for r in rows)
    assert any(r['event']=='trace_stream_reopened' for r in rows)
    assert logger.writer_errors==0


def test_unwritable_stream_marks_incomplete_in_emergency_channel(main_window,qapp,tmp_path,qtbot,monkeypatch):
    import logging
    from logging.handlers import RotatingFileHandler
    logger=trace.install(qapp,main_window,tmp_path/'logs')
    try:
        qtbot.waitUntil(lambda:logger.events_written>=2,timeout=5000)
        original=RotatingFileHandler._open
        def denied(handler):
            if handler.baseFilename==str(logger.path.resolve()):raise OSError('injected disk full')
            return original(handler)
        monkeypatch.setattr(RotatingFileHandler,'_open',denied)
        for reference in logging._handlerList:
            handler=reference()
            if isinstance(handler,RotatingFileHandler) and handler.baseFilename==str(logger.path.resolve()):handler.close()
        trace.record('force_write_failure',priority=trace.PRIORITY_ERROR)
        qtbot.waitUntil(lambda:logger.writer_errors==1,timeout=5000)
    finally:logger.close()
    assert logger.trace_incomplete
    startup=next((tmp_path/'logs').glob('Prism-startup-*.jsonl'))
    entries=[json.loads(line) for line in startup.read_text(encoding='utf-8').splitlines()]
    assert any(r['event']=='trace_writer_failed' and r['trace_incomplete'] for r in entries)


def test_thread_and_destructor_exceptions_preserve_host_hooks(monkeypatch):
    import sys
    previous=[];events=[]
    monkeypatch.setattr(threading,'excepthook',lambda args:previous.append('thread'))
    monkeypatch.setattr(sys,'unraisablehook',lambda args:previous.append('unraisable'))
    monkeypatch.setattr(audit,'event',lambda domain,stage,outcome,**fields:events.append((stage,fields)))
    restore=audit.install_failure_hooks()
    try:
        threading.excepthook(SimpleNamespace(exc_value=ValueError('worker failed')))
        sys.unraisablehook(SimpleNamespace(exc_value=RuntimeError('destructor failed')))
    finally:restore()
    assert previous==['thread','unraisable']
    assert [stage for stage,_ in events]==['uncaught_thread','unraisable']
    assert events[0][1]['error_type']=='ValueError'
