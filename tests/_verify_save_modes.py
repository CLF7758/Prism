r"""保存模式与失败语义：原地增量 vs 另存复制、取消、冲突、回执后 dirty。

**证据脚本**，只调用 `prism.fileio.partial_save` / `prism.partial_project`，
写入全在专用副本（pytest tmp 目录）。不改产品代码。

覆盖任务 7 里之前没测的部分：
* 原地增量保存（写 1 行）与另存整库复制的耗时分别多少（捕获/写入/确认三段）；
* `cancel` 回执在第 N 次调用返回 True 时：目标不生成、源不被改；
* 捕获后源被外部改动（冲突）：目标不生成、外部改动保留；
* 保存回执之后，保存期间产生的新编辑是否仍为 dirty。

运行：
    .\.venv\Scripts\python.exe -m pytest tests/_verify_save_modes.py -q -s
"""
from contextlib import closing
import json
from pathlib import Path
import queue
import shutil
import sqlite3
import time

import pytest

from prism.fileio.partial_save import (PartialSaveCancelled, PartialSaveConflict,
                                       write_partial_snapshot)
from prism.partial_project import PartialProjectSession


OUT = Path('artifacts/full-audit').resolve()
RESULT = OUT / 'save-modes-20261021.json'
CANVAS_A, CANVAS_B = 'canvas-a', 'canvas-b'
PROJECT_ID = 'save-modes'
ITEMS = 200

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

CASES = []


def record(case, name, expected, call):
    started = time.perf_counter()
    try:
        actual = call() or {}
        passed = bool(actual.pop('ok', False))
        error = None
    except BaseException as problem:                     # noqa: BLE001 - evidence
        actual, passed = {}, False
        error = f'{type(problem).__name__}: {problem}'
    CASES.append(dict(case=case, name=name, expected=expected, actual=actual,
                      passed=passed, error=error,
                      seconds=round(time.perf_counter() - started, 3)))
    print(f'[{"PASS" if passed else "FAIL"}] {case} {name}'
          + (f' :: {error}' if error else ''), flush=True)


class Point:
    __slots__ = ('_x', '_y')

    def __init__(self, x, y):
        self._x, self._y = x, y

    def x(self):
        return self._x

    def y(self):
        return self._y


class Item:
    TYPE = 'pixmap'

    def __init__(self, save_id, data, x=0.0, y=0.0):
        self.save_id = save_id
        self.data = dict(data)
        self._point = Point(x, y)

    def pos(self):
        return self._point

    def zValue(self):
        return 0.0

    def scale(self):
        return 1.0

    def rotation(self):
        return 0.0

    def flip(self):
        return 1

    def get_extra_save_data(self):
        return dict(self.data)


class Scene:
    def __init__(self, items):
        self._items = list(items)
        self.workspace_pages = [dict(id=CANVAS_A, kind='canvas', title=CANVAS_A,
                                     content=CANVAS_A, tags=[], parent=None,
                                     permanent=False)]
        self.note_html = '<p>n</p>'
        self.mindmap_tree = {'root': {}}
        self.attachments = {}
        self.items_to_add = queue.Queue()
        self._prism_archive_items = set()
        self.material_records = []

    def iter_assets(self):
        return iter(self._items)


def build(path):
    with sqlite3.connect(path) as connection:
        connection.executescript(SCHEMA)
        connection.execute('INSERT INTO prism_metadata VALUES(?,?)',
                           ('project_id', PROJECT_ID))
        for key in range(1, ITEMS + 1):
            canvas = CANVAS_A if key <= ITEMS // 2 else CANVAS_B
            extra = dict(canvasId=canvas, filename=f'{key}.png',
                         imageWidth=64, imageHeight=64, notes='before', tags=['t'])
            blob = (f'blob-{key}-'.encode() * 64)
            connection.execute('INSERT INTO items VALUES(?,?,?,?,?,?,?,?,?)',
                               (key, 'pixmap', key * 1.0, 2.0, 0.0, 1.0, 0.0, 1,
                                json.dumps(extra, ensure_ascii=False)))
            connection.execute('INSERT INTO sqlar(name,item_id,mode,sz,data) VALUES(?,?,?,?,?)',
                               (f'{key}.png', key, 0o644, len(blob), blob))
        connection.execute('INSERT INTO workspace_pages VALUES(?,?,?,?,?)',
                           (CANVAS_A, 'canvas', CANVAS_A,
                            json.dumps({'__prism_page_payload__': True,
                                        'content': CANVAS_A, 'tags': [],
                                        'parent': None, 'permanent': False}), 0))
        connection.execute('INSERT INTO board_notes VALUES(?,?,?,?)', (1, 0, '<p>n</p>', 0))
        connection.execute('INSERT INTO board_mindmap VALUES(?,?,?)',
                           (0, json.dumps({'root': {}}), 0))


