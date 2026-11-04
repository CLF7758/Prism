"""Typed read failures shared by the inspector and display decoder."""
import errno
from dataclasses import dataclass
import sqlite3

from prism.storage.contracts import StorageError, StorageErrorKind


@dataclass(frozen=True)
class ReadFailure:
    kind: str
    retryable: bool
    detail: str


def read_failure(error):
    if isinstance(error, StorageError):
        return ReadFailure(error.kind.value, error.kind is StorageErrorKind.SOURCE_BUSY, str(error))
    if isinstance(error, FileNotFoundError):
        return ReadFailure('missing', False, str(error))
    if (isinstance(error, OSError) and (getattr(error, 'winerror', None) in (32, 33)
            or error.errno in (errno.EAGAIN, errno.EBUSY))):
        return ReadFailure('source_busy', True, str(error))
    if isinstance(error, PermissionError):
        return ReadFailure('permission_denied', False, str(error))
    if isinstance(error, sqlite3.Error):
        code = getattr(error, 'sqlite_errorcode', 0) or 0
        if (code & 0xff) in (sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED):
            return ReadFailure('source_busy', True, str(error))
        return ReadFailure('database_error', False, str(error))
    return ReadFailure('read_failed', False, str(error))
