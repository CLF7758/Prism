"""Partially loaded projects must save through the *real* GUI path.

``tests/test_partial_project.py`` proves the contract against a fake scene:
``PartialProjectSession.capture`` in, ``write_partial_snapshot`` out. This
file re-proves the same contract through the code the shipped UI actually
runs:

    PrismGraphicsView.do_save -> _capture_partial_save (GUI thread)
                              -> ThreadedIO(fileio.save_prism, partial_snapshot=...)
                              -> SQLiteIO.write -> write_partial_snapshot (worker thread)
                              -> on_saving_finished -> _acknowledge_partial_save

Two behaviours matter here and neither is exercised by the pure unit tests:

* the diff base comes from a real ``PrismGraphicsScene`` whose items are real
  Qt widgets, so ``item_save_columns``/``get_extra_save_data`` decide what is
  "unchanged" -- not a hand-written dict;
* a refused capture must stop *before* a worker is created, and the reason
  must reach the user, because a fallback to the full writer would treat every
  unloaded record as deleted.
"""
import json
from pathlib import Path
import sqlite3
from types import SimpleNamespace

import pytest

from prism.fileio.partial_save import item_save_columns
from prism.items import PrismTextItem
from prism.partial_project import PartialProjectSession


PNG = Path(__file__).parent / 'assets' / 'test3x3.png'

#: Canvas A is the page this file opens; canvas B is never loaded.
CANVAS_A = (1, 2, 3, 4, 5)
CANVAS_B = (6, 7, 8, 9)
EDITED = 2

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

#: Tables a partial save must not touch: none of them was loaded for canvas A.
UNLOADED_TABLES = ('sqlar', 'attachments', 'workspace_pages', 'workspace_nodes',
                   'material_records', 'board_notes', 'board_mindmap',
                   'prism_metadata', 'future_extension')


def rows(path, table):
    with sqlite3.connect(Path(path).as_uri() + '?mode=ro', uri=True) as connection:
        return connection.execute(f'SELECT * FROM "{table}" ORDER BY 1').fetchall()


def row_map(path, table):
    return {row[0]: row for row in rows(path, table)}


def text_item(key):
    """A real Qt item whose saved columns match a row this test writes."""
    item = PrismTextItem(f'card {key}')
    item.setPos(float(key * 10), float(key * 5))
    item.setZValue(float(key))
    item.save_id = key
    item.canvas_id = 'A' if key in CANVAS_A else 'B'
    item.title = f'title {key}'
    item.notes = f'note {key}'
    return item


@pytest.fixture
def project(main_window, tmp_path):
    """A temp .prism whose canvas A is loaded into the real scene.

    Each row is written from ``item_save_columns`` so the diff base and the
    in-memory items agree exactly; the only change any test sees is the one it
    makes itself. ``prism_metadata.project_id`` mirrors what
    ``SQLiteIO.read()`` copies into ``scene.project_id`` -- without that the
    GUI drops the save result and never acknowledges it.
    """
    scene = main_window.view.scene
    items = [text_item(key) for key in CANVAS_A + CANVAS_B]
    source = tmp_path / 'workspace.prism'
    png = PNG.read_bytes()
    with sqlite3.connect(source) as connection:
        connection.executescript(SCHEMA)
        connection.execute('INSERT INTO prism_metadata VALUES(?,?)',
                           ('project_id', scene.project_id))
        for item in items:
            connection.execute('INSERT INTO items VALUES(?,?,?,?,?,?,?,?,?)',
                               (item.save_id, *item_save_columns(item)))
            connection.execute('INSERT INTO sqlar(name,item_id,mode,sz,data) '
                               'VALUES(?,?,?,?,?)',
                               (f'{item.save_id}.png', item.save_id, 0o644,
                                len(png), png))
        for order, page_id in enumerate(('default-canvas', 'notes')):
            connection.execute('INSERT INTO workspace_pages VALUES(?,?,?,?,?)',
                               (page_id, 'document', page_id,
                                json.dumps({'__prism_page_payload__': True,
                                            'content': page_id, 'tags': [],
                                            'parent': None, 'permanent': False}),
                                order))
        connection.execute('INSERT INTO workspace_nodes VALUES(?,?)',
                           ('n1', '{"pageId":"default-canvas"}'))
        connection.execute('INSERT INTO material_records VALUES(?,?)',
                           ('m1', '{"name":"material"}'))
        connection.execute('INSERT INTO board_notes VALUES(?,?,?,?)',
                           (1, 0, scene.note_html, 0))
        connection.execute('INSERT INTO board_mindmap VALUES(?,?,?)',
                           (0, None if scene.mindmap_tree is None
                            else json.dumps(scene.mindmap_tree, ensure_ascii=False), 0))
        connection.execute('INSERT INTO attachments(id,board_id,name,mime,blob,created_at) '
                           'VALUES(?,?,?,?,?,?)',
                           (1, 0, 'board.png', 'image/png', png, 0))
        connection.execute('INSERT INTO future_extension VALUES(?,?)',
                           (1, b'not-a-known-table\x00\xff'))
    # Load only canvas A. Canvas B never enters the scene, like a page scope.
    for item in items:
        if item.save_id in CANVAS_A:
            scene.addItem(item)
    yield SimpleNamespace(source=source, items=items, scene=scene,
                          view=main_window.view, window=main_window)


