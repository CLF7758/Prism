"""Durable bounded material history, stored as changed rows, not whole models."""
from copy import deepcopy
from .state_store import encode_state, decode_state

LIMIT = 100
COLLECTIONS = {'materials': 'working_id', 'assignments': 'binding_id'}


def load_history(block, target, *, reset_source=False):
    history = deepcopy(block.get('history') or {})
    if history and history.get('schema_version') != 1:
        raise ValueError('Unsupported material history version')
    scope = [target.source_revision, target.catalog_revision]
    if history and history.get('scope') != scope:
        if reset_source:
            return dict(schema_version=1, scope=scope, undo=[], redo=[])
        raise ValueError('Material source/catalog changed; history cannot be applied')
    return history or dict(schema_version=1, scope=scope, undo=[], redo=[])


def append_history(history, before, after, gesture_id):
    history = deepcopy(history)
    left, right = encode_state(before), encode_state(after)
    changes = {}
    for field, key in COLLECTIONS.items():
        a = {row[key]: row for row in left[field]}
        b = {row[key]: row for row in right[field]}
        apos = {row[key]: i for i, row in enumerate(left[field])}
        bpos = {row[key]: i for i, row in enumerate(right[field])}
        changed = {rid: {'before': a.get(rid), 'after': b.get(rid),
                         'before_index': apos.get(rid), 'after_index': bpos.get(rid)}
                   for rid in a.keys() | b.keys() if a.get(rid) != b.get(rid)}
        if changed:
            changes[field] = changed
    for field in left.keys() | right.keys():
        if field in COLLECTIONS or field in ('schema_version', 'state_revision'):
            continue
        if left.get(field) != right.get(field) or (field in left) != (field in right):
            changes.setdefault('fields', {})[field] = dict(before=left.get(field), after=right.get(field),
                before_present=field in left, after_present=field in right)
    if changes:
        history['undo'].append(dict(gesture_id=gesture_id, changes=changes))
        history['undo'] = history['undo'][-LIMIT:]
        history['redo'] = []
    return history


def move_history(history, state, direction):
    history = deepcopy(history)
    source, destination, old, new = ('undo', 'redo', 'after', 'before') if direction == 'undo' else ('redo', 'undo', 'before', 'after')
    if direction not in ('undo', 'redo'):
        raise ValueError('Invalid history direction')
    if not history[source]:
        raise ValueError('No material history to ' + direction)
    entry = history[source].pop()
    payload = encode_state(state)
    for field, key in COLLECTIONS.items():
        rows = {row[key]: row for row in payload[field]}
        inserted = []
        for rid, change in entry['changes'].get(field, {}).items():
            if rows.get(rid) != change[old]:
                raise ValueError('Material history conflicts with current state')
            if change[new] is None:
                rows.pop(rid, None)
            else:
                if rid not in rows:
                    inserted.append((change.get(new + '_index', len(rows)), rid))
                rows[rid] = deepcopy(change[new])
        ordered = [row for row in rows.values() if row[key] not in {rid for _, rid in inserted}]
        for position, rid in sorted(inserted):
            ordered.insert(position, rows[rid])
        payload[field] = ordered
    for field, change in entry['changes'].get('fields', {}).items():
        if payload.get(field) != change[old] or (field in payload) != change[old + '_present']:
            raise ValueError('Material history conflicts with current fields')
        if change[new + '_present']:
            payload[field] = deepcopy(change[new])
        else:
            payload.pop(field, None)
    history[destination].append(entry)
    return decode_state(payload, state.target), history
