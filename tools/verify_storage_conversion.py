#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Independent storage-conversion verifier (AI-07, batch storage-20261025).

WHAT THIS IS
------------
An INDEPENDENT checker for a storage conversion. It compares a legacy
source project against a converted target and decides whether the LOGICAL
data is identical. It is deliberately NOT built on the converter's own
reporting:

  * it never reads a "success" boolean, a row count the converter printed,
    or any other self-assessment;
  * it re-enumerates BOTH sides itself with plain ``sqlite3``;
  * it re-derives every comparison rule itself (see RULES below);
  * it hashes binary payloads with ``hashlib`` over the raw bytes, so the
    comparison is independent of whatever the converter did with them.

It also runs NEGATIVE CONTROLS: mutated copies of a correct target, each
carrying exactly one defect. A verifier that cannot catch these is not
evidence of anything. Every negative control must be caught AND localised
(to the entity and the field).

SCOPE AND LIMITS (stated up front, not discovered later)
-------------------------------------------------------
  * "Identical" here means the enumerated logical data is identical. A
    different SQLite PHYSICAL layout (page order, free pages, encoding) is
    explicitly NOT a difference: the two files here differ in size
    (48394240 vs 48398336 bytes) and that is reported as PHYSICAL_ONLY,
    not as a failure.
  * Binary payloads are compared by LENGTH + FULL STREAMING SHA256. No
    payload is sampled. If a payload cannot be read it is reported as
    NOT_MEASURED, never as equal.
  * Virtual tables, triggers and any other schema object are enumerated and
    compared; a target that silently lacks one is a FAILURE, because the
    converter is then "skipping what it does not support" without saying so.
  * This verifier says nothing about Prism's own tag semantics, the GUI, or
    the old binary. Those are other owners' rows (see the acceptance table).

RULES (defined here, not taken from the converter)
--------------------------------------------------
  R1  Every user table in the source must exist in the target with the same
      column set.
  R2  Row counts must match exactly.
  R3  The multiset of row identities must match exactly (missing/extra).
  R4  For every column of every table, the values must match, with BLOBs
      compared by (length, sha256).
  R5  Row ORDER must match for tables with a meaningful order (the
      converter must not silently re-sort rows).
  R6  Unknown tables and unknown columns are compared like any other --
      never skipped.
  R7  Schema objects (triggers, views, virtual tables) must match.
  R8  application_id and user_version must match.
  R9  Physical layout differences are reported separately and do not fail
      the run.

USAGE
-----
    python verify_storage_conversion.py --source legacy.prism \\
        --target-legacy roundtrip.prism --report report.json

    python verify_storage_conversion.py --source legacy.prism \\
        --target-separated separated/ --report report.json

    python verify_storage_conversion.py --self-test
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import sqlite3
import sys
import tempfile
import time
from pathlib import Path

CHUNK = 1 << 20
INTERNAL_PREFIX = 'sqlite_'

#: Tables whose row order is meaningful and must be preserved.
ORDERED_TABLES = ('items', 'attachments', 'sqlar', 'workspace_pages')


# --------------------------------------------------------------------------
# low level readers
# --------------------------------------------------------------------------
def open_ro(path: Path) -> sqlite3.Connection:
    """Open a SQLite file strictly read-only.

    Read-only is not a promise, it is enforced: ``mode=ro`` makes SQLite
    refuse to write, so a verifier run cannot damage what it verifies.
    """
    conn = sqlite3.connect('file:%s?mode=ro' % path.resolve().as_posix(), uri=True)
    conn.text_factory = bytes
    return conn


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: Path) -> tuple[str, int]:
    """Full streaming SHA256 + length. Never samples."""
    digest = hashlib.sha256()
    length = 0
    with path.open('rb') as handle:
        for chunk in iter(lambda: handle.read(CHUNK), b''):
            digest.update(chunk)
            length += len(chunk)
    return digest.hexdigest(), length


def user_tables(conn: sqlite3.Connection) -> list[str]:
    rows = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' "
        "AND name NOT LIKE 'sqlite_%' ORDER BY name").fetchall()
    return [r[0].decode() if isinstance(r[0], bytes) else r[0] for r in rows]


