"""Metadata-only domain baselines and explicit diffs for partial project saves.

No source reads occur during ``DomainTracker.capture``. Missing rows are never
interpreted as deletes: the caller must explicitly authorize each removed id.
The transaction writer remains responsible for optimistic conflict checking.
"""
from contextlib import closing
from dataclasses import dataclass
import json
from pathlib import Path
import sqlite3
from types import SimpleNamespace

from prism.fileio.partial_save import (PartialReadSnapshot, PartialSaveCancelled,
    PartialSaveConflict, RowInsert, RowPatch, _project_id)

DOMAIN_TABLES = ('material_records', 'workspace_nodes', 'model_records')
SLOTS = frozenset(('basecolor', 'normal', 'roughness', 'metallic', 'ao',
                   'opacity', 'emissive', 'sss', 'displacement'))

#: Tables whose payload carries an ``id`` that must match its row key.
#: The model domain stores its identifier under ``id`` as well, so the same
#: baseline check covers it.
_DOMAIN_ID_KEY = 'id'


def read_domain_baseline(source, *, cancel=None, batch_size=128):
    """Read both complete metadata tables in one stable, cancellable snapshot.

    Source identity and data_version are checked through the end of the read.
    Missing legacy tables are empty. No attachments or original blobs are read.
    """
    if not 1 <= batch_size <= 1024:
        raise ValueError('batch_size must be between 1 and 1024')
    path = Path(source).resolve(strict=True)
    identity = (path.stat().st_dev, path.stat().st_ino)
    records = []
    with closing(sqlite3.connect(path.as_uri() + '?mode=ro', uri=True,
                                timeout=.25)) as db:
        db.execute('BEGIN')
        version = db.execute('PRAGMA data_version').fetchone()[0]
        project_id = _project_id(db)
        schema = db.execute('PRAGMA user_version').fetchone()[0]
        tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        for table in DOMAIN_TABLES:
            if table not in tables:
                continue
            cursor = db.execute(f'SELECT id,payload FROM {table} ORDER BY id')
            while True:
                if cancel and cancel():
                    raise PartialSaveCancelled('Domain baseline cancelled')
                rows = cursor.fetchmany(batch_size)
                if not rows:
                    break
                for key, payload in rows:
                    obj = json.loads(payload)
                    if not isinstance(obj, dict) or obj.get('id') != key:
                        raise PartialSaveConflict('Domain payload id does not match its row')
                    records.append((table, key, (payload,)))
        db.commit()
        if (db.execute('PRAGMA data_version').fetchone()[0] != version or
                identity != (path.stat().st_dev, path.stat().st_ino)):
            raise PartialSaveConflict('Project changed during domain baseline read')
    return PartialReadSnapshot(str(path), identity, project_id, schema, tuple(records))


def _diff(before, current, path=(), table=None):
    updates, removals = {}, []
    for key, value in current.items():
        old = before.get(key)
        if key not in before or old != value:
            if isinstance(old, dict) and isinstance(value, dict):
                nested, removed = _diff(old, value, path + (key,), table)
                if nested:
                    updates[key] = nested
                removals.extend(removed)
            else:
                updates[key] = value
    # Only names owned by this application may be implicitly removed. Future
    # fields omitted by a loader remain on disk, including nested fields.
    # The material library owns a few slot names; the model domain owns
    # nothing, so a field that vanishes from a model record is never a
    # deletion.
    if table == 'material_records':
        for key in before.keys() - current.keys():
            if (path == ('textures',) and key in SLOTS or
                    path == ('preview',) and key == 'discardedAdjustments' or
                    path == ('preview', 'properties') and key in
                    ('slots', 'flipX', 'flipY', 'alphaCutoff')):
                removals.append(path + (key,))
    return updates, removals


