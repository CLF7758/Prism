"""A page can leave the scene without losing edits or impersonating deletion."""
from dataclasses import replace
import json

import pytest

from prism.fileio.partial_save import PartialSaveConflict, write_partial_snapshot
from prism.partial_project import PartialProjectSession, read_baseline
from prism.source_store import read_archive
from tests.test_partial_project import (FakeItem, FakeScene, item_rows, loaded_items,
                                       opened, pages_of, png, project)


def load_items(project, ids):
    stored = item_rows(project)
    return [FakeItem(save_id=key, data=json.loads(stored[key][-1]),
                     x=stored[key][2], y=stored[key][3], z=stored[key][4]) for key in ids]


def detach(session, scene, items):
    token = session.prepare_unload(items, scene)
    for item in items:
        scene._items.remove(item)
    session.commit_unload(token, scene)
    return token


def test_new_page_registration_keeps_previous_page_dirty(project):
    session = opened(project)
    scene = FakeScene(loaded_items(project), pages_of(project))
    session.attach(scene)
    scene._items[0].data['notes'] = 'page A edit'
    baseline = read_baseline(project, [('items', key) for key in (5, 6)])
    added = load_items(project, (5, 6))
    scene._items.extend(added)
    session.register_loaded(baseline, added, scene)
    added[0].data['notes'] = 'page B edit'
    snapshot = session.capture(scene)
    assert {(c.table, c.key) for c in snapshot.changes} == {('items', 1), ('items', 5)}
    write_partial_snapshot(snapshot)
    saved = item_rows(project)
    assert json.loads(saved[1][-1])['notes'] == 'page A edit'
    assert json.loads(saved[5][-1])['notes'] == 'page B edit'
    assert len(saved) == 12


@pytest.mark.parametrize('dirty', [False, True])
def test_explicit_unload_is_not_a_delete(project, dirty):
    session = opened(project)
    items = loaded_items(project)
    scene = FakeScene(items, pages_of(project))
    if dirty:
        items[0].data['notes'] = 'kept while unloaded'
    detach(session, scene, items)
    snapshot = session.capture(scene)
    assert len(snapshot.changes) == int(dirty)
    receipt = write_partial_snapshot(snapshot)
    session.acknowledge(snapshot, receipt, scene=scene)
    assert len(item_rows(project)) == 12
    assert session.capture(scene).changes == ()
    assert session.retained_item(1) is items[0]


def test_unannounced_disappearance_still_refuses_save(project):
    session = opened(project)
    scene = FakeScene(loaded_items(project), pages_of(project))
    scene._items.pop()
    with pytest.raises(PartialSaveConflict, match='no longer on this page'):
        session.capture(scene)


def test_unload_is_two_phase_and_must_finish_before_saving(project):
    session = opened(project)
    scene = FakeScene(loaded_items(project), pages_of(project))
    token = session.prepare_unload(scene._items[:1], scene)
    with pytest.raises(PartialSaveConflict, match='unload is still in progress'):
        session.capture(scene)
    with pytest.raises(PartialSaveConflict, match='Remove all'):
        session.commit_unload(token, scene)
    session.cancel_unload(token, scene)
    assert session.capture(scene).changes == ()
    with pytest.raises(PartialSaveConflict, match='stale'):
        session.commit_unload(token, scene)


def test_edit_during_unload_requires_cancel_and_keeps_edit(project):
    session = opened(project)
    scene = FakeScene(loaded_items(project), pages_of(project))
    item = scene._items[0]
    token = session.prepare_unload([item], scene)
    scene._items.remove(item)
    item.data['notes'] = 'edited before unload completed'
    with pytest.raises(PartialSaveConflict, match='changed during page unload'):
        session.commit_unload(token, scene)
    with pytest.raises(PartialSaveConflict, match='Restore removed'):
        session.cancel_unload(token, scene)
    scene._items.append(item)
    session.cancel_unload(token, scene)
    assert len(session.capture(scene).changes) == 1


def test_unload_after_snapshot_does_not_lose_newer_edit_on_ack(project):
    session = opened(project)
    scene = FakeScene(loaded_items(project), pages_of(project))
    item = scene._items[0]
    item.data['notes'] = 'first'
    snapshot = session.capture(scene)
    item.data['notes'] = 'newer edit'
    detach(session, scene, [item])
    receipt = write_partial_snapshot(snapshot)
    session.acknowledge(snapshot, receipt, scene=scene)
    follow_up = session.capture(scene)
    assert json.loads(item_rows(project)[1][-1])['notes'] == 'first'
    assert len(follow_up.changes) == 1
    write_partial_snapshot(follow_up)
    assert json.loads(item_rows(project)[1][-1])['notes'] == 'newer edit'


