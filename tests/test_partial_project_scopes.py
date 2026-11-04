"""Cross-page command boundaries and complete metadata domains in real saves."""
import copy
import json
import sqlite3

import pytest

from prism.fileio.partial_save import PartialSaveConflict, write_partial_snapshot
from prism.partial_project import PartialProjectSession
from tests.test_partial_project import (FakeScene, item_rows, loaded_items,
    pages_of, png, project, rows)
from tests.test_partial_domains import material, node


def canvases(project):
    with sqlite3.connect(project) as db:
        for key in ('A', 'B'):
            db.execute('INSERT INTO workspace_pages VALUES(?,?,?,?,?)',
                       (key, 'canvas', key, '{}', 5))


def scoped(project):
    canvases(project)
    session = PartialProjectSession.open(project, item_ids=(1, 2, 3, 4),
        page_ids=('A',), include_boards=True)
    scene = FakeScene(loaded_items(project), [{'id': 'A', 'kind': 'canvas', 'title': 'A'}])
    return session, scene


@pytest.mark.parametrize('target', ['B', 'missing', 'd1', '__unassigned__'])
def test_raw_canvas_change_never_becomes_a_silent_move(project, target):
    session, scene = scoped(project)
    scene._items[0].data['canvasId'] = target
    with pytest.raises(PartialSaveConflict, match='explicit move'):
        session.capture(scene)
    assert json.loads(item_rows(project)[1][-1])['canvasId'] == 'A'


def test_explicit_move_to_unloaded_canvas_roundtrips_and_undo_survives_ack(project):
    session, scene = scoped(project)
    item = scene._items[0]
    originals = rows(project, 'sqlar')
    session.authorize_canvas_move([item], 'B', scene)
    item.data['canvasId'] = 'B'
    snapshot = session.capture(scene)
    assert snapshot.read_guards[0][1] == 'B'
    receipt = write_partial_snapshot(snapshot)
    session.acknowledge(snapshot, receipt, scene=scene)
    assert json.loads(item_rows(project)[1][-1])['canvasId'] == 'B'
    # The command's undo target was admitted before its first application.
    item.data['canvasId'] = 'A'
    snapshot = session.capture(scene)
    receipt = write_partial_snapshot(snapshot)
    session.acknowledge(snapshot, receipt, scene=scene)
    assert json.loads(item_rows(project)[1][-1])['canvasId'] == 'A'
    assert rows(project, 'sqlar') == originals


@pytest.mark.parametrize('target', ['missing', 'd1', '__unassigned__', None])
def test_invalid_destination_is_refused_before_any_move(project, target):
    session, scene = scoped(project)
    with pytest.raises(PartialSaveConflict):
        session.authorize_canvas_move(scene._items, target, scene)
    assert all(item.data['canvasId'] == 'A' for item in scene._items)


def test_target_deleted_after_capture_aborts_transaction(project):
    session, scene = scoped(project)
    item = scene._items[0]
    session.authorize_canvas_move([item], 'B', scene)
    item.data['canvasId'] = 'B'
    snapshot = session.capture(scene)
    with sqlite3.connect(project) as db:
        db.execute('DELETE FROM workspace_pages WHERE id="B"')
    with pytest.raises(PartialSaveConflict, match='dependency changed'):
        write_partial_snapshot(snapshot)
    assert json.loads(item_rows(project)[1][-1])['canvasId'] == 'A'


def test_second_move_during_save_is_still_dirty(project):
    session, scene = scoped(project)
    item = scene._items[0]
    session.authorize_canvas_move([item], 'B', scene)
    item.data['canvasId'] = 'B'
    snapshot = session.capture(scene)
    session.authorize_canvas_move([item], 'default-canvas', scene)
    item.data['canvasId'] = 'default-canvas'
    receipt = write_partial_snapshot(snapshot)
    session.acknowledge(snapshot, receipt, scene=scene)
    follow_up = session.capture(scene)
    assert any(change.table == 'items' and change.key == 1 for change in follow_up.changes)
    write_partial_snapshot(follow_up)
    assert json.loads(item_rows(project)[1][-1])['canvasId'] == 'default-canvas'


def test_materialized_proxy_move_is_authorized_for_its_logical_record(project):
    from types import SimpleNamespace
    session, scene = scoped(project)
    record = scene._items[0]
    proxy = SimpleNamespace(_asset_record=record)
    session.authorize_canvas_move([proxy], 'B', scene)
    record.data['canvasId'] = 'B'
    write_partial_snapshot(session.capture(scene))
    assert json.loads(item_rows(project)[1][-1])['canvasId'] == 'B'