def open_canvas_a(project, *, include_boards=True):
    session = PartialProjectSession.open(
        project.source, item_ids=CANVAS_A, page_ids=(), pages_complete=False,
        include_boards=include_boards)
    session.attach(project.scene)
    return session


def test_one_page_edit_saves_through_the_view_and_keeps_unloaded_rows(
        project, qtbot, monkeypatch):
    view, scene = project.view, project.scene
    session = open_canvas_a(project)

    # The baseline and the loaded page agree: an untouched page writes nothing.
    assert session.capture(scene).changes == ()

    edited = next(item for item in project.items if item.save_id == EDITED)
    edited.notes = 'edited on canvas A'
    edited.tags = ['reviewed']
    before = {table: rows(project.source, table)
              for table in ('items',) + UNLOADED_TABLES}

    events = []
    monkeypatch.setattr('prism.view.trace_record',
                        lambda event, **kwargs: events.append((event, kwargs)))

    target = project.source.parent / 'saved copy.prism'
    view.do_save(str(target), create_new=True)

    # Acknowledged only after the worker's receipt was applied to the session.
    qtbot.waitUntil(lambda: session.acknowledged is not None, timeout=20000)
    qtbot.waitUntil(lambda: getattr(view, '_save_partial_snapshot', None) is None,
                    timeout=5000)

    assert session.invalid is None
    assert session.revision == 1
    receipt = session.acknowledged
    assert Path(receipt.filename) == target.resolve()
    # The item and its newly declared project tag are one transaction.
    assert receipt.changed_rows == 2
    assert json.loads(dict(rows(target, 'prism_metadata'))['tags']) == ['reviewed']
    assert [event for event, _kwargs in events
            if event == 'project_partial_save_acknowledged']
    # The GUI really took the partial branch, and updated the window's file.
    assert view.filename == str(target)

    # The source project was read, never rewritten.
    assert rows(project.source, 'items') == before['items']

    original_items = {row[0]: row for row in before['items']}
    saved = row_map(target, 'items')
    assert len(saved) == len(original_items) == len(CANVAS_A) + len(CANVAS_B)
    for key, original in original_items.items():
        if key == EDITED:
            continue
        # Unloaded canvas B rows and untouched canvas A rows are byte-identical.
        assert saved[key] == original, key
    assert saved[EDITED][:-1] == original_items[EDITED][:-1]  # geometry untouched
    data = json.loads(saved[EDITED][-1])
    assert data['notes'] == 'edited on canvas A'
    assert data['tags'] == ['reviewed']
    assert data['title'] == f'title {EDITED}'         # untouched keys survive
    assert data['canvasId'] == 'A'

    for table in UNLOADED_TABLES:
        if table == 'prism_metadata':
            expected = dict(before[table])
            expected['tags'] = json.dumps(['reviewed'], ensure_ascii=False)
            assert dict(rows(target, table)) == expected
        else:
            assert rows(target, table) == before[table], table
    assert [row[0] for row in rows(target, 'attachments')] == [1]
    assert rows(target, 'sqlar')[0][-1] == PNG.read_bytes()

    # The acknowledged revision moved the base forward: nothing is left dirty.
    assert session.capture(scene).changes == ()


def test_a_loaded_item_leaving_the_scene_refuses_before_any_worker(
        project, monkeypatch):
    view, scene, window = project.view, project.scene, project.window
    session = open_canvas_a(project, include_boards=False)

    events = []
    monkeypatch.setattr('prism.view.trace_record',
                        lambda event, **kwargs: events.append((event, kwargs)))
    worker_before = getattr(view, 'worker', None)
    window.statusBar().clearMessage()

    gone = next(item for item in project.items if item.save_id == 3)
    scene.removeItem(gone)

    target = project.source.parent / 'refused.prism'
    view.do_save(str(target), create_new=True)

    # Refused on the GUI thread: no worker was created, nothing was written.
    assert getattr(view, '_save_partial_snapshot', None) is None
    assert getattr(view, 'worker', None) is worker_before
    assert not target.exists()
    assert session.revision == 0 and session.acknowledged is None
    assert session.invalid is None

    refused = [kwargs for event, kwargs in events if event == 'project_save_refused']
    assert refused, [event for event, _kwargs in events]
    assert 'no longer on this page' in refused[0]['reason']
    message = window.statusBar().currentMessage()
    assert 'partly open' in message and '3' in message


def test_items_still_installing_refuse_the_gui_save(project, monkeypatch):
    view, scene, window = project.view, project.scene, project.window
    session = open_canvas_a(project, include_boards=False)

    events = []
    monkeypatch.setattr('prism.view.trace_record',
                        lambda event, **kwargs: events.append((event, kwargs)))
    worker_before = getattr(view, 'worker', None)
    window.statusBar().clearMessage()

    scene.items_to_add.put(({'type': 'text', 'data': {'text': 'pending'}}, False))
    target = project.source.parent / 'pending.prism'
    try:
        view.do_save(str(target), create_new=True)
    finally:
        scene.items_to_add.get_nowait()

    assert getattr(view, '_save_partial_snapshot', None) is None
    assert getattr(view, 'worker', None) is worker_before
    assert not target.exists()
    assert session.revision == 0 and session.acknowledged is None
    refused = [kwargs for event, kwargs in events if event == 'project_save_refused']
    assert refused and 'still installing' in refused[0]['reason']
    assert 'partly open' in window.statusBar().currentMessage()
