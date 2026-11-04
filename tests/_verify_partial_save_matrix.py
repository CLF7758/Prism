r"""按固定清单验证部分保存，以及目前不支持的操作是否明确被阻止。

这是**证据脚本**，不是产品代码：只调用 `prism.partial_project` /
`prism.fileio.partial_save` / `prism.fileio.load_prism`，写入一律发生在专用
副本上，源工程前后做整文件 SHA256 比对。

每个用例记录：预期 → 实际 → 是否通过。结果写
`artifacts/full-audit/partial-save-matrix-20261021.json`。

运行：
    .\.venv\Scripts\python.exe -m pytest tests/_verify_partial_save_matrix.py -q
"""
from contextlib import closing
import hashlib
import json
from pathlib import Path
import queue
import shutil
import sqlite3
import time

import pytest

from prism.fileio.partial_save import (PartialSaveConflict, RowPatch,
                                        write_partial_snapshot)
from prism.fileio.attachments_store import StoredAttachments
from prism.partial_project import PartialProjectSession


OUT = Path('artifacts/full-audit').resolve()
RESULT = OUT / 'partial-save-matrix-20261021.json'

CANVAS_A = (1, 2, 3, 4, 5, 6, 7, 8)
CANVAS_B = tuple(range(9, 21))          # 未加载的页
PAGES = ('canvas-a', 'canvas-b', 'notes')
PROJECT_ID = 'matrix-project'
SCHEMA = """
PRAGMA user_version=4;
CREATE TABLE items(id INTEGER PRIMARY KEY,type TEXT,x,y,z,scale,rotation,flip,data JSON);
CREATE TABLE sqlar(name TEXT PRIMARY KEY,item_id INTEGER UNIQUE,mode INT,mtime INT,sz INT,data BLOB);
CREATE TABLE prism_metadata(key TEXT PRIMARY KEY,value TEXT);
CREATE TABLE workspace_pages(id TEXT PRIMARY KEY,kind,title,data JSON,sort_order);
CREATE TABLE workspace_nodes(id TEXT PRIMARY KEY,payload JSON NOT NULL);
CREATE TABLE material_records(id TEXT PRIMARY KEY,payload JSON NOT NULL);
CREATE TABLE attachments(id INTEGER PRIMARY KEY AUTOINCREMENT,board_id INTEGER NOT NULL DEFAULT 0,
    name TEXT NOT NULL,mime TEXT,blob BLOB,created_at INTEGER NOT NULL DEFAULT 0);
CREATE TABLE board_notes(id INTEGER PRIMARY KEY,board_id INTEGER NOT NULL DEFAULT 0,
    html TEXT NOT NULL DEFAULT '',updated_at INTEGER NOT NULL DEFAULT 0);
CREATE TABLE board_mindmap(board_id INTEGER PRIMARY KEY,tree JSON,updated_at INTEGER NOT NULL DEFAULT 0);
CREATE TABLE future_extension(id INTEGER PRIMARY KEY,original BLOB);
"""
TABLES = ('items', 'sqlar', 'attachments', 'workspace_pages', 'workspace_nodes',
          'material_records', 'board_notes', 'board_mindmap', 'prism_metadata',
          'future_extension')

CASES = []


def digest(path):
    value = hashlib.sha256()
    with Path(path).open('rb') as handle:
        for block in iter(lambda: handle.read(4 * 1024 * 1024), b''):
            value.update(block)
    return value.hexdigest()


def table_rows(path, table):
    with closing(sqlite3.connect(Path(path).as_uri() + '?mode=ro', uri=True)) as connection:
        return connection.execute(f'SELECT * FROM "{table}" ORDER BY 1').fetchall()


