"""AI-04 D08 preflight: its own negative controls.

These tests are the package's own; they live in the package directory and
use the ``test_storage_ai04_`` prefix so no other agent's test file is
touched.

The schema constants are read with AST, exactly like the feasibility
prototype did: importing Prism must not be needed to build a fixture, and
no Qt application may be created by a preflight test.
"""
import ast
import importlib.util
import sqlite3
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]

#: The module under test lives in this package's own isolated copy.  It is
#: loaded by path under a private name so it can never shadow the real
#: ``prism`` package for anybody else's test.
MODULE_PATH = ROOT / 'prism/storage/migration_plan.py'
_spec = importlib.util.spec_from_file_location('ai04_migration_plan', MODULE_PATH)
_module = importlib.util.module_from_spec(_spec)
# dataclasses resolve the defining module through sys.modules, so register it
# before executing the source.
sys.modules['ai04_migration_plan'] = _module
_spec.loader.exec_module(_module)

MigrationPhase = _module.MigrationPhase
preflight_source = _module.preflight_source
plan_phases = _module.plan_phases
space_upper_bound = _module.space_upper_bound


def _schema_constants():
    """Read APPLICATION_ID / USER_VERSION / SCHEMA without importing Prism."""
    tree = ast.parse((ROOT / 'prism/fileio/schema.py').read_text(encoding='utf-8'))
    values = {}
    for node in tree.body:
        if isinstance(node, ast.Assign) and isinstance(node.targets[0], ast.Name):
            name = node.targets[0].id
            if name in ('APPLICATION_ID', 'USER_VERSION'):
                values[name] = ast.literal_eval(node.value)
            elif name == 'SCHEMA':
                values[name] = [ast.literal_eval(element) for element in node.value.elts]
    return values


CONSTANTS = _schema_constants()


def _write_legacy(path: Path, extra_sql=()):
    connection = sqlite3.connect(str(path))
    try:
        connection.execute('PRAGMA application_id = {}'.format(CONSTANTS['APPLICATION_ID']))
        connection.execute('PRAGMA user_version = {}'.format(CONSTANTS['USER_VERSION']))
        for statement in CONSTANTS['SCHEMA']:
            connection.execute(statement)
        for statement in extra_sql:
            connection.execute(statement)
        connection.execute(
            "INSERT INTO items (id, type, x, y, data) VALUES (1, 'image', 0, 0, '{}')")
        connection.execute(
            "INSERT INTO attachments (id, board_id, name, mime, blob, created_at) "
            "VALUES (1, 0, 'a.png', 'image/png', ?, 0)", (b'\x89PNG-bytes',))
        connection.execute(
            "INSERT INTO sqlar (name, item_id, data) VALUES ('a.png', 1, ?)",
            (b'\xff\xd8jpeg-bytes',))
        connection.commit()
    finally:
        connection.close()


def test_plain_legacy_source_is_supported(tmp_path):
    source = tmp_path / 'plain.prism'
    _write_legacy(source)
    result = preflight_source(source)
    assert result.supported, result.blockers
    assert result.blockers == []
    names = {table.name for table in result.tables}
    assert 'items' in names and 'attachments' in names and 'sqlar' in names
    assert result.space.row_count >= 3
    assert result.space.original_bytes > 0


def test_original_blob_columns_are_identified(tmp_path):
    source = tmp_path / 'blobs.prism'
    _write_legacy(source)
    result = preflight_source(source)
    by_name = {table.name: table for table in result.tables}
    assert by_name['attachments'].original_blob_columns == ('blob',)
    assert by_name['sqlar'].original_blob_columns == ('data',)
    assert by_name['attachments'].carries_original_bytes


def test_trigger_is_blocked(tmp_path):
    source = tmp_path / 'trigger.prism'
    _write_legacy(source, (
        "CREATE TRIGGER items_ai AFTER INSERT ON items BEGIN "
        "INSERT INTO board_notes (board_id, html) VALUES (0, 'x'); END",
    ))
    result = preflight_source(source)
    assert not result.supported
    assert any('trigger' in blocker for blocker in result.blockers)


def test_view_is_blocked(tmp_path):
    source = tmp_path / 'view.prism'
    _write_legacy(source, ("CREATE VIEW items_view AS SELECT id FROM items",))
    result = preflight_source(source)
    assert not result.supported
    assert any('view' in blocker for blocker in result.blockers)


def test_virtual_table_is_blocked(tmp_path):
    source = tmp_path / 'vtab.prism'
    _write_legacy(source, (
        "CREATE VIRTUAL TABLE docs USING fts5(body)",
    ))
    result = preflight_source(source)
    assert not result.supported
    assert any('virtual table' in blocker for blocker in result.blockers)


def test_unknown_table_is_carried_and_reported(tmp_path):
    source = tmp_path / 'unknown.prism'
    _write_legacy(source, (
        "CREATE TABLE future_extension (id INTEGER PRIMARY KEY, payload TEXT)",
    ))
    result = preflight_source(source)
    assert result.supported, result.blockers
    assert any('unknown table' in warning for warning in result.warnings)
    names = {table.name for table in result.tables}
    assert 'future_extension' in names


def test_active_write_ahead_log_is_blocked(tmp_path):
    source = tmp_path / 'wal.prism'
    _write_legacy(source)
    (tmp_path / 'wal.prism-wal').write_bytes(b'not-an-empty-wal')
    result = preflight_source(source)
    assert not result.supported
    assert any('write-ahead log' in blocker for blocker in result.blockers)


def test_non_sqlite_file_is_refused_and_left_untouched(tmp_path):
    source = tmp_path / 'not-a-db.bin'
    source.write_bytes(b'this is not a database at all')
    before = source.read_bytes()
    result = preflight_source(source)
    assert not result.supported
    assert any('not a sqlite database' in blocker for blocker in result.blockers)
    assert source.read_bytes() == before


def test_missing_path_is_refused_and_nothing_is_created(tmp_path):
    source = tmp_path / 'absent.prism'
    result = preflight_source(source)
    assert not result.supported
    assert any('does not exist' in blocker for blocker in result.blockers)
    assert not source.exists()
    assert list(tmp_path.iterdir()) == []


def test_source_file_bytes_are_not_modified(tmp_path):
    source = tmp_path / 'untouched.prism'
    _write_legacy(source)
    before = source.read_bytes()
    preflight_source(source)
    assert source.read_bytes() == before


def test_space_upper_bound_carries_a_margin(tmp_path):
    source = tmp_path / 'space.prism'
    _write_legacy(source)
    result = preflight_source(source)
    bound = space_upper_bound(result)
    assert bound['total_bytes'] == result.space.total_bytes
    assert bound['required_bytes'] > bound['total_bytes']
    assert bound['margin_bytes'] == int(bound['total_bytes'] * bound['reserve_ratio'])


def test_phases_are_ordered_and_each_has_a_promise():
    phases = plan_phases()
    assert [phase for phase, _ in phases] == [
        MigrationPhase.DISCOVER, MigrationPhase.PREFLIGHT,
        MigrationPhase.COPY_METADATA, MigrationPhase.COPY_OBJECTS,
        MigrationPhase.VERIFY, MigrationPhase.FINALIZE, MigrationPhase.PUBLISH,
    ]
    for _, promise in phases:
        assert promise
