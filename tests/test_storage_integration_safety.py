"""Integration gates: the real legacy writer and scoped capture, not mocks."""
import hashlib
import json
import sqlite3
from types import SimpleNamespace

import pytest

from tests.test_partial_project import (project, png, FakeScene, loaded_items,
                                 opened, pages_of, rows)
from prism.fileio.partial_save import PartialSaveConflict, write_partial_snapshot
from prism.storage.contracts import ProjectIdentity, receipt_of


def metadata(path):
    return {key: value for key, value in rows(path, 'prism_metadata')}


@pytest.mark.parametrize('changed', [None, (), ('items/1',)])
def test_commit_completion_is_independent_of_change_inventory(changed):
    outcome = SimpleNamespace(operation_id='save-1', commit_state='complete',
                              ack_state='failed', terminal='committed_unconfirmed',
                              changed_entities=changed, changed_entities_complete=False)
    receipt = receipt_of(outcome, identity=ProjectIdentity('p', 's', 'o'))
    assert receipt.complete and receipt.is_durable
    assert receipt.changed_entities == changed
    assert receipt.changed_entities_complete is False
    assert receipt.as_dict()['changed_entities'] == (None if changed is None else list(changed))


def test_all_global_metadata_survives_partial_save_and_next_edit(project, tmp_path):
    initial = dict(tags=['old'], categories=['c'], library_queries=[],
                   tag_system={'siblings': {}, 'parents': {}, 'future': {'keep': 1}})
    with sqlite3.connect(project) as conn:
        conn.executemany('INSERT INTO prism_metadata VALUES(?,?)',
                         [(key, json.dumps(value)) for key, value in initial.items()])
        conn.execute('INSERT INTO prism_metadata VALUES(?,?)', ('unknown', 'opaque'))
    before = {table: rows(project, table) for table in
              ('items', 'sqlar', 'workspace_pages', 'attachments', 'future_extension')}
    session = opened(project)
    scene = FakeScene(loaded_items(project), pages_of(project))
    scene.tag_names = ['old', 'new']
    scene.category_names = ['c', 'new-category']
    scene.library_queries = [{'id': 'q', 'expression': 'new'}]
    scene.tag_system = {'siblings': {'alias': 'new'}, 'parents': {}}
    snapshot = session.capture(scene)
    assert {c.key for c in snapshot.changes if c.table == 'prism_metadata'} == set(initial)
    # Newer UI edits must not be acknowledged away by the old snapshot.
    scene.tag_names.append('during-write')
    target = tmp_path / 'saved.prism'
    receipt = write_partial_snapshot(snapshot, target)
    session.acknowledge(snapshot, receipt, scene=scene, filename=target)
    saved = metadata(target)
    assert json.loads(saved['tags']) == ['old', 'new']
    assert json.loads(saved['categories']) == scene.category_names
    assert json.loads(saved['library_queries']) == scene.library_queries
    assert json.loads(saved['tag_system'])['future'] == {'keep': 1}
    assert saved['unknown'] == 'opaque'
    for table, expected in before.items():
        assert rows(target, table) == expected
    next_snapshot = session.capture(scene)
    assert [(c.table, c.key) for c in next_snapshot.changes] == [('prism_metadata', 'tags')]
    next_receipt = write_partial_snapshot(next_snapshot, target)
    session.acknowledge(next_snapshot, next_receipt, scene=scene)
    assert session.capture(scene).changes == ()


def test_global_metadata_external_conflict_is_not_overwritten(project, tmp_path):
    with sqlite3.connect(project) as conn:
        conn.execute('INSERT INTO prism_metadata VALUES(?,?)', ('tags', '["old"]'))
    session = opened(project)
    scene = FakeScene(loaded_items(project), pages_of(project))
    scene.tag_names = ['edited']
    snapshot = session.capture(scene)
    with sqlite3.connect(project) as conn:
        conn.execute('UPDATE prism_metadata SET value=? WHERE key=?', ('["external"]', 'tags'))
    target = tmp_path / 'conflicted.prism'
    with pytest.raises(PartialSaveConflict):
        write_partial_snapshot(snapshot, target)
    assert not target.exists()
    assert metadata(project)['tags'] == '["external"]'


@pytest.mark.parametrize('version,appid', [(99999, 0), (4, 123456), (0, 0)])
def test_formal_writer_refuses_future_and_foreign_database_without_backup(tmp_path, version, appid):
    from prism.fileio.sql import SQLiteIO
    target = tmp_path / 'foreign.prism'
    with sqlite3.connect(target) as conn:
        conn.execute(f'PRAGMA user_version={version}')
        conn.execute(f'PRAGMA application_id={appid}')
        conn.execute('CREATE TABLE evidence(value BLOB)')
        conn.execute('INSERT INTO evidence VALUES(?)', (b'keep\x00\xff',))
    digest = hashlib.sha256(target.read_bytes()).digest()
    writer = SQLiteIO(str(target), SimpleNamespace())
    with pytest.raises(sqlite3.OperationalError):
        writer._establish_connection()
    assert hashlib.sha256(target.read_bytes()).digest() == digest
    assert not target.with_suffix('.prism.bak').exists()