def blob_hashes(path, table, column, key):
    result = {}
    with closing(sqlite3.connect(Path(path).as_uri() + '?mode=ro', uri=True)) as connection:
        for rowid, identity, size in connection.execute(
                f'SELECT rowid,{key},length({column}) FROM {table}'):
            if size is None:
                result[identity] = None
                continue
            value = hashlib.sha256()
            with connection.blobopen(table, column, rowid, readonly=True) as stream:
                for block in iter(lambda: stream.read(1024 * 1024), b''):
                    value.update(block)
            result[identity] = value.hexdigest()
    return result


def record(case_id, name, expected, call):
    """Run one case and keep its expected/actual/verdict, however it ends."""
    started = time.perf_counter()
    try:
        actual = call() or {}
        passed = bool(actual.pop('ok', False))
        error = None
    except BaseException as problem:                     # noqa: BLE001 - evidence
        actual, passed = {}, False
        error = f'{type(problem).__name__}: {problem}'
    entry = dict(case=case_id, name=name, expected=expected, actual=actual,
                 passed=passed, error=error,
                 seconds=round(time.perf_counter() - started, 3))
    CASES.append(entry)
    print(f'[{"PASS" if passed else "FAIL"}] {case_id:>2} {name}'
          + (f' :: {error}' if error else ''), flush=True)
    return entry


class Point:
    __slots__ = ('_x', '_y')

    def __init__(self, x, y):
        self._x, self._y = x, y

    def x(self):
        return self._x

    def y(self):
        return self._y


class Item:
    """Just the surface a save reads; deliberately not a Qt widget."""

    TYPE = 'pixmap'

    def __init__(self, save_id, data, x=0.0, y=0.0, z=0.0, source=None,
                 source_path=None):
        self.save_id = save_id
        self.data = dict(data)
        self._point = Point(x, y)
        self._z = z
        self._source = source
        self._source_path = source_path

    def pos(self):
        return self._point

    def zValue(self):
        return self._z

    def scale(self):
        return 1.0

    def rotation(self):
        return 0.0

    def flip(self):
        return 1

    def get_extra_save_data(self):
        return dict(self.data)

    def original_bytes(self):
        return self._source

    def original_format(self):
        return 'png' if self._source else None

    def pixmap_to_bytes(self):
        return self._source, 'png'

    def get_filename_for_export(self, image_format):
        return self.data.get('filename') or 'asset.png'


class Scene:
    def __init__(self, items, pages, note, tree, attachments=None):
        self._items = list(items)
        self.workspace_pages = list(pages)
        self.note_html = note
        self.mindmap_tree = tree
        self.attachments = attachments if attachments is not None else {}
        self.items_to_add = queue.Queue()
        self._prism_archive_items = set()
        self.material_records = []

    def iter_assets(self):
        return iter(self._items)


def material_record():
    """A record `validate_material_records` accepts, so a real reopen works."""
    return dict(id='m1', projectId=PROJECT_ID, schemaVersion=1, name='material',
                notes='', folderId=None, tags=[], textures={}, preview={},
                nativePackages=[], status='ready',
                createdAt='2026-10-21T00:00:00', updatedAt='2026-10-21T00:00:00')