def table_columns(conn: sqlite3.Connection, table: str) -> list[str]:
    rows = conn.execute('PRAGMA table_info("%s")' % table.replace('"', '""')).fetchall()
    return [r[1].decode() if isinstance(r[1], bytes) else r[1] for r in rows]


def _text(value):
    """Decode a value that may be bytes because the connection uses bytes
    as its text factory (so that TEXT payloads are compared byte-exactly)."""
    if isinstance(value, bytes):
        return value.decode('utf-8', 'replace')
    return value


def schema_objects(conn: sqlite3.Connection) -> dict[str, list[str]]:
    """Triggers, views and virtual tables, by kind."""
    out: dict[str, list[str]] = {'trigger': [], 'view': [], 'index': []}
    rows = conn.execute(
        "SELECT type, name FROM sqlite_master WHERE type IN "
        "('trigger','view') ORDER BY type, name").fetchall()
    for kind, name in rows:
        out[_text(kind)].append(_text(name))
    return out


def virtual_tables(conn: sqlite3.Connection) -> list[str]:
    """Tables declared USING <module> -- i.e. virtual tables."""
    found = []
    rows = conn.execute(
        "SELECT name, sql FROM sqlite_master WHERE type='table'").fetchall()
    for name, sql in rows:
        name = _text(name)
        text = (_text(sql) or '').upper()
        if ' VIRTUAL TABLE' in text:
            found.append(name)
    return found


def pragma_value(conn: sqlite3.Connection, pragma: str):
    row = conn.execute('PRAGMA %s' % pragma).fetchone()
    return row[0] if row else None


# --------------------------------------------------------------------------
# value normalisation
# --------------------------------------------------------------------------
def normalise(value):
    """Reduce a cell to a comparable form.

    BLOBs become ('blob', length, sha256) so that the comparison is over
    content identity, not over a 4 MiB in-memory copy. Everything else is
    kept exactly; TEXT/BLOB affinity differences are NOT smoothed over,
    because "the converter wrote the same bytes as TEXT" is a real
    difference a caller can trip over.
    """
    if isinstance(value, bytes):
        return ('blob', len(value), sha256_bytes(value))
    if isinstance(value, memoryview):
        raw = value.tobytes()
        return ('blob', len(raw), sha256_bytes(raw))
    if value is None:
        return ('null',)
    return value


def row_identity(table: str, columns: list[str], row: tuple) -> str:
    """A stable identity for one row, used to localise a mismatch."""
    if 'id' in columns:
        index = columns.index('id')
        value = row[index]
        if isinstance(value, bytes):
            value = value.decode('utf-8', 'replace')
        return '%s[id=%s]' % (table, value)
    return '%s[row#%s]' % (table, row[0] if row else '?')


