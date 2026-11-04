"""Existing save-operation adapter for directory projects."""
from dataclasses import replace
from pathlib import Path
import json
import shutil
import tempfile
import uuid

from .managed import (root_for, database_path, rebase_snapshot, commit_partial,
                      STATE)
from .migration import convert_legacy, _manifest, read_manifest, _check
from .leases import exclusive_destination, project_lease
from .contracts import StorageError, StorageErrorKind


def _copy_directory(source, destination, prepare, cancel,*,direct=False):
    import sqlite3
    from contextlib import closing
    source, destination = Path(source), Path(destination)
    if destination.exists() or destination.is_relative_to(source):
        raise ValueError('Save As needs a new directory outside the source project')
    staging = destination if direct else destination.with_name(destination.name+'.incomplete')
    with exclusive_destination(destination), exclusive_destination(source/'metadata.sqlite'), project_lease(source):
        _check(cancel)
        if staging.exists():
            raise FileExistsError('A previous Save As checkpoint exists: '+str(staging))
        def copy(src, dst):
            _check(cancel)
            return shutil.copy2(src, dst)
        shutil.copytree(source, staging, copy_function=copy,
                        ignore=shutil.ignore_patterns('*.lock', '*-wal', '*-shm', '*-journal'))
        (staging/'.prism-storage.lock').write_bytes(b'\0')
        value=read_manifest(staging)
        value['storage_instance_id']=uuid.uuid4().hex
        with closing(sqlite3.connect(staging/'metadata.sqlite')) as connection:
            connection.execute(f'UPDATE {STATE} SET instance=?,revision=0 WHERE id=1', (value['storage_instance_id'],))
            connection.commit()
        _manifest(staging/'project.json',value)
        prepare(staging/'metadata.sqlite')
        _check(cancel)
        if not direct:staging.rename(destination)


def save_directory(filename, scene, *, worker=None, board_snapshot=None, partial_snapshot=None):
    from prism.fileio.partial_save import PartialSaveReceipt
    from prism.partial_project import PartialProjectSession
    from prism.fileio.sql import SQLiteIO
    cancel=(lambda: worker.canceled) if worker is not None else None
    target=Path(filename).resolve()
    entry=(target.with_name(target.name[:-5]) if target.name.lower().endswith('.prism.data') else None)
    if entry is not None and entry.exists():raise FileExistsError(entry)
    from prism.fileio.sql import is_readonly_project
    if is_readonly_project(scene,str(target)):
        raise StorageError(StorageErrorKind.READ_ONLY,'工程以只读方式打开，不能写回')
    receipt=None
    def prepare(metadata):
        nonlocal receipt
        if partial_snapshot is not None:
            receipt=commit_partial(rebase_snapshot(partial_snapshot, metadata), metadata, cancel=cancel)
    if partial_snapshot is not None:
        source_folder=root_for(partial_snapshot.source)
        if target.name=='metadata.sqlite' and source_folder==target.parent:
            receipt=commit_partial(partial_snapshot, target, cancel=cancel,
                commit_report=(lambda state,reason='',**kw: setattr(worker,'save_write_result',
                    dict(commit_state=state,reason=reason,**kw))) if worker is not None else None)
            metadata=target
        else:
            if source_folder is not None:
                _copy_directory(source_folder,target,prepare,cancel,direct=entry is not None)
            else:
                convert_legacy(partial_snapshot.source,target,experimental=True,managed=True,
                               cancel=cancel,prepare=prepare,publish_directory=entry is None)
            metadata=target/'metadata.sqlite'
            receipt=replace(receipt,filename=str(metadata),source_before=None,source_after=None)
    else:
        # First save of a new scene. The old serializer creates a private
        # legacy snapshot once; it is never the user's final storage file.
        class Signal:
            def __init__(self): self.errors=[]
            def emit(self,*args):
                if len(args)>1: self.errors=list(args[1])
        class PhaseWorker:
            def __init__(self,outer):
                self.outer=outer;self.finished=Signal();self.commit_started=Signal();self.cancel_too_late=Signal()
                self.begin_processing=Signal();self.progress=Signal()
            def __getattr__(self,name):
                if self.outer is not None:return getattr(self.outer,name)
                if name=='canceled':return False
                raise AttributeError(name)
        phase=PhaseWorker(worker)
        with tempfile.TemporaryDirectory(prefix='prism-first-save-') as temporary:
            legacy=Path(temporary)/'snapshot.prism'
            io=SQLiteIO(str(legacy),scene,create_new=True,worker=phase if worker is not None else None,
                        board_snapshot=board_snapshot)
            try:
                io.write()
                if phase.finished.errors:raise RuntimeError('; '.join(phase.finished.errors))
            finally:io._close_connection()
            convert_legacy(legacy,target,experimental=True,managed=True,cancel=cancel,
                           publish_directory=entry is None)
            metadata=target/'metadata.sqlite'
            if worker is not None:
                worker.managed_rebind=(str(legacy),str(metadata))
        if worker is not None:
            state=board_snapshot or scene
            worker.managed_captured_items=list(scene.items_for_save())
            ids=tuple(item.save_id for item in scene.items_for_save())
            pages=tuple(page['id'] for page in getattr(state,'workspace_pages',()))
            worker.managed_session=PartialProjectSession.open(metadata,item_ids=ids,page_ids=pages,
                pages_complete=True,include_domains=True,attachments=True)
    if entry is not None:
        _check(cancel)
        from .entrypoint import publish
        publish(target,entry)
    if worker is not None:
        if receipt is not None:worker.partial_save_receipt=receipt
        worker.save_write_result=dict(commit_state='committed_complete',cancelled=False,
            changed_entities=None,changed_entities_complete=False,
            cancel_too_late=bool(worker.canceled),
            changed_row_count=receipt.changed_rows if receipt is not None else None)
        worker.finished.emit(str(metadata),[])
    return receipt


def save_legacy_copy(snapshot, filename, *, cancel=None, commit_report=None):
    from .legacy_export import export_legacy
    from prism.fileio.partial_save import _write_partial_snapshot
    import os
    target=Path(filename).resolve()
    target.parent.mkdir(parents=True,exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='prism-legacy-export-',dir=target.parent) as temporary:
        exported=Path(temporary)/'copy.prism'
        export_legacy(root_for(snapshot.source),exported,experimental=True,cancel=cancel)
        receipt=_write_partial_snapshot(rebase_snapshot(snapshot,exported),exported,cancel=cancel)
        _check(cancel)
        os.replace(exported,target)
        if commit_report:commit_report('committed_complete','Legacy copy published')
        return replace(receipt,filename=str(target),source_before=None,source_after=None)

