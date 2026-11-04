"""A partially loaded project must save only what was opened and edited.

The page read at open time is the entire diff base: rows that were never
loaded stay untouched, a loaded row that vanishes is refused instead of being
treated as deleted, and an acknowledged save only advances the rows the
receipt covered.
"""
import json
from pathlib import Path
import queue
import sqlite3

import pytest

from prism.fileio.partial_save import PartialSaveConflict, write_partial_snapshot
from prism.fileio.attachments_store import StoredAttachments
from prism.partial_project import PartialProjectSession


class _Point:
    def __init__(self, x, y):
        self._x, self._y = x, y

    def x(self):
        return self._x

    def y(self):
        return self._y


class FakeItem:
    """Only the surface a save reads; no Qt widget is needed."""

    TYPE = 'pixmap'

    def __init__(self, save_id=None, data=None, x=0.0, y=0.0, z=0.0, source=None):
        self.save_id = save_id
        self.data = dict(data or {})
        self._point = _Point(x, y)
        self._z = z
        self.scale_value = 1.0
        self.rotation_value = 0.0
        self.flip_value = 1
        self._source = source

    def pos(self):
        return self._point

    def zValue(self):
        return self._z

    def scale(self):
        return self.scale_value

    def rotation(self):
        return self.rotation_value

    def flip(self):
        return self.flip_value

    def get_extra_save_data(self):
        return dict(self.data)

    # New-asset surface, mirroring PrismPixmapItem.
    def original_bytes(self):
        return self._source

    def original_format(self):
        return 'png' if self._source else None

    def pixmap_to_bytes(self):
        return self._source, 'png'

    def get_filename_for_export(self, image_format):
        return f'{self.data.get("filename", "asset")}'


_UNCHANGED = object()


class FakeScene:
    """Stands in for the scene: only the surfaces a save reads."""

    def __init__(self, items, pages=(), note='<p>old</p>', tree=_UNCHANGED, attachments=None):
        self._items = list(items)
        self.workspace_pages = list(pages)
        self.note_html = note
        self.mindmap_tree = {'root': {}} if tree is _UNCHANGED else tree
        self.attachments = attachments if attachments is not None else {}
        self.items_to_add = queue.Queue()
        self._prism_archive_items = set()

    def iter_assets(self):
        return iter(self._items)


def rows(path, table):
    with sqlite3.connect(path) as connection:
        return connection.execute(f'SELECT * FROM {table} ORDER BY 1').fetchall()


def item_rows(path):
    return {row[0]: row for row in rows(path, 'items')}


@pytest.fixture
def png():
    return (Path(__file__).parent / 'assets/test3x3.png').read_bytes()


@pytest.fixture
def project(tmp_path, png):
    source = tmp_path / 'partial project.prism'
    with sqlite3.connect(source) as connection:
        connection.executescript('''
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
        ''')
        connection.execute('INSERT INTO prism_metadata VALUES(?,?)', ('project_id', 'stable-project'))
        for key in range(1, 13):
            extra = dict(canvasId='A' if key <= 4 else 'B', filename=f'{key}.png',
                         imageWidth=3, imageHeight=3, tags=['keep'], notes='before',
                         future={'nested': ['unknown', key]})
            connection.execute('INSERT INTO items VALUES(?,?,?,?,?,?,?,?,?)',
                               (key, 'pixmap', key * 10, 0, 0, 1, 0, 1, json.dumps(extra)))
            connection.execute('INSERT INTO sqlar(name,item_id,mode,sz,data) VALUES(?,?,?,?,?)',
                               (f'{key}.png', key, 0o644, len(png), png))
        for order, (key, content) in enumerate([('d1', 'first'), ('d2', 'second'), ('map', None)]):
            connection.execute('INSERT INTO workspace_pages VALUES(?,?,?,?,?)',
                               (key, 'mindmap' if key == 'map' else 'document', key,
                                json.dumps(dict(__prism_page_payload__=True, content=content,
                                                tags=['tag'], parent=None, permanent=False)), order))
        connection.execute('INSERT INTO workspace_nodes VALUES(?,?)', ('n1', '{"pageId":"d1"}'))
        connection.execute('INSERT INTO material_records VALUES(?,?)', ('m1', '{"name":"material"}'))
        connection.execute('INSERT INTO board_notes VALUES(?,?,?,?)', (1, 0, '<p>old</p>', 0))
        connection.execute('INSERT INTO board_mindmap VALUES(?,?,?)', (0, '{"root":{}}', 0))
        connection.execute('INSERT INTO attachments(id,board_id,name,mime,blob,created_at) '
                           'VALUES(?,?,?,?,?,?)', (1, 0, 'one.png', 'image/png', png, 0))
        connection.execute('INSERT INTO future_extension VALUES(?,?)', (1, b'future binary\x00\xff'))
    return source


