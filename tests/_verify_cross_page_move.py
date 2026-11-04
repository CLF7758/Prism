r"""跨页移动回归：不支持的操作必须被明确拒绝，静默写入不能算通过。

这是**证据脚本**，不是产品代码。它只调用
`prism.partial_project.PartialProjectSession` /
`prism.fileio.partial_save.write_partial_snapshot` /
`prism.fileio.load_prism`，所有写入都发生在专用副本上，源工程前后做整文件
SHA256 比对。

两组证据：
  * `test_cross_page_move_matrix`  —— 6 种移动场景，session + writer 层（与 GUI
    保存调用同一个 `write_partial_snapshot`）。
  * `test_gui_save_reports_success` —— 情况 2/3 走真实 `view.do_save`，回答
    "界面是不是把这次保存当成成功了"。

逐项结果写：
    artifacts/full-audit/cross-page-move-20261021.json
    artifacts/full-audit/cross-page-move-gui-20261021.json

**不冒充界面验证**：本文件能证明"写入了哪个画布 / 是否被拒绝 / 回执是否确认"，
不能证明"某个素材在画布视图里可见"。后者没有正式入口覆盖，报告里标未测。

运行：
    .\.venv\Scripts\python.exe -m pytest tests/_verify_cross_page_move.py -q
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

from prism.fileio.partial_save import PartialSaveConflict, write_partial_snapshot
from prism.partial_project import PartialProjectSession


OUT = Path('artifacts/full-audit').resolve()
RESULT = OUT / 'cross-page-move-20261021.json'
GUI_RESULT = OUT / 'cross-page-move-gui-20261021.json'

CANVAS_A = 'canvas-a'          # 已加载
CANVAS_B = 'canvas-b'          # 已加载
CANVAS_C = 'canvas-c'          # 未加载
DEFAULT_CANVAS = 'default-canvas'
MISSING_CANVAS = 'no-such-canvas'
PAGES = (CANVAS_A, CANVAS_B, CANVAS_C)
LOADED_PAGES = (CANVAS_A, CANVAS_B)
PROJECT_ID = 'moves-project'

#: id -> (canvasId, 是否显式写入 canvasId 键)
ITEMS = {1: (CANVAS_A, True), 2: (CANVAS_A, True), 3: (CANVAS_A, True),
         4: (CANVAS_B, True), 5: (CANVAS_B, True),
         6: (DEFAULT_CANVAS, True),
         7: (DEFAULT_CANVAS, False)}   # 没有 canvasId 键 == 隐式默认画布
LOADED_ITEMS = tuple(ITEMS)

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


def canvas_of(path, item_id):
    """(canvasId, 是否显式存在该键) —— 只用于取证，不作为界面验证。"""
    with closing(sqlite3.connect(Path(path).as_uri() + '?mode=ro', uri=True)) as connection:
        payload = connection.execute('SELECT data FROM items WHERE id=?',
                                     (item_id,)).fetchone()[0]
    data = json.loads(payload) if payload else {}
    return data.get('canvasId', DEFAULT_CANVAS), 'canvasId' in data


def effective_canvas(path, item_id, pages):
    """按产品语义折算素材会落到哪里：不存在且非默认 -> UNASSIGNED。"""
    canvas_id, _explicit = canvas_of(path, item_id)
    if canvas_id in pages or canvas_id == DEFAULT_CANVAS:
        return canvas_id
    return 'UNASSIGNED'


def canvas_members(path):
    """canvasId -> [item id]，用于查"消失/重复"。"""
    groups = {}
    for row in table_rows(path, 'items'):
        data = json.loads(row[-1]) if row[-1] else {}
        groups.setdefault(data.get('canvasId', DEFAULT_CANVAS), []).append(row[0])
    return {key: sorted(value) for key, value in groups.items()}


def consistency(source, target):
    """除 items 外逐表一致；原件与附件单独点名。"""
    altered = [table for table in TABLES if table != 'items'
               and table_rows(target, table) != table_rows(source, table)]
    return dict(altered_tables=altered,
                originals_identical=table_rows(target, 'sqlar') == table_rows(source, 'sqlar'),
                attachments_identical=(table_rows(target, 'attachments')
                                       == table_rows(source, 'attachments')))


def reopen_canvases(path, monkeypatch):
    """真实 loader 重开，返回 save_id -> canvas_id（不猜，不查库代替）。"""
    from PyQt6 import QtGui
    from prism.fileio import load_prism
    from prism.scene import PrismGraphicsScene
    monkeypatch.setattr('prism.thumbnail_cache.start_background_build', lambda: None)
    scene = PrismGraphicsScene(QtGui.QUndoStack())
    load_prism(str(path), scene)
    while not scene.items_to_add.empty():
        scene.add_queued_items()
    result = {}
    for item in scene.iter_assets():
        save_id = getattr(item, 'save_id', None) or getattr(item, 'original_save_id', None)
        if save_id is not None:
            result[save_id] = item.canvas_id
    return result


def record(case, name, expected, call):
    started = time.perf_counter()
    try:
        actual = call() or {}
        passed = bool(actual.pop('ok', False))
        error = None
    except BaseException as problem:                     # noqa: BLE001 - evidence
        actual, passed = {}, False
        error = f'{type(problem).__name__}: {problem}'
    entry = dict(case=case, name=name, expected=expected, actual=actual,
                 passed=passed, error=error,
                 seconds=round(time.perf_counter() - started, 3))
    CASES.append(entry)
    print(f'[{"PASS" if passed else "FAIL"}] {case} {name}'
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
    TYPE = 'pixmap'

    def __init__(self, save_id, data, x=0.0, y=0.0, z=0.0):
        self.save_id = save_id
        self.data = dict(data)
        self._point = Point(x, y)
        self._z = z

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


class Scene:
    def __init__(self, items, pages):
        self._items = list(items)
        self.workspace_pages = list(pages)
        self.note_html = '<p>note</p>'
        self.mindmap_tree = {'root': {}}
        self.attachments = {}
        self.items_to_add = queue.Queue()
        self._prism_archive_items = set()
        self.material_records = []

    def iter_assets(self):
        return iter(self._items)


def build_project(path):
    with sqlite3.connect(path) as connection:
        connection.executescript(SCHEMA)
        connection.execute('INSERT INTO prism_metadata VALUES(?,?)',
                           ('project_id', PROJECT_ID))
        for item_id, (canvas_id, explicit) in ITEMS.items():
            extra = dict(filename=f'{item_id}.png', imageWidth=64, imageHeight=64,
                         title=f'asset {item_id}', notes='before', tags=['keep'],
                         unknownField={'nested': ['must survive', item_id]})
            if explicit:
                extra['canvasId'] = canvas_id
            blob = f'original-{item_id}-'.encode() * 4
            connection.execute('INSERT INTO items VALUES(?,?,?,?,?,?,?,?,?)',
                               (item_id, 'pixmap', item_id * 10.0, 5.0, float(item_id),
                                1.0, 0.0, 1, json.dumps(extra, ensure_ascii=False)))
            connection.execute('INSERT INTO sqlar(name,item_id,mode,sz,data) VALUES(?,?,?,?,?)',
                               (f'{item_id}.png', item_id, 0o644, len(blob), blob))
        for order, page_id in enumerate(PAGES):
            connection.execute('INSERT INTO workspace_pages VALUES(?,?,?,?,?)',
                               (page_id, 'canvas', page_id,
                                json.dumps({'__prism_page_payload__': True,
                                            'content': page_id, 'tags': [],
                                            'parent': None, 'permanent': False},
                                           ensure_ascii=False), order))
        connection.execute('INSERT INTO board_notes VALUES(?,?,?,?)',
                           (1, 0, '<p>note</p>', 0))
        connection.execute('INSERT INTO board_mindmap VALUES(?,?,?)',
                           (0, json.dumps({'root': {}}), 0))
        for attachment_id, name in ((1, 'one.png'), (2, 'two.png')):
            connection.execute(
                'INSERT INTO attachments(id,board_id,name,mime,blob,created_at) '
                'VALUES(?,?,?,?,?,?)',
                (attachment_id, 0, name, 'image/png',
                 b'attachment-' + name.encode(), 0))
        connection.execute('INSERT INTO future_extension VALUES(?,?)',
                           (1, b'unknown-table\x00\xff'))


@pytest.fixture(scope='module')
def workspace(tmp_path_factory):
    root = tmp_path_factory.mktemp('cross-page-move')
    source = root / 'moves-source.prism'
    build_project(source)
    pages = [dict(id=row[0], kind=row[1], title=row[2],
                  content=json.loads(row[3])['content'], tags=[], parent=None,
                  permanent=False)
             for row in table_rows(source, 'workspace_pages')]
    return dict(root=root, source=source, pages=pages)


def open_session(source):
    """canvas-a / canvas-b 已加载；canvas-c 未加载。"""
    return PartialProjectSession.open(
        source, item_ids=LOADED_ITEMS, page_ids=LOADED_PAGES,
        pages_complete=False, include_boards=True)


def scene_for(workspace):
    """只放**已加载**的页面与素材；未加载的 canvas-c 不该出现在场景里。"""
    rows = {row[0]: row for row in table_rows(workspace['source'], 'items')}
    items = [Item(item_id, json.loads(rows[item_id][-1]),
                  x=item_id * 10.0, y=5.0, z=float(item_id))
             for item_id in LOADED_ITEMS]
    pages = [page for page in workspace['pages'] if page['id'] in LOADED_PAGES]
    return Scene(items, pages)


def test_cross_page_move_matrix(workspace, qapp, monkeypatch):
    source = workspace['source']
    source_before = digest(source)
    pages = set(PAGES)

    def target(name):
        return workspace['root'] / f'{name}.prism'

    def move(source_id, new_canvas, name):
        """Try one move; return what happened without letting it escape."""
        session = open_session(source)
        scene = scene_for(workspace)
        item = next(entry for entry in scene._items if entry.save_id == source_id)
        item.data['canvasId'] = new_canvas
        refused, error = None, None
        try:
            snapshot = session.capture(scene)
            receipt = write_partial_snapshot(snapshot, target(name))
        except PartialSaveConflict as problem:
            refused, error = True, str(problem)
        except BaseException as problem:                  # noqa: BLE001
            refused, error = True, f'{type(problem).__name__}: {problem}'
        written = target(name).exists()
        observed = canvas_of(target(name), source_id) if written else (None, None)
        return dict(session=session, refused=bool(refused), error=error,
                    written=written,
                    written_canvas=observed[0], explicit_key=observed[1],
                    members=canvas_members(target(name)) if written else {},
                    consistency=(consistency(source, target(name)) if written else {}),
                    reopened=(reopen_canvases(target(name), monkeypatch)
                              if written else {}))

    def case_1_loaded_to_loaded():
        moved = move(1, CANVAS_B, 'move-loaded')
        members = moved['members']
        return dict(ok=(not moved['refused'] and moved['written']
                        and moved['written_canvas'] == CANVAS_B
                        and members.get(CANVAS_A) == [2, 3]
                        and members.get(CANVAS_B) == [1, 4, 5]
                        and sum(len(ids) for ids in members.values()) == len(LOADED_ITEMS)
                        and moved['consistency']['altered_tables'] == []
                        and moved['consistency']['originals_identical']
                        and moved['consistency']['attachments_identical']
                        and moved['reopened'].get(1) == CANVAS_B),
                    outcome='written' if moved['written'] else 'refused',
                    written_canvas=moved['written_canvas'],
                    members=moved['members'],
                    reopened_canvas=moved['reopened'].get(1),
                    consistency=moved['consistency'])

    def case_2_unloaded_to():
        moved = move(1, CANVAS_C, 'move-unloaded')
        effective = (effective_canvas(target('move-unloaded'), 1, pages)
                     if moved['written'] else None)
        return dict(ok=moved['refused'],
                    outcome='refused' if moved['refused'] else 'written',
                    refused=moved['refused'], message=moved['error'],
                    written_canvas=moved['written_canvas'],
                    target_canvas_loaded=False,
                    effective_canvas=effective,
                    members=moved['members'],
                    reopened_canvas=moved['reopened'].get(1),
                    consistency=moved['consistency'])

    def case_3_missing_canvas():
        moved = move(1, MISSING_CANVAS, 'move-missing')
        effective = (effective_canvas(target('move-missing'), 1, pages)
                     if moved['written'] else None)
        return dict(ok=moved['refused'],
                    outcome='refused' if moved['refused'] else 'written',
                    refused=moved['refused'], message=moved['error'],
                    written_canvas=moved['written_canvas'],
                    effective_canvas=effective,
                    members=moved['members'],
                    reopened_canvas=moved['reopened'].get(1),
                    consistency=moved['consistency'])

    def case_4a_to_default():
        moved = move(2, DEFAULT_CANVAS, 'move-to-default')
        members = moved['members']
        return dict(ok=(not moved['refused'] and moved['written']
                        and moved['written_canvas'] == DEFAULT_CANVAS
                        and members.get(DEFAULT_CANVAS) == [2, 6, 7]
                        and moved['reopened'].get(2) == DEFAULT_CANVAS
                        and moved['consistency']['altered_tables'] == []),
                    outcome='written' if moved['written'] else 'refused',
                    written_canvas=moved['written_canvas'],
                    members=moved['members'],
                    reopened_canvas=moved['reopened'].get(2),
                    consistency=moved['consistency'])

    def case_4b_from_default():
        moved = move(6, CANVAS_A, 'move-from-default')
        members = moved['members']
        return dict(ok=(not moved['refused'] and moved['written']
                        and moved['written_canvas'] == CANVAS_A
                        and members.get(CANVAS_A) == [1, 2, 3, 6]
                        and moved['reopened'].get(6) == CANVAS_A
                        and moved['consistency']['altered_tables'] == []),
                    outcome='written' if moved['written'] else 'refused',
                    written_canvas=moved['written_canvas'],
                    members=moved['members'],
                    reopened_canvas=moved['reopened'].get(6),
                    consistency=moved['consistency'])

    def case_5_second_move_before_ack():
        """保存尚未结束时再次移动同一张图：两步都要可判定，不能丢或重。"""
        session = open_session(source)
        scene = scene_for(workspace)
        item = next(entry for entry in scene._items if entry.save_id == 1)
        item.data['canvasId'] = CANVAS_B                  # 第一次移动
        first = session.capture(scene)
        item.data['canvasId'] = CANVAS_C                  # 保存未结束时再移动
        second = session.capture(scene)
        first_refused, second_refused = None, None
        try:
            receipt = write_partial_snapshot(first, target('twice-1'))
        except PartialSaveConflict as problem:
            first_refused = str(problem)
            receipt = None
        if receipt is not None:
            session.acknowledge(first, receipt, scene=scene, filename=str(target('twice-1')))
        try:
            write_partial_snapshot(second, target('twice-2'))
        except PartialSaveConflict as problem:
            second_refused = str(problem)
        after_first = (canvas_of(target('twice-1'), 1)
                       if target('twice-1').exists() else (None, None))
        after_second = (canvas_of(target('twice-2'), 1)
                        if target('twice-2').exists() else (None, None))
        members = (canvas_members(target('twice-2'))
                   if target('twice-2').exists() else {})
        pending = len(session.capture(scene).changes) if receipt is not None else None
        # 第一次（已加载目标）必须成功；第二次（未加载目标）必须被拒绝。
        return dict(ok=(first_refused is None and after_first[0] == CANVAS_B
                        and second_refused is not None and not target('twice-2').exists()),
                    first_refused=first_refused, first_written=after_first[0],
                    second_refused=second_refused,
                    second_written=after_second[0],
                    second_target_exists=target('twice-2').exists(),
                    members=members, pending_after_ack=pending)

    def case_6_undo_redo():
        session = open_session(source)
        scene = scene_for(workspace)
        item = next(entry for entry in scene._items if entry.save_id == 3)
        original_canvas = item.data.get('canvasId', DEFAULT_CANVAS)
        item.data['canvasId'] = CANVAS_B                  # 移动（模拟）
        moved = session.capture(scene)
        item.data['canvasId'] = original_canvas           # 撤销（模拟）
        undone = session.capture(scene)
        item.data['canvasId'] = CANVAS_B                  # 重做（模拟）
        redone = session.capture(scene)
        write_partial_snapshot(redone, target('undo-redo'))
        reopened = reopen_canvases(target('undo-redo'), monkeypatch)
        members = canvas_members(target('undo-redo'))
        return dict(ok=(len(moved.changes) == 1 and len(undone.changes) == 0
                        and len(redone.changes) == 1
                        and reopened.get(3) == CANVAS_B
                        and members.get(CANVAS_A) == [1, 2]
                        and consistency(source, target('undo-redo'))['altered_tables'] == []),
                    moved_changes=len(moved.changes),
                    undone_changes=len(undone.changes),
                    redone_changes=len(redone.changes),
                    reopened_canvas=reopened.get(3),
                    members=members,
                    consistency=consistency(source, target('undo-redo')))

    record('1', 'loaded canvas -> loaded canvas',
           '正确完成：归属变为 canvas-b，无消失/重复，其它表一致，重开后归属正确',
           case_1_loaded_to_loaded)
    record('2', 'loaded canvas -> unloaded canvas',
           '必须明确拒绝；静默写入不算通过', case_2_unloaded_to)
    record('3', 'loaded canvas -> non-existent canvas',
           '必须明确拒绝；写到不存在的画布等同于让素材落到未分类', case_3_missing_canvas)
    record('4a', 'normal canvas -> default canvas',
           '默认画布是隐式合法画布：应正确完成（修复 #1 时不能误拦）', case_4a_to_default)
    record('4b', 'default canvas -> normal canvas',
           '应正确完成：归属变为 canvas-a，重开后正确', case_4b_from_default)
    record('5', 'second move while the save is still open',
           '第一次（已加载目标）成功；保存未结束时的第二次（未加载目标）必须拒绝',
           case_5_second_move_before_ack)
    record('6', 'move -> undo -> redo -> save -> reopen',
           '撤销后无变更；重做后 1 个变更；重开归属 canvas-b；其它表一致', case_6_undo_redo)

    total = len(CASES)
    passed = sum(1 for case in CASES if case['passed'])
    after = digest(source)
    summary = dict(source=str(source), source_sha256_before=source_before,
                   source_sha256_after=after, source_unchanged=after == source_before,
                   generated_at=time.strftime('%Y-%m-%dT%H:%M:%S'),
                   loaded_pages=list(LOADED_PAGES), unloaded_page=CANVAS_C,
                   missing_page=MISSING_CANVAS,
                   note=('undo/redo 用内存 canvasId 值变化模拟，不经过 QUndoStack 命令栈；'
                         '画布视图可见性未测（没有正式入口覆盖）'),
                   total=total, passed=passed, failed=total - passed, cases=CASES)
    RESULT.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding='utf-8')
    print(f'MATRIX {passed}/{total} passed; source_unchanged={summary["source_unchanged"]}',
          flush=True)
    print(f'REPORT {RESULT}', flush=True)


def test_gui_save_reports_success(main_window, qtbot, monkeypatch, tmp_path):
    """情况 2/3 走真实 do_save：界面是把这次保存当成功了，还是拒绝了？"""
    from prism.fileio.partial_save import item_save_columns
    from prism.items import PrismTextItem

    scene = main_window.view.scene
    view = main_window.view
    entries, png = [], b'gui-original-bytes'
    for item_id, canvas_id in ((1, CANVAS_A), (2, CANVAS_A), (3, CANVAS_C)):
        item = PrismTextItem(f'move {item_id}')
        item.setPos(item_id * 10.0, 5.0)
        item.setZValue(float(item_id))
        item.save_id = item_id
        item.canvas_id = canvas_id
        item.title = f'title {item_id}'
        entries.append(item)
    source = tmp_path / 'gui-moves.prism'
    with sqlite3.connect(source) as connection:
        connection.executescript(SCHEMA)
        connection.execute('INSERT INTO prism_metadata VALUES(?,?)',
                           ('project_id', scene.project_id))
        for item in entries:
            connection.execute('INSERT INTO items VALUES(?,?,?,?,?,?,?,?,?)',
                               (item.save_id, *item_save_columns(item)))
            connection.execute('INSERT INTO sqlar(name,item_id,mode,sz,data) '
                               'VALUES(?,?,?,?,?)',
                               (f'{item.save_id}.png', item.save_id, 0o644, len(png), png))
        connection.execute('INSERT INTO workspace_pages VALUES(?,?,?,?,?)',
                           (CANVAS_A, 'canvas', CANVAS_A,
                            json.dumps({'__prism_page_payload__': True, 'content': CANVAS_A,
                                        'tags': [], 'parent': None, 'permanent': False},
                                       ensure_ascii=False), 0))
        connection.execute('INSERT INTO board_notes VALUES(?,?,?,?)',
                           (1, 0, scene.note_html, 0))
        connection.execute('INSERT INTO board_mindmap VALUES(?,?,?)',
                           (0, None if scene.mindmap_tree is None else
                            json.dumps(scene.mindmap_tree, ensure_ascii=False), 0))

    # 只加载 canvas-a 的两条；第 3 条留在未加载的 canvas-c。
    for item in entries:
        if item.save_id in (1, 2):
            scene.addItem(item)
    # 场景里只放已加载的页面（部分加载时未加载页不会出现在 scene 中）。
    scene.workspace_pages = [
        dict(id=CANVAS_A, kind='canvas', title=CANVAS_A, content=CANVAS_A,
             tags=[], parent=None, permanent=False)]

    results = []
    for label, move_to in (('2', CANVAS_C), ('3', MISSING_CANVAS)):
        session = PartialProjectSession.open(
            source, item_ids=(1, 2), page_ids=(CANVAS_A,),
            pages_complete=False, include_boards=True)
        session.attach(scene)
        moved = next(item for item in scene.items() if getattr(item, 'save_id', None) == 1)
        moved.canvas_id = move_to
        events = []
        monkeypatch.setattr('prism.view.trace_record',
                            lambda event, **kwargs: events.append((event, kwargs)))
        target = tmp_path / f'gui-move-{label}.prism'
        view.do_save(str(target), create_new=True)
        qtbot.waitUntil(lambda: session.acknowledged is not None
                        or getattr(view, '_save_partial_snapshot', None) is None,
                        timeout=20000)
        refused = [kwargs for event, kwargs in events if event == 'project_save_refused']
        written = target.exists()
        written_canvas = (canvas_of(target, 1)[0] if written else None)
        results.append(dict(
            label=label, moved_to=move_to, refused_message=refused[0]['reason'] if refused else None,
            acknowledged=(session.acknowledged is not None),
            receipt_changed_rows=(session.acknowledged.changed_rows
                                  if session.acknowledged else None),
            target_written=written, written_canvas=written_canvas,
            status_message=main_window.statusBar().currentMessage(),
            reported_success=(session.acknowledged is not None and not refused),
        ))
        # 清理：撤掉会话标记，避免影响下一轮
        scene._partial_project_session = None
        scene._partial_project_source = None
        moved.canvas_id = CANVAS_A
        view.undo_stack.setClean()

    silent = [row for row in results if row['target_written'] and not row['refused_message']]
    payload = dict(cases=results, silent_writes=[row['label'] for row in silent],
                   generated_at=time.strftime('%Y-%m-%dT%H:%M:%S'),
                   note=('do_save 真实路径；reported_success=True 表示界面确认了这次保存。'
                         '此处只证明"界面是否视为成功"，不证明画布视图可见性'))
    GUI_RESULT.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding='utf-8')
    print(f'GUI silent_writes={payload["silent_writes"]}', flush=True)
    print(f'REPORT {GUI_RESULT}', flush=True)
