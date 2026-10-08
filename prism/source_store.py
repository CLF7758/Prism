"""Disk snapshots of unsaved imports, without keeping originals in RAM."""
from pathlib import Path
import shutil
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