def load_changes(path):
    with closing(sqlite3.connect(Path(path).as_uri() + '?mode=ro', uri=True)) as connection:
        return connection.execute('SELECT id, data FROM items ORDER BY id').fetchall()


@pytest.fixture
def workspace(tmp_path):
    source = tmp_path / 'save-modes.prism'
    build(source)
    return dict(root=tmp_path, source=source,
                item_ids=tuple(range(1, ITEMS // 2 + 1)),
                scene=Scene([Item(key, dict(canvasId=CANVAS_A, filename=f'{key}.png',
                                            imageWidth=64, imageHeight=64,
                                            notes='before', tags=['t']),
                                  x=key * 1.0, y=2.0)
                             for key in range(1, ITEMS // 2 + 1)]))


def test_save_modes(workspace, qapp):
    source = workspace['source']

    def session():
        return PartialProjectSession.open(source, item_ids=workspace['item_ids'],
                                          page_ids=(CANVAS_A,), pages_complete=False,
                                          include_boards=True)

    def case_incremental_vs_copy():
        """同一场景：原地增量（写 1 行）vs 另存整库复制；三段分别计时。"""
        runs = {}
        for label, target in (('in_place', None), ('save_as', workspace['root'] / 'copy.prism')):
            session_instance = session()
            scene = workspace['scene']
            scene._items[0].data['notes'] = f'{label} edit'
            started = time.perf_counter()
            snapshot = session_instance.capture(scene)
            capture_ms = round((time.perf_counter() - started) * 1000, 3)
            started = time.perf_counter()
            receipt = write_partial_snapshot(snapshot, target)
            write_ms = round((time.perf_counter() - started) * 1000, 3)
            started = time.perf_counter()
            session_instance.acknowledge(snapshot, receipt, scene=scene,
                                        filename=str(target or source))
            ack_ms = round((time.perf_counter() - started) * 1000, 3)
            saved_path = Path(receipt.filename)
            notes = json.loads(load_changes(saved_path)[0][1])['notes']
            runs[label] = dict(capture_ms=capture_ms, write_ms=write_ms, ack_ms=ack_ms,
                               changed_rows=receipt.changed_rows,
                               bytes=saved_path.stat().st_size,
                               target=str(saved_path), notes=notes)
        ratio = (round(runs['save_as']['write_ms'] / runs['in_place']['write_ms'], 2)
                 if runs['in_place']['write_ms'] else None)
        return dict(ok=(runs['in_place']['notes'] == 'in_place edit'
                        and runs['save_as']['notes'] == 'save_as edit'
                        and runs['in_place']['changed_rows'] == 1),
                    runs=runs, save_as_over_in_place=ratio,
                    note='copy 目标为整库复制，耗时含复制；两者不可互相外推到十万图')

    def case_cancel():
        target = workspace['root'] / 'cancel.prism'
        session_instance = session()
        scene = workspace['scene']
        scene._items[1].data['notes'] = 'cancel edit'
        snapshot = session_instance.capture(scene)
        calls = {'n': 0}

        def cancel():
            calls['n'] += 1
            return calls['n'] >= 2          # 第一次允许（拷贝阶段），第二次触发取消

        error = None
        try:
            write_partial_snapshot(snapshot, target, cancel=cancel)
        except PartialSaveCancelled as problem:
            error = str(problem)
        return dict(ok=(error is not None and not target.exists()
                        and json.loads(load_changes(source)[1][1])['notes'] == 'before'),
                    cancel_calls=calls['n'], error=error,
                    target_exists=target.exists(),
                    source_untouched=json.loads(load_changes(source)[1][1])['notes'] == 'before')

    def case_conflict():
        work = workspace['root'] / 'conflict-source.prism'
        shutil.copyfile(source, work)
        session_instance = PartialProjectSession.open(
            work, item_ids=workspace['item_ids'], page_ids=(CANVAS_A,),
            pages_complete=False, include_boards=True)
        scene = workspace['scene']
        scene._items[2].data['notes'] = 'mine'
        snapshot = session_instance.capture(scene)
        with closing(sqlite3.connect(work)) as other:
            payload = other.execute('SELECT data FROM items WHERE id=3').fetchone()[0]
            changed = json.loads(payload)
            changed['notes'] = 'theirs'
            other.execute('UPDATE items SET data=? WHERE id=3',
                          (json.dumps(changed, ensure_ascii=False),))
            other.commit()
        target = workspace['root'] / 'conflict.prism'
        error = None
        try:
            write_partial_snapshot(snapshot, target)
        except PartialSaveConflict as problem:
            error = str(problem)
        theirs = json.loads(load_changes(work)[2][1])['notes']
        return dict(ok=(error is not None and not target.exists() and theirs == 'theirs'),
                    error=error, target_exists=target.exists(),
                    external_edit_kept=theirs == 'theirs')

    def case_dirty_after_receipt():
        session_instance = session()
        scene = workspace['scene']
        scene._items[3].data['notes'] = 'first'
        snapshot = session_instance.capture(scene)
        scene._items[3].data['notes'] = 'second (during save)'
        target = workspace['root'] / 'dirty.prism'
        receipt = write_partial_snapshot(snapshot, target)
        first_saved = json.loads(load_changes(target)[3][1])['notes']
        session_instance.acknowledge(snapshot, receipt, scene=scene, filename=str(target))
        follow_up = session_instance.capture(scene)
        return dict(ok=(first_saved == 'first' and len(follow_up.changes) == 1
                        and 'second' in follow_up.changes[0].after[-1]),
                    first_saved=first_saved, pending_after_ack=len(follow_up.changes))

    def case_coverage_table():
        """会话覆盖了哪些表：从实现里读（只读事实，不是断言功能完成）。"""
        from prism.partial_project import (ITEM_TABLE, MINDMAP_TABLE, NOTE_TABLE,
                                           PAGE_TABLE, REMOVABLE_ITEM_KEYS)
        return dict(ok=True,
                    covered_tables=[ITEM_TABLE, PAGE_TABLE, NOTE_TABLE, MINDMAP_TABLE],
                    not_covered=['material_records', 'workspace_nodes', 'attachments(blobs)',
                                 'items hard delete'],
                    removable_keys=sorted(REMOVABLE_ITEM_KEYS),
                    note=('未覆盖的表在"已加载"时会显式拒绝保存（已被 case 11/12 验证）；'
                          '按复核要求，拒绝保存不算功能完成'))

    record('7.1', 'incremental vs save-as', '两者都正确写入；分段耗时分开记录',
           case_incremental_vs_copy)
    record('7.2', 'cancel keeps target absent', '取消后不生成目标、源不被改', case_cancel)
    record('7.3', 'conflict keeps target absent', '冲突后不生成目标、外部编辑保留', case_conflict)
    record('7.4', 'dirty after receipt', '回执只确认已写内容，新编辑仍 dirty',
           case_dirty_after_receipt)
    record('7.5', 'coverage table', '列清已覆盖/未覆盖的表与可移除键', case_coverage_table)

    total = len(CASES)
    passed = sum(1 for case in CASES if case['passed'])
    RESULT.parent.mkdir(parents=True, exist_ok=True)
    RESULT.write_text(json.dumps(dict(generated_at=time.strftime('%Y-%m-%dT%H:%M:%S'),
                                      items=ITEMS, total=total, passed=passed,
                                      cases=CASES), ensure_ascii=False, indent=2),
                      encoding='utf-8')
    print(f'RESULT {passed}/{total} -> {RESULT}', flush=True)
