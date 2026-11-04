"""Material persistence boundary. No scheduler, Qt or renderer ownership."""
from copy import deepcopy
from dataclasses import asdict, dataclass, replace
import json
import uuid
from .state_store import encode_state

@dataclass(frozen=True)
class CommitResult:
    before: dict
    after: dict
    registry: dict
    command_id: str


def write_material_commit(db, *, model_id, expected_revision, previous_block,
                          state, initial_state, snapshots, resources, resource_plan,
                          stored, add_catalog, request, signature):
    """Write inside the caller's resource transaction, never commit it here.

    All input containers are borrowed read-only. Failure leaves publication to
    the resource transaction owner; this function cannot acknowledge UI state.
    """
    row = db.execute('SELECT payload FROM model_records WHERE id=?', (model_id,)).fetchone()
    if row is None:
        raise ValueError('Model record no longer exists')
    before = json.loads(row[0])
    if int(before['record_revision']) != int(expected_revision):
        raise ValueError('Project record changed during material commit')
    if len(stored) != len(resource_plan.resource_ids) + int(add_catalog):
        raise ValueError('Unexpected material resource batch size')
    registry = resource_plan.register(resources, stored)
    block = deepcopy(previous_block)
    if add_catalog:
        resource = stored[-1]
        block['catalog_ref'] = dict(attachment_id=resource.attachment_id, sha256=resource.sha256)
    defaults = {m.working_id: m for m in initial_state.materials}
    sparse = replace(state,
        materials=tuple(m for m in state.materials if defaults.get(m.working_id) != m),
        assignments=tuple(a for a in state.assignments if a not in initial_state.assignments))
    block.update(schema_version=1, source_revision=state.target.source_revision,
        catalog_revision=state.target.catalog_revision, state=encode_state(sparse),
        resources=registry, snapshots=deepcopy(snapshots))
    command_id = uuid.uuid4().hex
    block.setdefault('receipts', {})[request.request_id] = dict(signature=signature,
        before=asdict(request.target), after=asdict(state.target), command_id=command_id)
    after = deepcopy(before)
    after['multimaterial'] = block
    after['record_revision'] = before['record_revision'] + 1
    after.setdefault('thumbnail', {})['state'] = 'stale'
    db.execute('UPDATE model_records SET payload=? WHERE id=?',
        (json.dumps(after, ensure_ascii=False, allow_nan=False), model_id))
    return CommitResult(before, after, registry, command_id)
