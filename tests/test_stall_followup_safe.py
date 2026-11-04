"""No-window regression for verified catalog/detail/install scheduling defects."""
import ast
from contextlib import closing
import os
from pathlib import Path
import sqlite3
import threading
from types import MethodType, SimpleNamespace
from unittest.mock import Mock

import pytest
from PyQt6 import QtCore
from prism import project_catalog_index as catalog
from prism.widgets.model_library import ModelLibrary
from prism.model_library.selection import QueryIdentity
from prism.widgets.asset_browser import AssetBrowser

ROOT = Path('C:/Users/admin/Desktop/Prism-main/Prism-main')


def test_tag_aggregate_does_not_publish_if_file_signature_changes(monkeypatch):
    db = sqlite3.connect(':memory:')
    db.execute('CREATE TABLE assets(canvas_id TEXT,tags TEXT,kind TEXT,deleted_at TEXT)')
    db.executemany('INSERT INTO assets VALUES(?,?,?,NULL)', [('A','["tag"]','pixmap')]*1000)
    state = {'revision': 1}
    class Connection:
        def execute(self, *args):
            cursor = db.execute(*args)
            if args[0].startswith('SELECT canvas_id,tags'):
                state['revision'] = 2
            return cursor
        def close(self):
            pass
        def set_progress_handler(self, *args):
            db.set_progress_handler(*args)
    index = catalog.ProjectCatalogIndex.__new__(catalog.ProjectCatalogIndex)
    index.source = Path('memory-source')
    index.cache = Path('memory-cache')
    index._cache = lambda **kwargs: Connection()
    index._ready = lambda connection: None
    monkeypatch.setattr(catalog, '_fingerprint', lambda path: {'revision':state['revision']})
    try:
        with pytest.raises(catalog.CatalogNotReady):
            index.tag_groups()
        assert getattr(index, '_tag_groups_cache', None) is None
    finally:
        db.close()


def test_tag_aggregate_reuses_only_same_signature_and_cancellation(monkeypatch):
    db = sqlite3.connect(':memory:')
    db.execute('CREATE TABLE assets(canvas_id TEXT,tags TEXT,kind TEXT,deleted_at TEXT)')
    db.executemany('INSERT INTO assets VALUES(?,?,?,?)',[
        ('A','["one","one/child"]','pixmap',None),('A','["one"]','pixmap',None),
        ('A','["one"]','text',None),('B','["gone"]','pixmap','deleted')])
    trace = []
    db.set_trace_callback(trace.append)
    class Connection:
        def execute(self, *args):
            return db.execute(*args)
        def set_progress_handler(self,*args):
            db.set_progress_handler(*args)
        def close(self):
            pass
    index = catalog.ProjectCatalogIndex.__new__(catalog.ProjectCatalogIndex)
    index.source,index.cache = Path('source'),Path('cache')
    index._cache=lambda **kw:Connection()
    index._ready=lambda connection:None
    revision={'value':1}
    monkeypatch.setattr(catalog,'_fingerprint',lambda path: {'revision':revision['value']})
    try:
        first=index.tag_groups(cancel=lambda:False)
        assert len(first)==2
        assert index.tag_groups(cancel=lambda:False) is first
        assert len([q for q in trace if q.startswith('SELECT canvas_id,tags')])==1
        revision['value']=2
        second=index.tag_groups()
        assert second==first and second is not first
        with pytest.raises(catalog.CatalogBuildCancelled):
            index.tag_groups(cancel=lambda:True)
    finally:
        db.close()


class FakePool:
    def __init__(self):
        self.tasks=[]
    def start(self, task):
        self.tasks.append(task)