def page(page_id='d1', content='first'):
    return {'id': page_id, 'kind': 'mindmap' if page_id == 'map' else 'document',
            'title': page_id, 'content': content, 'tags': ['tag'],
            'parent': None, 'permanent': False}


def pages_of(project):
    """The three pages, exactly as the reader would hand them to the scene."""
    return [page(row[0], json.loads(row[3])['content']) for row in rows(project, 'workspace_pages')]


def loaded_items(project):
    """Canvas A: the page a scoped open would actually load."""
    return [FakeItem(save_id=key, data=json.loads(row[-1]), x=row[2], y=row[3],
                     z=row[4])
            for key, row in sorted(item_rows(project).items()) if key <= 4]


def opened(project, item_ids=(1, 2, 3, 4), pages_complete=True, attachments=False):
    pages = [row[0] for row in rows(project, 'workspace_pages')]
    return PartialProjectSession.open(
        project, item_ids=item_ids,
        page_ids=pages if pages_complete else pages[:1],
        pages_complete=pages_complete, attachments=attachments)


def test_one_page_edit_keeps_every_unloaded_row_and_source(project, tmp_path):
    before = {table: rows(project, table) for table in (
        'sqlar', 'attachments', 'workspace_nodes', 'material_records',
        'board_notes', 'board_mindmap', 'future_extension', 'workspace_pages')}
    session = opened(project)
    items = loaded_items(project)
    scene = FakeScene(items, pages_of(project))
    items[0].data['notes'] = 'edited on this page'
    receipt = write_partial_snapshot(session.capture(scene), tmp_path / 'saved.prism')

    assert receipt.changed_rows == 1
    target = tmp_path / 'saved.prism'
    for table, expected in before.items():
        assert rows(target, table) == expected, table
    saved, original = item_rows(target), item_rows(project)
    assert len(saved) == 12
    edited = json.loads(saved[1][-1])
    assert edited['notes'] == 'edited on this page'
    assert edited['future'] == {'nested': ['unknown', 1]}
    for key in range(2, 13):
        assert saved[key] == original[key]


def test_an_untouched_page_writes_no_rows(project, tmp_path):
    session = opened(project)
    scene = FakeScene(loaded_items(project), pages_of(project))
    snapshot = session.capture(scene)
    assert snapshot.changes == ()
    target = tmp_path / 'copy.prism'
    assert write_partial_snapshot(snapshot, target).changed_rows == 0
    assert item_rows(target) == item_rows(project)


def test_trash_then_restore_roundtrip(project, tmp_path):
    session = opened(project)
    items = loaded_items(project)
    scene = FakeScene(items, pages_of(project))
    items[1].data['deletedAt'] = 1234
    trashed = tmp_path / 'trashed.prism'
    snapshot = session.capture(scene)
    receipt = write_partial_snapshot(snapshot, trashed)
    session.acknowledge(snapshot, receipt, scene=scene, filename=str(trashed))
    assert json.loads(item_rows(trashed)[2][-1])['deletedAt'] == 1234

    # Restoring drops the marker; a merge cannot remove a key on its own.
    del items[1].data['deletedAt']
    snapshot = session.capture(scene)
    restored = tmp_path / 'restored.prism'
    receipt = write_partial_snapshot(snapshot, restored)
    session.acknowledge(snapshot, receipt, scene=scene, filename=str(restored))
    assert 'deletedAt' not in json.loads(item_rows(restored)[2][-1])
    assert json.loads(item_rows(restored)[2][-1]) == json.loads(item_rows(project)[2][-1])


def test_a_loaded_item_that_vanishes_is_refused(project):
    session = opened(project)
    scene = FakeScene(loaded_items(project)[1:], pages_of(project))
    with pytest.raises(PartialSaveConflict, match='explicit trash'):
        session.capture(scene)


def test_an_item_from_outside_the_page_is_refused(project):
    session = opened(project)
    items = loaded_items(project)
    items.append(FakeItem(save_id=7, data=json.loads(item_rows(project)[7][-1])))
    with pytest.raises(PartialSaveConflict, match='not loaded with this page'):
        session.capture(FakeScene(items, pages_of(project)))