@pytest.mark.parametrize('commit,expected', [('unknown', 'unknown'), ('incomplete', 'incomplete')])
def test_cancel_terminal_does_not_invent_rollback(commit, expected):
    outcome = SimpleNamespace(operation_id='save-unknown', commit_state=commit,
                              ack_state='unknown', terminal='cancelled')
    receipt = receipt_of(outcome, identity=ProjectIdentity('p', 's', 'o'))
    assert receipt.durable_state.value == expected
    assert not receipt.complete and not receipt.is_durable
    assert receipt.changed_after_capture is None


def test_router_binds_selected_path_and_open_epoch(project, tmp_path):
    from prism.storage.project_backend import BackendRouter
    from prism.storage.legacy_backend import LegacyBackend
    second = tmp_path / 'other.prism'
    second.write_bytes(project.read_bytes())
    with sqlite3.connect(second) as conn:
        conn.execute("DELETE FROM items WHERE id>2")
        conn.execute("UPDATE prism_metadata SET value='other-project' WHERE key='project_id'")
    router = BackendRouter([LegacyBackend(str(project))])
    opened_second = router.open(str(second))
    assert opened_second.query() == (1, 2)
    assert opened_second.identity().project_id == 'other-project'
    assert router.open(str(second)).identity().open_epoch != opened_second.identity().open_epoch
    with pytest.raises(ValueError, match='Unsupported'):
        opened_second.query(tag='silently-ignored-before')
    assert opened_second.query(limit=0) == ()


def test_backend_reads_original_and_checks_source_revision(project, png):
    from prism.storage.legacy_backend import LegacyBackend
    from prism.storage.contracts import SourceRef, SourceKind, StorageError
    backend = LegacyBackend(str(project))
    ref = SourceRef(SourceKind.LEGACY_ARCHIVE, '1', media_role='image',
                    byte_length=len(png), content_revision=hashlib.sha256(png).hexdigest())
    assert backend.read_source(ref) == png
    with pytest.raises(StorageError, match='revision'):
        backend.read_source(SourceRef(SourceKind.LEGACY_ARCHIVE, '1', media_role='image',
                                      content_revision='0'*64))


def test_backend_commit_uses_real_partial_writer_without_acknowledging_scene(project, tmp_path):
    from prism.storage.legacy_backend import LegacyBackend
    session = opened(project)
    scene = FakeScene(loaded_items(project), pages_of(project))
    scene._items[0].data['notes'] = 'saved'
    snapshot = session.capture(scene)
    target = tmp_path / 'backend-save.prism'
    receipt = LegacyBackend(str(project), writable=True).commit(
        snapshot=snapshot, operation_id='backend-save', target=target)
    assert receipt.complete and receipt.is_durable
    assert receipt.ack_state.value == 'not_applicable'
    assert receipt.durable_state.value == 'committed_unconfirmed'
    assert session.acknowledged is None
    assert json.loads(dict((r[0], r[-1]) for r in rows(target, 'items'))[1])['notes'] == 'saved'


def test_partial_writer_also_refuses_future_version(project, tmp_path):
    session = opened(project)
    scene = FakeScene(loaded_items(project), pages_of(project))
    scene._items[0].data['notes'] = 'edit'
    snapshot = session.capture(scene)
    from dataclasses import replace
    with sqlite3.connect(project) as conn:
        conn.execute('PRAGMA user_version=99999')
    snapshot = replace(snapshot, schema_version=99999)
    before = hashlib.sha256(project.read_bytes()).digest()
    target = tmp_path / 'future-save.prism'
    with pytest.raises(PartialSaveConflict, match='Unsupported'):
        write_partial_snapshot(snapshot, target)
    assert not target.exists()
    assert hashlib.sha256(project.read_bytes()).digest() == before


def test_legacy_revision_does_not_hash_whole_archive(project, monkeypatch):
    import builtins
    from pathlib import Path
    from prism.storage.legacy_backend import LegacyBackend
    original_open = builtins.open
    reads = []
    class GuardedReader:
        def __init__(self, handle): self.handle = handle
        def __enter__(self): return self
        def __exit__(self, *args): self.handle.close()
        def read(self, count=-1):
            reads.append(count)
            assert 0 <= count <= 100, 'revision must never scan original bytes'
            return self.handle.read(count)
    def guarded_open(path, *args, **kwargs):
        handle = original_open(path, *args, **kwargs)
        return GuardedReader(handle) if Path(path) == project else handle
    monkeypatch.setattr(builtins, 'open', guarded_open)
    assert len(LegacyBackend(str(project)).source_revision()) == 64
    assert reads == [100]


@pytest.mark.parametrize('raw,state', [
    (None, 'not_loaded'), ('null', 'empty_content'), ('""', 'empty_content'),
    ('{"__prism_page_payload__":true}', 'not_loaded'),
    ('{"__prism_page_payload__":true,"content":null}', 'empty_content')])
def test_page_state_and_raw_bytes_come_from_one_read(project, monkeypatch, raw, state):
    from prism.fileio import sql
    from prism.storage.source_ref import resolve_legacy_page
    with sqlite3.connect(project) as conn:
        conn.execute('UPDATE workspace_pages SET data=? WHERE id=?', (raw, 'd1'))
    queries = []
    original = sql._read_only_connection
    def connection(source):
        conn = original(source)
        conn.set_trace_callback(queries.append)
        return conn
    monkeypatch.setattr(sql, '_read_only_connection', connection)
    result = resolve_legacy_page(project, 'd1')
    assert result.state.value == state
    assert len([query for query in queries if query.lstrip().upper().startswith('SELECT')]) == 1
