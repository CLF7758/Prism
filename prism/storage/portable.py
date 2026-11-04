"""Lossless portable ZIP and coordinated pack maintenance."""
import os
from pathlib import Path
import shutil
import sqlite3
import tempfile
import zipfile
import uuid

from .contracts import StorageError, StorageErrorKind
from .managed import root_for
from .migration import read_manifest, _check
from .leases import project_lease, exclusive_destination


def compact(folder, *, cancel=None):
    folder=Path(folder).resolve()
    read_manifest(folder)
    from .dos_repository import DosObjectRepository
    with exclusive_destination(folder/'metadata.sqlite'), project_lease(folder,exclusive=True):
        _check(cancel)
        repository=DosObjectRepository(folder/'objects')
        try:
            # Reversible object compression; never an image/video encoder.
            repository.compact_loose(check=lambda: _check(cancel))
        finally:repository.close()


def export_bundle(folder, destination, *, cancel=None):
    folder,destination=Path(folder).resolve(),Path(destination).resolve()
    if destination.exists() or destination.is_relative_to(folder):raise FileExistsError(destination)
    read_manifest(folder)
    destination.parent.mkdir(parents=True,exist_ok=True)
    fd,name=tempfile.mkstemp(prefix=destination.name+'.',suffix='.partial',dir=destination.parent);os.close(fd)
    temporary=Path(name)
    try:
        with exclusive_destination(folder/'metadata.sqlite'),project_lease(folder):
            with zipfile.ZipFile(temporary,'w',compression=zipfile.ZIP_DEFLATED,compresslevel=6,allowZip64=True) as archive:
                for path in folder.rglob('*'):
                    _check(cancel)
                    if not path.is_file() or path.name.endswith(('.lock','-wal','-shm','-journal')):continue
                    if path.is_symlink():raise ValueError('External symlinks cannot be included in a portable project')
                    archive.write(path,path.relative_to(folder).as_posix())
        _check(cancel)
        with temporary.open('r+b') as handle:os.fsync(handle.fileno())
        os.link(temporary,destination)
    finally:temporary.unlink(missing_ok=True)


def open_bundle(source,destination,*,cancel=None):
    source,destination=Path(source).resolve(),Path(destination).resolve()
    if destination.exists():raise FileExistsError(destination)
    staging=destination.with_name(destination.name+'.incomplete')
    if staging.exists():raise FileExistsError(staging)
    with exclusive_destination(destination),zipfile.ZipFile(source) as archive:
        names=set()
        for info in archive.infolist():
            name=info.filename.replace('\\','/')
            parts=name.rstrip('/').split('/')
            if (name.startswith('/') or ':' in name or '..' in Path(name).parts or
                name.casefold() in names or any(not p or p in ('.','..') or p.rstrip(' .')!=p for p in parts) or
                any(p.split('.')[0].upper() in {'CON','PRN','AUX','NUL',*[f'COM{i}' for i in range(1,10)],*[f'LPT{i}' for i in range(1,10)]} for p in parts) or
                ((info.external_attr>>16)&0o170000)==0o120000):
                raise ValueError('Unsafe or duplicate portable project entry')
            names.add(name.casefold())
        if sum(info.file_size for info in archive.infolist())>shutil.disk_usage(destination.parent).free:
            raise StorageError(StorageErrorKind.INSUFFICIENT_SPACE,'Not enough space to open the portable project')
        staging.mkdir()
        for info in archive.infolist():
            _check(cancel)
            target=staging/info.filename.replace('\\','/')
            if info.is_dir():target.mkdir(parents=True,exist_ok=True);continue
            target.parent.mkdir(parents=True,exist_ok=True)
            with archive.open(info) as src,target.open('xb') as dst:shutil.copyfileobj(src,dst,256*1024)
        (staging/'.prism-storage.lock').write_bytes(b'\0')
        value=read_manifest(staging)
        # Validate every referenced original before presenting the project.
        from .managed import repository_reader
        from contextlib import closing
        with closing(sqlite3.connect((staging/'metadata.sqlite').as_uri()+'?mode=ro',uri=True)) as connection:
            with repository_reader(staging) as repository:
                for key,length in connection.execute('SELECT DISTINCT hashkey,size FROM object_refs'):
                    _check(cancel)
                    result=repository.verify(key)
                    if not result.ok or result.length!=length:
                        raise StorageError(StorageErrorKind.CORRUPT,'Portable original failed verification')
        if value['version']==2:
            value['storage_instance_id']=uuid.uuid4().hex
            from .managed import STATE
            from .migration import _manifest
            with closing(sqlite3.connect(staging/'metadata.sqlite')) as connection:
                connection.execute(f'UPDATE {STATE} SET instance=? WHERE id=1',(value['storage_instance_id'],));connection.commit()
            _manifest(staging/'project.json',value)
        else:
            from .managed import initialize
            initialize(staging)
        staging.rename(destination)
    return destination
