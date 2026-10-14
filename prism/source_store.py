"""Original storage: import snapshots, read-only archive access and size metadata."""
from pathlib import Path
import os
import shutil
import sqlite3
import tempfile
import threading
import uuid

_directory = None
_lock = threading.Lock()


def retain_file(filename):
    global _directory
    with _lock:
        if _directory is None:
            _directory = tempfile.TemporaryDirectory(prefix='prism-originals-')
        target = Path(_directory.name) / (uuid.uuid4().hex + Path(filename).suffix)
    shutil.copyfile(filename, target)
    return str(target)


def retain_bytes(data, suffix='.png'):
    global _directory
    with _lock:
        if _directory is None:
            _directory = tempfile.TemporaryDirectory(prefix='prism-originals-')
        target = Path(_directory.name) / (uuid.uuid4().hex + suffix)
    target.write_bytes(data)
    return str(target)


def read_archive(archive):
    """Read original bytes with a thread-local, read-only connection."""
    filename, item_id = archive
    connection = sqlite3.connect(Path(filename).resolve().as_uri() + '?mode=ro', uri=True)
    try:
        row = connection.execute('SELECT data FROM sqlar WHERE item_id = ?',
                                 (item_id,)).fetchone()
        return row[0] if row else None
    finally:
        connection.close()


class ArchiveReadSession:
    """Bounded, worker-local read-only connections for one batch operation."""
    def __init__(self):
        import threading
        self._local = threading.local()
        self._lock = threading.Lock()
        self._connections = []

    def read(self, archive):
        from collections import OrderedDict
        filename, item_id = archive
        if not hasattr(self._local, 'archives'):
            self._local.archives = OrderedDict()
        archives = self._local.archives
        connection = archives.get(filename)
        if connection is None:
            connection = sqlite3.connect(Path(filename).resolve().as_uri()+'?mode=ro',
                                         uri=True, check_same_thread=False)
            archives[filename] = connection
            with self._lock:
                self._connections.append(connection)
            while len(archives) > 2:
                _, oldest = archives.popitem(last=False)
                oldest.close()
                with self._lock:
                    self._connections.remove(oldest)
        else:
            archives.move_to_end(filename)
        cursor = connection.execute('SELECT data FROM sqlar WHERE item_id=?', (item_id,))
        try:
            row = cursor.fetchone()
            return row[0] if row else None
        finally:
            cursor.close()

    def close(self):
        with self._lock:
            for connection in self._connections:
                connection.close()
            self._connections.clear()


def storage_bytes(items, cache):
    """Read stored byte counts without decoding originals; cache archive metadata."""
    total = 0
    archive_keys = {}
    for item in items:
        if isinstance(item, dict):
            continue
        source = getattr(item, '_prism_source', None) or getattr(item, '_asset_source', None)
        try:
            if source:
                if source[0] not in archive_keys:
                    stat = os.stat(source[0])
                    archive_keys[source[0]] = (source[0], stat.st_mtime_ns, stat.st_size)
                key = archive_keys[source[0]]
                if key not in cache:
                    archive = sqlite3.connect(Path(source[0]).resolve().as_uri() + '?mode=ro', uri=True)
                    try:
                        sizes = dict(archive.execute('SELECT item_id, COALESCE(sz,length(data)) FROM sqlar'))
                    finally:
                        archive.close()
                    for previous in [previous for previous in cache if previous[0] == source[0]]:
                        del cache[previous]
                    cache[key] = sizes
                size = cache[key].get(source[1])
                if size is None:
                    return None
                total += size
            elif getattr(item, '_source_blob', None):
                total += len(item._source_blob)
            elif os.path.isfile(getattr(item, '_source_path', '') or getattr(item, 'filename', '') or ''):
                total += os.path.getsize(getattr(item, '_source_path', '') or item.filename)
            else:
                return None
        except (OSError, sqlite3.Error):
            return None
    return total