def domains(project):
    obj, folder = material('m1'), node('n1')
    for entry in (obj, folder):
        entry['projectId'] = 'stable-project'
    obj['folderId'] = 'n1'
    obj['future'] = {'keep': 'unknown'}
    with sqlite3.connect(project) as db:
        db.execute('UPDATE material_records SET payload=?', (json.dumps(obj),))
        db.execute('UPDATE workspace_nodes SET payload=?', (json.dumps(folder),))
    session = PartialProjectSession.open(project, item_ids=(1, 2, 3, 4),
        page_ids=[page['id'] for page in pages_of(project)], pages_complete=True,
        include_domains=True)
    scene = FakeScene(loaded_items(project), pages_of(project))
    scene.material_records, scene.workspace_nodes = [copy.deepcopy(obj)], [copy.deepcopy(folder)]
    scene.material_library_state = {}
    return session, scene


def test_domain_edit_insert_and_save_as_advance_only_saved_values(project, tmp_path):
    session, scene = domains(project)
    scene.material_records[0]['notes'] = 'first'
    scene.workspace_nodes[0]['title'] = 'renamed folder'
    scene.material_library_state = {'folderId': 'n1', 'viewMode': 'list'}
    snapshot = session.capture(scene)
    scene.material_records[0]['notes'] = 'typed during save'
    target = tmp_path / 'domain-copy.prism'
    receipt = write_partial_snapshot(snapshot, target)
    session.acknowledge(snapshot, receipt, scene=scene, filename=str(target))
    saved = json.loads(rows(target, 'material_records')[0][1])
    assert saved['notes'] == 'first' and saved['future'] == {'keep': 'unknown'}
    assert json.loads(rows(target, 'workspace_nodes')[0][1])['title'] == 'renamed folder'
    assert any(c.table == 'material_records' for c in session.capture(scene).changes)
    new = material('new')
    new['projectId'] = 'stable-project'
    scene.material_records.append(new)
    snapshot = session.capture(scene)
    receipt = write_partial_snapshot(snapshot)
    session.acknowledge(snapshot, receipt, scene=scene)
    assert len(rows(target, 'material_records')) == 2
    assert session.capture(scene).changes == ()
    assert json.loads(rows(project, 'material_records')[0][1])['notes'] == ''


def test_complete_domain_scope_rejects_disappearance_but_saves_soft_delete(project):
    session, scene = domains(project)
    record = scene.material_records.pop()
    with pytest.raises(PartialSaveConflict, match='disappeared'):
        session.capture(scene)
    record['deletedAt'] = '2026-10-21'
    scene.material_records.append(record)
    write_partial_snapshot(session.capture(scene))
    assert json.loads(rows(project, 'material_records')[0][1])['deletedAt'] == '2026-10-21'


def test_domain_conflict_rolls_back_other_item_edits(project):
    session, scene = domains(project)
    scene._items[0].data['notes'] = 'must not leak'
    scene.material_records[0]['notes'] = 'ours'
    snapshot = session.capture(scene)
    with sqlite3.connect(project) as db:
        obj = json.loads(db.execute('SELECT payload FROM material_records').fetchone()[0])
        obj['notes'] = 'theirs'
        db.execute('UPDATE material_records SET payload=?', (json.dumps(obj),))
    with pytest.raises(PartialSaveConflict, match='Record changed'):
        write_partial_snapshot(snapshot)
    assert json.loads(item_rows(project)[1][-1])['notes'] == 'before'


def test_domain_parent_removed_after_capture_aborts_material_save(project):
    session, scene = domains(project)
    scene.material_records[0]['notes'] = 'ours'
    snapshot = session.capture(scene)
    with sqlite3.connect(project) as db:
        db.execute('DELETE FROM workspace_nodes WHERE id="n1"')
    with pytest.raises(PartialSaveConflict, match='membership changed'):
        write_partial_snapshot(snapshot)
    assert json.loads(rows(project, 'material_records')[0][1])['notes'] == ''


def test_concurrent_domain_insert_is_not_ignored(project):
    session, scene = domains(project)
    scene.workspace_nodes[0]['title'] = 'ours'
    snapshot = session.capture(scene)
    with sqlite3.connect(project) as db:
        extra = node('external')
        extra['projectId'] = 'stable-project'
        db.execute('INSERT INTO workspace_nodes VALUES(?,?)', ('external', json.dumps(extra)))
    with pytest.raises(PartialSaveConflict, match='membership changed'):
        write_partial_snapshot(snapshot)


def test_material_state_keeps_unloaded_views_and_future_fields(project):
    original = {'views': {'unloaded': {'future': 1}, 'root': {'future': 2, 'search': 'old'}}}
    with sqlite3.connect(project) as db:
        db.execute('INSERT INTO prism_metadata VALUES(?,?)',
                   ('material_library_state', json.dumps(original)))
    session, scene = domains(project)
    scene.material_library_state = {'views': {'root': {'search': ''}}}
    write_partial_snapshot(session.capture(scene))
    stored = dict(rows(project, 'prism_metadata'))
    state = json.loads(stored['material_library_state'])
    assert state['views']['unloaded'] == {'future': 1}
    assert state['views']['root'] == {'future': 2, 'search': ''}


