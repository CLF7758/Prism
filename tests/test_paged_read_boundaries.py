"""Paged IO must bound originals and must not replace newer editor content."""
import json
import sqlite3
from types import SimpleNamespace

import pytest
from PyQt6 import QtGui

from prism.fileio.sql import SQLiteIO, load_deferred_board_extras
from prism.scene import PrismGraphicsScene


def make_project(tmp_path):
    path = tmp_path / 'bounded.prism'
    scene = PrismGraphicsScene(QtGui.QUndoStack())
    writer = SQLiteIO(str(path), scene, create_new=True)
    writer.create_schema_on_new()
    writer.write_board_extras()
    for key in (1, 2):
        payload = json.dumps(dict(imageWidth=128, imageHeight=64,
                                  canvasId='canvas-a', filename='original.png'))
        writer.ex('INSERT INTO items(id,type,x,y,z,scale,rotation,flip,data) '
                  'VALUES (?,"pixmap",0,0,0,1,0,0,?)', (key, payload))
        writer.ex('INSERT INTO sqlar(item_id,name,mode,mtime,sz,data) '
                  'VALUES (?, ?, 0, 0, ?, ?)',
                  (key, str(key), 64, b'original bytes must stay on disk' * 2))
    writer.connection.commit()
    writer._close_connection()
    return path


def test_paged_images_do_not_fetch_original_blob(qapp, tmp_path, monkeypatch):
    path = make_project(tmp_path)
    scene = PrismGraphicsScene(QtGui.QUndoStack())
    reader = SQLiteIO(str(path), scene, readonly=True)
    original = reader._read_rows
    observed = {}

    def read_rows(label, statement, parameters=()):
        rows = original(label, statement, parameters)
        observed[label] = rows
        return rows

    monkeypatch.setattr(reader, '_read_rows', read_rows)
    reader.read(scope=[1])
    assert observed['embedded_glb'] == []
    assert [row[0] for row in observed['image_metadata']] == [1]
    assert len(observed['original_sizes']) == 1
    assert scene._partial_project_source
    assert scene._loaded_scope_ids == (1,)
    reader._close_connection()


def test_empty_scope_never_becomes_full_read(qapp, tmp_path, monkeypatch):
    path = make_project(tmp_path)
    scene = PrismGraphicsScene(QtGui.QUndoStack())
    reader = SQLiteIO(str(path), scene, readonly=True)
    original = reader._read_rows
    observed = {}

    def read_rows(label, statement, parameters=()):
        rows = original(label, statement, parameters)
        observed[label] = rows
        return rows

    monkeypatch.setattr(reader, '_read_rows', read_rows)
    reader.read(scope=[])
    assert observed['image_metadata'] == []
    assert observed['native_item_metadata'] == []
    assert observed['embedded_glb'] == []
    assert observed['original_sizes'] == []
    assert scene._partial_project_source
    assert scene._loaded_scope_ids == ()
    reader._close_connection()


def test_paged_page_headers_do_not_transfer_document_bodies(qapp, tmp_path):
    path = make_project(tmp_path)
    payload = json.dumps({'__prism_page_payload__': True,
                          'content': '<p>' + 'large body' * 100000 + '</p>',
                          'tags': ['reference'], 'parent': 'folder',
                          'permanent': True, 'futureHeader': 7})
    with sqlite3.connect(path) as connection:
        connection.execute('INSERT INTO workspace_pages(id,kind,title,data,sort_order) '
                           'VALUES (?,?,?,?,?)', ('doc', 'document', 'Document', payload, 0))
    scene = PrismGraphicsScene(QtGui.QUndoStack())
    reader = SQLiteIO(str(path), scene, readonly=True)
    headers = reader._workspace_page_headers()
    assert len(headers) == 1
    header = json.loads(headers[0][3])
    assert 'content' not in header
    assert header['tags'] == ['reference']
    assert header['futureHeader'] == 7
    assert len(headers[0][3]) < 256
    reader.read(scope=[])
    assert scene.workspace_pages == [dict(id='doc', kind='document', title='Document',
        tags=['reference'], parent='folder', permanent=True)]
    reader._close_connection()
    load_deferred_board_extras(scene, names={'workspace_pages'}, page_ids={'doc'})
    assert scene.workspace_pages[0]['content'] == json.loads(payload)['content']


