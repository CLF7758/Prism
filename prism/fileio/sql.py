# This file is part of Prism.
#
# Prism is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# Prism is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with Prism.  If not, see <https://www.gnu.org/licenses/>.

"""Prism's native file format is using SQLite. Embedded files are
stored in an sqlar table so that they can be extracted using sqlite's
archive command line option.

For more info, see:

https://www.sqlite.org/appfileformat.html
https://www.sqlite.org/sqlar.html
"""

import dataclasses
import json
import logging
import os
import pathlib
import shutil
import sqlite3
import tempfile
import threading
import time
import uuid
import hashlib
from contextlib import closing, contextmanager

from prism.performance_trace import measured, operation, open_phase, record as trace_record
from prism.performance_trace import span, mode, MODE_DIAGNOSTIC

from PyQt6 import QtCore, QtGui
from PyQt6.QtCore import QUrl

from prism import constants
from prism import thumbnail_cache
from prism.i18n import _
from prism.items import (PrismPixmapItem, PrismErrorItem, PrismVideoItem,
                         PrismGlbItem)
from prism.tags import TagSystem, tag_system
from .errors import PrismFileIOError, IMG_LOADING_ERROR_MSG
from .schema import SCHEMA, USER_VERSION, MIGRATIONS, APPLICATION_ID


logger = logging.getLogger(__name__)


#: SQL statements slower than this get a trace event with the statement
#: text, row count and the calling code path. Faster calls only pay for a
#: perf_counter, so per-row queries in hot loops stay cheap.
SLOW_SQL_MS = 5.0


def _sql_label(args):
    """A compact, stable label for a statement: first argument, whitespace
    collapsed, truncated. Bound parameter *values* are never included."""
    statement = args[0] if args else ''
    if not isinstance(statement, str):
        return repr(statement)[:120]
    return ' '.join(statement.split())[:120]


@contextmanager
def timed_sql(label, mode):
    """Time one SQL statement; record an event only when it is slow or fails.

    The event carries the caller stack (``sql.execute``/``sql.fetch`` are
    caller events), so a slow query on the GUI thread names the code that
    asked for it -- the one attribution the stall histogram cannot give
    for time spent inside the sqlite C extension.
    """
    stage = {'rows': None}
    started = time.perf_counter()
    failed = False
    try:
        yield stage
    except BaseException:
        failed = True
        raise
    finally:
        elapsed = (time.perf_counter()-started)*1000
        if failed or elapsed >= SLOW_SQL_MS:
            trace_record('sql.execute' if mode in ('execute', 'executemany')
                         else 'sql.fetch', query=label, mode=mode,
                         duration_ms=round(elapsed, 3), failed=failed,
                         rows=stage.get('rows'),
                         thread=threading.current_thread().name)


#: Bound parameters one scope id costs in the scoped reader: the
#: ``native_item_metadata`` statement repeats the id list once per UNION arm.
SCOPED_BINDINGS_PER_ID = 3

#: How many scope ids the scoped pixmap query binds per statement. A fetch
#: event therefore never reports more than one page of images.
SCOPED_IMAGE_PAGE = 256

#: The scoped statements, with one ``%s`` for the bound id list.
SCOPED_IMAGE_SQL = (
    'SELECT items.id, type, x, y, z, scale, rotation, flip, items.data '
    'FROM items '
    'WHERE items.type = "pixmap" AND items.id IN (%s) '
    'AND EXISTS (SELECT 1 FROM sqlar WHERE item_id=items.id)')

SCOPED_NATIVE_SQL = (
    'SELECT items.id, type, x, y, z, scale, rotation, flip, items.data '
    'FROM items '
    'WHERE items.type IN ("text", "path") AND items.id IN (%s) '
    'UNION ALL '
    'SELECT items.id, type, x, y, z, scale, rotation, flip, items.data '
    'FROM items '
    'WHERE items.type = "video" AND items.id IN (%s) '
    'UNION ALL '
    'SELECT items.id, type, x, y, z, scale, rotation, flip, items.data '
    'FROM items '
    'WHERE items.type = "glb" AND items.id IN (%s)')

SCOPED_GLB_BLOB_SQL = (
    # 先按 items 表过滤 glb 类型，再碰 sqlar：把 "sqlar WHERE item_id IN
    # (scope)" 放在外层会让执行计划先命中 scope 里的**图片**行，投影
    # 的 data 列迫使 SQLite 把这些图片的 BLOB 从 55 GB 归档里读出来再
    # 丢弃——按页打开 100 条图片曾被这一条卡了 262.1 秒（实测）。
    'SELECT item_id, data FROM sqlar WHERE item_id IN '
    '(SELECT id FROM items WHERE type="glb" AND id IN (%s))')

SCOPED_ORIGINAL_SIZE_SQL = (
    # 绝不碰 length(data)：当 sz 为 NULL 时 length() 会读整个 BLOB 页。
    # sz 为 NULL 的行按"未知大小"处理（不当作空文件），下游对空文件的
    # 判断回落到 os.path.isfile，与检查器"未知"渲染口径一致。
    'SELECT item_id, sz, COALESCE(sz,0)=0 '
    'FROM sqlar WHERE item_id IN (%s)')


def sql_variable_limit():
    """``SQLITE_MAX_VARIABLE_NUMBER`` of the runtime SQLite (999 ... 32766)."""
    try:
        connection = sqlite3.connect(':memory:')
    except sqlite3.Error:
        return 999
    try:
        return int(connection.getlimit(sqlite3.SQLITE_LIMIT_VARIABLE_NUMBER))
    except (AttributeError, sqlite3.Error):
        return 999
    finally:
        connection.close()


def _chunked(values, size):
    """Split ``values`` into consecutive chunks of at most ``size`` items."""
    size = max(1, int(size))
    for start in range(0, len(values), size):
        yield values[start:start + size]


def _placeholders(chunk):
    """``?,?,?`` for one chunk of bound ids."""
    return ','.join('?' for _ in chunk)


def _native_arm(kind):
    """Which UNION arm of the native statement produced this row."""
    if kind in ('text', 'path'):
        return 0
    if kind == 'video':
        return 1
    return 2


def _ensure_grayscale_hook(item):
    """Give a pixmap-like item the grayscale hook the scene calls.

    ``PrismGraphicsScene.addItem`` calls ``_sync_grayscale_effect`` on every
    item whose TYPE is 'pixmap' or 'glb', but only ``PrismPixmapItem``
    implements it. Without the hook a project that holds a 3D model cannot
    be added to the canvas at all -- neither by the full reader nor by the
    paged one. A model has no grayscale toggle of its own, so the hook does
    nothing.
    """
    if not hasattr(item, '_sync_grayscale_effect'):
        item._sync_grayscale_effect = lambda: None


def build_native_item(data, *, filename, glb_blob=None, video_embedded=False):
    """Build the scene item for one text/path/video/glb row.

    The branches are the ones the full reader uses, so a scoped open and the
    paged continuation install the same item instead of two look-alikes.
    ``data`` is the decoded item row and is updated in place: it either
    gains an ``item`` key, or -- when the source file is gone -- its type
    becomes ``PrismErrorItem.TYPE`` and its payload an error message.
    """
    kind = data['type']
    if kind == 'video':
        payload = data['data']
        if video_embedded:
            item = PrismVideoItem(video_url=None,
                                  filename=payload.get('filename'))
            item._is_reference = False
            item._prism_source = (str(filename), data['save_id'])
            data['item'] = item
        elif payload.get('videoPath'):
            path = payload['videoPath']
            if os.path.exists(path):
                item = PrismVideoItem(video_url=QUrl.fromLocalFile(path),
                                      filename=payload.get('filename'))
                item._is_reference = True
                data['item'] = item
            else:
                data['data'] = {'text': f'Video file missing: {path}'}
                data['type'] = PrismErrorItem.TYPE
        else:
            data['data'] = {'text': 'Video data missing'}
            data['type'] = PrismErrorItem.TYPE
    elif kind == 'glb':
        payload = data['data']
        if glb_blob:
            item = PrismGlbItem(filename=payload.get('filename'),
                                glb_blob=glb_blob)
            _ensure_grayscale_hook(item)
            data['item'] = item
        elif payload.get('modelPath'):
            path = payload['modelPath']
            if os.path.exists(path):
                item = PrismGlbItem(filename=payload.get('filename'),
                                    glb_path=path)
                _ensure_grayscale_hook(item)
                item._is_reference = True
                data['item'] = item
            else:
                data['data'] = {'text': f'3D model file missing: {path}'}
                data['type'] = PrismErrorItem.TYPE
        else:
            data['data'] = {'text': '3D model data missing'}
            data['type'] = PrismErrorItem.TYPE
    return data


def is_prism_file(path):
    """Check whether the file at the given path is a Prism project."""

    from prism.storage.managed import root_for
    return (str(path).lower().endswith('.prism.data') or
            os.path.splitext(str(path))[1].lower() in ('.prism','.prismproj') or root_for(path) is not None)


#: Attribute the GUI sets on the scene to ask for a read-only project.
#: ``SQLiteIO.read()`` consumes it and turns it into a mark on the scene;
#: the writer refuses to write back to a marked project.
READONLY_PROJECT_REQUEST = 'readonly_project_request'

#: A read-only open of an old project migrates a throwaway copy, and the
#: copy costs as much disk as the project itself.  A project bigger than
#: this is refused with an explanation instead of being copied -- the
#: 51 GB stress fixture does not fit next to anything.
READONLY_MIGRATION_COPY_LIMIT = 2 * 1024 ** 3


def _normalized_path(path):
    """One spelling of a path, so two names of one file compare equal."""
    try:
        return os.path.normcase(os.path.abspath(str(path)))
    except (TypeError, ValueError):
        return str(path)


def request_readonly_project(scene, filename):
    """Ask for the next open of ``filename`` to be a read-only project.

    The GUI calls this *before* the project is loaded.  Two things follow
    from it: the project is marked read-only on the scene, and saving it
    back is refused -- so an autosave cannot rewrite a file the user only
    wanted to look at.

    This is the counterpart of the 2026-10-22 accident: opening a 51 GB
    fixture was enough for the autosave to rewrite it 45 minutes later.
    The reader is always a read-only connection, so "the file was opened"
    says nothing about the writer's licence; this says it explicitly.
    """
    setattr(scene, READONLY_PROJECT_REQUEST, _normalized_path(filename))


def is_readonly_project(scene, filename=None):
    """True when the project on ``scene`` was opened read-only.

    ``filename`` narrows the question to one file: a project marked
    read-only does not stop the user from saving a *different* file
    ("save as" onto a copy).
    """
    if not getattr(scene, 'readonly_project', False):
        return False
    if filename is None:
        return True
    marked = getattr(scene, 'readonly_project_path', None)
    if not marked:
        return False
    return _normalized_path(marked) == _normalized_path(filename)


def handle_sqlite_errors(func):
    def wrapper(self, *args, **kwargs):
        try:
            return func(self, *args, **kwargs)
        except Exception as e:
            logger.exception(f'Error while reading/writing {self.filename}')
            try:
                # Try to roll back transaction if there is any
                if (hasattr(self, '_connection')
                        and self._connection.in_transaction):
                    self.ex('ROLLBACK')
                    logger.debug('Transaction rolled back')
            except sqlite3.Error:
                pass
            self._close_connection()
            if self.worker:
                self.worker.finished.emit(self.filename, [str(e)])
            else:
                raise PrismFileIOError(msg=str(e), filename=self.filename) from e

    return wrapper


