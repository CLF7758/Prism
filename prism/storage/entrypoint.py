"""Small versioned .prism launcher; original bytes live beside it.

Publication is one exclusive file link, never a rename of an open directory.
The SQLite version/application id also make old writers refuse this file.
"""
from contextlib import closing
import os
from pathlib import Path
import sqlite3
import tempfile

from .contracts import StorageError, StorageErrorKind

APPLICATION_ID=0x50524c31
VERSION=5


def display_project_name(path):
    """UI label only: no disk/SQLite reads while painting menus or titles."""
    path = Path(path)
    name = path.parent.name if path.name.lower() in ('metadata.sqlite','project.json') else path.name
    for suffix in ('.prism.data','.prismproj','.prism'):
        if name.lower().endswith(suffix):
            return name[:-len(suffix)]
    return name


def is_entry(path):
    path=Path(path)
    if path.suffix.lower()!='.prism' or not path.is_file():return False
    with path.open('rb') as stream:header=stream.read(100)
    return (header[:16]==b'SQLite format 3\0' and len(header)==100
            and int.from_bytes(header[68:72],'big')==APPLICATION_ID)


def resolve(path):
    path=Path(path).resolve()
    with closing(sqlite3.connect(path.as_uri()+'?mode=ro',uri=True)) as connection:
        version=connection.execute('PRAGMA user_version').fetchone()[0]
        if version!=VERSION:raise StorageError(StorageErrorKind.UNSUPPORTED_VERSION,'Unsupported Prism entry version')
        row=connection.execute('SELECT directory,instance FROM prism_entry WHERE id=1').fetchone()
    if row is None or row[0]!=path.name+'.data':
        raise StorageError(StorageErrorKind.CORRUPT,'Invalid project data directory binding')
    folder=path.parent/row[0]
    from .migration import read_manifest
    value=read_manifest(folder)
    if value.get('storage_instance_id')!=row[1]:
        raise StorageError(StorageErrorKind.CORRUPT,'Project entry and data belong to different projects')
    return folder


def publish(folder,entry):
    folder,entry=Path(folder).resolve(),Path(entry).resolve()
    if entry.exists():raise FileExistsError(entry)
    if folder!=entry.with_name(entry.name+'.data'):
        raise ValueError('Project data must be the entry filename followed by .data')
    from .migration import read_manifest
    value=read_manifest(folder)
    fd,name=tempfile.mkstemp(prefix=entry.name+'.',suffix='.entry-partial',dir=entry.parent)
    os.close(fd);temporary=Path(name)
    try:
        with closing(sqlite3.connect(temporary)) as connection:
            connection.execute('PRAGMA synchronous=FULL')
            connection.execute(f'PRAGMA application_id={APPLICATION_ID}')
            connection.execute(f'PRAGMA user_version={VERSION}')
            connection.execute('CREATE TABLE prism_entry(id INTEGER PRIMARY KEY CHECK(id=1),directory TEXT NOT NULL,instance TEXT NOT NULL)')
            connection.execute('INSERT INTO prism_entry VALUES(1,?,?)',(folder.name,value['storage_instance_id']))
            connection.commit()
        with temporary.open('r+b') as stream:os.fsync(stream.fileno())
        os.link(temporary,entry)  # Atomic no-overwrite publication on the same volume.
    finally:temporary.unlink(missing_ok=True)
    return entry


def recent_path(folder):
    folder=Path(folder)
    if folder.name.lower().endswith('.prism.data'):
        entry=folder.with_name(folder.name[:-5])
        if is_entry(entry) and resolve(entry)==folder:return entry
    return folder


def ensure_entry(folder):
    """Restore only a verified writable project's small launcher, not its data.

    Interrupted conversion checkpoints are not launchable projects. Existing
    files are never overwritten, including an old-format .prism original.
    """
    folder = Path(folder).resolve()
    if not folder.is_dir() or not folder.name.lower().endswith('.prism.data'):
        raise ValueError('Select the matching .prism.data project directory')
    from .migration import read_manifest
    try:
        value = read_manifest(folder)
    except StorageError as error:
        if error.kind is StorageErrorKind.NOT_LOADED:
            raise StorageError(StorageErrorKind.NOT_LOADED,
                '转换尚未完成，尚不能生成启动入口。请继续转换或使用工程恢复；旧原件保持不变。') from error
        raise
    if value.get('version') != 2 or value.get('phase') != 'verified':
        raise StorageError(StorageErrorKind.NOT_LOADED,
                           'Conversion is not complete. Resume conversion or use project recovery first.')
    entry = folder.with_name(folder.name[:-5])
    if entry.exists():
        if is_entry(entry) and resolve(entry) == folder:
            return entry
        raise FileExistsError('An existing file occupies the project entry path; it was not overwritten: '+str(entry))
    return publish(folder, entry)


def save_as_entry(path):
    """Normalize the user-facing Save As name; the writer owns .data creation."""
    path = Path(path)
    if path.name.lower().endswith('.prism.data'):
        path = path.with_name(path.name[:-5])
    elif path.suffix.lower() == '.prismproj':
        path = path.with_suffix('.prism')
    elif path.suffix.lower() != '.prism':
        path = path.with_name(path.name+'.prism')
    return path


def convert(source,entry,*,cancel=None,progress=None):
    entry=Path(entry).resolve()
    if entry.exists():raise FileExistsError(entry)
    from .migration import convert_legacy,_check
    folder=entry.with_name(entry.name+'.data')
    if folder.exists():
        import json
        value=json.loads((folder/'project.json').read_text(encoding='utf-8'))
        if value.get('version')==2:
            raise StorageError(StorageErrorKind.SOURCE_BUSY,
                               'Verified data already exists. Use project recovery to publish its missing entry, or choose a new name.')
    convert_legacy(source,folder,experimental=True,managed=True,cancel=cancel,
                   progress=progress,publish_directory=False)
    _check(cancel)
    return publish(folder,entry)