def test_restoration_reuses_dirty_logical_object_not_old_disk_copy(project):
    session = opened(project)
    scene = FakeScene(loaded_items(project), pages_of(project))
    item = scene._items[0]
    item.data['notes'] = 'not on disk'
    detach(session, scene, [item])
    baseline = read_baseline(project, [('items', 1)])
    stale = load_items(project, [1])[0]
    scene._items.append(stale)
    with pytest.raises(PartialSaveConflict, match='retained logical asset'):
        session.register_loaded(baseline, [stale], scene)
    assert session.retained_item(1) is item
    scene._items.remove(stale)
    scene._items.append(item)
    session.register_loaded(baseline, [item], scene)
    assert session.retained_item(1) is None
    assert item.data['notes'] == 'not on disk'
    write_partial_snapshot(session.capture(scene))
    assert json.loads(item_rows(project)[1][-1])['notes'] == 'not on disk'


def test_unregistered_restoration_cannot_duplicate_an_evicted_asset(project):
    session = opened(project)
    scene = FakeScene(loaded_items(project), pages_of(project))
    item = scene._items[0]
    detach(session, scene, [item])
    scene._items.append(item)
    with pytest.raises(PartialSaveConflict, match='Register restored'):
        session.capture(scene)


@pytest.mark.parametrize('field,value', [('project_id', 'other'), ('source_identity', (-1, -1)),
                                        ('schema_version', 999), ('source', 'other.prism')])
def test_wrong_project_registration_does_not_change_baseline(project, field, value):
    session = opened(project)
    scene = FakeScene(loaded_items(project), pages_of(project))
    session.attach(scene)
    before = session.loaded()
    snapshot = read_baseline(project, [('items', 5)])
    added = load_items(project, [5])
    scene._items.extend(added)
    with pytest.raises(PartialSaveConflict, match='another project'):
        session.register_loaded(replace(snapshot, **{field: value}), added, scene)
    assert session.loaded() == before


def test_registration_cannot_reset_existing_dirty_baseline(project):
    session = opened(project)
    scene = FakeScene(loaded_items(project), pages_of(project))
    session.attach(scene)
    item = scene._items[0]
    item.data['notes'] = 'dirty'
    baseline = read_baseline(project, [('items', 1)])
    table, key, row = baseline.records[0]
    changed = (*row[:-1], json.dumps({'notes': 'wrong new baseline'}))
    with pytest.raises(PartialSaveConflict, match='cannot replace'):
        session.register_loaded(replace(baseline, records=((table, key, changed),)), [item], scene)
    assert len(session.capture(scene).changes) == 1


def test_new_unsaved_asset_cannot_be_unloaded(project, png):
    session = opened(project)
    scene = FakeScene(loaded_items(project), pages_of(project))
    fresh = FakeItem(data={'filename': 'new.png'}, source=png)
    scene._items.append(fresh)
    with pytest.raises(PartialSaveConflict, match='Save new assets'):
        session.prepare_unload([fresh], scene)


def test_resident_dirty_item_cannot_be_replaced_by_same_id_disk_object(project):
    session = opened(project)
    scene = FakeScene(loaded_items(project), pages_of(project))
    session.attach(scene)
    original = scene._items[0]
    original.data['notes'] = 'must survive'
    scene._items.remove(original)
    replacement = load_items(project, [1])[0]
    scene._items.append(replacement)
    baseline = read_baseline(project, [('items', 1)])
    with pytest.raises(PartialSaveConflict, match='canonical resident'):
        session.register_loaded(baseline, [replacement], scene)
    with pytest.raises(PartialSaveConflict, match='disappeared or was replaced'):
        session.capture(scene)
    scene._items.remove(replacement)
    scene._items.append(original)
    assert len(session.capture(scene).changes) == 1


def test_unload_token_also_freezes_asset_id(project):
    session = opened(project)
    scene = FakeScene(loaded_items(project), pages_of(project))
    item = scene._items[0]
    token = session.prepare_unload([item], scene)
    scene._items.remove(item)
    item.save_id = 999
    with pytest.raises(PartialSaveConflict, match='id changed'):
        session.commit_unload(token, scene)
    item.save_id = 1
    scene._items.append(item)
    session.cancel_unload(token, scene)
    assert session.capture(scene).changes == ()


def test_save_as_rebinds_resident_and_parked_original_sources(project, tmp_path, png):
    session = opened(project)
    scene = FakeScene(loaded_items(project), pages_of(project))
    for item in scene._items:
        item._prism_source = (str(project), item.save_id)
    parked = scene._items[0]
    parked.data['notes'] = 'before save-as'
    detach(session, scene, [parked])
    snapshot = session.capture(scene)
    parked.data['notes'] = 'after snapshot'
    target = tmp_path / 'rebound-multipage.prism'
    receipt = write_partial_snapshot(snapshot, target)
    session.acknowledge(snapshot, receipt, scene=scene, filename=str(target))
    assert parked._prism_source == (str(target.resolve()), parked.save_id)
    assert all(item._prism_source[0] == str(target.resolve()) for item in scene._items)
    import gc
    gc.collect()  # Release connection cycles created by the legacy fixture helpers.
    project.rename(project.with_suffix('.old.prism'))  # disposable fixture, never a user source
    assert read_archive(parked._prism_source) == png
    assert len(session.capture(scene).changes) == 1
    write_partial_snapshot(session.capture(scene))
    assert json.loads(item_rows(target)[1][-1])['notes'] == 'after snapshot'