def test_material_preview_and_new_original_are_saved_with_metadata(project, png):
    from prism.fileio.attachments_store import StoredAttachments, sqlite_attachment_loader
    session, scene = domains(project)
    scene.attachments = StoredAttachments(sqlite_attachment_loader(project))
    scene.attachments._meta[1] = ('one.png', 'image/png')
    scene.attachments[2] = ('preview.png', 'image/png', png)
    scene.material_records[0]['preview']['thumbnailAttachmentId'] = 2
    snapshot = session.capture(scene)
    assert snapshot.attachment_writes and snapshot.required_attachments == (2,)
    receipt = write_partial_snapshot(snapshot)
    session.acknowledge(snapshot, receipt, scene=scene)
    assert rows(project, 'attachments')[-1][4] == png
    assert json.loads(rows(project, 'material_records')[0][1])['preview']['thumbnailAttachmentId'] == 2


def test_new_material_attachment_reference_without_bytes_is_refused(project):
    from prism.fileio.attachments_store import StoredAttachments, sqlite_attachment_loader
    session, scene = domains(project)
    scene.attachments = StoredAttachments(sqlite_attachment_loader(project))
    scene.attachments._meta[1] = ('one.png', 'image/png')
    scene.material_records[0]['preview']['thumbnailAttachmentId'] = 999
    with pytest.raises(PartialSaveConflict, match='attachment is missing'):
        session.capture(scene)


def test_move_into_trashed_canvas_folder_is_refused(project):
    session, scene = scoped(project)
    scene.workspace_nodes = [
        {'id': 'folder', 'deletedAt': 'now'},
        {'id': 'page', 'pageId': 'B', 'parentId': 'folder'}]
    with pytest.raises(PartialSaveConflict, match='recycle bin'):
        session.authorize_canvas_move(scene._items, 'B', scene)


def test_bulk_material_metadata_is_not_limited_to_1024_changes(project):
    session, scene = domains(project)
    for index in range(1200):
        record = material(f'bulk-{index}')
        record['projectId'] = 'stable-project'
        scene.material_records.append(record)
    snapshot = session.capture(scene)
    assert len(snapshot.changes) == 1200
    receipt = write_partial_snapshot(snapshot)
    session.acknowledge(snapshot, receipt, scene=scene)
    assert len(rows(project, 'material_records')) == 1201
    assert session.capture(scene).changes == ()


def test_attachment_removed_after_capture_rolls_back_new_binding(project):
    from prism.fileio.attachments_store import StoredAttachments, sqlite_attachment_loader
    session, scene = domains(project)
    scene.attachments = StoredAttachments(sqlite_attachment_loader(project))
    scene.attachments._meta[1] = ('one.png', 'image/png')
    scene.material_records[0]['preview']['thumbnailAttachmentId'] = 1
    snapshot = session.capture(scene)
    with sqlite3.connect(project) as db:
        db.execute('DELETE FROM attachments WHERE id=1')
    with pytest.raises(PartialSaveConflict, match='attachment is missing'):
        write_partial_snapshot(snapshot)
    assert 'thumbnailAttachmentId' not in json.loads(rows(project, 'material_records')[0][1])['preview']


def test_old_valid_reference_deleted_during_notes_save_is_not_ignored(project):
    from prism.fileio.attachments_store import StoredAttachments, sqlite_attachment_loader
    _session, _scene = domains(project)
    with sqlite3.connect(project) as db:
        record = json.loads(db.execute('SELECT payload FROM material_records').fetchone()[0])
        record['preview']['thumbnailAttachmentId'] = 1
        db.execute('UPDATE material_records SET payload=?', (json.dumps(record),))
    session = PartialProjectSession.open(project, item_ids=(1, 2, 3, 4),
        page_ids=[p['id'] for p in pages_of(project)], pages_complete=True, include_domains=True)
    scene = _scene
    scene.material_records = [record]
    scene.attachments = StoredAttachments(sqlite_attachment_loader(project))
    scene.attachments._meta[1] = ('one.png', 'image/png')
    record['notes'] = 'notes-only edit'
    snapshot = session.capture(scene)
    assert 1 in snapshot.required_attachments
    with sqlite3.connect(project) as db:
        db.execute('UPDATE attachments SET blob=NULL WHERE id=1')
    with pytest.raises(PartialSaveConflict, match='attachment is missing'):
        write_partial_snapshot(snapshot)
    assert json.loads(rows(project, 'material_records')[0][1])['notes'] == ''
