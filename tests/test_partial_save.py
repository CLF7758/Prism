"""Partially loaded data must not cause missing widgets to delete stored rows."""
import hashlib
import json
from pathlib import Path
import sqlite3
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from prism.fileio.partial_save import (RowPatch, PartialSaveConflict, PartialSaveCancelled,
                                       capture_partial_save, write_partial_snapshot,
                                       read_partial_records)


def rows(path, table):
    with sqlite3.connect(path) as connection:
        return connection.execute(f'SELECT * FROM {table} ORDER BY 1').fetchall()


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


@pytest.fixture
def project(tmp_path):
    source = tmp_path/'partial source #.prism'
    png = (Path(__file__).parent/'assets/test3x3.png').read_bytes()
    with sqlite3.connect(source) as connection:
        connection.executescript('''
            PRAGMA user_version=4;
            CREATE TABLE items(id INTEGER PRIMARY KEY,type TEXT,x,y,z,scale,rotation,flip,data JSON);
            CREATE TABLE sqlar(name TEXT,item_id INTEGER UNIQUE,sz INTEGER,data BLOB);
            CREATE TABLE prism_metadata(key TEXT PRIMARY KEY,value TEXT);
            CREATE TABLE workspace_pages(id TEXT PRIMARY KEY,kind,title,data JSON,sort_order);
            CREATE TABLE material_records(id TEXT PRIMARY KEY,payload JSON);
            CREATE TABLE workspace_nodes(id TEXT PRIMARY KEY,payload JSON);
            CREATE TABLE attachments(id INTEGER PRIMARY KEY,name TEXT,blob BLOB);
            CREATE TABLE future_extension(id INTEGER PRIMARY KEY,original BLOB);
        ''')
        connection.execute('INSERT INTO prism_metadata VALUES(?,?)', ('project_id', 'stable-project'))
        for key in range(1, 13):
            extra = dict(canvasId='A' if key <= 4 else 'B', filename=f'{key}.png',
                         imageWidth=3, imageHeight=3, tags=['keep'], notes='before',
                         future={'nested': ['unknown', key]}, deletedAt='old marker' if key == 1 else None)
            connection.execute('INSERT INTO items VALUES(?,?,?,?,?,?,?,?,?)',
                               (key, 'pixmap', key*10, 0, 0, 1, 0, 1, json.dumps(extra)))
            connection.execute('INSERT INTO sqlar VALUES(?,?,?,?)', (f'{key}.png', key, len(png), png))
        for key, kind in [('d1', 'document'), ('d2', 'document'), ('map', 'mindmap')]:
            connection.execute('INSERT INTO workspace_pages VALUES(?,?,?,?,?)',
                               (key, kind, key, json.dumps(dict(content='saved '+key, tags=['tag'], future='keep')), 0))
        connection.execute('INSERT INTO material_records VALUES(?,?)', ('m1', '{"name":"material","unknown":42}'))
        connection.execute('INSERT INTO workspace_nodes VALUES(?,?)', ('n1', '{"pageId":"d1"}'))
        connection.execute('INSERT INTO attachments VALUES(?,?,?)', (1, 'original.png', png))
        connection.execute('INSERT INTO future_extension VALUES(?,?)', (1, b'future binary\x00\xff'))
    return source


def test_one_loaded_item_keeps_every_unloaded_table_and_source(project, tmp_path):
    preserved = {table: rows(project, table) for table in (
        'sqlar', 'attachments', 'workspace_pages', 'workspace_nodes', 'material_records', 'future_extension')}
    before = rows(project, 'items')
    source_hash = digest(project)
    updates = {'notes': 'edited', 'tags': ['new']}
    snapshot = capture_partial_save(project, [RowPatch('items', 1,
        json_updates=(('data', updates),), json_removals=(('data', ('deletedAt',)),))])
    updates['notes'] = 'later edit must not leak into snapshot'
    target = tmp_path/'saved.prism'
    receipt = write_partial_snapshot(snapshot, target)
    assert receipt.revision == snapshot.revision and receipt.changed_rows == 1
    assert digest(project) == source_hash
    for table, expected in preserved.items():
        assert rows(target, table) == expected
    actual = rows(target, 'items')
    assert actual[1:] == before[1:]
    saved = json.loads(actual[0][-1])
    assert saved['notes'] == 'edited' and saved['tags'] == ['new']
    assert saved['future'] == {'nested': ['unknown', 1]} and 'deletedAt' not in saved