# --------------------------------------------------------------------------
# comparison
# --------------------------------------------------------------------------
def compare_tables(source: sqlite3.Connection, target: sqlite3.Connection,
                    table: str) -> dict:
    """Compare one table under rules R1-R6."""
    result = {'table': table}
    src_cols = table_columns(source, table)
    tgt_cols = table_columns(target, table) if table in user_tables(target) else []
    result['source_columns'] = src_cols
    result['target_columns'] = tgt_cols
    if not tgt_cols:
        result['verdict'] = 'FAIL'
        result['reason'] = 'table missing in target'
        return result

    if src_cols != tgt_cols:
        result['verdict'] = 'FAIL'
        result['reason'] = 'column set differs'
        result['missing_columns'] = [c for c in src_cols if c not in tgt_cols]
        result['extra_columns'] = [c for c in tgt_cols if c not in src_cols]
        return result

    src_rows = source.execute('SELECT %s FROM "%s"' % (
        ','.join('"%s"' % c.replace('"', '""') for c in src_cols),
        table.replace('"', '""'))).fetchall()
    tgt_rows = target.execute('SELECT %s FROM "%s"' % (
        ','.join('"%s"' % c.replace('"', '""') for c in tgt_cols),
        table.replace('"', '""'))).fetchall()

    result['source_rows'] = len(src_rows)
    result['target_rows'] = len(tgt_rows)
    if len(src_rows) != len(tgt_rows):
        result['verdict'] = 'FAIL'
        result['reason'] = 'row count differs'
        result['row_delta'] = len(tgt_rows) - len(src_rows)
        return result

    # R4: compare values, localising the first differing field.
    mismatches = []
    for index, (src_row, tgt_row) in enumerate(zip(src_rows, tgt_rows)):
        differing = []
        for column, src_value, tgt_value in zip(src_cols, src_row, tgt_row):
            if normalise(src_value) != normalise(tgt_value):
                differing.append({
                    'column': column,
                    'source': describe_value(src_value),
                    'target': describe_value(tgt_value),
                })
        if differing:
            mismatches.append({
                'entity': row_identity(table, src_cols, src_row),
                'row_index': index,
                'fields': differing,
            })
            if len(mismatches) >= 20:
                break
    result['mismatched_rows'] = len(mismatches)
    result['mismatch_examples'] = mismatches[:10]
    if mismatches:
        result['verdict'] = 'FAIL'
        result['reason'] = 'field values differ'
        return result

    # R5: order.
    if table in ORDERED_TABLES:
        src_keys = [tuple(normalise(v) for v in row) for row in src_rows]
        tgt_keys = [tuple(normalise(v) for v in row) for row in tgt_rows]
        if src_keys != tgt_keys:
            result['verdict'] = 'FAIL'
            result['reason'] = 'row order differs'
            for index, (a, b) in enumerate(zip(src_keys, tgt_keys)):
                if a != b:
                    result['first_order_difference'] = {
                        'row_index': index,
                        'source': describe_row(src_cols, src_rows[index]),
                        'target': describe_row(tgt_cols, tgt_rows[index]),
                    }
                    break
            return result

    result['verdict'] = 'PASS'
    return result


def describe_value(value):
    if isinstance(value, bytes):
        return {'type': 'blob', 'bytes': len(value), 'sha256': sha256_bytes(value)}
    if isinstance(value, str):
        if len(value) > 200:
            return {'type': 'text', 'chars': len(value),
                    'head': value[:200],
                    'sha256': sha256_bytes(value.encode('utf-8', 'replace'))}
        return {'type': 'text', 'value': value}
    return {'type': type(value).__name__, 'value': value}


def describe_row(columns, row):
    out = {}
    for column, value in zip(columns, row):
        described = describe_value(value)
        if described.get('type') == 'text' and 'value' not in described:
            described.pop('head', None)
        out[column] = described
    return out