def test_save_as_receipt_during_pending_unload_rebinds_before_commit(project, tmp_path):
    session = opened(project)
    scene = FakeScene(loaded_items(project), pages_of(project))
    session.attach(scene)
    item = scene._items[0]
    item._prism_source = (str(project), item.save_id)
    snapshot = session.capture(scene)
    token = session.prepare_unload([item], scene)
    scene._items.remove(item)
    target = tmp_path / 'pending-save-as.prism'
    receipt = write_partial_snapshot(snapshot, target)
    session.acknowledge(snapshot, receipt, scene=scene, filename=str(target))
    assert item._prism_source[0] == str(target.resolve())
    session.commit_unload(token, scene)
    assert session.capture(scene).changes == ()


def test_mind_map_full_value_replacement_can_remove_old_fields(project):
    from tests.test_partial_project import rows
    session = opened(project)
    scene = FakeScene(loaded_items(project), pages_of(project), tree={'children': []})
    snapshot = session.capture(scene)
    receipt = write_partial_snapshot(snapshot)
    session.acknowledge(snapshot, receipt, scene=scene)
    assert json.loads(rows(project, 'board_mindmap')[0][1]) == {'children': []}
    assert session.capture(scene).changes == ()


def test_explicit_mind_map_clear_is_acknowledged_without_losing_new_tree(project):
    from tests.test_partial_project import rows
    session = opened(project)
    scene = FakeScene(loaded_items(project), pages_of(project))
    session.authorize_mindmap_clear()
    scene.mindmap_tree = None
    snapshot = session.capture(scene)
    scene.mindmap_tree = {'root': {'text': 'typed during clear save'}}
    receipt = write_partial_snapshot(snapshot)
    session.acknowledge(snapshot, receipt, scene=scene)
    assert rows(project, 'board_mindmap')[0][1] is None
    follow_up = session.capture(scene)
    assert any(c.table == 'board_mindmap' for c in follow_up.changes)
    write_partial_snapshot(follow_up)
    assert json.loads(rows(project, 'board_mindmap')[0][1]) == scene.mindmap_tree


def test_page_content_replaces_removed_tree_fields_but_keeps_unknown_envelope(project):
    import sqlite3
    from tests.test_partial_project import rows
    with sqlite3.connect(project) as db:
        payload = json.loads(db.execute('SELECT data FROM workspace_pages WHERE id="map"').fetchone()[0])
        payload['content'] = {'root': {'text': 'old', 'obsolete': 'remove me'}}
        payload['futureEnvelope'] = {'keep': True}
        db.execute('UPDATE workspace_pages SET data=? WHERE id="map"', (json.dumps(payload),))
    session = opened(project)
    scene = FakeScene(loaded_items(project), pages_of(project))
    page = next(p for p in scene.workspace_pages if p['id'] == 'map')
    page['content'] = {'root': {'text': 'new'}}
    snapshot = session.capture(scene)
    receipt = write_partial_snapshot(snapshot)
    session.acknowledge(snapshot, receipt, scene=scene)
    stored = next(row for row in rows(project, 'workspace_pages') if row[0] == 'map')
    payload = json.loads(stored[3])
    assert payload['content'] == {'root': {'text': 'new'}}
    assert payload['futureEnvelope'] == {'keep': True}
    assert session.capture(scene).changes == ()


def test_unloaded_page_header_edit_does_not_clear_content_or_tags(project):
    from tests.test_partial_project import rows
    session = opened(project)
    pages = pages_of(project)
    index = next(i for i, p in enumerate(pages) if p['id'] == 'd1')
    pages[index] = {'id': 'd1', 'kind': 'document', 'title': 'header renamed'}
    scene = FakeScene(loaded_items(project), pages)
    snapshot = session.capture(scene)
    write_partial_snapshot(snapshot)
    stored = next(row for row in rows(project, 'workspace_pages') if row[0] == 'd1')
    payload = json.loads(stored[3])
    assert stored[2] == 'header renamed'
    assert payload['content'] == 'first' and payload['tags'] == ['tag']


def test_explicit_empty_page_content_is_saved_as_empty(project):
    from tests.test_partial_project import rows
    session = opened(project)
    pages = pages_of(project)
    next(p for p in pages if p['id'] == 'd1')['content'] = None
    scene = FakeScene(loaded_items(project), pages)
    write_partial_snapshot(session.capture(scene))
    stored = next(row for row in rows(project, 'workspace_pages') if row[0] == 'd1')
    assert json.loads(stored[3])['content'] is None