def test_same_file_changes_only_explicit_rows_and_preserves_unknown_columns(project):
    with sqlite3.connect(project) as connection:
        connection.execute('ALTER TABLE items ADD COLUMN future_column TEXT DEFAULT "keep"')
    before = rows(project, 'items')
    snapshot = capture_partial_save(project, [RowPatch('items', 2, fields=(('x', 99),),
                                                       json_updates=(('data', {'notes': 'new'}),))])
    write_partial_snapshot(snapshot)
    after = rows(project, 'items')
    assert after[0] == before[0] and after[2:] == before[2:]
    assert after[1][2] == 99 and after[1][-1] == 'keep'


def test_missing_rows_are_kept_and_only_explicit_page_delete_is_applied(project):
    snapshot = capture_partial_save(project, [RowPatch('workspace_pages', 'd1', delete=True)])
    write_partial_snapshot(snapshot)
    assert [row[0] for row in rows(project, 'workspace_pages')] == ['d2', 'map']
    assert len(rows(project, 'items')) == 12 and len(rows(project, 'sqlar')) == 12
    assert len(rows(project, 'attachments')) == 1


def test_trash_restore_keeps_original_asset_bytes(project):
    source = rows(project, 'sqlar')
    snapshot = capture_partial_save(project, [RowPatch('items', 2,
        json_updates=(('data', {'deletedAt': 'trash-version'}),))])
    write_partial_snapshot(snapshot)
    restore = capture_partial_save(project, [RowPatch('items', 2,
        json_removals=(('data', ('deletedAt',)),))])
    write_partial_snapshot(restore)
    assert rows(project, 'sqlar') == source
    with pytest.raises(ValueError, match='purge'):
        capture_partial_save(project, [RowPatch('items', 2, delete=True)])


def test_conflict_validates_whole_snapshot_before_any_update(project):
    snapshot = capture_partial_save(project, [
        RowPatch('items', 1, fields=(('x', 500),)), RowPatch('items', 2, fields=(('x', 600),))])
    with sqlite3.connect(project) as connection:
        connection.execute('UPDATE items SET x=123 WHERE id=2')
    before = digest(project)
    with pytest.raises(PartialSaveConflict):
        write_partial_snapshot(snapshot)
    assert digest(project) == before


def test_cancel_after_first_write_rolls_back_all_rows(project):
    snapshot = capture_partial_save(project, [
        RowPatch('items', 1, fields=(('x', 500),)), RowPatch('items', 2, fields=(('x', 600),))])
    calls = []
    def cancel():
        calls.append(True)
        return len(calls) == 5
    before = digest(project)
    with pytest.raises(PartialSaveCancelled):
        write_partial_snapshot(snapshot, cancel=cancel)
    assert digest(project) == before


def test_failed_save_as_does_not_replace_existing_destination(project, tmp_path):
    snapshot = capture_partial_save(project, [RowPatch('items', 1, fields=(('x', 500),))])
    target = tmp_path/'existing.prism'
    target.write_bytes(b'previous user file')
    with patch('prism.fileio.partial_save.os.replace', side_effect=PermissionError('locked target')):
        with pytest.raises(PermissionError):
            write_partial_snapshot(snapshot, target)
    assert target.read_bytes() == b'previous user file'
    assert list(tmp_path.glob('existing.prism.*.partial')) == []