def detail_fixture():
    class Details:
        _record=None
        _pending_tags=_pending_notes=None
        def __init__(self):
            self.shown=[]
            self.material_panel=SimpleNamespace(target=lambda:None,reset=Mock())
        def show_summary(self, summary):
            self._record=None
        def show_record(self, record, cover, capabilities):
            self._record=record
            self.shown.append(record)
        def set_capabilities(self,*args):
            pass
        def _show_cover(self, cover):
            pass
    calls=[]
    def getter(model):
        calls.append((model,threading.get_ident()))
        return SimpleNamespace(model_id=model)
    class Receiver(QtCore.QObject):
        @QtCore.pyqtSlot(object)
        def _record_read_finished(self, outcome):
            ModelLibrary._record_read_finished(self, outcome)
    sink=Receiver()
    values=SimpleNamespace(service=SimpleNamespace(get=getter,appearance_slots=None),
        _summaries={'A':SimpleNamespace(thumbnail_data_url='coverA'),
                    'B':SimpleNamespace(thumbnail_data_url='coverB')},
        selected_id='A',selection=SimpleNamespace(current='A'),_scope_token='s#1',
        _identity=QueryIdentity(revision=1),_pending_appearance=None,
        _workers={},_pool=FakePool(),_record_worker=None,details=Details())
    sink.__dict__.update(vars(values))
    sink._ensure_pool=lambda:None
    sink._effective_capabilities=lambda:None
    for name in ('_record_getter','_load_record','_show_loaded_record','_has_draft'):
        method=getattr(ModelLibrary,name,None)
        if method is not None:setattr(sink,name,MethodType(method,sink))
    return sink,calls


def test_selected_record_read_never_runs_on_gui_and_deduplicates():
    QtCore.QCoreApplication.instance() or QtCore.QCoreApplication([])
    sink,calls=detail_fixture()
    sink._load_record('A')
    sink._load_record('A')
    assert calls==[], 'metadata path resolve / SQL must not execute in input handler'
    assert len(sink._pool.tasks)==1
    task=sink._pool.tasks[0]
    caller=threading.get_ident()
    thread=threading.Thread(target=task.run)
    thread.start();thread.join(5)
    assert not thread.is_alive()
    assert calls==[('A',thread.ident)] and thread.ident!=caller


def test_old_record_result_never_installs_into_new_selection_or_scope():
    sink,calls=detail_fixture()
    sink._load_record('A')
    assert calls==[]
    worker=sink._pool.tasks[-1]
    sink.selected_id='B';sink.selection.current='B'
    sink._record_read_finished((worker,worker.token,worker.identity,SimpleNamespace(model_id='A'),None))
    assert sink.details.shown==[]
    sink.selected_id='A';sink.selection.current='A'
    sink._scope_token='different#2'
    sink._record_read_finished((worker,worker.token,worker.identity,SimpleNamespace(model_id='A'),None))
    assert sink.details.shown==[]


def test_record_draft_is_not_replaced_by_late_result():
    sink,calls=detail_fixture()
    sink._load_record('A')
    assert calls==[]
    worker=sink._pool.tasks[-1]
    draft=SimpleNamespace(model_id='A')
    sink.details._record=draft;sink.details._pending_notes='draft'
    sink._record_read_finished((worker,worker.token,worker.identity,SimpleNamespace(model_id='A'),None))
    assert sink.details._record is draft and sink.details.shown==[]


def test_install_repeated_requests_queue_one_callback(monkeypatch):
    timers=[]
    monkeypatch.setattr(QtCore.QTimer,'singleShot',lambda delay,callback:timers.append(callback))
    pending={}
    sink=SimpleNamespace(_catalog_pending=pending,_catalog_install_valid=lambda p:p is pending,
        _maybe_publish_grid_preview=Mock(),_catalog_install_progress=Mock(),
        _install_catalog_rows=Mock())
    sink._queue_catalog_install=MethodType(AssetBrowser._queue_catalog_install,sink)
    sink._queue_catalog_install(pending)
    sink._queue_catalog_install(pending)
    assert len(timers)==1
    timers[0]()
    assert sink._install_catalog_rows.call_count==1
    sink._queue_catalog_install(pending)
    assert len(timers)==2


