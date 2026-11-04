"""Bounded model/material evidence adapter for the existing trace writer.

No files, project scans, renderer imports, image reads or background threads.
The counters describe observed calls, never prove persistence or pixel quality.
"""
from collections import Counter, OrderedDict, deque
from contextlib import contextmanager
from dataclasses import asdict, is_dataclass
from functools import wraps
import inspect
import hashlib
import ntpath
import threading
import time
import uuid

from prism import performance_trace as trace
from prism.diagnostics import Redactor, SENSITIVE

_lock=threading.RLock()
_counts=Counter()
_recent=deque(maxlen=128)
_durations={}
_active=OrderedDict()
_last_stale=OrderedDict()
_failures=0
_allowed=frozenset(('project_id','project_session','open_epoch','model_id','material_id',
    'working_id','binding_id','request_id','job_id','gesture_id','source_revision',
    'catalog_revision','state_revision','draft_generation','generation','slot','channel',
    'attachment_id','sha256','signature','texture_signature','record_revision','action',
    'reason','error_type','error_code','outcome','queue_ms','size_bytes','width','height',
    'color_space','maps','file_count','cache_hit','viewer_id','received_request_id',
    'received_generation','expected_request_id','expected_generation','count','operation_id',
    'durable','phase','scope','failed','material_count','model_count','correlation_id',
    'error_site','call_chain','stack_digest',
    # Scheduler counters. Without them the snapshot event carried nothing but
    # the model id, so the queue depth was invisible in every trace.
    'submitted','replaced','completed','pending','active','heap_entries'))


def _safe(fields):
    redactor=Redactor();result={}
    for key,value in fields.items():
        if key not in _allowed or value is None:continue
        if isinstance(value,str):
            value='<sensitive>' if SENSITIVE.search(value) else redactor.string(value)[:400]
        elif not isinstance(value,(bool,int,float)):continue
        result[key]=value
    return result


def event(domain,stage,outcome,**fields):
    """Fail-open; callers must never need to catch a logging exception."""
    global _failures
    try:
        if trace.mode()==trace.MODE_OFF:return
        data=_safe(fields)
        data.update(domain=domain,stage=stage,outcome=outcome,library_event_schema='library_event/1')
        with _lock:
            _counts[(domain,stage,outcome)]+=1
            _recent.append(dict(data))
            if outcome=='stale':
                key=(domain,stage,data.get('expected_request_id'),data.get('reason'))
                now=time.monotonic()
                if now-_last_stale.get(key,-10)<1:
                    _counts[(domain,stage,'coalesced_log_lines')]+=1
                    return
                _last_stale[key]=now;_last_stale.move_to_end(key)
                while len(_last_stale)>256:_last_stale.popitem(last=False)
        trace.record('library.'+stage+'.'+outcome,
            priority=trace.PRIORITY_ERROR if outcome=='failed' or data.get('failed') else trace.PRIORITY_STATE,
            **data)
    except Exception:
        with _lock:_failures+=1


@contextmanager
def stage(domain,name,**fields):
    """A measured call boundary; completed means this call returned only."""
    if trace.mode()==trace.MODE_OFF:
        yield {}
        return
    operation_id=uuid.uuid4().hex
    started=time.perf_counter()
    metadata=dict(fields,operation_id=operation_id)
    mutable={}
    event(domain,name,'started',**metadata)
    with _lock:
        if len(_active)>=64:
            _active.popitem(last=False);_counts[('diagnostic','active','evicted')]+=1
        _active[operation_id]=dict(domain=domain,stage=name,started=started,**_safe(metadata))
    try:
        yield mutable
    except BaseException as error:
        event(domain,name,'failed',**dict(metadata,**exception_fields(error)))
        raise
    else:
        event(domain,name,mutable.pop('outcome','completed'),**dict(metadata,**mutable))
    finally:
        elapsed=(time.perf_counter()-started)*1000
        with _lock:
            _active.pop(operation_id,None)
            key=(domain,name)
            if key not in _durations:
                if len(_durations)>=96:_durations.pop(next(iter(_durations)))
                _durations[key]=deque(maxlen=128)
            _durations[key].append(elapsed)
        # Duration is scalar metadata, not an image/asset size.
        try:trace.record('library.stage.duration',domain=domain,stage=name,
                         operation_id=operation_id,duration_ms=elapsed)
        except Exception:pass


def observed(domain,name):
    """Instrument low-frequency service edges, never per-frame code."""
    def decorate(function):
        signature=inspect.signature(function)
        @wraps(function)
        def call(*args,**kwargs):
            if trace.mode()==trace.MODE_OFF:return function(*args,**kwargs)
            values=signature.bind_partial(*args,**kwargs).arguments
            metadata={k:v for k,v in values.items() if k in _allowed}
            if 'record_id' in values:metadata['material_id']=values['record_id']
            if 'self' in values:
                owner=values['self']
                metadata['project_id']=getattr(owner,'project_id',None)
                metadata.setdefault('material_id',getattr(owner,'selected_id',None))
            candidate=values.get('request') or values.get('prepared') or values.get('receipt')
            target=(getattr(candidate,'target',None) or values.get('target')
                    or getattr(candidate,'after',None)
                    or getattr(values.get('state'),'target',None)
                    or getattr(values.get('self'),'_target',None))
            if is_dataclass(target):metadata.update(asdict(target))
            record=values.get('record')
            if isinstance(record,dict):metadata['material_id']=record.get('id')
            request=candidate
            if request is not None:
                metadata.update(request_id=getattr(request,'request_id',None),
                                gesture_id=getattr(request,'gesture_id',None))
            with trace.operation('library.'+domain+'.'+name,**_safe(metadata)), stage(domain,name,**metadata) as result:
                value=function(*args,**kwargs)
                if value is False:result.update(outcome='rejected',reason='operation_returned_false')
                if isinstance(value,dict) and isinstance(value.get('textures'),dict):
                    result['maps']=len(value['textures'])
                return value
        return call
    return decorate