def test_the_writer_says_whether_a_discarded_write_was_a_cancel(project, tmp_path):
    """``not_started`` is the disk fact; "cancelled" is a second fact.

    A write that failed before the target changed leaves nothing on disk, so
    the writer publishes ``not_started``.  That is not the same as saying the
    user cancelled it, and the save adapter turns the two into different
    terminal states: a cancel is quiet, a failure is reported with its
    reason.
    """
    snapshot = capture_partial_save(project, [RowPatch('items', 1, fields=(('x', 500),))])
    target = tmp_path/'existing.prism'
    target.write_bytes(b'previous user file')
    reports = []

    def report(state, reason='', **extra):
        reports.append((state, reason, extra))

    with patch('prism.fileio.partial_save.os.replace', side_effect=PermissionError('locked target')):
        with pytest.raises(PermissionError):
            write_partial_snapshot(snapshot, target, commit_report=report)
    assert reports, 'the writer did not report the discarded write'
    state, _reason, extra = reports[0]
    assert state == 'not_started'
    assert not extra.get('cancelled'), 'a failed write was reported as a cancel'

    reports.clear()
    calls = []

    def cancel():
        calls.append(True)
        return len(calls) == 5

    with pytest.raises(PartialSaveCancelled):
        write_partial_snapshot(snapshot, target, cancel=cancel, commit_report=report)
    assert reports, 'the writer did not report the cancelled write'
    state, _reason, extra = reports[0]
    assert state == 'not_started'
    assert extra.get('cancelled') is True, 'a cancel was not reported as a cancel'


def test_project_replaced_or_identity_changed_is_rejected(project):
    snapshot = capture_partial_save(project, [RowPatch('items', 1, fields=(('x', 500),))])
    with sqlite3.connect(project) as connection:
        connection.execute("UPDATE prism_metadata SET value='another-project' WHERE key='project_id'")
    with pytest.raises(PartialSaveConflict):
        write_partial_snapshot(snapshot)


def test_writer_routes_explicit_snapshot_without_reading_live_scene(project, tmp_path):
    from prism.fileio import save_prism
    snapshot = capture_partial_save(project, [RowPatch('items', 1, fields=(('x', 500),))])
    target = tmp_path/'through-public-api.prism'
    receipt = save_prism(str(target), None, create_new=True, partial_snapshot=snapshot)
    assert receipt.revision == snapshot.revision and receipt.filename == str(target)
    assert rows(target, 'items')[0][2] == 500
    assert len(rows(target, 'items')) == 12


def test_legacy_writer_refuses_partial_scene_before_touching_target(project):
    from prism.fileio.sql import SQLiteIO
    from prism.fileio.errors import PrismFileIOError
    before = digest(project)
    scene = SimpleNamespace(_partial_project_source=str(project))
    with pytest.raises(PrismFileIOError, match='部分保存快照'):
        SQLiteIO(str(project), scene, create_new=True).write()
    assert digest(project) == before


def test_noop_does_not_rewrite_and_invalid_patches_fail_closed(project):
    snapshot = capture_partial_save(project, [RowPatch('items', 2, fields=(('x', 20),))])
    assert snapshot.changes == ()
    before = digest(project)
    assert write_partial_snapshot(snapshot).changed_rows == 0
    assert digest(project) == before
    for bad in [RowPatch('attachments', 1), RowPatch('items', 1, fields=(('data', '{}'),)),
                RowPatch('prism_metadata', 'project_id', fields=(('value', 'bad'),))]:
        with pytest.raises(ValueError):
            capture_partial_save(project, [bad])


def test_loaded_baseline_detects_external_edit_before_save_capture(project):
    baseline = read_partial_records(project, [('items', 1), ('items', 2)])
    with sqlite3.connect(project) as connection:
        connection.execute('UPDATE items SET x=321 WHERE id=1')
    before = digest(project)
    with pytest.raises(PartialSaveConflict, match='after page was loaded'):
        capture_partial_save(project, [RowPatch('items', 1, fields=(('x', 500),))], baseline=baseline)
    assert digest(project) == before