def test_error_items_are_left_alone(project):
    session = opened(project)
    items = loaded_items(project)
    broken = FakeItem(save_id=None, data={'text': 'Image could not be loaded'})
    broken.TYPE = 'error'
    broken.original_save_id = 3
    items.append(broken)
    assert session.capture(FakeScene(items, pages_of(project))).changes == ()


def test_items_still_installing_block_the_save(project):
    session = opened(project)
    scene = FakeScene(loaded_items(project), pages_of(project))
    scene.items_to_add.put(({'type': 'pixmap'}, False))
    with pytest.raises(PartialSaveConflict, match='still installing'):
        session.capture(scene)


def test_loaded_materials_the_scope_cannot_verify_block_the_save(project):
    """The material library is not in this scope, so its edits must not be dropped."""
    session = opened(project)
    scene = FakeScene(loaded_items(project), pages_of(project))
    scene.material_records = [{'id': 'm1', 'name': 'material'}]
    with pytest.raises(PartialSaveConflict, match='cannot verify: material_records'):
        session.capture(scene)
    # An unloaded (empty) library is not an obstacle: nothing can be edited there.
    scene.material_records = []
    assert session.capture(scene).changes == ()


def test_a_new_asset_is_inserted_with_its_original_bytes(project, tmp_path, monkeypatch, png):
    from prism import thumbnail_cache
    deferred = []
    monkeypatch.setattr(thumbnail_cache, 'defer_preview',
                        lambda key, blob: deferred.append(len(blob)))
    session = opened(project)
    items = loaded_items(project)
    fresh = FakeItem(save_id=None, data={'filename': 'fresh.png', 'notes': 'added'}, source=png)
    items.append(fresh)
    scene = FakeScene(items, pages_of(project))
    snapshot = session.capture(scene)
    assert len(snapshot.new_items) == 1
    target = tmp_path / 'with-new.prism'
    receipt = write_partial_snapshot(snapshot, target)

    saved = item_rows(target)
    assert len(saved) == 13
    new_id = max(saved)
    assert new_id not in item_rows(project)
    with sqlite3.connect(target) as connection:
        name, blob = connection.execute(
            'SELECT name, data FROM sqlar WHERE item_id=?', (new_id,)).fetchone()
    assert blob == png and name == 'fresh.png'
    assert deferred == [len(png)]
    session.acknowledge(snapshot, receipt, scene=scene, filename=str(target))
    assert fresh.save_id == new_id and fresh in scene._prism_archive_items
    # The base now covers the inserted row, so a second save is a no-op.
    assert session.capture(scene).changes == ()


def test_a_new_asset_edited_during_the_save_is_refused(project, tmp_path, monkeypatch, png):
    from prism import thumbnail_cache
    monkeypatch.setattr(thumbnail_cache, 'defer_preview', lambda key, blob: None)
    session = opened(project)
    items = loaded_items(project)
    fresh = FakeItem(save_id=None, data={'filename': 'fresh.png'}, source=png)
    items.append(fresh)
    scene = FakeScene(items, pages_of(project))
    snapshot = session.capture(scene)
    fresh.data['notes'] = 'typed while saving'
    target = tmp_path / 'refused.prism'
    with pytest.raises(PartialSaveConflict, match='changed while'):
        write_partial_snapshot(snapshot, target)
    assert not target.exists()


def test_page_edits_and_a_new_page_are_partial(project, tmp_path):
    session = opened(project)
    original = {row[0]: row for row in rows(project, 'workspace_pages')}
    new_page = {'id': 'd3', 'kind': 'document', 'title': 'third',
                'content': {'text': 'new'}, 'tags': [], 'parent': None, 'permanent': False}
    scene = FakeScene(loaded_items(project),
                      [page('d1', 'rewritten'), page('d2', 'second'), page('map', None), new_page])
    target = tmp_path / 'pages.prism'
    assert write_partial_snapshot(session.capture(scene), target).changed_rows == 2
    saved = {row[0]: row for row in rows(target, 'workspace_pages')}
    assert json.loads(saved['d1'][3])['content'] == 'rewritten'
    assert json.loads(saved['d3'][3])['content'] == {'text': 'new'}
    assert saved['d2'] == original['d2'] and saved['map'] == original['map']