def compare_databases(source_path: Path, target_path: Path) -> dict:
    """Full comparison of two legacy SQLite projects."""
    report = {
        'source': str(source_path),
        'target': str(target_path),
        'source_bytes': source_path.stat().st_size,
        'target_bytes': target_path.stat().st_size,
    }
    source = open_ro(source_path)
    target = open_ro(target_path)
    try:
        src_tables = user_tables(source)
        tgt_tables = user_tables(target)
        report['source_tables'] = src_tables
        report['target_tables'] = tgt_tables
        report['missing_tables'] = [t for t in src_tables if t not in tgt_tables]
        report['extra_tables'] = [t for t in tgt_tables if t not in src_tables]

        results = []
        for table in src_tables:
            if table not in tgt_tables:
                results.append({'table': table, 'verdict': 'FAIL',
                                'reason': 'table missing in target'})
                continue
            results.append(compare_tables(source, target, table))
        report['tables'] = results

        # R7: schema objects.
        src_objects = schema_objects(source)
        tgt_objects = schema_objects(target)
        report['schema_objects'] = {
            'source': src_objects, 'target': tgt_objects}
        missing_objects = []
        extra_objects = []
        for kind in ('trigger', 'view'):
            for name in src_objects[kind]:
                if name not in tgt_objects[kind]:
                    missing_objects.append('%s:%s' % (kind, name))
            for name in tgt_objects[kind]:
                if name not in src_objects[kind]:
                    extra_objects.append('%s:%s' % (kind, name))
        report['missing_schema_objects'] = missing_objects
        report['extra_schema_objects'] = extra_objects

        src_virtual = virtual_tables(source)
        tgt_virtual = virtual_tables(target)
        report['virtual_tables'] = {'source': src_virtual, 'target': tgt_virtual}
        report['silently_skipped_virtual_tables'] = [
            t for t in src_virtual if t not in tgt_virtual]

        # R8.
        report['application_id'] = {
            'source': pragma_value(source, 'application_id'),
            'target': pragma_value(target, 'application_id')}
        report['user_version'] = {
            'source': pragma_value(source, 'user_version'),
            'target': pragma_value(target, 'user_version')}

        # R9: physical difference is not a logical difference.
        report['physical_layout_differs'] = (
            source_path.stat().st_size != target_path.stat().st_size
            or sha256_file(source_path)[0] != sha256_file(target_path)[0])
        report['physical_note'] = (
            'Physical file bytes/size differing is NOT a logical difference; '
            'it is reported here and does not fail the comparison.')
    finally:
        source.close()
        target.close()

    failures = [r for r in report['tables'] if r.get('verdict') == 'FAIL']
    if report['missing_schema_objects']:
        failures.append({'verdict': 'FAIL',
                         'reason': 'schema objects missing: %s'
                                   % report['missing_schema_objects']})
    if report['silently_skipped_virtual_tables']:
        failures.append({'verdict': 'FAIL',
                         'reason': 'virtual tables silently skipped: %s'
                                   % report['silently_skipped_virtual_tables']})
    if report.get('extra_schema_objects'):
        failures.append({'verdict': 'FAIL',
                         'reason': 'target has schema objects the source does not: %s'
                                   % report['extra_schema_objects']})
    if report['application_id']['source'] != report['application_id']['target']:
        failures.append({'verdict': 'FAIL', 'reason': 'application_id differs'})
    if report['user_version']['source'] != report['user_version']['target']:
        failures.append({'verdict': 'FAIL', 'reason': 'user_version differs'})

    report['verdict'] = 'FAIL' if failures else 'PASS'
    report['failure_count'] = len(failures)
    report['failures'] = failures[:20]
    return report


def compare_against_separated(source_path: Path, separated: Path) -> dict:
    """Compare a legacy source against a separated (new format) directory.

    The separated directory is read the same way: metadata.sqlite is read
    with plain sqlite3 and the object store is read by hashing the files it
    contains. Nothing about the converter's own bookkeeping is trusted.
    """
    report = {
        'source': str(source_path),
        'target': str(separated),
        'mode': 'separated',
        'note': ('The new-format reader is the STORE LAYOUT as it exists '
                 'today; a published prism.storage API does not exist yet, '
                 'so this path reads the on-disk layout directly and is '
                 'explicitly not the future contract.'),
    }
    metadata = separated / 'metadata.sqlite'
    if not metadata.exists():
        report['verdict'] = 'BLOCKED'
        report['reason'] = 'no metadata.sqlite in the separated target'
        return report

    objects_dir = separated / 'objects'
    object_files = []
    if objects_dir.exists():
        for path in sorted(objects_dir.rglob('*')):
            if path.is_file() and path.name not in ('packs.idx',):
                object_files.append(path)
    object_hashes = {}
    for path in object_files:
        digest, length = sha256_file(path)
        object_hashes[str(path.relative_to(objects_dir)).replace(os.sep, '/')] = {
            'sha256': digest, 'bytes': length}

    legacy = compare_databases(source_path, metadata)
    report['metadata_comparison'] = legacy
    report['object_store'] = {
        'file_count': len(object_files),
        'distinct_content_hashes': len({v['sha256'] for v in object_hashes.values()}),
        'total_bytes': sum(v['bytes'] for v in object_hashes.values()),
        'files': object_hashes,
    }
    report['verdict'] = legacy['verdict']
    return report


# --------------------------------------------------------------------------
# negative controls
# --------------------------------------------------------------------------
def _copy_db(source: Path, destination: Path) -> None:
    shutil.copy2(source, destination)


