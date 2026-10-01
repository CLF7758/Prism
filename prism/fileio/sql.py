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

import json
import logging
import os
import pathlib
import shutil
import sqlite3
import tempfile
import time
import uuid

from PyQt6 import QtGui
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


def is_prism_file(path):
    """Check whether the file at the given path is a bee file."""

    return os.path.splitext(path)[1] == '.prism'


def handle_sqlite_errors(func):
    def wrapper(self, *args, **kwargs):
        try:
            func(self, *args, **kwargs)
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
                 worker=None):
        self.scene = scene
        self.create_new = create_new
        self.filename = filename
        self.readonly = readonly
        self.worker = worker
        self.retry = False

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

    def _backup_before_overwrite(self):
        """Keep a copy of a file we are about to rebuild from scratch."""
        backup = f'{self.filename}.bak'
        try:
            shutil.copyfile(self.filename, backup)
            logger.warning(
                'Unreadable file kept as %s before rebuilding', backup)
        except OSError:
            logger.exception('Could not back up %s', self.filename)

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

    def _establish_connection(self):
        # A previous connection may still be open - for example when a
        # failed migration asked for a rebuild.  Windows refuses to replace
        # a file while the old handle is alive, so drop it first.
        self._close_connection()

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
            uri = f'{uri}?mode=rw'
        self._connection = sqlite3.connect(uri, uri=True)
        self._cursor = self.connection.cursor()
        if not self.create_new:
            try:
                self._migrate()
            except Exception:
                # Updating a file failed; try creating it from scratch instead
                logger.exception('Error migrating bee file')
                self.create_new = True
                self._establish_connection()

    def _migrate(self):
        """Migrate database if necessary."""

        version = self.fetchone('PRAGMA user_version')[0]
        logger.debug(f'Found bee file version: {version}')
        if version >= USER_VERSION:
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
            try:
                # See whether file is writable so we can migrate it directly
                self.ex('PRAGMA application_id=%s' % APPLICATION_ID)
            except sqlite3.Error:
                logger.debug('File not writable; use temporary copy instead')
                self._connection.close()
                self._tmpdir = tempfile.TemporaryDirectory(
                    prefix=constants.APPNAME)
                tmpname = os.path.join(self._tmpdir.name, 'mig.prism')
                shutil.copyfile(self.filename, tmpname)
                self._connection = sqlite3.connect(tmpname)
                self._cursor = self.connection.cursor()

        self.ex('BEGIN TRANSACTION')
        for i in range(version, USER_VERSION):
            logger.debug(f'Migrating from version {i} to {i + 1}...')
            for migration in MIGRATIONS[i + 1]:
                self.ex(migration)
        self.write_meta()
        self.connection.commit()
        logger.debug('Migration finished')

    @property
    def connection(self):
        if not hasattr(self, '_connection'):
            self._establish_connection()
        return self._connection

    @property
    def cursor(self):
        if not hasattr(self, '_cursor'):
            self._establish_connection()
        return self._cursor

    def ex(self, *args, **kwargs):
        return self.cursor.execute(*args, **kwargs)

    def exmany(self, *args, **kwargs):
        return self.cursor.executemany(*args, **kwargs)

    def fetchone(self, *args, **kwargs):
        self.ex(*args, **kwargs)
        return self.cursor.fetchone()

    def fetchall(self, *args, **kwargs):
        self.ex(*args, **kwargs)
        return self.cursor.fetchall()

    def write_meta(self):
        self.ex('PRAGMA application_id=%s' % APPLICATION_ID)
        self.ex('PRAGMA user_version=%s' % USER_VERSION)
        self.ex('PRAGMA foreign_keys=ON')

    def create_schema_on_new(self):
        if self.create_new:
            self.write_meta()
            for schema in SCHEMA:
                self.ex(schema)

    @handle_sqlite_errors
    def read(self):
        has_metadata = self.fetchone(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='prism_metadata'")
        if has_metadata:
            row = self.fetchone("SELECT value FROM prism_metadata WHERE key='categories'")
            self.scene.category_names = json.loads(row[0]) if row else []
        else:
            from prism.config import PrismSettings
            legacy = PrismSettings().value('UserCategories/list', [])
            self.scene.category_names = legacy if isinstance(legacy, list) else []
        rows = self.fetchall(
            'SELECT items.id, type, x, y, z, scale, rotation, flip, '
            'items.data, sqlar.data '
            'FROM sqlar JOIN items on sqlar.item_id = items.id')
        # Avoid OUTER JOIN for performance reasons; fetch text items
        # and referenced video items separately
        rows.extend(self.fetchall(
            'SELECT items.id, type, x, y, z, scale, rotation, flip, '
            ' items.data, null as data '
            'FROM items '
            'WHERE items.type = "text" '
            'UNION ALL '
            'SELECT items.id, type, x, y, z, scale, rotation, flip, '
            ' items.data, null as data '
            'FROM items '
            'WHERE items.type = "video" AND items.id NOT IN '
            ' (SELECT item_id FROM sqlar) '
            'UNION ALL '
            'SELECT items.id, type, x, y, z, scale, rotation, flip, '
            ' items.data, null as data '
            'FROM items '
            'WHERE items.type = "glb" AND items.id NOT IN '
            ' (SELECT item_id FROM sqlar)'))
        if self.worker:
            self.worker.begin_processing.emit(len(rows))

        for i, row in enumerate(rows):
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
                item = PrismPixmapItem(QtGui.QImage())
                if not item.load_stored_image(row[9]):
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
                        + IMG_LOADING_ERROR_MSG)
                    data['type'] = PrismErrorItem.TYPE
                else:
                    if 'colorGroup' not in data['data']:
                        item.analyze_color_group()
                data['item'] = item

            elif data['type'] == 'video':
                blob = row[9]
                vdata = data['data']
                if blob:
                    # Embedded video: materialize a temp file for
                    # playback and thumbnail capture, keep it around
                    # until the item is cleaned up.
                    ext = os.path.splitext(
                        vdata.get('filename', ''))[1] or '.mp4'
                    tmp = tempfile.NamedTemporaryFile(
                        suffix=ext, delete=False)
                    tmp.write(blob)
                    tmp.close()
                    item = PrismVideoItem(
                        video_url=QUrl.fromLocalFile(tmp.name),
                        filename=vdata.get('filename'),
                        video_blob=blob)
                    item._tmp_video_file = tmp
                    data['item'] = item
                elif vdata.get('videoPath'):
                    path = vdata['videoPath']
                    if os.path.exists(path):
                        item = PrismVideoItem(
                            video_url=QUrl.fromLocalFile(path),
                            filename=vdata.get('filename'))
                        item._is_reference = True
                        data['item'] = item
                    else:
                        data['data'] = {
                            'text': f'Video file missing: {path}'}
                        data['type'] = PrismErrorItem.TYPE
                else:
                    data['data'] = {'text': 'Video data missing'}
                    data['type'] = PrismErrorItem.TYPE

            elif data['type'] == 'glb':
                blob = row[9]
                gdata = data['data']
                if blob:
                    # Embedded model: parsed straight from the bytes, no
                    # temporary file needed since nothing replays it.
                    item = PrismGlbItem(
                        filename=gdata.get('filename'), glb_blob=blob)
                    data['item'] = item
                elif gdata.get('modelPath'):
                    path = gdata['modelPath']
                    if os.path.exists(path):
                        item = PrismGlbItem(
                            filename=gdata.get('filename'), glb_path=path)
                        item._is_reference = True
                        data['item'] = item
                    else:
                        data['data'] = {
                            'text': f'3D model file missing: {path}'}
                        data['type'] = PrismErrorItem.TYPE
                else:
                    data['data'] = {'text': '3D model data missing'}
                    data['type'] = PrismErrorItem.TYPE

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
        self.read_board_extras()
        # Images that were decoded the slow way are turned into cached
        # previews in the background, so the next open can skip it.
        thumbnail_cache.start_background_build()
        if self.worker:
            self.worker.finished.emit(self.filename, [])

    @handle_sqlite_errors
    def write(self):
        if self.readonly:
            raise sqlite3.OperationalError(
                'Attempt to write to a readonly database')
        try:
            self.create_schema_on_new()
            self.write_data()
        except Exception:
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
                self.write()

    def write_data(self):
        self.ex('CREATE TABLE IF NOT EXISTS prism_metadata (key TEXT PRIMARY KEY, value TEXT)')
        names = list(self.scene.category_names)
        for item in self.scene.items_for_save():
            for name in getattr(item, '_categories', []):
                if name not in names:
                    names.append(name)
        self.ex('INSERT OR REPLACE INTO prism_metadata VALUES (?, ?)',
                ('categories', json.dumps(names, ensure_ascii=False)))
        tag_names = list(getattr(self.scene, 'tag_names', []))
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
                    tag_system(self.scene).to_dict(), ensure_ascii=False)))
        self.connection.commit()
        to_delete = {row[0] for row in self.fetchall('SELECT id from ITEMS')}
        # We don't want to touch existing items that are displayed as errors:
        keep = {item.original_save_id
                for item in self.scene.items_by_type(PrismErrorItem.TYPE)}
        logger.debug(f'Not saving error items: {keep}')
        to_delete = to_delete - keep

        to_save = list(self.scene.items_for_save())
        if self.worker:
            self.worker.begin_processing.emit(len(to_save))
        for i, item in enumerate(to_save):
            logger.debug(f'Saving {item} with id {item.save_id}')
            if item.save_id:
                self.update_item(item)
                # `discard` 而不是 `remove`：同一个 save_id 出现两次
                # （比如复制的图元带着原件的 id 过来）时，第二次 remove
                # 会抛 KeyError —— 日志里出现过 13 次，让整次保存中断。
                # 那条记录本来就打算删掉的，丢掉它正是想要的结果。
                to_delete.discard(item.save_id)
            else:
                self.insert_item(item)
            if self.worker:
                self.worker.progress.emit(i)
                if self.worker.canceled:
                    break
        self.delete_items(to_delete)
        self.write_board_extras()
        self.ex('VACUUM')
        self.connection.commit()
        # Newly written images get their previews built in the background
        thumbnail_cache.start_background_build()
        if self.worker:
            self.worker.finished.emit(self.filename, [])

    def delete_items(self, to_delete):
        to_delete = [(pk,) for pk in to_delete]
        self.exmany('DELETE FROM items WHERE id=?', to_delete)
        self.exmany('DELETE FROM sqlar WHERE item_id=?', to_delete)

    # ── Document note, attachments and mind map ──────────────────

    def read_board_extras(self):
        """Load the note, its attachments and the mind map tree.

        All three live outside the item tables, so a .prism file stays a
        single self contained file: dragging an image into the note embeds
        it here instead of pointing at a path on disk.
        """
        self.scene.note_html = ''
        self.scene.attachments = {}
        self.scene.mindmap_tree = None
        self.scene.workspace_pages = []
        self.scene.workspace_nodes = []
        self.scene.project_id = uuid.uuid4().hex
        try:
            row = self.fetchone(
                "SELECT value FROM prism_metadata WHERE key='project_id'")
            if row and row[0]:
                self.scene.project_id = row[0]
        except sqlite3.OperationalError:
            pass
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
            row = self.fetchone(
                'SELECT tree FROM board_mindmap WHERE board_id = 0')
            if row and row[0]:
                self.scene.mindmap_tree = json.loads(row[0])
            table = self.fetchone(
                "SELECT name FROM sqlite_master WHERE type='table' "
                "AND name='workspace_pages'")
            if table:
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
                            '__prism_page_payload__'):
                        tags = list(content.get('tags', []))
                        parent = content.get('parent')
                        permanent = bool(content.get('permanent'))
                        content = content.get('content')
                    self.scene.workspace_pages.append({
                        'id': page_id, 'kind': kind, 'title': title,
                        'content': content, 'tags': tags,
                        'parent': parent, 'permanent': permanent})
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

    def write_board_extras(self):
        """Persist the note, its attachments and the mind map tree."""
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
        self.ex('CREATE TABLE IF NOT EXISTS prism_metadata ('
                'key TEXT PRIMARY KEY, value TEXT)')
        self.ex('INSERT OR REPLACE INTO prism_metadata VALUES (?, ?)',
                ('project_id', self.scene.project_id))
        now = int(time.time())
        html = getattr(self.scene, 'note_html', '') or ''
        self.ex('INSERT OR REPLACE INTO board_notes '
                '(board_id, html, updated_at) VALUES (0, ?, ?)', (html, now))

        for attachment_id, entry in (
                getattr(self.scene, 'attachments', {}) or {}).items():
            name, mime, blob = entry
            self.ex('INSERT OR REPLACE INTO attachments '
                    '(id, board_id, name, mime, blob, created_at) '
                    'VALUES (?, 0, ?, ?, ?, ?)',
                    (attachment_id, name, mime, blob, now))

        # Attachments removed from the note are dropped from the file too.
        keep = set((getattr(self.scene, 'attachments', {}) or {}).keys())
        existing = {row[0] for row in self.fetchall(
            'SELECT id FROM attachments WHERE board_id = 0')}
        stale = [(pk,) for pk in existing - keep]
        if stale:
            self.exmany('DELETE FROM attachments WHERE id=?', stale)

        tree = getattr(self.scene, 'mindmap_tree', None)
        if tree is not None:
            self.ex('INSERT OR REPLACE INTO board_mindmap '
                    '(board_id, tree, updated_at) VALUES (0, ?, ?)',
                    (json.dumps(tree, ensure_ascii=False), now))
        self.ex('DELETE FROM workspace_pages')
        for order, page in enumerate(
                getattr(self.scene, 'workspace_pages', []) or []):
            self.ex(
                'INSERT INTO workspace_pages '
                '(id, kind, title, data, sort_order) VALUES (?, ?, ?, ?, ?)',
                (page.get('id'), page.get('kind'), page.get('title'),
                 json.dumps({'__prism_page_payload__': True,
                             'content': page.get('content'),
                             'tags': page.get('tags', []),
                             'parent': page.get('parent'),
                             'permanent': bool(page.get('permanent'))},
                            ensure_ascii=False), order))
        self.ex('DELETE FROM workspace_nodes')
        for node in getattr(self.scene, 'workspace_nodes', []) or []:
            self.ex('INSERT INTO workspace_nodes (id, payload) VALUES (?, ?)',
                    (node['id'], json.dumps(node, ensure_ascii=False)))
        self.connection.commit()

    def insert_item(self, item):
        self.ex(
            'INSERT INTO items (type, x, y, z, scale, rotation, flip, '
            'data) '
            'VALUES (?, ?, ?, ?, ?, ?, ?, ?)',
            (item.TYPE, item.pos().x(), item.pos().y(), item.zValue(),
             item.scale(), item.rotation(), item.flip(),
             json.dumps(item.get_extra_save_data())))
        item.save_id = self.cursor.lastrowid

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
        elif hasattr(item, 'pixmap_to_bytes'):
            # Keep the imported bytes exactly as received.  Preview
            # adjustments and the user's storage-format preference must not
            # silently turn a PNG/HDR/other source into a new lossy image
            # when the .prism file is saved.
            pixmap = item.original_bytes() if hasattr(item, 'original_bytes') else None
            imgformat = item.original_format() if pixmap and hasattr(item, 'original_format') else None
            if not pixmap:
                pixmap, imgformat = item.pixmap_to_bytes()
            imgformat = imgformat or 'png'
            name = item.get_filename_for_export(imgformat)
            self.ex(
                'INSERT INTO sqlar (item_id, name, mode, sz, data) '
                'VALUES (?, ?, ?, ?, ?)',
                (item.save_id, name, 0o644, len(pixmap), pixmap))
            # The image now lives in the project file; queue a preview so
            # that opening this project next time does not decode it.
            thumbnail_cache.defer_preview(
                thumbnail_cache.digest(pixmap), pixmap)
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
        self.connection.commit()