def _validate(materials, nodes, project_id, models=(), cancel=None):
    from prism.material_service import validate_material_records
    from prism.model_library.project_storage import validate_model_records
    from prism.workspace_service import ResourceTreeService
    validate_material_records(materials, project_id)
    validate_model_records(list(models), project_id)
    index = {}
    for node in nodes:
        if cancel is not None and cancel():
            raise PartialSaveCancelled('Domain validation cancelled')
        if not isinstance(node, dict) or not isinstance(node.get('id'), str) or not node['id']:
            raise ValueError('Invalid resource tree id')
        index[node['id']] = node
    # Reuse the exact tree service validator without constructing a QObject or
    # deep-copying all records as ResourceTreeService.__init__ would do.
    # ``model_section_enabled`` is True here on purpose: the flag belongs to the
    # UI and decides whether a *new* model folder may be created. A node that is
    # already stored in the project -- including a model folder written by a
    # build that had the section open -- must still validate and be saveable,
    # otherwise a partial save would refuse the project's own data.
    adapter = SimpleNamespace(nodes=nodes, project_id=project_id,
        model_section_enabled=True, _by_id=index.get,
        _title=ResourceTreeService._title)
    adapter._section_allowed = lambda section: (
        ResourceTreeService._section_allowed(adapter, section))
    ResourceTreeService._validate_all(adapter)
    json.dumps(nodes, allow_nan=False)
    for material in materials:
        folder = material.get('folderId')
        if folder is not None:
            node = index.get(folder)
            if not node or node.get('section') != 'material' or node.get('nodeType') != 'folder':
                raise ValueError('Material folder does not exist in resource tree')


@dataclass(frozen=True)
class DomainChanges:
    patches: tuple
    inserts: tuple
    baseline: PartialReadSnapshot


class DomainTracker:
    """Own complete domain coverage; advance only to acknowledged save values."""
    def __init__(self, baseline):
        self.identity = baseline
        self._rows = {(t, k): row for t, k, row in baseline.records if t in DOMAIN_TABLES}
        self._decoded = {key: json.loads(row[0]) for key, row in self._rows.items()}

    def capture(self, materials, nodes, models=(), *, explicit_deletions=(), cancel=None):
        materials, nodes = list(materials), list(nodes)
        models = list(models)
        _validate(materials, nodes, self.identity.project_id, models, cancel=cancel)
        current = {(table, record['id']): record
            for table, records in zip(DOMAIN_TABLES, (materials, nodes, models))
            for record in records}
        deletes = set(explicit_deletions)
        if not deletes <= self._rows.keys() or deletes & current.keys():
            raise PartialSaveConflict('Explicit domain deletion is not an absent covered row')
        missing = self._rows.keys() - current.keys()
        if missing != deletes:
            raise PartialSaveConflict('Domain records disappeared without explicit deletion')
        patches, inserts = [], []
        for key, record in current.items():
            if cancel is not None and cancel():
                raise PartialSaveCancelled('Domain capture cancelled')
            table, row_id = key
            before = self._decoded.get(key)
            if before is None:
                inserts.append(RowInsert(table, (('id', row_id), ('payload',
                    json.dumps(record, ensure_ascii=False, allow_nan=False))), tag=key))
            elif before != record:
                updates, removals = _diff(before, record, table=table)
                if updates or removals:
                    # Freeze only changed metadata, not every record or texture.
                    updates = json.loads(json.dumps(updates, allow_nan=False))
                    patches.append(RowPatch(table, row_id,
                        json_updates=(('payload', updates),) if updates else (),
                        json_removals=(('payload', tuple(removals)),) if removals else ()))
        patches.extend(RowPatch(t, k, delete=True) for t, k in sorted(deletes))
        baseline = PartialReadSnapshot(self.identity.source, self.identity.source_identity,
            self.identity.project_id, self.identity.schema_version,
            tuple((t, k, row) for (t, k), row in self._rows.items()))
        return DomainChanges(tuple(patches), tuple(inserts), baseline)

    def acknowledge(self, snapshot):
        """Call only after a matching writer receipt; never consume live state."""
        if (snapshot.source != self.identity.source or
                snapshot.source_identity != self.identity.source_identity or
                snapshot.project_id != self.identity.project_id or
                snapshot.schema_version != self.identity.schema_version):
            raise PartialSaveConflict('Save acknowledgment belongs to another project')
        for change in snapshot.changes:
            key = change.table, change.key
            if change.table not in DOMAIN_TABLES:
                continue
            if change.after is None:
                self._rows.pop(key, None)
                self._decoded.pop(key, None)
            else:
                self._rows[key] = change.after
                self._decoded[key] = json.loads(change.after[0])