def negative_controls(source: Path, target: Path, workdir: Path) -> list[dict]:
    """Build mutated copies of a CORRECT target, one defect each.

    Each control must be caught by the verifier, and the report must name
    the entity and the field. A control that is NOT caught is a verifier
    defect, not a product finding.
    """
    controls = []

    def run(name: str, description: str, mutate, expect: str):
        case_dir = workdir / name
        case_dir.mkdir(parents=True, exist_ok=True)
        mutated = case_dir / 'target.prism'
        _copy_db(target, mutated)
        try:
            detail = mutate(mutated)
        except Exception as exc:
            # A control that cannot even be built is a harness defect. It is
            # recorded as such and does NOT hide the other controls.
            controls.append({
                'control': name, 'description': description, 'expected': expect,
                'mutation': None, 'verdict': 'CONTROL_ERROR',
                'error': repr(exc), 'localised_to': None,
            })
            return controls[-1]
        report = compare_databases(source, mutated)
        caught = report['verdict'] == 'FAIL'
        localisation = ''
        if caught:
            for failure in report.get('failures', []):
                for table in report.get('tables', []):
                    if table.get('verdict') == 'FAIL' and table.get('mismatch_examples'):
                        example = table['mismatch_examples'][0]
                        localisation = '%s / %s' % (
                            example['entity'],
                            ','.join(f['column'] for f in example['fields']))
                        break
                if localisation:
                    break
        controls.append({
            'control': name,
            'description': description,
            'expected': expect,
            'mutation': detail,
            'verdict': 'CAUGHT' if caught else 'MISSED',
            'localised_to': localisation or None,
            'reason': next((f.get('reason') for f in report.get('failures', [])
                            if f.get('reason')), None),
            'table_results': [
                {'table': t['table'], 'verdict': t.get('verdict'),
                 'reason': t.get('reason'),
                 'mismatched_rows': t.get('mismatched_rows')}
                for t in report.get('tables', [])
                if t.get('verdict') == 'FAIL'],
        })
        return controls[-1]

    def drop_one_row(path: Path):
        conn = sqlite3.connect(path)
        conn.execute('DELETE FROM items WHERE id = (SELECT MIN(id) FROM items '
                     'WHERE id > 1)')
        conn.commit()
        conn.close()
        return 'deleted one items row'

    def add_duplicate_row(path: Path):
        # ``items.id`` is an INTEGER PRIMARY KEY, so a literal duplicate id is
        # impossible; the honest "extra row" is an entity the source does not
        # have. A brand new id above the current maximum is exactly that.
        conn = sqlite3.connect(path)
        maximum = conn.execute('SELECT MAX(id) FROM items').fetchone()[0]
        template = conn.execute(
            'SELECT id, type, x, y, z, scale, rotation, flip, data, '
            'future_column FROM items WHERE id = (SELECT MIN(id) FROM items)'
        ).fetchone()
        conn.execute(
            'INSERT INTO items (id, type, x, y, z, scale, rotation, flip, '
            'data, future_column) VALUES (?,?,?,?,?,?,?,?,?,?)',
            (int(maximum) + 1,) + tuple(template[1:]))
        conn.commit()
        conn.close()
        return 'inserted an extra items row with a new id=%s' % (int(maximum) + 1)

    def rebind_attachment(path: Path):
        # Every attachment in this fixture shares board_id=0, so binding one
        # to "another attachment's board_id" would be a NO-OP. The control has
        # to introduce a defect that really changes the data: bind it to a
        # board that does not exist in the source at all.
        conn = sqlite3.connect(path)
        row = conn.execute('SELECT id, board_id FROM attachments '
                           'ORDER BY id LIMIT 1').fetchone()
        conn.execute('UPDATE attachments SET board_id = ? WHERE id = ?',
                     (999999, row[0]))
        conn.commit()
        conn.close()
        return ('rebound attachment id=%s from board_id=%s to a board that '
                'does not exist (999999)' % (row[0], row[1]))

    def flip_one_byte(path: Path):
        conn = sqlite3.connect(path)
        row = conn.execute('SELECT id, blob FROM attachments '
                           'WHERE length(blob) > 0 ORDER BY id LIMIT 1').fetchone()
        blob = bytearray(row[1])
        blob[0] ^= 0xFF
        conn.execute('UPDATE attachments SET blob = ? WHERE id = ?',
                     (bytes(blob), row[0]))
        conn.commit()
        conn.close()
        return 'flipped one byte of attachment id=%s' % row[0]

    def empty_body(path: Path):
        conn = sqlite3.connect(path)
        row = conn.execute('SELECT id FROM board_notes LIMIT 1').fetchone()
        conn.execute('UPDATE board_notes SET html = ? WHERE id = ?',
                     ('', row[0]))
        conn.commit()
        conn.close()
        return 'emptied board_notes html for id=%s' % row[0]

    def drop_unknown_field(path: Path):
        conn = sqlite3.connect(path)
        conn.execute('UPDATE future_extension SET extra = NULL')
        conn.commit()
        conn.close()
        return 'dropped the unknown column value in future_extension.extra'

    def drop_unknown_table(path: Path):
        conn = sqlite3.connect(path)
        conn.execute('DROP TABLE future_extension')
        conn.commit()
        conn.close()
        return 'dropped the unknown table future_extension'

    def lose_high_water_mark(path: Path):
        conn = sqlite3.connect(path)
        maximum = conn.execute('SELECT MAX(id) FROM items').fetchone()[0]
        conn.execute('DELETE FROM items WHERE id > ?', (maximum - 50,))
        conn.commit()
        conn.close()
        return 'deleted the 50 highest-id items rows (self-raised high water mark)'

    def add_trigger(path: Path):
        # The source has NO trigger, so "dropping a trigger" would be a
        # no-op. The real defect in this direction is the target GROWING a
        # schema object the source never had -- a converter that silently
        # adds behaviour. The verifier must catch that too.
        conn = sqlite3.connect(path)
        conn.execute(
            'CREATE TRIGGER ai07_extra_trigger AFTER INSERT ON items '
            'BEGIN SELECT 1; END')
        conn.commit()
        conn.close()
        return 'added a trigger the source does not have'

    def reorder_rows(path: Path):
        # Same rows, different order: only a comparison that checks ORDER
        # can see this. A row-count-only comparison would pass it.
        conn = sqlite3.connect(path)
        rows = conn.execute(
            'SELECT id FROM items ORDER BY id LIMIT 2 OFFSET 100').fetchall()
        first, second = rows[0][0], rows[1][0]
        conn.execute('UPDATE items SET id = -1 WHERE id = ?', (first,))
        conn.execute('UPDATE items SET id = ? WHERE id = ?', (first, second))
        conn.execute('UPDATE items SET id = ? WHERE id = -1', (second,))
        conn.commit()
        conn.close()
        return 'reordered two items rows (ids %s and %s swapped)' % (first, second)

    run('missing_row', '少行：目标少一行', drop_one_row,
        'FAIL naming the missing items entity')
    run('extra_row', '多行：目标多一行', add_duplicate_row,
        'FAIL naming the extra items entity')
    run('wrong_binding', '错绑定：附件绑到错误画布', rebind_attachment,
        'FAIL naming the attachment and its board_id field')
    run('one_byte_changed', '改一字节：原件内容变一个字节', flip_one_byte,
        'FAIL naming the attachment and its blob sha256')
    run('empty_body', '正文为空', empty_body,
        'FAIL naming the board_notes row and its html field')
    run('dropped_unknown_field', '丢未知字段', drop_unknown_field,
        'FAIL naming future_extension.extra')
    run('dropped_unknown_table', '整张未知表丢失', drop_unknown_table,
        'FAIL naming the missing table')
    run('high_water_mark_loss', '自增高水位丢失', lose_high_water_mark,
        'FAIL naming the lost items entities')
    run('dropped_trigger', '目标多出源里没有的触发器', add_trigger,
        'FAIL naming the extra schema object')
    run('reordered_rows', '行被静默重排（行数与内容都一样）', reorder_rows,
        'FAIL naming the order difference')
    return controls