@pytest.fixture(scope='module')
def workspace(tmp_path_factory):
    """A dedicated copy: the matrix never touches anything else."""
    root = tmp_path_factory.mktemp('partial-matrix')
    source = root / 'verify-source.prism'
    items = []
    with sqlite3.connect(source) as connection:
        connection.executescript(SCHEMA)
        connection.execute('INSERT INTO prism_metadata VALUES(?,?)',
                           ('project_id', PROJECT_ID))
        for key in range(1, 21):
            canvas = 'A' if key in CANVAS_A else 'B'
            extra = dict(canvasId=canvas, filename=f'{key}.png',
                         imageWidth=64, imageHeight=64, title=f'asset {key}',
                         notes='before', tags=['keep'],
                         unknownField={'nested': ['must survive', key]})
            blob = f'original-{key}-'.encode() * 8
            items.append((key, canvas, json.loads(json.dumps(extra)), blob))
            connection.execute('INSERT INTO items VALUES(?,?,?,?,?,?,?,?,?)',
                               (key, 'pixmap', key * 10.0, 5.0, float(key), 1.0, 0.0, 1,
                                json.dumps(extra, ensure_ascii=False)))
            connection.execute('INSERT INTO sqlar(name,item_id,mode,sz,data) VALUES(?,?,?,?,?)',
                               (f'{key}.png', key, 0o644, len(blob), blob))
        for order, page_id in enumerate(PAGES):
            connection.execute('INSERT INTO workspace_pages VALUES(?,?,?,?,?)',
                               (page_id, 'canvas' if order < 2 else 'document', page_id,
                                json.dumps({'__prism_page_payload__': True,
                                            'content': page_id, 'tags': [],
                                            'parent': None, 'permanent': False},
                                           ensure_ascii=False), order))
        connection.execute('INSERT INTO workspace_nodes VALUES(?,?)',
                           ('n1', '{"id":"n1","pageId":"canvas-a"}'))
        connection.execute('INSERT INTO material_records VALUES(?,?)',
                           ('m1', json.dumps(material_record(), ensure_ascii=False)))
        connection.execute('INSERT INTO board_notes VALUES(?,?,?,?)',
                           (1, 0, '<p>before</p>', 0))
        connection.execute('INSERT INTO board_mindmap VALUES(?,?,?)',
                           (0, '{"root":{}}', 0))
        for attachment_id, name in ((1, 'one.png'), (2, 'two.png')):
            connection.execute(
                'INSERT INTO attachments(id,board_id,name,mime,blob,created_at) '
                'VALUES(?,?,?,?,?,?)',
                (attachment_id, 0, name, 'image/png',
                 b'attachment-' + name.encode(), 0))
        connection.execute('INSERT INTO future_extension VALUES(?,?)',
                           (1, b'unknown-table\x00\xff'))
    pages = [dict(id=row[0], kind=row[1], title=row[2],
                  content=json.loads(row[3])['content'], tags=[], parent=None,
                  permanent=False)
             for row in table_rows(source, 'workspace_pages')]
    return dict(root=root, source=source, items=items, pages=pages)


def new_scene(workspace, attachments=None):
    """Canvas A loaded; canvas B, materials and the tree left unloaded."""
    items = [Item(key, data, x=key * 10.0, y=5.0, z=float(key))
             for key, canvas, data, _blob in workspace['items'] if canvas == 'A']
    return Scene(items, workspace['pages'], '<p>before</p>', {'root': {}}, attachments)


def open_session(workspace, attachments=False):
    return PartialProjectSession.open(
        workspace['source'], item_ids=CANVAS_A, page_ids=PAGES, pages_complete=True,
        include_boards=True, attachments=attachments)


def attach_store(source):
    store = StoredAttachments()
    for row in table_rows(source, 'attachments'):
        store._meta[row[0]] = (row[2], row[3])
    return store


def snapshot_of(path):
    return {table: table_rows(path, table) for table in TABLES}


