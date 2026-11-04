"""Cooperating-process writer lease; OS releases it if a process crashes.

The small lock file is retained: unlinking a live lock creates an inode race.
This is not a pin against arbitrary third-party disk-objectstore maintenance.
"""
from contextlib import contextmanager, ExitStack
import hashlib
import tempfile
import os
import errno
from pathlib import Path

from .contracts import StorageError, StorageErrorKind


def _windows_lock(handle,exclusive):
    # CRT LK_NBRLCK is not a Windows shared LockFileEx lock. Independent
    # decoder handles consequently excluded one another even in this process.
    import ctypes
    from ctypes import wintypes as w
    import msvcrt
    class Overlapped(ctypes.Structure):
        _fields_=[('Internal',ctypes.c_size_t),('InternalHigh',ctypes.c_size_t),
                  ('Offset',w.DWORD),('OffsetHigh',w.DWORD),('hEvent',w.HANDLE)]
    kernel=ctypes.WinDLL('kernel32',use_last_error=True)
    kernel.LockFileEx.argtypes=[w.HANDLE,w.DWORD,w.DWORD,w.DWORD,w.DWORD,ctypes.POINTER(Overlapped)]
    kernel.UnlockFileEx.argtypes=[w.HANDLE,w.DWORD,w.DWORD,w.DWORD,ctypes.POINTER(Overlapped)]
    native=msvcrt.get_osfhandle(handle.fileno());state=Overlapped()
    if not kernel.LockFileEx(native,1|(2 if exclusive else 0),0,1,0,ctypes.byref(state)):
        raise ctypes.WinError(ctypes.get_last_error())
    def release():
        if not kernel.UnlockFileEx(native,0,1,0,ctypes.byref(state)):
            raise ctypes.WinError(ctypes.get_last_error())
    return release


@contextmanager
def exclusive_destination(destination):
    destination = Path(destination).resolve()
    # The destination directory does not exist during initial publication.
    # A stable application-owned lock also avoids littering its parent.
    base = Path(os.environ.get('LOCALAPPDATA') or os.environ.get('XDG_RUNTIME_DIR') or tempfile.gettempdir())
    key = hashlib.sha256(os.path.normcase(str(destination)).encode('utf-8')).hexdigest()
    path = base / 'Prism' / 'locks' / (key+'.lock')
    path.parent.mkdir(parents=True,exist_ok=True)
    legacy = destination.with_name('.' + destination.name + '.prism-convert.lock')
    with ExitStack() as stack:
        stack.enter_context(_exclusive_file(path))
        # Honor an existing older cooperating writer; never unlink a lock
        # whose inode/handle may still be held by another process.
        if legacy.exists():stack.enter_context(_exclusive_file(legacy))
        yield


@contextmanager
def _exclusive_file(path):
    handle = path.open('a+b')
    acquired = False
    try:
        if path.stat().st_size == 0:
            handle.write(b'\0')
            handle.flush()
        handle.seek(0)
        try:
            if os.name == 'nt':
                import msvcrt
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            acquired = True
        except OSError as error:
            raise StorageError(StorageErrorKind.SOURCE_BUSY,
                               'Another conversion owns this destination') from error
        yield
    finally:
        if acquired:
            handle.seek(0)
            if os.name == 'nt':
                import msvcrt
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        handle.close()


@contextmanager
def project_lease(folder, *, exclusive=False):
    """Pin packs across cooperating processes. Appends use a shared pin;
    packing/collection requires the exclusive pin. Read-only opens never
    create a file inside the source project.
    """
    path = Path(folder) / '.prism-storage.lock'
    handle = path.open('r+b' if exclusive else 'rb')
    acquired = False
    try:
        if os.name == 'nt':
            release=_windows_lock(handle,exclusive)
        else:
            import fcntl
            fcntl.flock(handle.fileno(), (fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH) | fcntl.LOCK_NB)
        acquired = True
        yield
    except OSError as error:
        contended = (getattr(error, 'winerror', None) in (32, 33) if os.name == 'nt'
                     else error.errno in (errno.EAGAIN, errno.EACCES))
        if not acquired and contended:
            raise StorageError(StorageErrorKind.SOURCE_BUSY, 'Project maintenance is in progress') from error
        raise
    finally:
        try:
            if acquired:
                handle.seek(0)
                if os.name == 'nt':
                    release()
                else:
                    import fcntl
                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        finally:
            handle.close()