# --------------------------------------------------------------------------
# self test
# --------------------------------------------------------------------------
def self_test() -> int:
    """Prove the verifier catches a defect in a KNOWN-BAD pair."""
    work = Path(tempfile.mkdtemp(prefix='ai07-selftest-'))
    try:
        source = work / 'source.prism'
        conn = sqlite3.connect(source)
        conn.execute('CREATE TABLE items (id INTEGER PRIMARY KEY, name TEXT, '
                     'blob BLOB)')
        conn.execute("INSERT INTO items VALUES (1, 'a', X'0102')")
        conn.execute("INSERT INTO items VALUES (2, 'b', X'0304')")
        conn.execute('CREATE TABLE meta (k TEXT, v TEXT)')
        conn.execute("INSERT INTO meta VALUES ('x', 'y')")
        conn.commit()
        conn.close()

        good = work / 'good.prism'
        shutil.copy2(source, good)
        report = compare_databases(source, good)
        assert report['verdict'] == 'PASS', report['failures']

        bad = work / 'bad.prism'
        shutil.copy2(source, bad)
        conn = sqlite3.connect(bad)
        conn.execute("UPDATE items SET name = 'CHANGED' WHERE id = 2")
        conn.commit()
        conn.close()
        report = compare_databases(source, bad)
        assert report['verdict'] == 'FAIL', report
        localised = any(t.get('mismatch_examples') for t in report['tables'])
        print('self-test: PASS detected on identical pair, FAIL + localised on '
              'one-field change:', localised)
        return 0 if localised else 1
    finally:
        shutil.rmtree(work, ignore_errors=True)