def test_catalog_scope_instance_reuses_current_scope_and_warmed_index(monkeypatch):
    from prism.project_open_scope import ProjectOpenScope
    import sys
    tree=ast.parse((ROOT/'prism/view.py').read_text(encoding='utf-8'))
    owner=next(n for n in tree.body if isinstance(n,ast.ClassDef) and n.name=='PrismGraphicsView')
    method=next(n for n in owner.body if isinstance(n,ast.FunctionDef) and n.name=='_catalog_scope_instance')
    method.decorator_list=[]
    ns={'fileio':SimpleNamespace(is_prism_file=lambda f:True),'os':os,
        '_catalog_cache_path':lambda f:'cache.sqlite','logger':Mock(),
        'trace_record':Mock(),'_':lambda s:s}
    exec(compile(ast.fix_missing_locations(ast.Module(body=[method],type_ignores=[])),'actual-view-method','exec'),ns)
    class Scope:
        def __init__(self,source,cache):
            self.project_path=source;self.cache_path=cache
            self.index=SimpleNamespace(source=source,cache=cache)
            self._telemetry_cache='old'
            self.ready=True
    monkeypatch.setattr(sys.modules[ProjectOpenScope.__module__],'ProjectOpenScope',Scope)
    sink=SimpleNamespace(_open_scope=None)
    method=ns['_catalog_scope_instance']
    first=method(sink,'source.prism')
    assert method(sink,'source.prism') is first
    warmed=SimpleNamespace(source='source.prism',cache='cache.sqlite')
    assert method(sink,'source.prism',cache_path='cache.sqlite',index=warmed) is first
    assert first.index is warmed
    new=method(sink,'different.prism',cache_path='cache.sqlite')
    assert new is not first and new.index is not warmed


def test_loading_summary_disables_metadata_even_for_writable_service():
    from prism.widgets.model_details import ModelDetails
    from prism.model_library.contracts import ModelCapabilities
    sink=SimpleNamespace(_record=None,_multimaterial_owned=False,slots={},
        title=Mock(),tags=Mock(),notes=Mock(),slot_notice=Mock(),apply_button=Mock())
    caps=ModelCapabilities(edit_metadata=True)
    ModelDetails.set_capabilities(sink,caps)
    sink.title.setReadOnly.assert_called_with(True)
    sink.tags.setReadOnly.assert_called_with(True)
    sink.notes.setReadOnly.assert_called_with(True)
    sink._record=SimpleNamespace(model_id='A')
    ModelDetails.set_capabilities(sink,caps)
    sink.title.setReadOnly.assert_called_with(False)
    sink.tags.setReadOnly.assert_called_with(False)
    sink.notes.setReadOnly.assert_called_with(False)


def test_install_checkpoint_never_keeps_a_live_timer_flag():
    received=[]
    sink=SimpleNamespace(_catalog_match_context=None,
        _catalog_results=SimpleNamespace(put_pending=lambda digest,p:received.append(p)))
    pending={'requested_digest':'query','install_queued':True,'offset':3}
    AssetBrowser._remember_catalog_install(sink,pending)
    assert received[0]['offset']==3
    assert 'install_queued' not in received[0]
    assert pending['install_queued'] is True


def test_install_queue_can_retry_after_publication_exception(monkeypatch):
    timers=[]
    monkeypatch.setattr(QtCore.QTimer,'singleShot',lambda delay,callback:timers.append(callback))
    pending={}
    sink=SimpleNamespace(_catalog_pending=pending,_catalog_install_valid=lambda p:p is pending,
        _maybe_publish_grid_preview=Mock(side_effect=ValueError('injected failure')),
        _catalog_install_progress=Mock(),_install_catalog_rows=Mock())
    with pytest.raises(ValueError):
        AssetBrowser._queue_catalog_install(sink,pending)
    assert pending['install_queued'] is False
    sink._maybe_publish_grid_preview=Mock()
    AssetBrowser._queue_catalog_install(sink,pending)
    assert len(timers)==1