def test_page_order_is_not_rewritten_by_a_page_scope(project, tmp_path):
    with sqlite3.connect(project) as connection:
        connection.execute("UPDATE workspace_pages SET sort_order=? WHERE id=?", (7, 'd1'))
    session = opened(project, pages_complete=False)
    scene = FakeScene(loaded_items(project), [page('d1', 'renamed')])
    target = tmp_path / 'scoped.prism'
    write_partial_snapshot(session.capture(scene), target)
    saved = {row[0]: row for row in rows(target, 'workspace_pages')}
    assert saved['d1'][4] == 7 and json.loads(saved['d1'][3])['content'] == 'renamed'

    pages_only = PartialProjectSession.open(project, item_ids=(1,), page_ids=['d1'],
                                            pages_complete=False)
    alone = FakeScene([FakeItem(save_id=1, data=json.loads(item_rows(project)[1][-1]))], [])
    with pytest.raises(PartialSaveConflict, match='page scope'):
        pages_only.capture(alone)


def test_note_and_mind_map_are_written_without_touching_other_tables(project, tmp_path):
    session = opened(project)
    scene = FakeScene(loaded_items(project), pages_of(project),
                      note='<p>new note</p>', tree={'root': {'text': 'a'}})
    target = tmp_path / 'boards.prism'
    write_partial_snapshot(session.capture(scene), target)
    assert rows(target, 'board_notes')[0][2] == '<p>new note</p>'
    assert json.loads(rows(target, 'board_mindmap')[0][1]) == {'root': {'text': 'a'}}
    assert rows(target, 'workspace_nodes') == rows(project, 'workspace_nodes')
    assert rows(target, 'material_records') == rows(project, 'material_records')
    assert rows(target, 'future_extension') == rows(project, 'future_extension')


def test_clearing_a_saved_mind_map_is_refused(project):
    session = opened(project)
    scene = FakeScene(loaded_items(project), pages_of(project), tree=None)
    with pytest.raises(PartialSaveConflict, match='full save'):
        session.capture(scene)


def test_attachment_edits_are_written_and_removals_are_explicit(project, tmp_path, png):
    attachments = StoredAttachments()
    attachments._meta = {1: ('one.png', 'image/png')}
    attachments._cache = {1: ('one.png', 'image/png', png)}
    attachments._dirty = set()
    session = opened(project, attachments=True)
    scene = FakeScene(loaded_items(project), pages_of(project), attachments=attachments)
    attachments[2] = ('two.png', 'image/png', png)
    del attachments[1]
    snapshot = session.capture(scene)
    assert [entry[0] for entry in snapshot.attachment_writes] == [2]
    assert snapshot.attachment_removals == (1,)
    target = tmp_path / 'attachments.prism'
    receipt = write_partial_snapshot(snapshot, target)
    session.acknowledge(snapshot, receipt, scene=scene, filename=str(target))
    assert [row[0] for row in rows(target, 'attachments')] == [2]
    assert attachments._dirty == set()


def test_acknowledging_keeps_edits_made_during_the_save(project):
    session = opened(project)
    items = loaded_items(project)
    scene = FakeScene(items, pages_of(project))
    items[0].data['notes'] = 'first edit'
    snapshot = session.capture(scene)
    items[0].data['notes'] = 'second edit, typed while saving'
    receipt = write_partial_snapshot(snapshot)
    session.acknowledge(snapshot, receipt, scene=scene)
    assert json.loads(item_rows(project)[1][-1])['notes'] == 'first edit'
    follow_up = session.capture(scene)
    assert len(follow_up.changes) == 1
    assert 'second edit' in follow_up.changes[0].after[-1]


def test_save_as_rebinds_the_session_to_the_new_file(project, tmp_path):
    session = opened(project)
    items = loaded_items(project)
    scene = FakeScene(items, pages_of(project))
    target = tmp_path / 'rebound.prism'
    items[0].data['notes'] = 'first'
    snapshot = session.capture(scene)
    receipt = write_partial_snapshot(snapshot, target)
    session.acknowledge(snapshot, receipt, scene=scene, filename=str(target))
    assert Path(session.source) == Path(target).resolve()

    items[0].data['notes'] = 'second'
    receipt = write_partial_snapshot(session.capture(scene), target)
    assert receipt.changed_rows == 1
    assert json.loads(item_rows(target)[1][-1])['notes'] == 'second'
    assert json.loads(item_rows(project)[1][-1])['notes'] == 'before'
