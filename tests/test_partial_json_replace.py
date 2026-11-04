"""Explicit replacement is opt-in; normal JSON edits still preserve fields."""
import json
import sqlite3

import pytest

from prism.fileio.partial_save import (
    PartialSaveCancelled, RowPatch, capture_partial_save, write_partial_snapshot,
)


@pytest.fixture
def project(tmp_path):
    path = tmp_path / 'replace.prism'
    with sqlite3.connect(path) as db:
        # A writable Prism fixture must declare a supported schema version.
        from prism.fileio.schema import USER_VERSION
        db.execute(f'PRAGMA user_version={USER_VERSION}')
        db.execute('CREATE TABLE board_mindmap(board_id TEXT PRIMARY KEY, tree JSON, updated_at TEXT, future TEXT)')
        for key in ('a', 'b'):
            db.execute('INSERT INTO board_mindmap VALUES(?,?,?,?)',
                       (key, json.dumps({'tree': {'children': [1]}, 'unknown': 42}), 'old', 'keep'))
    return path


def tree(path, key='a'):
    with sqlite3.connect(path) as db:
        return db.execute('SELECT tree, future FROM board_mindmap WHERE board_id=?', (key,)).fetchone()


@pytest.mark.parametrize('value', [{}, [], {'tree': {}}, {'other': 1}, None, False, 'text', 0])
def test_whole_replacement_can_clear_or_remove_tree(project, value):
    snapshot = capture_partial_save(project, [RowPatch(
        'board_mindmap', 'a', json_replacements=(('tree', value),))])
    write_partial_snapshot(snapshot)
    stored, future = tree(project)
    if value is None:
        assert stored is None
    else:
        # SQLite JSON declarations have NUMERIC affinity for scalar numbers.
        assert json.loads(stored) == value if isinstance(stored, str) else stored == value
    assert future == 'keep'
    assert json.loads(tree(project, 'b')[0]) == {'tree': {'children': [1]}, 'unknown': 42}


def test_replacement_snapshot_does_not_follow_mutation(project):
    value = {'tree': {'children': ['saved']}}
    snapshot = capture_partial_save(project, [RowPatch(
        'board_mindmap', 'a', json_replacements=(('tree', value),))])
    value['tree']['children'].append('later')
    write_partial_snapshot(snapshot)
    assert json.loads(tree(project)[0]) == {'tree': {'children': ['saved']}}


def test_default_merge_still_preserves_unknown_fields(project):
    write_partial_snapshot(capture_partial_save(project, [RowPatch(
        'board_mindmap', 'a', json_updates=(('tree', {'tree': {}}),))]))
    assert json.loads(tree(project)[0]) == {'tree': {'children': [1]}, 'unknown': 42}


@pytest.mark.parametrize('kwargs', [
    {'json_replacements': (('updated_at', {}),)},
    {'json_replacements': (('tree', {}), ('tree', []))},
    {'json_replacements': (('tree', {}),), 'json_updates': (('tree', {}),)},
    {'json_replacements': (('tree', {}),), 'json_removals': (('tree', ('tree',)),)},
    {'json_replacements': (('tree', float('nan')),)},
    {'json_replacements': (('tree', {'value': float('inf')}),)},
    {'json_replacements': (('tree', {'not-json'}),)},
])
def test_invalid_replacement_is_rejected_before_write(project, kwargs):
    before = project.read_bytes()
    with pytest.raises((ValueError, TypeError)):
        capture_partial_save(project, [RowPatch('board_mindmap', 'a', **kwargs)])
    assert project.read_bytes() == before


def test_cancel_after_first_update_rolls_back_all_replacements(project):
    before = project.read_bytes()
    snapshot = capture_partial_save(project, [RowPatch(
        'board_mindmap', key, json_replacements=(('tree', {}),)) for key in ('a', 'b')])
    calls = 0

    def cancel():
        nonlocal calls
        calls += 1
        return calls >= 3

    with pytest.raises(PartialSaveCancelled):
        write_partial_snapshot(snapshot, cancel=cancel)
    assert project.read_bytes() == before