class SQLiteIO:

    def __init__(self, filename, scene, create_new=False, readonly=False,
                 worker=None, board_snapshot=None, partial_snapshot=None):
        self.scene = scene
        self.board_snapshot = board_snapshot
        self.partial_snapshot = partial_snapshot
        self.create_new = create_new
        self.filename = filename
        self.readonly = readonly
        self.worker = worker
        self.retry = False
        #: Set by `_read_rows` once a `*_unsized` read comes back empty.
        #: While it is still True the cheap size projection has not been
        #: checked against an archive that really has unsized rows, so the
        #: reader still asks for them explicitly.
        self._sqlar_sizes_complete = False
        #: True once the connection was closed on purpose.  A closed
        #: connection is never silently reopened -- see `connection`.
        self._connection_closed = False

    def __del__(self):
        self._close_connection()

    def _close_connection(self):
        if hasattr(self, '_connection'):
            self._connection.close()
            delattr(self, '_connection')
        if hasattr(self, '_cursor'):
            delattr(self, '_cursor')
        if hasattr(self, '_tmpdir'):
            self._tmpdir.cleanup()
            delattr(self, '_tmpdir')
        # From here on the connection is gone on purpose.  Nothing may
        # bring it back implicitly: see `_refuse_reopen`.
        self._connection_closed = True

    def _backup_before_overwrite(self):
        """Keep a copy of a file we are about to rebuild from scratch."""
        backup = f'{self.filename}.bak'
        try:
            shutil.copyfile(self.filename, backup)
            attachments = getattr(self.scene, 'attachments', None)
            if hasattr(attachments, 'relocate_source'):
                attachments.relocate_source(self.filename, backup)
            logger.warning(
                'Unreadable file kept as %s before rebuilding', backup)
        except OSError:
            logger.exception('Could not back up %s', self.filename)
            raise
        sources = set(self.scene.items_for_save())
        sources.update(self.scene._prism_archive_items)
        for error in self.scene.items_by_type(PrismErrorItem.TYPE):
            source = getattr(error, '_asset_source', None)
            if source and pathlib.Path(source[0]).resolve() == pathlib.Path(self.filename).resolve():
                error._asset_source = (backup, source[1])
        for item in sources:
            source = getattr(item, '_prism_source', None)
            if source and pathlib.Path(source[0]).resolve() == pathlib.Path(self.filename).resolve():
                item._prism_source = (backup, source[1])

    def _remove_with_retry(self):
        """Delete the file we are about to rebuild, waiting out a stale lock.

        Windows refuses to delete a file while any handle is open, and the
        handle does not always belong to us: the thumbnail cache keeps one,
        the antivirus scanner takes one for a moment, and a second copy of
        Prism started on the same project holds one for as long as it lives.

        ``os.remove`` therefore fails transiently with
        ``PermissionError: [WinError 32]``  - that showed up six times in the
        log.  Retrying for a couple of seconds covers the scanner and the
        just-closed handle; if it still fails, the file really is in use and
        the caller gets a message naming the file instead of a bare errno.
        """
        last_error = None
        for attempt in range(8):
            try:
                os.remove(self.filename)
                return
            except FileNotFoundError:
                return
            except PermissionError as error:
                last_error = error
                # 0.05 s doubling up to ~1.6 s: long enough for a scanner,
                # short enough that a genuinely locked file fails fast.
                time.sleep(0.05 * (2 ** attempt))
        logger.error('Could not replace %s; it is still in use',
                     self.filename)
        raise PermissionError(
            f'{self.filename} 正被另一个程序占用，没法重建。'
            f'请关掉其它打开这个工程的窗口（或者结束占用它的进程）再试。'
        ) from last_error

    def _guard_existing_format(self):
        """Do not reinterpret a future or foreign SQLite file as corruption."""
        with open(self.filename, 'rb') as stream:
            if stream.read(16) != b'SQLite format 3\x00':
                return  # Existing damaged-file backup/recovery stays separate.
        uri = pathlib.Path(self.filename).resolve().as_uri() + '?mode=ro'
        with closing(sqlite3.connect(uri, uri=True)) as connection:
            connection.execute('BEGIN')
            version = connection.execute('PRAGMA user_version').fetchone()[0]
            app_id = connection.execute('PRAGMA application_id').fetchone()[0]
            if version > USER_VERSION:
                raise sqlite3.OperationalError(
                    f'Project format {version} is newer than supported {USER_VERSION}; '
                    'writing or rebuilding it is refused')
            if app_id not in (0, APPLICATION_ID):
                raise sqlite3.OperationalError(
                    'This SQLite file belongs to another application; writing is refused')
            if app_id == 0:
                tables = {r[0] for r in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'")}
                if version < 1 or not {'items', 'sqlar'} <= tables:
                    raise sqlite3.OperationalError(
                        'This SQLite file is not a supported legacy Prism project')

    def _establish_connection(self):
        # A previous connection may still be open - for example when a
        # failed migration asked for a rebuild.  Windows refuses to replace
        # a file while the old handle is alive, so drop it first.
        self._close_connection()

        # Refuse unsupported healthy formats before any backup/rebuild branch.
        # A format gate raised inside _migrate's broad recovery handler would
        # otherwise turn a future/foreign database into a destructive rebuild.
        if not self.readonly and os.path.isfile(self.filename):
            self._guard_existing_format()

        if (self.create_new
                and not self.readonly
                and os.path.exists(self.filename)):
            # Rebuilding discards whatever was there, so keep a copy first:
            # a project we failed to read must never be lost silently.
            self._backup_before_overwrite()
            self._remove_with_retry()

        if self.create_new:
            self.scene.clear_save_ids()

        uri = pathlib.Path(self.filename).resolve().as_uri()
        if self.readonly:
            # A read-only project must not be able to write -- not even
            # "just a pragma".  `mode=ro` opens the file read-only at the
            # OS level, so SQLite itself refuses every write and no
            # rollback journal can appear.  The old `mode=rw` left the
            # connection writable: the read path could then start a
            # transaction, and a project that was only being looked at
            # ended up with a `-journal` next to it.
            uri = f'{uri}?mode=ro'
        self._connection = sqlite3.connect(uri, uri=True)
        self._connection_thread_id = threading.get_ident()
        self._cursor = self.connection.cursor()
        self._connection_closed = False
        if not self.create_new:
            try:
                self._migrate()
            except (sqlite3.OperationalError, PermissionError):
                # Busy, readonly, disk-full and IO failures are not damaged
                # schemas. Rebuilding a healthy file cannot fix these errors.
                raise
            except Exception:
                if self.readonly:
                    raise
                # Updating a file failed; try creating it from scratch instead
                logger.exception('Error migrating Prism project')
                self.create_new = True
                self._establish_connection()

    def _apply_readonly_request(self):
        """Honour (or drop) a pending read-only project request.

        A request names the file it is about: a request for another file
        is not this file's business.  A normal open drops the mark, so a
        project that was opened read-only does not stay read-only after
        it is opened again for editing.
        """
        request = getattr(self.scene, READONLY_PROJECT_REQUEST, None)
        if request is not None:
            try:
                delattr(self.scene, READONLY_PROJECT_REQUEST)
            except AttributeError:
                pass
        current = _normalized_path(self.filename)
        if request is not None and _normalized_path(request) == current:
            self.scene.readonly_project = True
            self.scene.readonly_project_path = current
            logger.info('Project %s is open read-only; writing it back '
                        'is refused', self.filename)
            return
        self.scene.readonly_project = False
        self.scene.readonly_project_path = None

    def _guard_readonly_project(self):
        """Refuse to write back to a project that was opened read-only.

        The reader is always a read-only connection, so "the file was
        opened" says nothing about the writer's licence; the GUI says it
        explicitly with :func:`request_readonly_project`.  Saving over
        such a project is refused here -- the autosave that rewrote a
        51 GB fixture on 2026-10-22 goes through this very function.
        """
        if not is_readonly_project(self.scene, self.filename):
            return
        raise PrismFileIOError(
            '这个工程是以只读方式打开的，不能写回。'
            '要保存请用「另存为」换一个文件。', self.filename)

    def _migrate(self):
        """Migrate database if necessary."""

        version = self.fetchone('PRAGMA user_version')[0]
        logger.debug(f'Found Prism project version: {version}')
        if version > USER_VERSION:
            raise sqlite3.OperationalError(
                f'Project format {version} is newer than supported {USER_VERSION}; '
                'open with a newer Prism version')
        if version == USER_VERSION:
            logger.debug('Version ok; no migrations necessary')
            return
        if (version + 1) not in MIGRATIONS:
            # The migration table starts at the first released version, so
            # version 0 means an empty file or something that is not a Prism
            # database at all.  Say so plainly instead of raising KeyError
            # deeper down, where it looks like a crash.
            raise ValueError(
                f'not a Prism file (user_version={version})')

        if self.readonly:
            # A read-only project is never migrated in place.  Migrating
            # means writing, and "the file happens to be writable" is not
            # a licence to rewrite a project the caller asked to only look
            # at.  The migration runs on a throwaway copy instead; the
            # source file keeps its old version until somebody opens it
            # for writing.
            self._migrate_on_temporary_copy()

        self.ex('BEGIN TRANSACTION')
        for i in range(version, USER_VERSION):
            logger.debug(f'Migrating from version {i} to {i + 1}...')
            for migration in MIGRATIONS[i + 1]:
                self.ex(migration)
        self.write_meta()
        self.connection.commit()
        logger.debug('Migration finished')

    def _migrate_on_temporary_copy(self):
        """Run the migration on a copy; the source file stays untouched.

        The copy is thrown away when the connection is closed.  It costs
        as much disk as the project, so a project that does not fit is
        refused with an explanation rather than copied.
        """
        try:
            size = os.path.getsize(self.filename)
        except OSError:
            size = 0
        if size > READONLY_MIGRATION_COPY_LIMIT:
            raise PrismFileIOError(
                f'这个工程是旧版本（需要迁移），只读打开要在临时目录复制一份，'
                f'但它有 {size:,} 字节，太大了。请用可写方式打开一次完成迁移，'
                f'之后就能只读打开。', self.filename)
        logger.debug('File is not to be migrated in place; '
                     'using temporary copy instead')
        self._connection.close()
        self._tmpdir = tempfile.TemporaryDirectory(
            prefix=constants.APPNAME)
        tmpname = os.path.join(self._tmpdir.name, 'mig.prism')
        shutil.copyfile(self.filename, tmpname)
        self._connection = sqlite3.connect(tmpname)
        self._cursor = self.connection.cursor()

    @property
    def connection(self):
        if not hasattr(self, '_connection'):
            self._refuse_reopen()
            self._establish_connection()
        return self._connection

    @property
    def cursor(self):
        if not hasattr(self, '_cursor'):
            self._refuse_reopen()
            self._establish_connection()
        return self._cursor

    def _refuse_reopen(self):
        """A connection that was closed on purpose stays closed.

        Reopening it on the next attribute access used to hand out a
        **writable** handle to a file the user may have opened
        read-only, and it silently re-locked the file on Windows.
        Closing is a decision; the next attribute access must not
        undo it.  A caller that really wants a new connection asks
        for one explicitly with `_establish_connection()`.
        """
        if not self._connection_closed:
            return
        raise sqlite3.ProgrammingError(
            'the connection to %s was closed and cannot be reopened'
            % self.filename)

    def ex(self, *args, **kwargs):
        with timed_sql(_sql_label(args), 'execute'):
            return self.cursor.execute(*args, **kwargs)

    def exmany(self, *args, **kwargs):
        with timed_sql(_sql_label(args), 'executemany'):
            return self.cursor.executemany(*args, **kwargs)

    def fetchone(self, *args, **kwargs):
        self.ex(*args, **kwargs)
        with timed_sql(_sql_label(args), 'fetchone') as stage:
            row = self.cursor.fetchone()
            stage['rows'] = 1 if row is not None else 0
            return row

    def fetchall(self, *args, **kwargs):
        self.ex(*args, **kwargs)
        with timed_sql(_sql_label(args), 'fetchall') as stage:
            rows = self.cursor.fetchall()
            stage['rows'] = len(rows)
            return rows

    def _read_rows(self, label, statement, parameters=()):
        # Keep SQL execution separate from row transfer. These diagnostics do
        # not make this legacy full-load path a lazy loader.
        scope = getattr(self, 'scope_ids', None)
        fields = dict(query=label, query_id=uuid.uuid4().hex[:16],
            query_fingerprint=hashlib.sha256(statement.encode('utf-8')).hexdigest()[:16],
            scope_kind='full' if scope is None else 'ids',
            requested_ids_count=None if scope is None else len(scope),
            parameter_count=len(parameters), selects_original_blob=label == 'embedded_glb',
            connection_thread_id=getattr(self, '_connection_thread_id', None),
            busy_timeout_ms=5000, lock_wait_ms=None, physical_disk_bytes=None,
            process_read_transfer_bytes=None)
        started = time.perf_counter()
        with span('project.sql.query', stage=label, **fields):
            with span('project.sql.execute', **fields) as execution:
                stage = execution.extra
                # execute() may perform most of SQLite's work. It has not
                # returned rows to Python yet; 0 used to imply no SQL result.
                stage['rows'] = None
                stage['rows_returned'] = None
                try:
                    self.ex(statement, parameters)
                except sqlite3.Error as error:
                    stage['sqlite_errorcode'] = getattr(error, 'sqlite_errorcode', None)
                    stage['sqlite_errorname'] = getattr(error, 'sqlite_errorname', None)
                    raise
            with span('project.sql.fetch', **fields) as fetch:
                stage = fetch.extra
                rows = self.cursor.fetchall()
                stage['rows'] = stage['rows_returned'] = len(rows)
                # Bounded observation of returned objects only. Never read
                # another BLOB or encode giant JSON merely for diagnostics.
                sample = rows[:32]
                stage['payload_sample_rows'] = len(sample)
                stage['payload_sample_complete'] = len(sample) == len(rows)
                stage['returned_binary_bytes_sample'] = sum(
                    len(value) for row in sample for value in row
                    if isinstance(value, (bytes, bytearray)))
                stage['returned_text_chars_sample'] = sum(
                    len(value) for row in sample for value in row
                    if isinstance(value, str))
        elapsed = (time.perf_counter() - started) * 1000
        # Plans are diagnostic-only, slow SELECT-only, worker-only. A separate
        # cursor preserves the real cursor/result; this never reruns the SELECT.
        if (mode() == MODE_DIAGNOSTIC and elapsed >= 100
                and threading.current_thread() is not threading.main_thread()
                and statement.lstrip().upper().startswith('SELECT')):
            try:
                with closing(self.connection.cursor()) as plan_cursor:
                    plan = plan_cursor.execute('EXPLAIN QUERY PLAN ' + statement,
                                               parameters).fetchmany(32)
                trace_record('project.sql.plan', **fields, plan=plan,
                             duration_ms=round(elapsed, 3), plan_rows_limited=32)
            except sqlite3.Error as error:
                trace_record('project.sql.plan_unavailable', **fields,
                             sqlite_errorname=getattr(error, 'sqlite_errorname', None))
        if label.endswith('_unsized'):
            # This read exists to find archive rows that carry no stored byte
            # count. If it returned anything, the cheap projection cannot be
            # trusted on its own for those rows.
            self._sqlar_sizes_complete = not rows
        return rows

    def _read_scoped_chunks(self, label, statement, scope, size):
        """Run one scoped statement in chunks this runtime can bind.

        The id list is split into consecutive chunks; the merged rows are
        the rows an unchunked reader returns, in the same order. An empty
        scope still asks the question once, so the trace shows that the
        read happened and that it returned nothing.
        """
        rows = []
        empty = True
        for chunk in _chunked(scope, size):
            empty = False
            rows.extend(self._read_rows(label, statement % _placeholders(chunk),
                                        chunk))
        if empty:
            rows.extend(self._read_rows(label, statement % '', ()))
        return rows

    def _read_scoped_rows(self, scope, limit):
        """Read the scoped rows of a page-sized or larger scope.

        The scoped reader binds one parameter per scope id for the pixmap
        query and three for the native one (one per UNION arm), so a scope
        larger than the runtime's variable limit has to be read in pieces.
        The merged rows are exactly the rows an unchunked reader returns:
        the pixmap rows keep their order, and the native rows are grouped
        per UNION arm so the merged order is the unchunked one.
        """
        rows = self._read_scoped_chunks(
            'image_metadata', SCOPED_IMAGE_SQL, scope,
            max(1, min(SCOPED_IMAGE_PAGE, limit)))
        arms = ([], [], [])
        native_size = max(1, limit // SCOPED_BINDINGS_PER_ID)
        empty = True
        for chunk in _chunked(scope, native_size):
            empty = False
            placeholders = (_placeholders(chunk),) * SCOPED_BINDINGS_PER_ID
            fetched = self._read_rows('native_item_metadata',
                                      SCOPED_NATIVE_SQL % placeholders,
                                      chunk * SCOPED_BINDINGS_PER_ID)
            for row in fetched:
                arms[_native_arm(row[1])].append(row)
        if empty:
            placeholders = ('',) * SCOPED_BINDINGS_PER_ID
            self._read_rows('native_item_metadata', SCOPED_NATIVE_SQL % placeholders,
                            ())
        for arm in arms:
            rows.extend(arm)
        return rows

    def write_meta(self):
        self.ex('PRAGMA application_id=%s' % APPLICATION_ID)
        self.ex('PRAGMA user_version=%s' % USER_VERSION)
        self.ex('PRAGMA foreign_keys=ON')

    def create_schema_on_new(self):
        # Opening an invalid/missing destination can select a backed-up
        # rebuild. Resolve that decision before checking create_new.
        self.connection
        if self.create_new:
            self.write_meta()
            for schema in SCHEMA:
                self.ex(schema)

    @handle_sqlite_errors
    @measured('project.sql_read', 0)
    def read(self, scope=None):
        """读工程。`scope` 给定时只读这些 item id（有界按页打开）。

        `scope=None` 是旧的**全量**行为。给定 scope 时：

        * 只查这些 id 的 items、原件大小与 GLB blob，不再全表取回；
        * scope 里的 id 可能超过本运行时的 SQL 变量上限（老 SQLite 是
          999），所以 scoped 分支**按块读**：合并后的行集合与一次读完
          完全一致，`image_metadata` 的每次 fetch 仍然只报一页（≤256），
          非 pixmap 的行仍然记在 `native_item_metadata` 标签下；
        * **强制走虚拟记录分支** —— 否则一页 100 条会掉进 legacy 分支逐张
          读原图（那是小工程的路径，按页读会慢得离谱）；
        * 在 scene 上登记 `_loaded_scope_ids` 与 `_partial_project_source`，
          让保存必须走部分保存快照：全量 writer 会把未加载行当删除。

        按页读时 `read_board_extras(scope=...)` 延迟文档/脑图正文与每页
        content（页面清单/材质/资源树/附件索引仍全量），保存前按需物化。
        """
        if scope is not None:
            scope = tuple(dict.fromkeys(int(key) for key in scope))
        self.scope_ids = scope
        self._apply_readonly_request()
        has_metadata = self.fetchone(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='prism_metadata'")
        if has_metadata:
            row = self.fetchone("SELECT value FROM prism_metadata WHERE key='categories'")
            self.scene.category_names = json.loads(row[0]) if row else []
        else:
            from prism.config import PrismSettings
            legacy = PrismSettings().value('UserCategories/list', [])
            self.scene.category_names = legacy if isinstance(legacy, list) else []
        scope_limit = None
        if scope is not None:
            # A scope may hold more ids than this runtime can bind in one
            # statement, so the scoped statements read it in chunks.
            scope_limit = sql_variable_limit()
            rows = self._read_scoped_rows(scope, scope_limit)
        else:
            with open_phase('read_image_metadata') as info:
                info['query'] = 'image_metadata'
                rows = self._read_rows('image_metadata',
                    'SELECT items.id, type, x, y, z, scale, rotation, flip, '
                    'items.data '
                    'FROM items '
                    'WHERE items.type = "pixmap" AND items.id IN '
                    ' (SELECT item_id FROM sqlar)')
                info['rows'] = len(rows)
            # Avoid OUTER JOIN for performance reasons; fetch text items
            # and referenced video items separately
            with open_phase('read_native_item_metadata') as info:
                info['query'] = 'native_item_metadata'
                rows.extend(self._read_rows('native_item_metadata',
                    'SELECT items.id, type, x, y, z, scale, rotation, flip, '
                    ' items.data '
                    'FROM items '
                    'WHERE items.type IN ("text", "path") '
                    'UNION ALL '
                    'SELECT items.id, type, x, y, z, scale, rotation, flip, '
                    ' items.data '
                    'FROM items '
                    'WHERE items.type = "video" '
                    'UNION ALL '
                    'SELECT items.id, type, x, y, z, scale, rotation, flip, '
                    ' items.data '
                    'FROM items '
                    'WHERE items.type = "glb"'))
                info['rows'] = len(rows)
        # Models retain the old loader. Embedded videos stream to a playback
        # file rather than accumulating every original in media_blobs.
        media_blobs = {}
        if scope is not None:
            blob_rows = self._read_scoped_chunks(
                'embedded_glb', SCOPED_GLB_BLOB_SQL, scope, scope_limit)
        else:
            with open_phase('read_embedded_glb') as info:
                info['query'] = 'embedded_glb'
                blob_rows = self._read_rows('embedded_glb',
                    'SELECT item_id, data FROM sqlar '
                    'WHERE item_id IN (SELECT id FROM items '
                    '  WHERE type = "glb")')
                info['rows'] = len(blob_rows)
        for blob_row in blob_rows:
            blob = blob_row[1]
            if blob is None:
                from prism.storage.managed import root_for
                if root_for(self.filename) is not None:
                    from prism.source_store import read_archive
                    blob = read_archive((self.filename, blob_row[0]))
            media_blobs[blob_row[0]] = blob
        if self.worker:
            self.worker.begin_processing.emit(len(rows))

        from prism import asset_records
        # 按页读时强制虚拟分支：否则 100 条会走 legacy 分支逐张读原图。
        virtual_images = scope is not None or (
            sum(row[1] == 'pixmap' for row in rows) >= asset_records.MIN_VIRTUAL_IMAGES)
        # Every one of these 100 000 rows belongs to a table whose rows carry
        # a BLOB. Fetching them makes SQLite read those BLOBs off disk and hand
        # them to Python, even when the statement never names `data`: measured
        # 156 s to open a 51.81 GiB project on a mechanical disk, because 50 GiB
        # of image bytes travelled through the process for nothing.
        #
        # `sz` is the stored byte count and is present for every row written by
        # this schema, so the projection below never mentions the BLOB at all
        # and the same statement returns in well under a second. Rows whose
        # `sz` is NULL are counted separately so the archive's own accounting
        # stays correct instead of silently reading them as size 0.
        if scope is not None:
            original_metadata = self._read_scoped_chunks(
                'original_sizes', SCOPED_ORIGINAL_SIZE_SQL, scope, scope_limit)
        else:
            # A full load must NOT read the archive to learn the byte counts.
            #
            # `sqlar` holds every image byte, so a full scan of it means
            # streaming the whole project off the disk: measured 156 s for a
            # 51.81 GiB project on a mechanical disk, and 641 s when the same
            # file was being read by something else at the same time.
            #
            # The counts are only ever consumed by the "occupied space"
            # display -- and the asset browser already refuses to enumerate
            # them on the GUI thread (`asset_browser.storage_bytes` returns
            # None for catalog rows). So the eager scan fed a cache the
            # interface did not use, at the price of a three minute open.
            #
            # The size of an original is recoverable on demand from the
            # catalog index; until then the honest answer is "unknown", and
            # the inspector already renders that.
            trace_record('project_archive_sizes_skipped',
                         project=os.path.basename(str(self.filename)),
                         reason='lazy: the archive is not read on open')
            original_metadata = []
        original_sizes = {key: size for key, size, _empty in original_metadata}
        from prism.source_store import remember_archive_sizes
        if original_sizes:
            remember_archive_sizes(self.filename, original_sizes)
        empty_originals = {key for key, _size, empty in original_metadata if empty}

        with operation('project.records.prepare', rows_returned=len(rows),
                       records_visited=0, scope_kind='full' if scope is None else 'ids',
                       virtual_images=virtual_images) as prepared_work:
            for i, row in enumerate(rows):
                prepared_work['records_visited'] = i + 1
                data = {
                    'save_id': row[0],
                    'type': row[1],
                    'x': row[2],
                    'y': row[3],
                    'z': row[4],
                    'scale': row[5],
                    'rotation': row[6],
                    'flip': row[7],
                    'data': json.loads(row[8]),
                }

                if data['type'] == 'pixmap':
                    missing_original = (row[0] in empty_originals and not os.path.isfile(data['data'].get('filename') or ''))
                    if virtual_images and not missing_original:
                        extra = data['data']
                        if not (extra.get('imageWidth') and extra.get('imageHeight')):
                            from prism.image_decode import native_reader
                            from prism.source_store import read_archive
                            with native_reader(blob=read_archive((self.filename, row[0]))) as header:
                                size = header.size()
                                if header.transformation().value & 4:
                                    size.transpose()
                            if size.isValid() and not size.isEmpty():
                                extra['imageWidth'], extra['imageHeight'] = size.width(), size.height()
                        if extra.get('imageWidth') and extra.get('imageHeight'):
                            item = asset_records.ImageRecord(data, (self.filename, row[0]))
                            self.scene._prism_archive_items.add(item)
                            data['item'] = item
                            self.scene.add_item_later(data)
                            if self.worker:
                                if i % 32 == 0 or i == len(rows)-1:
                                    self.worker.progress.emit(i)
                                if self.worker.canceled:
                                    self.worker.finished.emit('', [])
                                    return
                            continue
                    item = PrismPixmapItem(QtGui.QImage())
                    item.filename = data['data'].get('filename')
                    self.scene._prism_archive_items.add(item)
                    # Deferred blob loading: the original bytes stay in the
                    # .prism archive until save/export/painting needs them.
                    item._prism_source = (self.filename, row[0])
                    img_w = data['data'].get('imageWidth', 0)
                    img_h = data['data'].get('imageHeight', 0)
                    loaded = not missing_original
                    if loaded and img_w and img_h:
                        item._image_size = QtCore.QSize(img_w, img_h)
                        item.reset_crop()
                    elif loaded:
                        # Legacy files have no dimensions. Read one image at a
                        # time for a preview, then release the original bytes.
                        blob = item.original_bytes()
                        loaded = bool(blob) and item.load_stored_image(blob)
                        if loaded:
                            item.release_source_blob()
                    if not loaded:
                        # 说清是**哪一个**素材。以前这里拼的是 `item.filename`，
                        # 而加载失败时那个字段常常是空的 —— 画布上就只有
                        # "Image could not be loaded: None"，看不出是谁。
                        # 现在用 id + 已知的线索（备注、分类）来指认。
                        clue = (item.filename
                                or data['data'].get('title')
                                or data['data'].get('notes')
                                or '、'.join(data['data'].get('categories') or [])
                                or _('no name stored'))
                        item = data['data']['text'] = (
                            f'Image could not be loaded: {clue}\n'
                            f'(item {row[0]})\n'
                            + ('原图数据为空，原文件也无法找到。请重新连接原图所在磁盘或恢复原文件。'
                               if missing_original else IMG_LOADING_ERROR_MSG))
                        data['type'] = PrismErrorItem.TYPE
                        data['data']['_asset_source'] = (self.filename, row[0])
                    data['item'] = item

                elif data['type'] in ('video', 'glb'):
                    # The native kinds share one construction path with the
                    # paged continuation, so both open routes install the same
                    # item instead of two look-alikes.
                    embedded = False
                    if data['type'] == 'video':
                        probe = self.fetchone(
                            'SELECT rowid,sz,length(data) FROM sqlar WHERE item_id=?',
                            (row[0],))
                        embedded = bool(probe and probe[2])
                        if not embedded:
                            from prism.storage.managed import root_for, binding
                            if root_for(self.filename) is not None:
                                ref = binding(self.filename, 'sqlar', row[0], item_id=True)
                                embedded = bool(ref and ref[1])
                    build_native_item(data, filename=self.filename,
                                      glb_blob=media_blobs.get(row[0]),
                                      video_embedded=embedded)

                self.scene.add_item_later(data)

                if self.worker:
                    # 进度每 32 个报一次。实测：1131 个素材逐个上报时，进度条
                    # 重绘加那行日志吃掉 1.17 秒，占整个打开时间的一半以上,
                    # 比读数据本身还贵。用户看不出 1131 个里差一两个的差别，
                    # 但看得出总时长。
                    # 取消检查必须每次都做 —— 它几乎不花钱，而漏一次就是
                    # 取消按下去要等一整个 32 才生效。
                    if i % 32 == 0 or i == len(rows) - 1:
                        logger.trace(f'Emit progress: {i}')
                        self.worker.progress.emit(i)
                    if self.worker.canceled:
                        self.worker.finished.emit('', [])
                        return
                    # Give main thread time to process items:
                    if i % 32 == 0:
                        self.worker.msleep(1)
        with operation('project.board_headers.read',
                       scope_kind='full' if scope is None else 'ids'):
            self.read_board_extras(scope=self.scope_ids)
        if scope is not None:
            # 按页打开：登记"这一页"是唯一的基线，并声明这是部分加载。
            # 全量 writer 会按"数据库 id 减去 scene id"推断删除，所以保存必须
            # 走 partial 快照；没登记基线时写会直接拒绝（宁可拒绝也不猜）。
            self.scene._loaded_scope_ids = scope
            self.scene._partial_project_source = str(self.filename)
            logger.debug('按页读取：%d 条（总 %s）', len(scope),
                         (self.fetchone('SELECT max(id) FROM items') or (0,))[0])
        # Images that were decoded the slow way are turned into cached
        # previews in the background, so the next open can skip it.
        thumbnail_cache.start_background_build()
        if self.worker:
            self.worker.finished.emit(self.filename, [])

    @handle_sqlite_errors
    @measured('project.sql_write', 0)
    def write(self):
        if self.readonly:
            raise sqlite3.OperationalError(
                'Attempt to write to a readonly database')
        self._guard_readonly_project()
        if self.partial_snapshot is not None:
            from .partial_save import write_partial_snapshot
            partial_source = getattr(self.scene, '__dict__', {}).get('_partial_project_source')
            if partial_source and (not self.partial_snapshot.baseline_checked or
                    pathlib.Path(partial_source).resolve() != pathlib.Path(self.partial_snapshot.source).resolve()):
                raise ValueError('Partial GUI saves require the loaded project baseline')
            if (self.create_new and pathlib.Path(self.filename).resolve() ==
                    pathlib.Path(self.partial_snapshot.source).resolve()):
                raise ValueError('A partial scene cannot rebuild its source project')
            self._guard_deferred_board_extras()
            # The partial writer knows the moment the target really changed:
            # for a save-as that is the atomic publication, for an in-place
            # save it is the commit. It reports that moment itself, so the
            # adapter never has to guess.
            def report(state, reason='', **extra):
                self._publish_write_result(state, reason=reason, **extra)

            self.partial_receipt = write_partial_snapshot(
                self.partial_snapshot, self.filename,
                cancel=(lambda: self.worker.canceled) if self.worker is not None else None,
                commit_report=report)
            if self.worker:
                self.worker.partial_save_receipt = self.partial_receipt
                # The receipt reports HOW MANY rows were touched. It does not
                # say which entities changed and it does not cover metadata,
                # domain or attachment changes, so the identity list stays
                # unknown instead of being faked.
                self._publish_write_result('committed_complete',
                    changed_entities=None,
                    changed_entities_complete=False,
                    changed_row_count=self._receipt_changed_row_count(self.partial_receipt))
                self.worker.finished.emit(self.filename, [])
            return self.partial_receipt
        if getattr(self.scene, '__dict__', {}).get('_partial_project_source'):
            raise ValueError('这个工程是部分打开的，必须先建立部分保存快照才能保存')
        if self.worker is not None and self.worker.canceled:
            # The cancel was already requested before the writer started. No
            # business row may be written, and the target must keep the state
            # it had before this save.
            self._publish_write_result('not_started', cancelled=True,
                reason='Cancelled before the first row was written')
            self.worker.finished.emit(self.filename, [
                _('Saving was canceled. Unsaved items remain on the canvas.')])
            return None
        previous_ids = [(item, item.save_id) for item in self.scene.items_for_save()]
        try:
            self.create_schema_on_new()
            self.write_data()
        except sqlite3.OperationalError:
            for item, save_id in previous_ids:
                item.save_id = save_id
            raise
        except sqlite3.DatabaseError:
            if self.retry:
                # Trying to recover failed
                raise
            else:
                self.retry = True
                # Try creating file from scratch and save again
                logger.exception(
                    f'Updating to existing file {self.filename} failed')
                self.create_new = True
                self._close_connection()
                # The connection was closed on purpose, so it is not
                # reopened implicitly any more.  Ask for a new one.
                self._establish_connection()
                self.write()
        except Exception:
            for item, save_id in previous_ids:
                item.save_id = save_id
            raise

    def _guard_deferred_board_extras(self):
        """按页打开时未物化的文档正文挡住保存，而不是被清空覆盖。

        partial 的 board 补丁无法区分"文档正文为空"和"正文尚未加载"，
        若带着空 ``scene.note_html`` 写盘会把原文清掉，宁可拒绝保存。
        脑图由 partial 的显式清空授权拒绝，页面正文由 ``_page_patches``
        回填磁盘原值，二者没有静默覆盖风险，故这里只拦 note_html。
        """
        deferred = getattr(self.scene, '_deferred_board_extras', None)
        if deferred and 'note_html' in deferred:
            raise ValueError(
                '文档正文尚未加载，保存已停止以免覆盖原文。'
                '请打开文档面板或完整打开工程后再保存。')

    def write_data(self):
        self._batch_write = True
        try:
            self._write_data_batch()
        except Exception:
            self.connection.rollback()
            raise
        finally:
            self._batch_write = False

    def _write_data_batch(self):
        metadata = self.board_snapshot if self.board_snapshot is not None else self.scene
        self.ex('CREATE TABLE IF NOT EXISTS prism_metadata (key TEXT PRIMARY KEY, value TEXT)')
        self.ex('INSERT OR REPLACE INTO prism_metadata VALUES (?, ?)',
                ('library_queries', json.dumps(getattr(metadata, 'library_queries', []), ensure_ascii=False)))
        names = list(metadata.category_names)
        for item in self.scene.items_for_save():
            for name in getattr(item, '_categories', []):
                if name not in names:
                    names.append(name)
        self.ex('INSERT OR REPLACE INTO prism_metadata VALUES (?, ?)',
                ('categories', json.dumps(names, ensure_ascii=False)))
        tag_names = list(getattr(metadata, 'tag_names', []))
        for item in self.scene.items_for_save():
            for name in getattr(item, '_tags', []):
                if name not in tag_names:
                    tag_names.append(name)
        self.ex('INSERT OR REPLACE INTO prism_metadata VALUES (?, ?)',
                ('tags', json.dumps(tag_names, ensure_ascii=False)))
        # The tag tree's mapping table (synonyms, implied parents) rides
        # along in the same key/value table; the tags themselves stay a
        # plain list of strings.
        self.ex('INSERT OR REPLACE INTO prism_metadata VALUES (?, ?)',
                ('tag_system', json.dumps(
                    tag_system(metadata).to_dict(), ensure_ascii=False)))
        # No commit here on purpose. The metadata used to be committed before
        # the item loop, which meant a cancelled save had already changed the
        # target even though the items were never written. Keeping the whole
        # save in one transaction is what makes "cancelled" able to mean
        # "nothing was written".
        for error in self.scene.items_by_type(PrismErrorItem.TYPE):
            source = getattr(error, '_asset_source', None)
            if not source:
                continue
            row = self.fetchone('SELECT id FROM items WHERE id=?', (error.original_save_id,))
            same_archive = pathlib.Path(source[0]).resolve() == pathlib.Path(self.filename).resolve()
            if same_archive and row:
                target_id = error.original_save_id
            else:
                with sqlite3.connect('file:' + pathlib.Path(source[0]).resolve().as_posix() + '?mode=ro', uri=True) as original:
                    record = original.execute('SELECT type,x,y,z,scale,rotation,flip,data FROM items WHERE id=?', (source[1],)).fetchone()
                    blob = original.execute('SELECT name,mode,mtime,sz,data FROM sqlar WHERE item_id=?', (source[1],)).fetchone()
                if not record or not blob:
                    raise ValueError('Cannot preserve damaged asset: original archive record is missing')
                self.ex('INSERT INTO items(type,x,y,z,scale,rotation,flip,data) VALUES (?,?,?,?,?,?,?,?)', record)
                target_id = self._cursor.lastrowid
                self.ex('INSERT INTO sqlar(name,item_id,mode,mtime,sz,data) VALUES (?,?,?,?,?,?)',
                        (f'{target_id}-' + (blob[0] or 'damaged-image'), target_id, *blob[1:]))
                error.original_save_id = target_id
                error._asset_source = (self.filename, target_id)
            metadata = dict(error._original_asset_metadata)
            metadata.pop('deletedAt', None)
            metadata.pop('_asset_source', None)
            metadata.pop('text', None)
            metadata.update(error.get_metadata_save_data())
            self.ex('UPDATE items SET data=? WHERE id=?', (json.dumps(metadata), target_id))
        to_delete = {row[0] for row in self.fetchall('SELECT id from ITEMS')}
        # We don't want to touch existing items that are displayed as errors:
        keep = {item.original_save_id
                for item in self.scene.items_by_type(PrismErrorItem.TYPE)}
        if keep:
            logger.debug('Preserving existing records for %d unavailable source items', len(keep))
        to_delete = to_delete - keep

        to_save = list(self.scene.items_for_save())
        saved = []
        inserted = []
        existing_ids = set(to_delete)
        # Writing an item changes the mapping between the item and its stored
        # source: the blob is dropped once the archive holds it, and a new row
        # takes a new save_id. A cancelled save has to put that mapping back,
        # otherwise the canvas would point at rows the rollback removed.
        previous_mapping = [(item, item.save_id,
                             getattr(item, '_source_path', None),
                             getattr(item, '_source_blob', None),
                             getattr(item, '_prism_source', None))
                            for item in to_save]
        if self.worker:
            self.worker.begin_processing.emit(len(to_save))
        for i, item in enumerate(to_save):
            logger.debug(f'Saving {item} with id {item.save_id}')
            if item.save_id in existing_ids:
                self.update_item(item)
                # `discard` 而不是 `remove`：同一个 save_id 出现两次
                # （比如复制的图元带着原件的 id 过来）时，第二次 remove
                # 会抛 KeyError —— 日志里出现过 13 次，让整次保存中断。
                # 那条记录本来就打算删掉的，丢掉它正是想要的结果。
                to_delete.discard(item.save_id)
            else:
                self.insert_item(item)
                inserted.append(item)
            saved.append(item)
            if (getattr(item, 'TYPE', None) == 'pixmap'
                    and (item._source_path or item._prism_source)):
                item._source_blob = None
            if self.worker:
                self.worker.progress.emit(i)
                if self.worker.canceled:
                    break
        if self.worker and self.worker.canceled:
            # The transaction is still open and nothing of this save has been
            # committed, so the cancel can be honoured honestly: roll the
            # transaction back and restore the in-memory mapping. The target
            # keeps the state it had before this save.
            self.connection.rollback()
            self._restore_item_mapping(previous_mapping, inserted)
            self._publish_write_result('rolled_back', cancelled=True,
                reason='Cancelled while writing; the transaction was rolled back')
            self.worker.finished.emit(self.filename, [
                _('Saving was canceled. Unsaved items remain on the canvas.')])
            return
        # Everything below this line can no longer be undone piece by piece.
        # The GUI disables Cancel here, and a cancel that still arrives is
        # reported as "too late" instead of pretending the save was undone.
        if self.worker is not None:
            commit_started = getattr(self.worker, 'commit_started', None)
            if commit_started is not None:
                commit_started.emit()
        if self.worker and self.worker.canceled:
            self.connection.rollback()
            self._restore_item_mapping(previous_mapping, inserted)
            self._publish_write_result('rolled_back', cancelled=True,
                reason='Cancelled before the commit; the transaction was rolled back')
            self.worker.finished.emit(self.filename, [
                _('Saving was canceled. Unsaved items remain on the canvas.')])
            return
        self.delete_items(to_delete)
        self.write_board_extras()
        # The commit is the disk fact. Fix it here, before anything that can
        # fail: a cache, a preview or a log line must never be able to turn a
        # committed save into a reported failure.
        try:
            self.connection.commit()
        except Exception as error:
            # The outcome of the commit is unknown: the database may or may
            # not have committed. Saying "nothing was written" would be a
            # guess, and a guess here loses data.
            self._publish_write_result('unknown',
                reason=f'The commit did not report a clear outcome: {error}')
            raise
        cancel_too_late = bool(self.worker and self.worker.canceled)
        self._publish_write_result('committed_complete',
            cancel_too_late=cancel_too_late,
            reason=('The commit had already completed when the cancel arrived'
                    if cancel_too_late else 'The transaction was committed'))
        if cancel_too_late and self.worker is not None:
            late = getattr(self.worker, 'cancel_too_late', None)
            if late is not None:
                late.emit()
        # Everything below is maintenance. It may fail; it must not change
        # the disk fact that was just published.
        try:
            from prism.source_store import remember_archive_sizes
            remember_archive_sizes(self.filename, dict(self.fetchall(
                'SELECT item_id, COALESCE(sz,length(data)) FROM sqlar')))
        except Exception:
            logger.exception('The save was committed, but the archive sizes '
                             'could not be remembered')
        try:
            for item in saved:
                if getattr(item, 'TYPE', None) == 'pixmap' and item.save_id:
                    item._prism_source = (str(self.filename), item.save_id)
                    item._source_path = None
                    item._source_blob = None
        except Exception:
            logger.exception('The save was committed, but the in-memory '
                             'source mapping could not be updated')
        try:
            # Newly written images get their previews built in the background
            thumbnail_cache.start_background_build()
        except Exception:
            logger.exception('The save was committed, but the preview build '
                             'could not be started')
        if self.worker:
            self.worker.finished.emit(self.filename, [])

    def _publish_write_result(self, commit_state, *, cancelled=False,
                              cancel_too_late=False, changed_entities=None,
                              changed_entities_complete=False,
                              changed_row_count=None, reason=''):
        """Publish plain facts about this transaction for the save adapter.

        The writer states what happened to its transaction and nothing else.
        The vocabulary, the user-facing message and the terminal state are
        owned by the save adapter (prism/fileio/save_preparation.py), which
        turns these plain values into the typed outcome.
        """
        if self.worker is None:
            return
        self.worker.save_write_result = {
            'commit_state': commit_state,
            'cancelled': bool(cancelled),
            'cancel_too_late': bool(cancel_too_late),
            # The identity list, when the writer knows every entity it
            # touched. None means "not known" and is not the same as empty.
            'changed_entities': changed_entities,
            'changed_entities_complete': bool(changed_entities_complete),
            # How many rows were touched. A count is not an identity list.
            'changed_row_count': changed_row_count,
            'reason': reason,
        }

    @staticmethod
    def _receipt_changed_row_count(receipt):
        """How many rows the partial receipt reports, or None when unknown.

        The receipt's ``changed_rows`` is an integer count of touched rows.
        It is NOT a list of identities, and turning it into one would claim
        that the writer knows which entities changed - it does not.
        """
        rows = getattr(receipt, 'changed_rows', None)
        if isinstance(rows, bool) or not isinstance(rows, int):
            return None
        return rows

    @staticmethod
    def _receipt_inserted_keys(receipt):
        """The identities of the rows the partial writer INSERTED.

        This is a subset: it says nothing about the rows that were updated or
        deleted, and nothing about metadata, domain or attachment changes.
        """
        inserted = getattr(receipt, 'inserted', None)
        if inserted is None:
            return None
        try:
            return tuple(inserted)
        except TypeError:
            return None

    def _restore_item_mapping(self, previous_mapping, inserted):
        """Put the in-memory mapping back after a rolled back save.

        Writing an item drops its blob and gives a new row a new save_id.
        After a rollback neither of those is true any more, so both have to
        go back, and an item that was inserted by this save must stop
        claiming that it lives in the archive.
        """
        archive = getattr(self.scene, '_prism_archive_items', None)
        for item, save_id, source_path, source_blob, prism_source in previous_mapping:
            item.save_id = save_id
            if getattr(item, 'TYPE', None) == 'pixmap':
                item._source_path = source_path
                item._source_blob = source_blob
                item._prism_source = prism_source
        if archive is not None:
            for item in inserted:
                archive.discard(item)

    def delete_items(self, to_delete):
        deleting = set(to_delete)
        from prism.source_store import retain_bytes
        for item in list(self.scene._prism_archive_items):
            source = item._prism_source
            if (source and source[1] in deleting
                    and pathlib.Path(source[0]).resolve() == pathlib.Path(self.filename).resolve()
                    and not item._source_path):
                blob = item.original_bytes()
                if blob:
                    item._source_path = retain_bytes(blob, '.' + (item.original_format() or 'png'))
                    item._source_blob = None
        to_delete = [(pk,) for pk in to_delete]
        self.exmany('DELETE FROM items WHERE id=?', to_delete)
        self.exmany('DELETE FROM sqlar WHERE item_id=?', to_delete)

    # ── Document note, attachments and mind map ──────────────────

    def _read_project_id(self):
        """Read the project id, and say why it could not be read.

        Three outcomes, kept apart on purpose:

        * the metadata table or the key is missing -- an older file, and
          the fallback id is used.  Nothing is wrong with the file.
        * the key is there -- that is the project's id.
        * the read failed -- the file is damaged, locked or unreadable.
          That reason is reported.  It used to be swallowed and turned
          into a random id, and the random id then showed up much later
          as "the material library is damaged" -- which hid the real
          cause behind a completely misleading message.
        """
        self._project_id_missing = False
        try:
            table = self.fetchone(
                "SELECT name FROM sqlite_master WHERE type='table' "
                "AND name='prism_metadata'")
        except sqlite3.Error as error:
            raise PrismFileIOError(
                f'读取工程元数据失败（{self.filename}）：{error}',
                self.filename) from error
        if not table:
            logger.info('No metadata table in %s; using a generated project id',
                        self.filename)
            self._project_id_missing = True
            return self._fallback_project_id()
        try:
            row = self.fetchone(
                "SELECT value FROM prism_metadata WHERE key='project_id'")
        except sqlite3.Error as error:
            raise PrismFileIOError(
                f'读取工程 project_id 失败（{self.filename}）：{error}',
                self.filename) from error
        if row and row[0]:
            return row[0]
        logger.info('No project id in %s; using a generated project id',
                    self.filename)
        self._project_id_missing = True
        return self._fallback_project_id()

    def _fallback_project_id(self):
        """The id used when the file does not carry one.

        It stays the one the scene already has, so repeated reads of the
        same file agree with each other.  A random id per read would make
        the material records of one file look foreign to the next read of
        the same file.
        """
        project_id = getattr(self.scene, 'project_id', None)
        if isinstance(project_id, str) and project_id.strip():
            return project_id
        return uuid.uuid4().hex

    def read_board_extras(self, scope=None):
        """Load the note, its attachments and the mind map tree.

        All three live outside the item tables, so a .prism file stays a
        single self contained file: dragging an image into the note embeds
        it here instead of pointing at a path on disk.

        ``scope=None`` 是旧的**全量**行为：文档正文（note_html）、脑图树
        （mindmap_tree）与每页完整 data 正文（workspace_pages[].content）
        全部物化。按页打开（scope 非 None）时这三类正文延迟加载：只读页面
        清单与轻量元数据（id/kind/title/tags/parent/permanent），正文留在
        文件里，按需经 :func:`load_deferred_board_extras` 物化；未物化字段
        登记到 ``scene._deferred_board_extras``，保存时由 ``write()`` 拒绝
        而不是把空值覆盖回文件。
        """
        self.scene.library_queries = []
        try:
            row = self.fetchone("SELECT value FROM prism_metadata WHERE key='library_queries'")
            value = json.loads(row[0]) if row and row[0] else []
            if isinstance(value, list):
                self.scene.library_queries = [q for q in value if isinstance(q, dict) and isinstance(q.get('title'), str)
                    and isinstance(q.get('text', ''), str) and isinstance(q.get('tag_expression', ''), str)
                    and isinstance(q.get('rating', 0), int) and 0 <= q.get('rating', 0) <= 5]
        except (sqlite3.OperationalError, ValueError, TypeError):
            pass

        self.scene.note_html = ''
        self.scene.attachments = {}
        self.scene.mindmap_tree = None
        self.scene.workspace_pages = []
        self.scene.workspace_nodes = []
        self.scene.material_records = []
        self.scene.model_records = []
        self.scene.material_library_state = {}
        self.scene.project_id = self._read_project_id()
        # 每次 read 都重置延迟标记：view 的 clear_scene 不清理这个私有属性，
        # 残留标记会让下一次全量打开后的保存被误拒。
        self.scene._deferred_board_extras = None
        self.scene._deferred_page_ids = set()
        self.scene._board_extras_source = None
        try:
            row = self.fetchone(
                "SELECT value FROM prism_metadata WHERE key='tags'")
            self.scene.tag_names = json.loads(row[0]) if row else []
        except (sqlite3.OperationalError, TypeError, ValueError):
            self.scene.tag_names = []
        try:
            row = self.fetchone(
                "SELECT value FROM prism_metadata WHERE key='tag_system'")
            self.scene.tag_system = TagSystem.from_dict(
                json.loads(row[0]) if row and row[0] else None)
        except (sqlite3.OperationalError, TypeError, ValueError):
            self.scene.tag_system = TagSystem()
        try:
            if scope is None:
                row = self.fetchone(
                    'SELECT html FROM board_notes WHERE board_id = 0')
                if row and row[0]:
                    self.scene.note_html = row[0]
            # 只读元数据，blob 留在文件里按需取。以前这里连 blob 一起读
            # 进来常驻内存 —— 秋雨参考.prism 里 406 个附件共 123.9 MB，
            # 而打开工程时一张都不需要显示：附件只在文档面板、脑图面板
            # 和导出时用到。
            from prism.fileio.attachments_store import load_attachments
            self.scene.attachments = load_attachments(
                self, board_id=0, filename=self.filename)
            if scope is None:
                row = self.fetchone(
                    'SELECT tree FROM board_mindmap WHERE board_id = 0')
                if row and row[0]:
                    self.scene.mindmap_tree = json.loads(row[0])
            table = self.fetchone(
                "SELECT name FROM sqlite_master WHERE type='table' "
                "AND name='workspace_pages'")
            if table:
                if scope is None:
                    for page_id, kind, title, data in self.fetchall(
                            'SELECT id, kind, title, data FROM workspace_pages '
                            'ORDER BY sort_order, rowid'):
                        try:
                            content = json.loads(data) if data else None
                        except (TypeError, ValueError):
                            content = None
                        tags = []
                        parent = None
                        permanent = False
                        if isinstance(content, dict) and content.get(
                                PAGE_PAYLOAD_KEY):
                            tags = list(content.get('tags', []))
                            parent = content.get('parent')
                            permanent = bool(content.get('permanent'))
                            content = content.get('content')
                        self.scene.workspace_pages.append({
                            'id': page_id, 'kind': kind, 'title': title,
                            'content': content, 'tags': tags,
                            'parent': parent, 'permanent': permanent})
                else:
                    # 按页打开：页面清单与轻量元数据仍全量，只有每页 data
                    # 正文（content）延迟。page dict 不含 'content' 键 ——
                    # partial 保存的 _page_patches 会对缺失的 content 回填
                    # 磁盘原值，正文不会被空值覆盖。
                    for page_id, kind, title, data in self._workspace_page_headers():
                        try:
                            envelope = json.loads(data) if data else None
                        except (TypeError, ValueError):
                            envelope = None
                        tags = []
                        parent = None
                        permanent = False
                        if isinstance(envelope, dict) and envelope.get(
                                PAGE_PAYLOAD_KEY):
                            tags = list(envelope.get('tags', []))
                            parent = envelope.get('parent')
                            permanent = bool(envelope.get('permanent'))
                        self.scene.workspace_pages.append({
                            'id': page_id, 'kind': kind, 'title': title,
                            'tags': tags, 'parent': parent,
                            'permanent': permanent})
            table = self.fetchone(
                "SELECT name FROM sqlite_master WHERE type='table' "
                "AND name='workspace_nodes'")
            if table:
                for (payload,) in self.fetchall(
                        'SELECT payload FROM workspace_nodes ORDER BY rowid'):
                    try:
                        node = json.loads(payload)
                    except (TypeError, ValueError) as exc:
                        raise ValueError('资源树节点数据损坏，已停止读取以避免覆盖') from exc
                    if not isinstance(node, dict) or not node.get('id'):
                        raise ValueError('资源树节点数据无效，已停止读取以避免覆盖')
                    self.scene.workspace_nodes.append(node)
        except sqlite3.OperationalError:
            # A file written before these tables existed; the migration
            # recreates them on the next save.
            logger.debug('No note/attachment tables in this file')

        table = self.fetchone(
            "SELECT name FROM sqlite_master WHERE type='table' "
            "AND name='material_records'")
        if table:
            try:
                material_rows=self.fetchall('SELECT id,payload FROM material_records ORDER BY rowid')
                records = [json.loads(payload) for (_key,payload) in material_rows]
                from prism.material_service import (validate_material_records,
                                                    rebind_material_records)
                validate_material_records(records, self.scene.project_id)
                rebind_material_records(self.scene, records,
                                        project_id=self.scene.project_id)
                from prism.library_observability import record_rows
                record_rows('material',material_rows,'read_validated',self.scene.project_id)
            except (ValueError, TypeError) as exc:
                # 说真话：把真正的原因写进消息，别让"工程 id 读不到"和
                # "记录真的坏了"都变成同一句"材质库数据损坏"。
                reason = str(exc)
                if getattr(self, '_project_id_missing', False):
                    raise ValueError(
                        '材质库记录与工程 id 对不上，而这个工程没有存 '
                        f'project_id（老工程没有这一项），无法比对：{reason}'
                    ) from exc
                raise ValueError(
                    '材质库数据损坏，已停止读取以避免覆盖：'
                    f'{reason}') from exc
            self.scene.material_records = records
        table = self.fetchone(
            "SELECT name FROM sqlite_master WHERE type='table' "
            "AND name='model_records'")
        if table:
            try:
                models = []
                model_rows=self.fetchall('SELECT id,payload FROM model_records ORDER BY rowid')
                for (_key,payload) in model_rows:
                    try:
                        model = json.loads(payload)
                    except (TypeError, ValueError) as exc:
                        raise ValueError('模型库数据损坏，已停止读取以避免覆盖') from exc
                    if not isinstance(model, dict) or not model.get('id'):
                        raise ValueError('模型库数据无效，已停止读取以避免覆盖')
                    models.append(model)
                from prism.model_library.project_storage import validate_model_records
                validate_model_records(models, self.scene.project_id)
                from prism.library_observability import record_rows
                record_rows('model',model_rows,'read_validated',self.scene.project_id)
            except (ValueError, TypeError) as exc:
                raise ValueError('模型库数据损坏，已停止读取以避免覆盖：' + str(exc)) from exc
            self.scene.model_records = models
        try:
            row = self.fetchone(
                "SELECT value FROM prism_metadata WHERE key='material_library_state'")
            state = json.loads(row[0]) if row and row[0] else {}
            if isinstance(state, dict):
                self.scene.material_library_state = state
        except (sqlite3.OperationalError, ValueError, TypeError):
            logger.debug('No saved material-library navigation state')
        if scope is not None:
            self.scene._deferred_board_extras = {
                'note_html', 'mindmap_tree', 'workspace_pages'}
            self.scene._deferred_page_ids = {
                page['id'] for page in self.scene.workspace_pages}
            self.scene._board_extras_source = str(self.filename)

    def _workspace_page_headers(self):
        """Do not transfer all editor bodies just to construct the page tree.

        SQLite still needs to inspect old JSON envelopes, but only the header
        crosses into Python. A ready sidecar can eventually avoid that scan;
        this projection does not pretend to eliminate source-page IO.
        """
        try:
            return self._read_rows('workspace_headers',
                "SELECT id,kind,title,CASE WHEN json_valid(data) THEN "
                "CASE WHEN json_type(data)='object' THEN "
                "json_remove(data,'$.content') ELSE NULL END ELSE NULL END "
                'FROM workspace_pages ORDER BY sort_order,rowid')
        except sqlite3.OperationalError as error:
            if 'no such function' not in str(error).lower():
                raise
            # Older packaged SQLite builds may lack JSON1. Preserve support,
            # explicitly reporting that their compatibility path reads bodies.
            logger.warning('SQLite JSON projection unavailable; reading page envelopes')
            return self._read_rows('workspace_headers_compatibility',
                'SELECT id,kind,title,data FROM workspace_pages ORDER BY sort_order,rowid')

    def write_board_extras(self):
        """Persist the note, its attachments and the mind map tree."""
        # This writes rows of its own (notes, attachments, the page
        # list, the material records), so it needs the same licence
        # check as `write()`: a project that was opened read-only must
        # not be written back, not even by the path that happens to
        # skip the item loop.
        self._guard_readonly_project()
        scene = self.board_snapshot if self.board_snapshot is not None else self.scene
        # Created here as well as in the schema, mirroring how write_data
        # handles prism_metadata: a file that was never fully initialised
        # still saves instead of failing on a missing table.
        self.ex('CREATE TABLE IF NOT EXISTS board_notes ('
                'id INTEGER PRIMARY KEY,'
                ' board_id INTEGER NOT NULL DEFAULT 0,'
                " html TEXT NOT NULL DEFAULT '',"
                ' updated_at INTEGER NOT NULL DEFAULT 0)')
        self.ex('CREATE TABLE IF NOT EXISTS attachments ('
                'id INTEGER PRIMARY KEY AUTOINCREMENT,'
                ' board_id INTEGER NOT NULL DEFAULT 0,'
                ' name TEXT NOT NULL, mime TEXT, blob BLOB,'
                ' created_at INTEGER NOT NULL DEFAULT 0)')
        self.ex('CREATE TABLE IF NOT EXISTS board_mindmap ('
                'board_id INTEGER PRIMARY KEY, tree JSON,'
                ' updated_at INTEGER NOT NULL DEFAULT 0)')
        self.ex('CREATE TABLE IF NOT EXISTS workspace_pages ('
                'id TEXT PRIMARY KEY, kind TEXT NOT NULL,'
                ' title TEXT NOT NULL, data JSON,'
                ' sort_order INTEGER NOT NULL DEFAULT 0)')
        self.ex('CREATE TABLE IF NOT EXISTS workspace_nodes ('
                'id TEXT PRIMARY KEY, payload JSON NOT NULL)')
        self.ex('CREATE TABLE IF NOT EXISTS material_records ('
                'id TEXT PRIMARY KEY, payload JSON NOT NULL)')
        # The model domain owns its table definition; the writer only
        # makes sure it exists (with the indexed projections).
        from prism.model_library.project_storage import ensure_model_tables
        ensure_model_tables(self.connection)
        self.ex('CREATE TABLE IF NOT EXISTS prism_metadata ('
                'key TEXT PRIMARY KEY, value TEXT)')
        self.ex('INSERT INTO prism_metadata VALUES (?, ?) '
                'ON CONFLICT(key) DO UPDATE SET value=excluded.value '
                'WHERE prism_metadata.value IS NOT excluded.value',
                ('project_id', scene.project_id))
        now = int(time.time())
        html = getattr(scene, 'note_html', '') or ''
        # board_id is not UNIQUE in old files. INSERT OR REPLACE appended a
        # new note on every save while read_board_extras kept reading the
        # first, stale row. Update the row the reader actually uses.
        note = self.fetchone('SELECT id FROM board_notes WHERE board_id=0 LIMIT 1')
        if note:
            self.ex('UPDATE board_notes SET html=?, updated_at=? '
                    'WHERE id=? AND html IS NOT ?', (html, now, note[0], html))
        else:
            self.ex('INSERT INTO board_notes (board_id, html, updated_at) '
                    'VALUES (0, ?, ?)', (html, now))

        attachments = getattr(scene, 'attachments', {}) or {}
        entries = (attachments.entries_for_save(self.connection, self.filename)
                   if hasattr(attachments, 'entries_for_save') else attachments.items())
        saved_versions = []
        for attachment_id, entry in entries:
            if attachment_id in getattr(attachments, '_dirty', ()):
                saved_versions.append((attachment_id, entry))
            name, mime, blob = entry
            if blob is None:
                raise ValueError(f'Original attachment {attachment_id} is unreadable; save stopped')
            self.ex('INSERT OR REPLACE INTO attachments '
                    '(id, board_id, name, mime, blob, created_at) '
                    'VALUES (?, 0, ?, ?, ?, ?)',
                    (attachment_id, name, mime, blob, now))

        # Attachments removed from the note are dropped from the file too.
        keep = set((getattr(scene, 'attachments', {}) or {}).keys())
        # The originals of a model live in the same table but belong to
        # the model domain.  The note's cleanup must not delete them.
        keep |= _model_attachment_ids(getattr(scene, 'model_records', ()) or ())
        existing = {row[0] for row in self.fetchall(
            'SELECT id FROM attachments WHERE board_id = 0')}
        stale = [(pk,) for pk in existing - keep]
        if stale:
            self.exmany('DELETE FROM attachments WHERE id=?', stale)

        tree = getattr(scene, 'mindmap_tree', None)
        if tree is not None:
            self.ex('INSERT INTO board_mindmap '
                    '(board_id, tree, updated_at) VALUES (0, ?, ?) '
                    'ON CONFLICT(board_id) DO UPDATE SET tree=excluded.tree, '
                    'updated_at=excluded.updated_at '
                    'WHERE board_mindmap.tree IS NOT excluded.tree',
                    (json.dumps(tree, ensure_ascii=False), now))
        page_rows = []
        for order, page in enumerate(
                getattr(scene, 'workspace_pages', []) or []):
            page_rows.append(
                (page.get('id'), page.get('kind'), page.get('title'),
                 json.dumps({PAGE_PAYLOAD_KEY: True,
                             'content': page.get('content'),
                             'tags': page.get('tags', []),
                             'parent': page.get('parent'),
                             'permanent': bool(page.get('permanent'))},
                            ensure_ascii=False), order))
        self._sync_extra_rows('workspace_pages',
                              ('id', 'kind', 'title', 'data', 'sort_order'), page_rows)
        from prism.material_service import validate_material_records
        records = getattr(scene, 'material_records', [])
        validate_material_records(records, scene.project_id)
        self._sync_extra_rows('material_records', ('id', 'payload'), [
            (record['id'], json.dumps(record, ensure_ascii=False, allow_nan=False))
            for record in records])
        self.ex('INSERT INTO prism_metadata VALUES (?, ?) '
                'ON CONFLICT(key) DO UPDATE SET value=excluded.value '
                'WHERE prism_metadata.value IS NOT excluded.value',
                ('material_library_state', json.dumps(
                    getattr(scene, 'material_library_state', {}), ensure_ascii=False)))
        models = list(getattr(scene, 'model_records', []) or [])
        from prism.model_library.project_storage import validate_model_records
        validate_model_records(models, scene.project_id)
        self._sync_extra_rows('model_records', ('id', 'payload'), [
            (model['id'], json.dumps(model, ensure_ascii=False, allow_nan=False))
            for model in models])
        self._sync_extra_rows('workspace_nodes', ('id', 'payload'), [
            (node['id'], json.dumps(node, ensure_ascii=False))
            for node in getattr(scene, 'workspace_nodes', []) or []])
        self.connection.commit()
        if hasattr(attachments, 'saved_to'):
            attachments.saved_to(self.filename, saved_versions)

    def _sync_extra_rows(self, table, columns, rows):
        """Keep stable rows and write only changed page/material metadata.

        Table and column names are internal schema constants, never user input.
        Source blobs use their separate attachment/sqlar lifecycle unchanged.
        """
        if table in ('material_records','model_records'):
            from prism.library_observability import record_rows
            record_rows('material' if table=='material_records' else 'model',rows,'write_prepared',
                        getattr(self.board_snapshot or self.scene,'project_id',None))
        existing = {row[0] for row in self.fetchall(f'SELECT id FROM {table}')}
        desired = {row[0] for row in rows}
        fields = columns[1:]
        updates = ', '.join(f'{field}=excluded.{field}' for field in fields)
        changed = ' OR '.join(f'{table}.{field} IS NOT excluded.{field}' for field in fields)
        placeholders = ','.join('?' for _ in columns)
        self.exmany(f'INSERT INTO {table} ({",".join(columns)}) VALUES ({placeholders}) '
                    f'ON CONFLICT(id) DO UPDATE SET {updates} WHERE {changed}', rows)
        self.exmany(f'DELETE FROM {table} WHERE id=?', [(key,) for key in existing-desired])

    def insert_item(self, item):
        self.ex(
            'INSERT INTO items (type, x, y, z, scale, rotation, flip, '
            'data) '
            'VALUES (?, ?, ?, ?, ?, ?, ?, ?)',
            (item.TYPE, item.pos().x(), item.pos().y(), item.zValue(),
             item.scale(), item.rotation(), item.flip(),
             json.dumps(item.get_extra_save_data())))
        item.save_id = self.cursor.lastrowid
        if getattr(item, 'TYPE', None) == 'pixmap':
            self.scene._prism_archive_items.add(item)

        if hasattr(item, 'TYPE') and item.TYPE == 'video':
            # Video items: embed blob in sqlar, or reference-only (skip)
            video_blob = getattr(item, '_video_blob', None)
            if video_blob and not getattr(item, '_is_reference', False):
                name = item.get_filename_for_export('video')
                self.ex(
                    'INSERT INTO sqlar (item_id, name, mode, sz, data) '
                    'VALUES (?, ?, ?, ?, ?)',
                    (item.save_id, name, 0o644, len(video_blob),
                     video_blob))
            elif not getattr(item, '_is_reference', False) and (
                    getattr(item, '_video_source_path', None) or getattr(item, '_prism_source', None)):
                # 内嵌视频可能尚未物化（打开只登记源引用）；保存前按需物化，
                # 否则会把 _video_source_path=None 当"没有视频"跳过写入。
                if hasattr(item, '_ensure_materialized'):
                    item._ensure_materialized()
                path = item._video_source_path
                size = os.path.getsize(path)
                name = item.get_filename_for_export('video')
                self.ex('INSERT INTO sqlar (item_id,name,mode,sz,data) VALUES (?,?,?,?,zeroblob(?))',
                        (item.save_id, name, 0o644, size, size))
                rowid = self.fetchone('SELECT rowid FROM sqlar WHERE item_id=?', (item.save_id,))[0]
                with self._connection.blobopen('sqlar', 'data', rowid, readonly=False) as output:
                    with open(path, 'rb') as stream:
                        shutil.copyfileobj(stream, output, length=1024*1024)
        elif getattr(item, 'TYPE', None) == 'glb':
            # Model items: embed the file into sqlar, or reference-only.
            # Must precede the pixmap branch below, which would otherwise
            # capture them because they are pixmap items too.
            glb_blob = getattr(item, '_glb_blob', None)
            if glb_blob and not getattr(item, '_is_reference', False):
                name = item.get_filename_for_export('glb')
                self.ex(
                    'INSERT INTO sqlar (item_id, name, mode, sz, data) '
                    'VALUES (?, ?, ?, ?, ?)',
                    (item.save_id, name, 0o644, len(glb_blob), glb_blob))
        elif getattr(item, 'TYPE', None) == 'pixmap' and hasattr(item, 'pixmap_to_bytes'):
            # Keep the imported bytes exactly as received.  Preview
            # adjustments and the user's storage-format preference must not
            # silently turn a PNG/HDR/other source into a new lossy image
            # when the .prism file is saved.
            pixmap = item.original_bytes() if hasattr(item, 'original_bytes') else None
            imgformat = item.original_format() if pixmap and hasattr(item, 'original_format') else None
            if not pixmap:
                if getattr(item, '_source_path', None) or getattr(item, '_prism_source', None):
                    raise PrismFileIOError(msg='Original image is missing; saving was stopped to preserve the project.', filename=item.filename)
                from prism.image_source import generated_png_bytes
                pixmap, imgformat = generated_png_bytes(item)
            imgformat = imgformat or 'png'
            name = item.get_filename_for_export(imgformat)
            self.ex(
                'INSERT INTO sqlar (item_id, name, mode, sz, data) '
                'VALUES (?, ?, ?, ?, ?)',
                (item.save_id, name, 0o644, len(pixmap), pixmap))
            # The image now lives in the project file; queue a preview so
            # that opening this project next time does not decode it.
            if not item.get_extra_save_data().get('displayThumbnail'):
                thumbnail_cache.defer_preview(
                    thumbnail_cache.digest(pixmap), pixmap)
        if not getattr(self, '_batch_write', False):
            self.connection.commit()

    def update_item(self, item):
        """Update item data.

        We only update the item data, not the pixmap data, as pixmap
        data never changes and is also time-consuming to save.
        """
        self.ex(
            'UPDATE items SET x=?, y=?, z=?, scale=?, rotation=?, flip=?, '
            'data=? '
            'WHERE id=?',
            (item.pos().x(), item.pos().y(), item.zValue(), item.scale(),
             item.rotation(), item.flip(),
             json.dumps(item.get_extra_save_data()),
             item.save_id))
        if not getattr(self, '_batch_write', False):
            self.connection.commit()




def _model_attachment_ids(records):
    """The project attachments the model domain owns.

    The originals of a model are stored in the project's attachment table.
    They are not pictures of the note, so the note's own cleanup must leave
    them alone.
    """
    from prism.model_library.project_storage import model_attachment_ids
    owned = set()
    for record in records or ():
        if not isinstance(record, dict):
            continue
        owned.update(model_attachment_ids(record))
    return owned

def load_deferred_board_extras(scene, names=None, *, page_ids=None):
    """按需物化按页打开时延迟的文档/脑图/页面正文。

    ``SQLiteIO.read_board_extras(scope=...)`` 在按页打开时只登记
    ``scene._deferred_board_extras`` 与 ``scene._board_extras_source``，
    不把正文读到 scene 上。文档/脑图面板（以及保存前）应调用本函数把
    需要的正文补上；物化成功后从 ``scene._deferred_board_extras`` 移除
    对应字段。失败时保留标记，``SQLiteIO.write`` 会拒绝保存而不是把
    空值覆盖回文件。
    """
    deferred = getattr(scene, '_deferred_board_extras', None)
    if not deferred:
        return
    wanted = set(deferred) if names is None else (set(names) & set(deferred))
    if not wanted:
        return
    source = getattr(scene, '_board_extras_source', None)
    if not source:
        raise ValueError('Deferred editor content has no source project')
    done = set()
    with closing(sqlite3.connect(
            pathlib.Path(source).resolve().as_uri() + '?mode=ro', uri=True)) as connection:
        tables = {row[0] for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        if 'note_html' in wanted:
            row = (connection.execute(
                'SELECT html FROM board_notes WHERE board_id=0').fetchone()
                if 'board_notes' in tables else None)
            scene.note_html = row[0] if row and row[0] else ''
            done.add('note_html')
        if 'mindmap_tree' in wanted:
            row = (connection.execute(
                'SELECT tree FROM board_mindmap WHERE board_id=0').fetchone()
                if 'board_mindmap' in tables else None)
            scene.mindmap_tree = json.loads(row[0]) if row and row[0] else None
            done.add('mindmap_tree')
        if 'workspace_pages' in wanted:
            _materialize_workspace_pages(scene, connection, page_ids=page_ids)
            if not getattr(scene, '_deferred_page_ids', ()):
                done.add('workspace_pages')
    deferred.difference_update(done)


def _materialize_workspace_pages(scene, connection, *, page_ids=None):
    """把每页 data 正文（content）补回 scene.workspace_pages，只读不改行。"""
    pages = getattr(scene, 'workspace_pages', None) or []
    by_id = {page.get('id'): page for page in pages}
    if not by_id:
        scene._deferred_page_ids = set()
        return
    # Presence, rather than truthiness, is important: ''/None may be a user's
    # explicit clear. Never replace content already supplied by an editor.
    pending = set(getattr(scene, '_deferred_page_ids', by_id)) & by_id.keys()
    pending.difference_update(key for key, page in by_id.items() if 'content' in page)
    scene._deferred_page_ids = pending
    wanted = pending if page_ids is None else pending.intersection(page_ids)
    keys = tuple(wanted)
    for start in range(0, len(keys), 128):
        batch = keys[start:start+128]
        placeholders = ','.join('?' for _ in batch)
        rows = connection.execute(
            'SELECT id, data FROM workspace_pages WHERE id IN ('
            + placeholders + ')', batch)
        missing = set(batch)
        for page_id, data in rows:
            envelope = json.loads(data) if data else None
            # Legacy page payloads were stored without the envelope.
            content = (envelope.get('content') if isinstance(envelope, dict)
                       and envelope.get(PAGE_PAYLOAD_KEY) else envelope)
            by_id[page_id]['content'] = content
            pending.discard(page_id)
            missing.discard(page_id)
        if missing:
            raise ValueError('Deferred pages disappeared from source: '
                             + ', '.join(sorted(missing)))


#: The marker a stored page body carries. A page row holds the body itself
#: (projects written before the envelope existed) or an envelope with the
#: body under this key.
PAGE_PAYLOAD_KEY = '__prism_page_payload__'


@dataclasses.dataclass(frozen=True)
class StoredBody:
    """What a project file holds, read without touching the scene.

    Three outcomes are kept apart, because they are three different facts:
    the file could not be read at all, the file has no such page, or the
    file has the page and here is its body.

    ``content`` is ``None`` both when the page is missing and when its body
    is empty; ``found`` tells the two apart.
    """
    found: bool = False
    content: object = None
    error: object = None
    # The same read's exact stored column, for lossless migration and for
    # distinguishing SQL NULL from JSON null / an empty body. No second query.
    raw_data: object = None
    raw_data_available: bool = False

    @property
    def readable(self):
        """True when the file could be read (whether or not it had the page)."""
        return self.error is None


@dataclasses.dataclass(frozen=True)
class StoredAttachment:
    """One attachment as it is stored in a project file, read-only."""
    found: bool = False
    name: str = ''
    mime: str = ''
    blob: bytes | None = None
    error: object = None

    @property
    def readable(self):
        return self.error is None


def _read_only_connection(source):
    """A short-lived read-only handle on a project file.

    The handle is opened for one statement and closed again right away.
    Holding it for a whole export would keep the user's project file locked -
    on Windows the user could not even move it while the export ran, and a
    failed export would leave the file pinned.
    """
    uri = pathlib.Path(source).resolve().as_uri() + '?mode=ro'
    return sqlite3.connect(uri, uri=True)


def read_page_body(source, page_id):
    """Read one page's stored body from a project file, read-only.

    Returns a :class:`StoredBody`. This function never touches the scene:
    materialising a body into the live scene is a different operation with a
    different owner (:func:`load_deferred_board_extras`), because it changes
    what the user is editing.

    An export uses this to write the text of a page the user never opened.
    The body it gets is the body that is in the file - not the one the user
    is typing, which the export takes from the scene instead.
    """
    if not source:
        return StoredBody(error=ValueError('No project file to read from'))
    try:
        with closing(_read_only_connection(source)) as connection:
            row = connection.execute(
                'SELECT data FROM workspace_pages WHERE id=?', (page_id,)).fetchone()
    except (sqlite3.Error, OSError) as error:
        return StoredBody(error=error)
    if row is None:
        return StoredBody(found=False)
    data = row[0]
    if not data:
        return StoredBody(found=True, content=None, raw_data=data, raw_data_available=True)
    try:
        envelope = json.loads(data)
    except (TypeError, ValueError) as error:
        return StoredBody(found=True, error=error, raw_data=data, raw_data_available=True)
    # Legacy page payloads were stored without the envelope.
    content = (envelope.get('content') if isinstance(envelope, dict)
               and envelope.get(PAGE_PAYLOAD_KEY) else envelope)
    return StoredBody(found=True, content=content, raw_data=data, raw_data_available=True)


def read_attachment(source, attachment_id):
    """Read one attachment from a project file, read-only.

    Returns a :class:`StoredAttachment`. Like :func:`read_page_body` this
    never touches the scene: an export that pulls a picture out of the
    project file must not put it into the document the user is editing.
    """
    if not source:
        return StoredAttachment(error=ValueError('No project file to read from'))
    try:
        with closing(_read_only_connection(source)) as connection:
            row = connection.execute(
                'SELECT name, mime, blob FROM attachments WHERE id=?',
                (attachment_id,)).fetchone()
    except (sqlite3.Error, OSError) as error:
        return StoredAttachment(error=error)
    if row is None:
        return StoredAttachment(found=False)
    blob = row[2]
    if blob is None:
        from prism.storage.managed import root_for, binding, read_bytes
        if root_for(source) is not None and binding(source, 'attachments', attachment_id) is not None:
            blob = read_bytes(source, 'attachments', attachment_id)
    return StoredAttachment(found=True, name=row[0] or '',
                            mime=row[1] or '', blob=blob)