@pytest.fixture
def deferred_scene(tmp_path):
    source = tmp_path / 'deferred.sqlite'
    with sqlite3.connect(source) as connection:
        connection.executescript('''
            CREATE TABLE board_notes(board_id INTEGER, html TEXT);
            CREATE TABLE board_mindmap(board_id INTEGER, tree TEXT);
            CREATE TABLE workspace_pages(id TEXT PRIMARY KEY, data TEXT);
        ''')
        connection.execute('INSERT INTO board_notes VALUES (0,?)', ('stored note',))
        connection.execute('INSERT INTO board_mindmap VALUES (0,?)',
                           (json.dumps({'data': {'text': 'stored tree'}}),))
        for page_id, content in [('a', 'stored a'), ('b', 'stored b')]:
            connection.execute('INSERT INTO workspace_pages VALUES (?,?)',
                (page_id, json.dumps({'__prism_page_payload__': True,
                                      'content': content})))
    return SimpleNamespace(
        _board_extras_source=str(source),
        _deferred_board_extras={'note_html', 'mindmap_tree', 'workspace_pages'},
        _deferred_page_ids={'a', 'b'},
        workspace_pages=[{'id': 'a'}, {'id': 'b'}],
        note_html='', mindmap_tree=None)


def test_only_requested_page_body_is_loaded(deferred_scene):
    scene = deferred_scene
    load_deferred_board_extras(scene, names={'workspace_pages'}, page_ids={'a'})
    assert scene.workspace_pages == [{'id': 'a', 'content': 'stored a'}, {'id': 'b'}]
    assert scene._deferred_page_ids == {'b'}
    assert 'workspace_pages' in scene._deferred_board_extras
    assert scene.note_html == ''


@pytest.mark.parametrize('edited', ['new edit', '', None])
def test_save_materialization_preserves_edits_and_explicit_clears(deferred_scene, edited):
    scene = deferred_scene
    scene.workspace_pages[0]['content'] = edited
    load_deferred_board_extras(scene)
    assert scene.workspace_pages[0]['content'] == edited
    assert scene.workspace_pages[1]['content'] == 'stored b'
    assert scene._deferred_board_extras == set()
    assert scene._deferred_page_ids == set()


def test_second_load_does_not_overwrite_first_page_edits(deferred_scene):
    scene = deferred_scene
    load_deferred_board_extras(scene, names={'workspace_pages'}, page_ids={'a'})
    scene.workspace_pages[0]['content'] = 'changed after opening'
    load_deferred_board_extras(scene)
    assert scene.workspace_pages[0]['content'] == 'changed after opening'


def test_legacy_unwrapped_body_is_retained(deferred_scene):
    scene = deferred_scene
    with sqlite3.connect(scene._board_extras_source) as connection:
        connection.execute('UPDATE workspace_pages SET data=? WHERE id="a"',
                           (json.dumps('legacy html'),))
    load_deferred_board_extras(scene, names={'workspace_pages'}, page_ids={'a'})
    assert scene.workspace_pages[0]['content'] == 'legacy html'


def test_invalid_body_keeps_pending_state(deferred_scene):
    scene = deferred_scene
    with sqlite3.connect(scene._board_extras_source) as connection:
        connection.execute('UPDATE workspace_pages SET data=? WHERE id="a"', ('{broken',))
    with pytest.raises(ValueError):
        load_deferred_board_extras(scene, names={'workspace_pages'}, page_ids={'a'})
    assert 'content' not in scene.workspace_pages[0]
    assert 'a' in scene._deferred_page_ids


def test_disappeared_page_is_not_presented_as_empty(deferred_scene):
    scene = deferred_scene
    with sqlite3.connect(scene._board_extras_source) as connection:
        connection.execute('DELETE FROM workspace_pages WHERE id="a"')
    with pytest.raises(ValueError, match='disappeared'):
        load_deferred_board_extras(scene, names={'workspace_pages'}, page_ids={'a'})
    assert 'content' not in scene.workspace_pages[0]
    assert 'a' in scene._deferred_page_ids


def test_schema_errors_are_not_swallowed_as_empty_content(deferred_scene):
    scene = deferred_scene
    with sqlite3.connect(scene._board_extras_source) as connection:
        connection.execute('ALTER TABLE board_notes RENAME COLUMN html TO unexpected')
    with pytest.raises(sqlite3.OperationalError):
        load_deferred_board_extras(scene, names={'note_html'})
    assert 'note_html' in scene._deferred_board_extras


@pytest.mark.parametrize('kind', ['note', 'mindmap'])
def test_explicit_default_editor_changes_retire_deferred_flag(deferred_scene, kind):
    scene = deferred_scene
    panel = SimpleNamespace(view=SimpleNamespace(scene=scene), page_id=None)
    if kind == 'note':
        from prism.widgets.document_panel import DocumentPanel
        DocumentPanel._set_scene_html(panel, 'new note')
        load_deferred_board_extras(scene)
        assert scene.note_html == 'new note'
    else:
        from prism.widgets.mindmap_panel import MindMapPanel
        MindMapPanel._set_scene_tree(panel, None)
        load_deferred_board_extras(scene)
        assert scene.mindmap_tree is None