def test_partial_save_matrix(workspace, qapp, monkeypatch):
    source = workspace['source']
    source_before = digest(source)

    def target(name):
        return workspace['root'] / f'{name}.prism'

    def case_edit():
        session, scene = open_session(workspace), new_scene(workspace)
        scene._items[0].data['notes'] = 'edited'
        scene._items[0].data['tags'] = ['keep', 'reviewed']
        original = {row[0]: row for row in table_rows(source, 'items')}
        receipt = write_partial_snapshot(session.capture(scene), target('edit'))
        saved = table_rows(target('edit'), 'items')
        changed = [row[0] for row in saved if row != original[row[0]]]
        data = json.loads(saved[0][-1])
        # 除 items 外每一张表都必须与源逐行一致（含 sqlar / attachments 的 BLOB）。
        altered = [table for table in TABLES if table != 'items'
                   and table_rows(target('edit'), table) != table_rows(source, table)]
        return dict(ok=(receipt.changed_rows == 1 and changed == [1]
                        and data['notes'] == 'edited'
                        and data['tags'] == ['keep', 'reviewed']
                        and data['unknownField'] == {'nested': ['must survive', 1]}
                        and saved[0][:-1] == original[1][:-1]
                        and len(saved) == 20 and not altered),
                    changed_rows=receipt.changed_rows, changed_ids=changed,
                    unknown_field_kept=data['unknownField'],
                    geometry_kept=saved[0][:-1] == original[1][:-1],
                    row_count=len(saved), altered_tables=altered)

    def case_no_change():
        session, scene = open_session(workspace), new_scene(workspace)
        before = snapshot_of(source)
        snapshot = session.capture(scene)
        receipt = write_partial_snapshot(snapshot, target('untouched'))
        after = snapshot_of(target('untouched'))
        differing = [table for table in TABLES if after[table] != before[table]]
        return dict(ok=(snapshot.changes == () and receipt.changed_rows == 0
                        and not differing),
                    changes=len(snapshot.changes), changed_rows=receipt.changed_rows,
                    differing_tables=differing)

    def case_insert():
        session = open_session(workspace)
        new_bytes = b'brand-new-image-bytes'
        fresh = Item(None, dict(filename='fresh.png', notes='added', canvasId='A'),
                     source=new_bytes)
        scene = new_scene(workspace)
        scene._items.append(fresh)
        snapshot = session.capture(scene)
        receipt = write_partial_snapshot(snapshot, target('insert'))
        rows = table_rows(target('insert'), 'items')
        existing = {key for key, *_ in workspace['items']}
        new_id = max({row[0] for row in rows} - existing)
        with closing(sqlite3.connect(target('insert'))) as connection:
            found = connection.execute('SELECT data FROM sqlar WHERE item_id=?',
                                       (new_id,)).fetchone()
        session.acknowledge(snapshot, receipt, scene=scene, filename=str(target('insert')))
        again = session.capture(scene)
        # 原有 20 份原件（sqlar 行含 BLOB）必须逐行不变。
        original_originals = {row[1]: row for row in table_rows(source, 'sqlar')}
        stored_originals = {row[1]: row for row in table_rows(target('insert'), 'sqlar')}
        kept = all(stored_originals.get(key) == row
                   for key, row in original_originals.items())
        return dict(ok=(len(snapshot.new_items) == 1 and len(rows) == 21
                        and found is not None and found[0] == new_bytes
                        and fresh.save_id == new_id
                        and fresh in scene._prism_archive_items
                        and again.changes == () and kept
                        and len(stored_originals) == 21),
                    new_items=len(snapshot.new_items), stored_rows=len(rows),
                    bytes_identical=found is not None and found[0] == new_bytes,
                    rebound=fresh.save_id == new_id,
                    second_capture_clean=again.changes == (),
                    original_blobs_kept=kept,
                    original_count=len(original_originals))

    def case_attachment_add():
        store = attach_store(source)
        store[3] = ('three.png', 'image/png', b'attachment-three.png')
        session = open_session(workspace, attachments=True)
        scene = new_scene(workspace, attachments=store)
        snapshot = session.capture(scene)
        receipt = write_partial_snapshot(snapshot, target('attach-add'))
        session.acknowledge(snapshot, receipt, scene=scene,
                            filename=str(target('attach-add')))
        hashes = blob_hashes(target('attach-add'), 'attachments', 'blob', 'id')
        original = blob_hashes(source, 'attachments', 'blob', 'id')
        return dict(ok=(len(snapshot.attachment_writes) == 1
                        and snapshot.attachment_writes[0][0] == 3
                        and sorted(hashes) == [1, 2, 3]
                        and hashes[1] == original[1] and hashes[2] == original[2]
                        and store._dirty == set()),
                    writes=len(snapshot.attachment_writes),
                    attachment_ids=sorted(hashes),
                    untouched_bytes=(hashes[1] == original[1] and hashes[2] == original[2]))

    def case_attachment_remove():
        store = attach_store(source)
        del store[1]
        session = open_session(workspace, attachments=True)
        scene = new_scene(workspace, attachments=store)
        snapshot = session.capture(scene)
        receipt = write_partial_snapshot(snapshot, target('attach-remove'))
        session.acknowledge(snapshot, receipt, scene=scene,
                            filename=str(target('attach-remove')))
        ids = [row[0] for row in table_rows(target('attach-remove'), 'attachments')]
        hashes = blob_hashes(target('attach-remove'), 'attachments', 'blob', 'id')
        original = blob_hashes(source, 'attachments', 'blob', 'id')
        return dict(ok=(snapshot.attachment_removals == (1,) and ids == [2]
                        and hashes[2] == original[2]),
                    removals=list(snapshot.attachment_removals), remaining_ids=ids,
                    untouched_bytes=hashes[2] == original[2])

    def case_trash_restore():
        session, scene = open_session(workspace), new_scene(workspace)
        scene._items[1].data['deletedAt'] = 1700000000
        trash_snapshot = session.capture(scene)
        trash_receipt = write_partial_snapshot(trash_snapshot, target('trash'))
        session.acknowledge(trash_snapshot, trash_receipt, scene=scene,
                            filename=str(target('trash')))
        trashed = json.loads(table_rows(target('trash'), 'items')[1][-1]).get('deletedAt')

        del scene._items[1].data['deletedAt']
        restore_snapshot = session.capture(scene)
        write_partial_snapshot(restore_snapshot, target('restored'))
        restored = json.loads(table_rows(target('restored'), 'items')[1][-1])
        return dict(ok=(trashed == 1700000000 and 'deletedAt' not in restored
                        and restored['notes'] == 'before'
                        and trash_receipt.changed_rows == 1),
                    trash_changed_rows=trash_receipt.changed_rows,
                    trashed_marker=trashed,
                    marker_after_restore=restored.get('deletedAt'),
                    other_fields_kept=restored['notes'] == 'before')

    def case_save_failure():
        # 真正的冲突源是**数据库里的行**在捕获之后被别处改了；改内存不构成冲突
        # （内存不是写入依据）。为了不动源工程，冲突场景在专用副本上跑。
        work = target('conflict-source')
        shutil.copyfile(source, work)
        session = PartialProjectSession.open(
            work, item_ids=CANVAS_A, page_ids=PAGES, pages_complete=True,
            include_boards=True)
        scene = new_scene(workspace)
        scene._items[0].data['notes'] = 'first'
        snapshot = session.capture(scene)
        with closing(sqlite3.connect(work)) as other:
            payload = other.execute('SELECT data FROM items WHERE id=1').fetchone()[0]
            changed = json.loads(payload)
            changed['notes'] = 'changed in another window'
            other.execute('UPDATE items SET data=? WHERE id=1',
                          (json.dumps(changed, ensure_ascii=False),))
            other.commit()
        conflict = None
        try:
            write_partial_snapshot(snapshot, target('conflict'))
        except PartialSaveConflict as problem:
            conflict = str(problem)

        broken = Item(None, dict(filename='gone.png'), source_path='missing-file.png')
        broken_scene = new_scene(workspace)
        broken_scene._items.append(broken)
        missing = None
        try:
            write_partial_snapshot(open_session(workspace).capture(broken_scene),
                                   target('missing-source'))
        except BaseException as problem:                  # noqa: BLE001
            missing = f'{type(problem).__name__}: {problem}'
        return dict(ok=(conflict is not None
                        and missing is not None
                        and not target('conflict').exists()
                        and not target('missing-source').exists()
                        and digest(source) == source_before),
                    conflict=conflict, missing_source=missing,
                    conflict_target_absent=not target('conflict').exists(),
                    missing_source_target_absent=not target('missing-source').exists(),
                    external_change_kept=json.loads(
                        table_rows(work, 'items')[0][-1])['notes'] == 'changed in another window',
                    source_unchanged=digest(source) == source_before)

    def case_edit_during_save():
        session, scene = open_session(workspace), new_scene(workspace)
        scene._items[0].data['notes'] = 'first edit'
        snapshot = session.capture(scene)
        scene._items[0].data['notes'] = 'second edit typed during save'
        receipt = write_partial_snapshot(snapshot, target('during-save'))
        saved_first = json.loads(table_rows(target('during-save'), 'items')[0][-1])['notes']
        session.acknowledge(snapshot, receipt, scene=scene,
                            filename=str(target('during-save')))
        follow_up = session.capture(scene)
        write_partial_snapshot(follow_up, target('during-save-2'))
        saved_second = json.loads(
            table_rows(target('during-save-2'), 'items')[0][-1])['notes']
        return dict(ok=(saved_first == 'first edit'
                        and len(follow_up.changes) == 1
                        and saved_second == 'second edit typed during save'),
                    first_written=saved_first, pending_after_ack=len(follow_up.changes),
                    second_written=saved_second)

    def case_save_as():
        session, scene = open_session(workspace), new_scene(workspace)
        scene._items[0].data['notes'] = 'first'
        snapshot = session.capture(scene)
        receipt = write_partial_snapshot(snapshot, target('save-as'))
        session.acknowledge(snapshot, receipt, scene=scene, filename=str(target('save-as')))
        rebound = Path(session.source) == target('save-as').resolve()
        scene._items[0].data['notes'] = 'second'
        second = session.capture(scene)
        receipt2 = write_partial_snapshot(second, target('save-as'))
        changed = json.loads(table_rows(target('save-as'), 'items')[0][-1])['notes']
        source_row = json.loads(table_rows(source, 'items')[0][-1])['notes']
        return dict(ok=(rebound and receipt2.changed_rows == 1
                        and changed == 'second' and source_row == 'before'),
                    session_rebound=rebound,
                    second_save_changed_rows=receipt2.changed_rows,
                    target_value=changed, source_untouched=source_row == 'before')

    def case_reopen():
        session, scene = open_session(workspace), new_scene(workspace)
        scene._items[0].data['notes'] = 'reopen marker'
        written = target('reopen')
        write_partial_snapshot(session.capture(scene), written)
        monkeypatch.setattr('prism.thumbnail_cache.start_background_build', lambda: None)
        from PyQt6 import QtGui
        from prism.fileio import load_prism
        from prism.scene import PrismGraphicsScene
        reopened = PrismGraphicsScene(QtGui.QUndoStack())
        load_prism(str(written), reopened)
        # 真实 loader 把素材排进 items_to_add；GUI 由 on_items_loaded 调度安装，
        # 这里手动排空，否则 iter_assets() 看到的是空场景（不是加载失败）。
        while not reopened.items_to_add.empty():
            reopened.add_queued_items()
        items = list(reopened.iter_assets())
        ids = sorted(getattr(item, 'save_id', None) or getattr(item, 'original_save_id', None)
                     for item in items)
        row = json.loads(table_rows(written, 'items')[0][-1])
        return dict(ok=(len(items) == 20 and ids == list(range(1, 21))
                        and row['notes'] == 'reopen marker'
                        and reopened.note_html == '<p>before</p>'
                        and reopened.project_id == PROJECT_ID),
                    loaded_items=len(items), ids_complete=ids == list(range(1, 21)),
                    marker_present=row['notes'] == 'reopen marker',
                    note_preserved=reopened.note_html == '<p>before</p>',
                    project_id_matches=reopened.project_id == PROJECT_ID)

    def case_material_blocked():
        session, scene = open_session(workspace), new_scene(workspace)
        scene.material_records = [dict(material_record(), name='edited')]
        message = None
        try:
            session.capture(scene)
        except PartialSaveConflict as problem:
            message = str(problem)
        return dict(ok=(message is not None and 'material_records' in message),
                    blocked=message is not None, message=message)

    def case_tree_blocked():
        session, scene = open_session(workspace), new_scene(workspace)
        scene.workspace_nodes = [{'id': 'n1', 'pageId': 'canvas-b'}]
        message = None
        try:
            session.capture(scene)
        except PartialSaveConflict as problem:
            message = str(problem)
        return dict(ok=(message is not None and 'workspace_nodes' in message),
                    blocked=message is not None, message=message)

    def case_cross_page_move():
        session, scene = open_session(workspace), new_scene(workspace)
        scene._items[0].data['canvasId'] = 'B'   # 移到未加载的页
        message, written_to = None, None
        try:
            write_partial_snapshot(session.capture(scene), target('cross-page'))
            written_to = json.loads(
                table_rows(target('cross-page'), 'items')[0][-1]).get('canvasId')
        except PartialSaveConflict as problem:
            message = str(problem)
        return dict(ok=written_to is None,
                    blocked=message is not None, message=message,
                    written_canvas_id=written_to)

    def case_hard_delete():
        session, scene = open_session(workspace), new_scene(workspace)
        scene._items.remove(scene._items[0])
        vanish = None
        try:
            session.capture(scene)
        except PartialSaveConflict as problem:
            vanish = str(problem)
        purge = None
        try:
            from prism.fileio.partial_save import capture_partial_save
            capture_partial_save(source, [RowPatch('items', 1, delete=True)])
        except BaseException as problem:                  # noqa: BLE001
            purge = f'{type(problem).__name__}: {problem}'
        return dict(ok=(vanish is not None and 'explicit trash' in vanish
                        and purge is not None and 'deletedAt' in purge),
                    vanished_item_blocked=vanish, purge_blocked=purge)

    record('1', 'edit one item',
           '只有 1 行变化；未知字段与几何列保留；总行数不变', case_edit)
    record('2', 'no change',
           '0 行写入；目标副本与源逐表一致', case_no_change)
    record('3', 'insert new image',
           '新增 1 行 + 原始字节；save_id 回填；再捕获无残留', case_insert)
    record('4', 'attachment add',
           '只写新增附件；原有附件字节不变', case_attachment_add)
    record('5', 'attachment remove',
           '被删附件消失；剩余附件字节不变', case_attachment_remove)
    record('6', 'trash then restore',
           'deletedAt 写入后可显式移除，其它字段保留', case_trash_restore)
    record('7', 'save failure',
           '冲突与缺失原件都中止；目标不生成；源哈希不变', case_save_failure)
    record('8', 'edit during save',
           '首次编辑落盘；保存期间的新编辑保留为待保存并随后落盘', case_edit_during_save)
    record('9', 'save as',
           '会话重绑到新文件；后续保存只改新文件，源不变', case_save_as)
    record('10', 'save then reopen',
           '真实 loader 重开：20 条全在；编辑与笔记保留', case_reopen)
    record('11', 'LIMIT material edit',
           '明确拒绝：会话没有材质基线', case_material_blocked)
    record('12', 'LIMIT resource-tree edit',
           '明确拒绝：会话没有资源树基线', case_tree_blocked)
    record('13', 'LIMIT cross-page move',
           '清单列为不支持：必须被拒绝，不得静默写入', case_cross_page_move)
    record('14', 'LIMIT hard delete',
           '图元消失与硬删除都拒绝；回收站标记是唯一路径', case_hard_delete)

    total = len(CASES)
    passed = sum(1 for case in CASES if case['passed'])
    after = digest(source)
    summary = dict(source=str(source), source_sha256_before=source_before,
                   source_sha256_after=after, source_unchanged=after == source_before,
                   generated_at=time.strftime('%Y-%m-%dT%H:%M:%S'),
                   canvas_loaded=list(CANVAS_A), canvas_unloaded=list(CANVAS_B),
                   total=total, passed=passed, failed=total - passed, cases=CASES)
    RESULT.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding='utf-8')
    print(f'MATRIX {passed}/{total} passed; source_unchanged={summary["source_unchanged"]}',
          flush=True)
    print(f'REPORT {RESULT}', flush=True)