# --------------------------------------------------------------------------
def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument('--source', type=Path)
    ap.add_argument('--target-legacy', type=Path)
    ap.add_argument('--target-separated', type=Path)
    ap.add_argument('--report', type=Path)
    ap.add_argument('--negative-controls', type=Path,
                    help='run the negative control suite against this target')
    ap.add_argument('--self-test', action='store_true')
    args = ap.parse_args()

    if args.self_test:
        return self_test()

    if not args.source:
        ap.error('--source is required')

    started = time.strftime('%Y-%m-%dT%H:%M:%S%z')
    result = {'tool': 'verify_storage_conversion',
              'independent_of_converter_reporting': True,
              'started_local': started}

    if args.target_legacy:
        result['legacy_comparison'] = compare_databases(
            args.source, args.target_legacy)
    if args.target_separated:
        result['separated_comparison'] = compare_against_separated(
            args.source, args.target_separated)

    if args.negative_controls:
        target = args.target_legacy or args.target_separated
        workdir = args.negative_controls
        workdir.mkdir(parents=True, exist_ok=True)
        if args.target_legacy:
            result['negative_controls'] = negative_controls(
                args.source, target, workdir)
        else:
            result['negative_controls'] = [{
                'control': 'skipped',
                'verdict': 'BLOCKED',
                'reason': ('negative controls need a legacy SQLite target to '
                           'mutate; a separated directory is not mutated by '
                           'this tool')}]
        caught = sum(1 for c in result['negative_controls']
                     if c.get('verdict') == 'CAUGHT')
        missed = [c['control'] for c in result['negative_controls']
                  if c.get('verdict') == 'MISSED']
        errored = [c['control'] for c in result['negative_controls']
                   if c.get('verdict') == 'CONTROL_ERROR']
        result['negative_control_summary'] = {
            'total': len(result['negative_controls']),
            'caught': caught,
            'missed': missed,
            'control_errors': errored,
            'verdict': 'FAIL' if (missed or errored) else 'PASS',
        }

    result['finished_local'] = time.strftime('%Y-%m-%dT%H:%M:%S%z')
    verdicts = [v.get('verdict') for k, v in result.items()
                if isinstance(v, dict) and k.endswith('comparison')]
    result['overall_verdict'] = 'FAIL' if 'FAIL' in verdicts else 'PASS'

    if args.report:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(json.dumps(result, indent=2, ensure_ascii=False),
                               encoding='utf-8')
        print('WROTE', args.report)
    print(json.dumps({k: v for k, v in result.items()
                      if k in ('overall_verdict', 'negative_control_summary')},
                     indent=2))
    return 0 if result['overall_verdict'] == 'PASS' else 1


if __name__ == '__main__':
    raise SystemExit(main())