def test_scope_forwards_cancellation_to_tag_aggregation():
    from prism.project_open_scope import ProjectOpenScope
    cancel=lambda:False
    sink=SimpleNamespace(_check_cancel=Mock(),index=SimpleNamespace(tag_groups=Mock(return_value=())))
    assert ProjectOpenScope.tag_groups(sink,cancel=cancel)==()
    sink.index.tag_groups.assert_called_once_with(cancel=cancel)


def test_record_receipt_installs_on_core_main_thread_without_widgets():
    import time
    app=QtCore.QCoreApplication.instance() or QtCore.QCoreApplication([])
    assert not hasattr(app,'topLevelWidgets')
    sink,calls=detail_fixture()
    main=threading.get_ident()
    installed=[]
    original=sink._show_loaded_record
    sink._show_loaded_record=lambda record,summary:(installed.append(threading.get_ident()),original(record,summary))
    sink._load_record('A')
    worker=sink._pool.tasks[-1]
    thread=threading.Thread(target=worker.run)
    thread.start();thread.join(5)
    deadline=time.monotonic()+3
    while not installed and time.monotonic()<deadline:
        app.processEvents(QtCore.QEventLoop.ProcessEventsFlag.AllEvents,10)
    assert installed==[main]
    assert calls[0][1]!=main
    assert sink.details.shown[0].model_id=='A'


def test_old_service_and_query_results_do_not_reenable_details():
    sink,calls=detail_fixture()
    sink._load_record('A')
    worker=sink._pool.tasks[-1]
    sink.service=SimpleNamespace(get=lambda x:None)
    sink._record_read_finished((worker,worker.token,worker.identity,SimpleNamespace(model_id='A'),None))
    assert sink.details.shown==[]
    sink._record_worker=worker;sink.service=worker.service
    sink._identity=QueryIdentity(revision=9)
    sink._record_read_finished((worker,worker.token,worker.identity,SimpleNamespace(model_id='A'),None))
    assert sink.details.shown==[]


def test_same_query_rapid_model_changes_only_accept_latest_result():
    sink,calls=detail_fixture()
    sink._load_record('A');old=sink._pool.tasks[-1]
    sink.selected_id='B';sink.selection.current='B'
    sink._load_record('B');latest=sink._pool.tasks[-1]
    sink._record_read_finished((old,old.token,old.identity,SimpleNamespace(model_id='A'),None))
    assert sink.details.shown==[]
    sink._record_read_finished((latest,latest.token,latest.identity,SimpleNamespace(model_id='B'),None))
    assert [r.model_id for r in sink.details.shown]==['B']


def _catalog_view_methods():
    tree=ast.parse((ROOT/'prism/view.py').read_text(encoding='utf-8'))
    owner=next(n for n in tree.body if isinstance(n,ast.ClassDef) and n.name=='PrismGraphicsView')
    names=('_catalog_build_received','_start_catalog_build','_catalog_scope_instance')
    methods=[n for n in owner.body if isinstance(n,ast.FunctionDef) and n.name in names]
    for method in methods:method.decorator_list=[]
    ns={'os':os,'threading':threading,'logger':Mock(),'trace_record':Mock(),
        '_':lambda text:text,'QtCore':QtCore,'Qt':QtCore.Qt,
        'trace_restore_context':lambda c:__import__('contextlib').nullcontext(),
        'trace_span':lambda *a,**kw:__import__('contextlib').nullcontext(),
        'fileio':SimpleNamespace(is_prism_file=lambda f:True),
        '_catalog_cache_path':lambda f:'cache.sqlite'}
    exec(compile(ast.fix_missing_locations(ast.Module(body=methods,type_ignores=[])),
                 'actual-catalog-view-methods','exec'),ns)
    return ns