def test_partial_gui_requires_matching_baseline_and_receipt_is_propagated(project, tmp_path):
    from prism.fileio import save_prism
    from prism.fileio.errors import PrismFileIOError
    from unittest.mock import Mock
    scene = SimpleNamespace(_partial_project_source=str(project))
    raw = capture_partial_save(project, [RowPatch('items', 1, fields=(('x', 500),))])
    with pytest.raises(PrismFileIOError, match='loaded project baseline'):
        save_prism(str(tmp_path/'unsafe.prism'), scene, create_new=True, partial_snapshot=raw)
    baseline = read_partial_records(project, [('items', 1)])
    snapshot = capture_partial_save(project, [RowPatch('items', 1, fields=(('x', 500),))], baseline=baseline)
    worker = Mock(canceled=False)
    target = tmp_path/'baseline-save.prism'
    receipt = save_prism(str(target), scene, create_new=True, partial_snapshot=snapshot, worker=worker)
    assert worker.partial_save_receipt is receipt and receipt.revision == snapshot.revision
    worker.finished.emit.assert_called_once_with(str(target), [])


def test_nested_json_merge_and_explicit_path_removal_keep_unknown_fields(project):
    with sqlite3.connect(project) as connection:
        extra = json.loads(connection.execute('SELECT data FROM items WHERE id=1').fetchone()[0])
        extra['preview'] = {'properties': {'roughness': .2, 'future': 42, 'removeMe': 'old'},
                            'unknownPreview': ['keep']}
        connection.execute('UPDATE items SET data=? WHERE id=1', (json.dumps(extra),))
    snapshot = capture_partial_save(project, [RowPatch('items', 1,
        json_updates=(('data', {'preview': {'properties': {'roughness': .5}}}),),
        json_removals=(('data', (('preview', 'properties', 'removeMe'),)),))])
    write_partial_snapshot(snapshot)
    actual = json.loads(rows(project, 'items')[0][-1])['preview']
    assert actual == {'properties': {'roughness': .5, 'future': 42}, 'unknownPreview': ['keep']}


def test_merge_and_removal_never_mutate_callers_patch(project):
    updates = {'new': {'keep': ['input'], 'removeMe': 1}}
    snapshot = capture_partial_save(project, [RowPatch('items', 1,
        json_updates=(('data', updates),),
        json_removals=(('data', (('new', 'removeMe'),)),))])
    assert updates == {'new': {'keep': ['input'], 'removeMe': 1}}
    updates['new']['keep'].append('later')
    write_partial_snapshot(snapshot)
    assert json.loads(rows(project, 'items')[0][-1])['new'] == {'keep': ['input']}


def test_save_as_target_changed_during_preparation_is_not_overwritten(project, tmp_path):
    target = tmp_path/'target.prism'
    target.write_bytes(b'previous target')
    snapshot = capture_partial_save(project, [RowPatch('items', 1, fields=(('x', 500),))])
    checks = []
    def cancel():
        checks.append(True)
        if len(checks) == 2:
            target.write_bytes(b'concurrent target edit')
        return False
    with pytest.raises(PartialSaveConflict, match='target changed'):
        write_partial_snapshot(snapshot, target, cancel=cancel)
    assert target.read_bytes() == b'concurrent target edit'


def test_source_changed_during_backup_is_not_published(project, tmp_path):
    target = tmp_path/'target.prism'
    target.write_bytes(b'previous target')
    snapshot = capture_partial_save(project, [RowPatch('items', 1, fields=(('x', 500),))])
    checks = []
    def cancel():
        checks.append(True)
        if len(checks) == 2:
            with sqlite3.connect(project) as connection:
                connection.execute("UPDATE material_records SET payload='{}' WHERE id='m1'")
        return False
    with pytest.raises(PartialSaveConflict, match='Source changed'):
        write_partial_snapshot(snapshot, target, cancel=cancel)
    assert target.read_bytes() == b'previous target'
