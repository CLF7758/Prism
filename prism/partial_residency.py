"""Explicit page eviction without confusing absent assets with deleted rows.

The first implementation retains canonical logical objects so existing undo
commands and original-source ownership remain valid. It is a correctness seam,
not a claim of bounded memory or a replacement for the image record layer.
"""
from dataclasses import dataclass
from pathlib import Path

from prism.fileio.partial_save import PartialSaveConflict, item_save_columns
from prism.item_lifecycle import logical_item


def key_of(item):
    return getattr(item, 'save_id', None) or getattr(item, 'original_save_id', None)


@dataclass(frozen=True)
class UnloadToken:
    nonce: object
    items: tuple
    keys: tuple
    values: tuple


class PartialAssetResidency:
    def __init__(self):
        self.parked = {}
        self.pending = {}
        self.residents = {}
        self.initialized = False

    def bind_live(self, live_items, known_ids):
        live = [logical_item(item) for item in live_items]
        identities = {id(item) for item in live}
        for key, expected in self.residents.items():
            if key not in self.parked and id(expected) not in identities:
                raise PartialSaveConflict(
                    f'Loaded item {key} is no longer on this page: it disappeared or was replaced '
                    'without an explicit unload; removing an asset needs an explicit trash command')
        for item in live:
            key = key_of(item)
            if key in known_ids and key not in self.parked:
                self.residents.setdefault(key, item)
        self.initialized = True

    def require_stable(self):
        if self.pending:
            raise PartialSaveConflict('A page unload is still in progress; finish or cancel it before saving')

    def prepare(self, items, live_items, known_ids, validate):
        self.require_stable()
        live_items = list(live_items)
        self.bind_live(live_items, known_ids)
        items = tuple(logical_item(item) for item in items)
        live = {id(logical_item(item)) for item in live_items}
        seen, values = set(), []
        for item in items:
            key = key_of(item)
            if key is None or key not in known_ids:
                raise PartialSaveConflict('Save new assets before unloading their page')
            if key in seen or key in self.parked or id(item) not in live:
                raise PartialSaveConflict('Only unique resident assets can be unloaded')
            seen.add(key)
            validate(item)
            values.append(item_save_columns(item))
        token = UnloadToken(object(), items, tuple(key_of(item) for item in items), tuple(values))
        self.pending[token.nonce] = token
        return token

    def commit(self, token, live_items):
        if self.pending.get(token.nonce) is not token:
            raise PartialSaveConflict('Page unload token is stale or belongs to another session')
        live = {key_of(logical_item(item)) for item in live_items}
        for item, key, expected in zip(token.items, token.keys, token.values):
            if key_of(item) != key:
                raise PartialSaveConflict('An asset id changed during page unload')
            if key_of(item) in live:
                raise PartialSaveConflict('Remove all unloading assets from the scene before committing')
            if item_save_columns(item) != expected:
                raise PartialSaveConflict('An asset changed during page unload; cancel and retry')
        self.parked.update((key_of(item), item) for item in token.items)
        del self.pending[token.nonce]

    def cancel(self, token, live_items):
        if self.pending.get(token.nonce) is not token:
            raise PartialSaveConflict('Page unload token is stale or belongs to another session')
        if any(key_of(item) != key for item, key in zip(token.items, token.keys)):
            raise PartialSaveConflict('An asset id changed during page unload')
        live = {id(logical_item(item)) for item in live_items}
        if any(id(item) not in live for item in token.items):
            raise PartialSaveConflict('Restore removed assets before canceling the page unload')
        del self.pending[token.nonce]

    def validate_restore(self, items, live_items):
        live = {}
        for item in live_items:
            item = logical_item(item)
            live.setdefault(key_of(item), []).append(item)
        for item in items:
            item = logical_item(item)
            key = key_of(item)
            retained = self.parked.get(key)
            resident = self.residents.get(key)
            if retained is not None and retained is not item:
                raise PartialSaveConflict('Restore the retained logical asset, not a stale disk copy')
            if resident is not None and resident is not item:
                raise PartialSaveConflict('Do not replace an existing canonical resident asset')
            if len(live.get(key, ())) != 1 or live[key][0] is not item:
                raise PartialSaveConflict('Loaded assets must have one canonical instance in the scene')

    def restored(self, items):
        for item in items:
            item = logical_item(item)
            self.parked.pop(key_of(item), None)
            self.residents[key_of(item)] = item

    def assets_for_save(self, live_items):
        self.require_stable()
        live = list(live_items)
        for item in live:
            if key_of(logical_item(item)) in self.parked:
                raise PartialSaveConflict('Register restored assets before saving their page')
        yield from live
        yield from self.parked.values()


def rebind_asset_sources(items, old_path, new_path):
    """Repoint only this project's archive references, not external media."""
    old_path, new_path = str(Path(old_path).resolve()), str(Path(new_path).resolve())
    resolved = {}
    for item in items:
        item = logical_item(item)
        source = getattr(item, '_prism_source', None)
        if not isinstance(source, tuple) or len(source) != 2:
            continue
        filename = str(source[0])
        if filename not in resolved:
            resolved[filename] = str(Path(filename).resolve())
        if resolved[filename] == old_path:
            item._prism_source = (new_path, source[1])