def test_worker_preheats_final_index_before_delivering_completion(monkeypatch):
    ns=_catalog_view_methods()
    order=[]
    first=SimpleNamespace(apply_save_receipts=lambda *a,**kw:None)
    final=SimpleNamespace(tag_groups=lambda **kw:order.append(('warm',threading.get_ident())))
    result=SimpleNamespace(complete=True,total_records=10,seconds=0,
                           lock_wait_seconds=0,resumed=False,processed_records=10)
    request={'index':first,'catalog_deltas':(),'cancel':threading.Event(),
             'build_id':'build','reason':'test','project':'source.prism','cache':'cache.sqlite',
             'context':None}
    def rebuild(view,req,recovery):
        req['index']=final
        return result
    fake=SimpleNamespace(_build_catalog_with_retry=rebuild)
    ns['PrismGraphicsView']=fake
    class Delivery:
        def emit(self,value):
            order.append(('deliver',value[1],value[0]['index']))
    sink=SimpleNamespace(_catalog_build_delivery=SimpleNamespace(delivered=Delivery()),
                         _catalog_status=lambda message:None)
    before=threading.get_ident()
    ns['_start_catalog_build'](sink,request)
    request['thread'].join(3)
    assert not request['thread'].is_alive()
    assert order[0][0]=='warm' and order[0][1]!=before
    assert order[1]==('deliver','complete',final)


def test_cancelled_terminal_does_not_install_or_notify():
    ns=_catalog_view_methods()
    installs=[]
    ns['PrismGraphicsView']=SimpleNamespace(_catalog_scope_instance=lambda *a,**kw:installs.append(kw),
                                           _start_catalog_build=Mock())
    cancel=threading.Event();cancel.set()
    called=[]
    request={'project_id':'p','epoch':1,'context':None,'build_id':'b','cancel':cancel,
             'callbacks':[lambda path:called.append(path)],'project':'source','cache':'cache','index':object()}
    sink=SimpleNamespace(_catalog_build_active=request,_catalog_build_epoch=1,
                         scene=SimpleNamespace(project_id='p'),_catalog_build_pending=None)
    ns['_catalog_build_received'](sink,(request,'complete',object(),None,None))
    assert installs==[] and called==[]


def test_source_changed_after_preheat_queues_existing_repair_and_preserves_subscribers():
    ns=_catalog_view_methods()
    repairs=[]
    request={'project_id':'p','epoch':1,'context':None,'build_id':'b','cancel':threading.Event(),
             'callbacks':[lambda path:None,lambda path:None],
             'project':'source','cache':'cache','index':object()}
    sink=SimpleNamespace(_catalog_build_active=request,_catalog_build_epoch=1,
         scene=SimpleNamespace(project_id='p'),_catalog_build_pending=None,
         _catalog_to_build={'project':'source','cache':'cache'},_catalog_status=Mock())
    def repair(view,source,on_ready):
        repairs.append(source)
        view._catalog_build_active={'callbacks':[on_ready]}
    ns['PrismGraphicsView']=SimpleNamespace(_catalog_scope_instance=lambda *a,**kw:SimpleNamespace(ready=False),
        _maybe_start_catalog_build=repair,_append_catalog_callback=lambda req,callback:req['callbacks'].append(callback))
    ns['_catalog_build_received'](sink,(request,'complete',object(),None,None))
    assert repairs==['source']
    assert sink._catalog_build_active['callbacks']==request['callbacks']


def test_scope_alias_reuses_preheated_canonical_index(monkeypatch):
    import sys
    from prism.project_open_scope import ProjectOpenScope
    ns=_catalog_view_methods()
    monkeypatch.setattr(os.path,'realpath',lambda p:os.path.abspath(str(p).replace('alias.prism','actual.prism')))
    class Scope:
        def __init__(self,source,cache):
            self.project_path=source;self.cache_path=cache
            self.index=SimpleNamespace(source=source,cache=cache)
            self.ready=True
    monkeypatch.setattr(sys.modules[ProjectOpenScope.__module__],'ProjectOpenScope',Scope)
    current=Scope('actual.prism','cache.sqlite')
    warmed=SimpleNamespace(source='actual.prism',cache='cache.sqlite')
    sink=SimpleNamespace(_open_scope=current)
    value=ns['_catalog_scope_instance'](sink,'alias.prism',cache_path='cache.sqlite',index=warmed)
    assert value is current and value.index is warmed
