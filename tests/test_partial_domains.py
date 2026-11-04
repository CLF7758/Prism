import copy
import json
import sqlite3

import pytest

from prism.partial_domains import DomainTracker, read_domain_baseline
from prism.fileio.partial_save import (PartialSaveCancelled, PartialSaveConflict,
    capture_partial_save, write_partial_snapshot)


def material(key='m'):
    return dict(id=key, projectId='p', schemaVersion=1, name='Wood', notes='',
        folderId=None, tags=[], textures={}, preview={}, nativePackages=[],
        status='draft', createdAt='now', updatedAt='now', deletedAt=None)


def node(key='folder'):
    return dict(id=key, projectId='p', section='material', nodeType='folder',
        title='Folder', parentId=None, order=0, deletedAt=None)


@pytest.fixture
def source(tmp_path):
    path = tmp_path / 'domains.prism'
    with sqlite3.connect(path) as db:
        db.executescript('CREATE TABLE prism_metadata(key TEXT PRIMARY KEY,value);'
            'CREATE TABLE material_records(id TEXT PRIMARY KEY,payload);'
            'CREATE TABLE workspace_nodes(id TEXT PRIMARY KEY,payload);')
        # This fixture is a supported Prism schema, not an unversioned SQLite
        # database. Keep the production format guard intact.
        from prism.fileio.schema import USER_VERSION
        db.execute(f'PRAGMA user_version={USER_VERSION}')
        db.execute('INSERT INTO prism_metadata VALUES(?,?)', ('project_id', 'p'))
        obj = material()
        obj['future'] = {'nested': 42}
        db.execute('INSERT INTO material_records VALUES(?,?)', ('m', json.dumps(obj)))
        db.execute('INSERT INTO workspace_nodes VALUES(?,?)', ('folder', json.dumps(node())))
    return path


def capture(source, tracker, materials, nodes, **kwargs):
    diff = tracker.capture(materials, nodes, **kwargs)
    return capture_partial_save(source, diff.patches, inserts=diff.inserts, baseline=diff.baseline)


def stored(source):
    with sqlite3.connect(source) as db:
        return json.loads(db.execute('SELECT payload FROM material_records WHERE id="m"').fetchone()[0])


def test_unknown_metadata_survives_missing_loader_field(source):
    tracker = DomainTracker(read_domain_baseline(source, batch_size=1))
    current = material()
    current['notes'] = 'edit'
    snapshot = capture(source, tracker, [current], [node()])
    write_partial_snapshot(snapshot)
    assert stored(source)['future'] == {'nested': 42}
    assert stored(source)['notes'] == 'edit'


def test_known_slot_and_adjustment_removal_preserves_unknown(source):
    original = material()
    original['textures'] = {'normal': {'channel': 'RGB'}, 'futureSlot': {'x': 1}}
    original['preview'] = {'properties': {'slots': {'normal': {'value': 2}},
        'flipX': True, 'future': 5}, 'discardedAdjustments': True, 'future': 6}
    with sqlite3.connect(source) as db:
        db.execute('UPDATE material_records SET payload=?', (json.dumps(original),))
    tracker = DomainTracker(read_domain_baseline(source))
    current = material()
    current['preview'] = {'properties': {}}
    snapshot = capture(source, tracker, [current], [node()])
    write_partial_snapshot(snapshot)
    after = stored(source)
    assert after['textures'] == {'futureSlot': {'x': 1}}
    assert after['preview'] == {'properties': {'future': 5}, 'future': 6}


def test_missing_row_requires_explicit_delete(source):
    tracker = DomainTracker(read_domain_baseline(source))
    with pytest.raises(PartialSaveConflict, match='disappeared'):
        tracker.capture([], [node()])
    snapshot = capture(source, tracker, [], [node()],
        explicit_deletions={('material_records', 'm')})
    write_partial_snapshot(snapshot)
    tracker.acknowledge(snapshot)
    assert not tracker.capture([], [node()]).patches


def test_insert_and_edits_during_save_stay_dirty(source):
    tracker = DomainTracker(read_domain_baseline(source))
    new = material('new')
    current = material()
    current['notes'] = 'first'
    snapshot = capture(source, tracker, [current, new], [node()])
    current['notes'] = 'second'
    new['notes'] = 'new edit'
    write_partial_snapshot(snapshot)
    tracker.acknowledge(snapshot)
    following = tracker.capture([current, new], [node()])
    assert not following.inserts
    assert {p.key for p in following.patches} == {'m', 'new'}
    assert stored(source)['notes'] == 'first'


def test_source_conflict_detected_by_writer_capture(source):
    tracker = DomainTracker(read_domain_baseline(source))
    current = material()
    current['notes'] = 'mine'
    with sqlite3.connect(source) as db:
        other = material()
        other['notes'] = 'theirs'
        db.execute('UPDATE material_records SET payload=? WHERE id="m"', (json.dumps(other),))
    with pytest.raises(PartialSaveConflict, match='changed'):
        capture(source, tracker, [current], [node()])


def test_capture_freezes_changed_payload_without_mutating_input(source):
    tracker = DomainTracker(read_domain_baseline(source))
    current = material()
    current['tags'] = ['a']
    before = copy.deepcopy(current)
    changes = tracker.capture([current], [node()])
    assert before == current
    current['tags'].append('b')
    assert changes.patches[0].json_updates[0][1]['tags'] == ['a']


@pytest.mark.parametrize('failure', ['cycle', 'duplicate', 'wrong_project', 'bad_folder'])
def test_domain_validation_matches_services(source, failure):
    tracker = DomainTracker(read_domain_baseline(source))
    current, nodes = material(), [node()]
    if failure == 'cycle':
        nodes[0]['parentId'] = 'folder'
    elif failure == 'duplicate':
        nodes.append(node())
    elif failure == 'wrong_project':
        current['projectId'] = 'other'
    else:
        current['folderId'] = 'absent'
    with pytest.raises(ValueError):
        tracker.capture([current], nodes)


def test_cancel_and_corrupt_id(source):
    with pytest.raises(PartialSaveCancelled):
        read_domain_baseline(source, cancel=lambda: True)
    with sqlite3.connect(source) as db:
        db.execute('UPDATE material_records SET payload=?', (json.dumps(material('wrong')),))
    with pytest.raises(PartialSaveConflict, match='id'):
        read_domain_baseline(source)