def exception_fields(error):
    """Only code locations; never read source lines or capture local variables."""
    import traceback
    frames=deque(maxlen=8)
    for frame,line in traceback.walk_tb(error.__traceback__):
        frames.append(ntpath.basename(frame.f_code.co_filename)+':'+str(line)+':'+frame.f_code.co_name)
    chain=' > '.join(frames)
    return dict(error_type=type(error).__name__,reason=str(error),
        error_site=frames[-1] if frames else 'unknown',call_chain=chain[-400:],
        stack_digest=hashlib.sha256(chain.encode('utf-8')).hexdigest()[:16])


def run_bound(context,function,*args,**kwargs):
    with trace.restore_context(context):return function(*args,**kwargs)


def project_boundary(view,boundary,**fields):
    """O(1), in-memory markers; counts never pretend partial loads are full."""
    try:
        scene=getattr(view,'scene',None)
        materials=getattr(scene,'material_records',None)
        models=getattr(scene,'model_records',None)
        event('project','library_persistence',boundary,
            project_id=getattr(scene,'project_id',None),
            record_revision=getattr(scene,'_material_content_revision',None),
            material_count=len(materials) if isinstance(materials,(list,tuple,dict)) else None,
            model_count=len(models) if isinstance(models,(list,tuple,dict)) else None,
            scope='resident_scene_metadata_only',**fields)
    except Exception:pass


def record_rows(domain,rows,phase,project_id,*,scope='serialized_metadata_row_multiset'):
    """Hash metadata already being read/written; no extra query or BLOB read.

    Equal digests mean equal serialized row multisets, not equal rendered
    pixels. A prepared write is explicitly not a committed transaction.
    """
    if trace.mode()==trace.MODE_OFF or trace._active is None:return
    try:
        started=time.perf_counter();digests=[];size=0
        for _key,payload in rows:
            raw=payload.encode('utf-8') if isinstance(payload,str) else bytes(payload)
            key=str(_key).encode('utf-8')
            size+=len(raw)
            digests.append(hashlib.sha256(len(key).to_bytes(8,'big')+key+raw).digest())
        digest=hashlib.sha256(b''.join(sorted(digests))).hexdigest()
        event(domain,'storage_rows',phase,project_id=project_id,sha256=digest,
            count=len(digests),size_bytes=size,durable=False,scope=scope)
        trace.record('library.storage_fingerprint.duration',domain=domain,count=len(digests),
                     size_bytes=size,duration_ms=(time.perf_counter()-started)*1000)
    except Exception:
        event(domain,'storage_rows','failed',reason='diagnostic_fingerprint_failed')


def install_failure_hooks():
    """Desktop startup only. Preserve host hooks; never install at import."""
    import sys
    import logging
    previous_thread=threading.excepthook
    previous_unraisable=sys.unraisablehook
    if getattr(previous_thread,'_prism_library_hook',False):return lambda:None
    def report(stage_name,error):
        try:
            fields=_safe(exception_fields(error))
            event('runtime',stage_name,'failed',**fields)
            logging.getLogger('prism.library_diagnostics').error(
                '%s: %s at %s: %s',stage_name,fields.get('error_type'),
                fields.get('error_site'),fields.get('reason'))
        except Exception:pass
    def thread_hook(arguments):
        try:report('uncaught_thread',arguments.exc_value)
        finally:previous_thread(arguments)
    thread_hook._prism_library_hook=True
    def unraisable_hook(arguments):
        try:report('unraisable',arguments.exc_value or RuntimeError('Unraisable exception without value'))
        finally:previous_unraisable(arguments)
    threading.excepthook=thread_hook
    sys.unraisablehook=unraisable_hook
    def restore():
        if threading.excepthook is thread_hook:threading.excepthook=previous_thread
        if sys.unraisablehook is unraisable_hook:sys.unraisablehook=previous_unraisable
    return restore


def snapshot():
    with _lock:
        now=time.perf_counter()
        distributions={}
        for (domain,name),values in _durations.items():
            ordered=sorted(values)
            def percentile(p):return ordered[min(len(ordered)-1,round((len(ordered)-1)*p))]
            distributions[domain+'.'+name]=dict(samples=len(ordered),p50_ms=percentile(.5),
                p95_ms=percentile(.95),p99_ms=percentile(.99),max_ms=max(ordered))
        return dict(schema='library_evidence/1',writer_active=trace._active is not None,
            trace_health=trace.trace_stats(),
            mode=trace.mode(),counts={'.'.join(k):v for k,v in _counts.items()},
            recent=list(_recent),active=[dict(v,age_ms=(now-v['started'])*1000) for v in _active.values()],
            timings=distributions,diagnostic_failures=_failures,
            bounds=dict(recent=128,active=64,timing_stages=96,samples_per_stage=128),
            limitations=['In-memory, process-local observed boundaries; not proof of bytes saved or displayed.',
                'Quantiles describe the last at most 128 calls; not a long-term performance baseline.',
                'Off mode, queue loss and log rotation may leave gaps; inspect the trace loss counters.',
                'GPU memory and exact disk I/O attribution require external profiling.'])
