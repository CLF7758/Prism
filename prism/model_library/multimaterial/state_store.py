"""The working-material state: encoding, three-state resolution, references.

This module is the data half of "every pixel on screen can be explained":

* ``effective_texture`` answers **where** a slot's texture comes from --
  local override, cleared, library snapshot, or the source definition -- and
  never merges the four into an anonymous "current value";
* ``effective_property`` does the same for numbers, keeping "absent", "0" and
  "disabled" three different answers;
* ``encode_state``/``decode_state`` round-trip a state without dropping fields a
  newer version wrote;
* ``collect_references`` lists every resource the current state depends on, so
  cleanup can keep them and history/leases can be added without guessing.

Nothing here touches a database, a file or a widget: it is the layer the storage
layout can be swapped under (see the A2 experiment report).
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Iterable, Mapping, Optional

from . import vocabulary
from .catalog import (
    MaterialCatalog,
    MaterialDefinition,
    binding_resolutions,
    definitions_by_id,
    texture_uses,
)
from .contracts import (
    MaterialAssignment,
    MaterialState,
    MaterialTarget,
    PatchAction,
    TexturePatch,
    TextureUse,
    WorkingMaterial,
)

#: Payload shape of the state section.
STATE_SCHEMA_VERSION = 1

#: Where an effective value comes from.  Every answer the UI shows must be one
#: of these; "unknown" is a bug, not a source.
SOURCE_WORKING = 'working'        # a local override
SOURCE_SNAPSHOT = 'snapshot'      # the managed library snapshot it was applied from
SOURCE_SOURCE = 'source'          # the original definition of the source file
SOURCE_CLEARED = 'cleared'        # explicitly removed; never falls back
SOURCE_NONE = 'none'              # nothing states a texture here
SOURCE_DISABLED = 'disabled'      # an explicitly disabled numeric value

SOURCES = (SOURCE_WORKING, SOURCE_SNAPSHOT, SOURCE_SOURCE, SOURCE_CLEARED,
           SOURCE_NONE, SOURCE_DISABLED)


@dataclass(frozen=True)
class EffectiveTexture:
    """One slot's resolved value, with its provenance."""

    slot: str
    source: str
    use: Optional[TextureUse] = None

    @property
    def has_texture(self) -> bool:
        return self.use is not None

    def as_dict(self) -> dict:
        return {'slot': self.slot, 'source': self.source,
                'resource_id': getattr(self.use, 'resource_id', '') if self.use else '',
                'locator': getattr(self.use, 'source_locator', '') if self.use else ''}


@dataclass(frozen=True)
class EffectiveProperty:
    """One numeric property's resolved value, with its provenance."""

    key: str
    source: str
    value: object = None

    @property
    def disabled(self) -> bool:
        return self.source == SOURCE_DISABLED

    def as_dict(self) -> dict:
        return {'key': self.key, 'source': self.source, 'value': self.value}


# ---------------------------------------------------------------------------
# three-state resolution
# ---------------------------------------------------------------------------
def texture_patch_map(material: WorkingMaterial) -> dict:
    return {patch.slot: patch for patch in material.texture_patches}


def effective_texture(slot: str, material: Optional[WorkingMaterial],
                      catalog: MaterialCatalog, *,
                      snapshots: Optional[Mapping] = None) -> EffectiveTexture:
    """Where one slot's texture comes from, in the fixed order of the contract.

    1. a local ``set`` patch wins;
    2. a local ``clear`` means "no texture here" -- it must **not** fall back to
       the base, otherwise clearing a normal map would silently restore an old
       one;
    3. ``inherit`` (or no patch at all) uses the base: the managed library
       snapshot when it has one, the source definition otherwise;
    4. nothing anywhere: the slot has no texture.  It is not black, not white and
       not a 1x1 placeholder.
    """
    if material is not None:
        patch = texture_patch_map(material).get(slot)
        if patch is not None:
            action = _action(patch)
            if action == PatchAction.SET:
                return EffectiveTexture(slot, SOURCE_WORKING, patch.value)
            if action == PatchAction.CLEAR:
                return EffectiveTexture(slot, SOURCE_CLEARED, None)
    base = _base_texture(slot, material, catalog, snapshots)
    return base


def _base_texture(slot: str, material: Optional[WorkingMaterial],
                  catalog: MaterialCatalog,
                  snapshots: Optional[Mapping]) -> EffectiveTexture:
    if material is not None and material.base_snapshot_ref:
        snapshot = (snapshots or {}).get(material.base_snapshot_ref)
        if snapshot is not None:
            use = texture_uses(_as_definition(snapshot)).get(slot)
            if use is not None:
                return EffectiveTexture(slot, SOURCE_SNAPSHOT, use)
    if material is not None and material.source_material_id:
        definition = definitions_by_id(catalog).get(material.source_material_id)
        if definition is not None:
            use = texture_uses(definition).get(slot)
            if use is not None:
                return EffectiveTexture(slot, SOURCE_SOURCE, use)
    return EffectiveTexture(slot, SOURCE_NONE, None)


def effective_properties(material: Optional[WorkingMaterial],
                         catalog: MaterialCatalog, *,
                         snapshots: Optional[Mapping] = None) -> dict:
    """``{key: EffectiveProperty}`` for every key any layer states."""
    base: dict = {}
    if material is not None and material.base_snapshot_ref:
        snapshot = (snapshots or {}).get(material.base_snapshot_ref)
        if snapshot is not None:
            for key, value in json.loads(_as_definition(snapshot).properties_json or '{}').items():
                base[key] = EffectiveProperty(key, SOURCE_SNAPSHOT, value)
    if material is not None and material.source_material_id:
        definition = definitions_by_id(catalog).get(material.source_material_id)
        if definition is not None:
            for key, value in json.loads(definition.properties_json or '{}').items():
                base.setdefault(key, EffectiveProperty(key, SOURCE_SOURCE, value))
    if material is None:
        return base
    for key, patch in _property_patches(material).items():
        if isinstance(patch, Mapping) and patch.get('disabled'):
            base[key] = EffectiveProperty(key, SOURCE_DISABLED, None)
        elif isinstance(patch, Mapping) and 'value' in patch:
            base[key] = EffectiveProperty(key, SOURCE_WORKING, patch['value'])
        else:
            base[key] = EffectiveProperty(key, SOURCE_WORKING, patch)
    return base


def _property_patches(material: WorkingMaterial) -> dict:
    try:
        data = json.loads(material.property_patch_json or '{}')
    except ValueError:
        return {}
    return data if isinstance(data, Mapping) else {}


def _action(patch: TexturePatch) -> PatchAction:
    action = patch.action
    if isinstance(action, PatchAction):
        return action
    return PatchAction(str(action))


def _as_definition(snapshot) -> MaterialDefinition:
    """A snapshot may arrive as a definition, a mapping or a payload string."""
    if isinstance(snapshot, MaterialDefinition):
        return snapshot
    if isinstance(snapshot, str):
        snapshot = json.loads(snapshot)
    if isinstance(snapshot, Mapping):
        from .catalog import _definition_from_payload  # local: payload shapes
        return _definition_from_payload(snapshot)
    raise TypeError(f'无法解释的快照：{type(snapshot)!r}')


# ---------------------------------------------------------------------------
# payload codec
# ---------------------------------------------------------------------------
STATE_FIELDS = frozenset({'schema_version', 'state_revision', 'materials',
                          'assignments', 'legacy_override', 'unknown'})

MATERIAL_FIELDS = frozenset({'working_id', 'source_material_id', 'display_name',
                             'base_snapshot_ref', 'textures', 'properties',
                             'provenance', 'unknown'})


def encode_state(state: MaterialState) -> dict:
    """Serialize the state section; unknown fields stay on the envelope."""
    payload = {
        'schema_version': STATE_SCHEMA_VERSION,
        'state_revision': int(state.target.state_revision),
        'materials': [_encode_material(item) for item in state.materials],
        'assignments': [{'binding_id': item.binding_id,
                         'working_id': item.working_id}
                        for item in state.assignments],
        'legacy_override': _json_value(state.legacy_override_json),
    }
    _restore_unknown(payload, state.unknown_json, STATE_FIELDS)
    return payload


def _restore_unknown(payload: dict, unknown_json, known) -> None:
    """Put back what a newer version wrote, without losing owned fields."""
    unknown = _json_value(unknown_json)
    if not isinstance(unknown, Mapping):
        return
    for key, value in (unknown.get('payload') or {}).items():
        if key not in known:
            payload.setdefault(key, value)
    rest = {key: value for key, value in unknown.items() if key != 'payload'}
    if rest:
        payload['unknown'] = rest


def decode_state(payload: Mapping, target: MaterialTarget) -> MaterialState:
    """Rebuild a state, keeping envelope fields this version does not know."""
    data = dict(payload or {})
    unknown = dict(data.get('unknown') or {})
    extras = {key: value for key, value in data.items() if key not in STATE_FIELDS}
    if extras:
        unknown['payload'] = extras
    revision = int(data.get('state_revision', target.state_revision) or 0)
    return MaterialState(
        target=MaterialTarget(target.project_session, target.open_epoch,
                              target.model_id, target.source_revision,
                              target.catalog_revision, revision),
        materials=tuple(_decode_material(item) for item in data.get('materials') or ()),
        assignments=tuple(MaterialAssignment(str(item.get('binding_id', '')),
                                             str(item.get('working_id', '')))
                          for item in data.get('assignments') or ()),
        legacy_override_json=_json_text(data.get('legacy_override') or {}),
        unknown_json=_json_text(unknown),
    )


def _encode_material(material: WorkingMaterial) -> dict:
    payload = {
        'working_id': material.working_id,
        'source_material_id': material.source_material_id,
        'display_name': material.display_name,
        'base_snapshot_ref': material.base_snapshot_ref,
        'textures': [_encode_patch(item) for item in material.texture_patches],
        'properties': _json_value(material.property_patch_json),
        'provenance': _json_value(material.provenance_json),
    }
    _restore_unknown(payload, material.unknown_json, MATERIAL_FIELDS)
    return payload


def _encode_patch(patch: TexturePatch) -> dict:
    action = _action(patch)
    payload = {'slot': patch.slot, 'action': action.value}
    if action == PatchAction.SET and patch.value is not None:
        payload['value'] = {
            'resource_id': patch.value.resource_id,
            'channels': patch.value.channels,
            'texcoord': int(patch.value.texcoord),
            'color_space': patch.value.color_space,
            'uv_transform': list(patch.value.uv_transform),
            'normal_convention': patch.value.normal_convention,
            'source_locator': patch.value.source_locator,
            'unknown': _json_value(patch.value.unknown_json),
        }
    return payload


def _decode_material(payload: Mapping) -> WorkingMaterial:
    data = dict(payload or {})
    unknown = dict(data.get('unknown') or {})
    extras = {key: value for key, value in data.items() if key not in MATERIAL_FIELDS}
    if extras:
        unknown['payload'] = extras
    return WorkingMaterial(
        working_id=str(data.get('working_id', '')),
        source_material_id=data.get('source_material_id') or None,
        display_name=str(data.get('display_name', '')),
        base_snapshot_ref=data.get('base_snapshot_ref') or None,
        texture_patches=tuple(_decode_patch(item) for item in data.get('textures') or ()),
        property_patch_json=_json_text(data.get('properties') or {}),
        provenance_json=_json_text(data.get('provenance') or {}),
        unknown_json=_json_text(unknown),
    )


def _decode_patch(payload: Mapping) -> TexturePatch:
    data = dict(payload or {})
    action = PatchAction(str(data.get('action', PatchAction.INHERIT.value)))
    value = None
    if action == PatchAction.SET and isinstance(data.get('value'), Mapping):
        raw = dict(data['value'])
        value = TextureUse(
            slot=str(data.get('slot', '')),
            resource_id=str(raw.get('resource_id', '')),
            channels=str(raw.get('channels', 'RGB')),
            texcoord=int(raw.get('texcoord', 0) or 0),
            color_space=str(raw.get('color_space', 'unknown')),
            uv_transform=tuple(float(item) for item in raw.get('uv_transform') or ()),
            normal_convention=str(raw.get('normal_convention', '')),
            source_locator=str(raw.get('source_locator', '')),
            unknown_json=_json_text(raw.get('unknown') or {}),
        )
    return TexturePatch(slot=str(data.get('slot', '')), action=action, value=value)


def state_signature(state: MaterialState) -> str:
    """A digest of the state's content, used to refuse stale publications."""
    payload = encode_state(state)
    payload.pop('unknown', None)
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, ensure_ascii=False).encode('utf-8')
    ).hexdigest()


# ---------------------------------------------------------------------------
# resources the state depends on
# ---------------------------------------------------------------------------
def collect_references(catalog: MaterialCatalog, state: MaterialState, *,
                       snapshots: Optional[Mapping] = None,
                       extra_roots: Optional[Iterable[str]] = None) -> dict:
    """``{resource_id: sorted reasons}`` for everything this state needs.

    Roots covered here: the source catalog, every working material's local
    override, the managed snapshot a material inherits from (and its textures),
    the inherited source definition, and whatever the caller adds (undo/redo
    history, preview leases, trashed snapshots).  A resource that is referenced
    by *any* root must survive cleanup; a resource referenced by none may be
    collected.

    ``extra_roots`` exists so the history and preview owners add their roots
    instead of keeping a second, divergent list.
    """
    reasons: dict = {}

    def note(resource_id: str, why: str) -> None:
        if not resource_id:
            return
        reasons.setdefault(resource_id, set()).add(why)

    for definition in catalog.definitions:
        for use in definition.textures:
            note(use.resource_id, f'catalog:{definition.material_id}:{use.slot}')

    materials = {item.working_id: item for item in state.materials}
    for material in state.materials:
        for patch in material.texture_patches:
            if _action(patch) == PatchAction.SET and patch.value is not None:
                note(patch.value.resource_id, f'working:{material.working_id}:{patch.slot}')
        if material.base_snapshot_ref:
            snapshot = (snapshots or {}).get(material.base_snapshot_ref)
            if snapshot is not None:
                note(material.base_snapshot_ref,
                     f'snapshot:{material.working_id}')
                for use in _as_definition(snapshot).textures:
                    note(use.resource_id,
                         f'snapshot:{material.base_snapshot_ref}:{use.slot}')
        source = definitions_by_id(catalog).get(material.source_material_id or '')
        if source is not None:
            for use in source.textures:
                note(use.resource_id, f'inherited:{material.working_id}:{use.slot}')

    for resource_id in extra_roots or ():
        note(resource_id, 'extra')

    del materials
    return {resource_id: sorted(why) for resource_id, why in sorted(reasons.items())}


def orphaned_resources(references: Mapping, held: Iterable[str]) -> tuple:
    """Resources the project holds that no root references any more."""
    referenced = set(references or ())
    return tuple(sorted(resource_id for resource_id in held
                        if resource_id not in referenced))


def referenced_working_ids(state: MaterialState) -> dict:
    """``{working_id: usage count}``; a material nobody assigns is a leftover."""
    counts: dict = {}
    for assignment in state.assignments:
        counts[assignment.working_id] = counts.get(assignment.working_id, 0) + 1
    return counts


def leftover_materials(state: MaterialState) -> tuple:
    """Working materials that no binding site uses (kept, but reported)."""
    used = referenced_working_ids(state)
    return tuple(item.working_id for item in state.materials
                 if item.working_id not in used)


def editable_slot_report(verified=()) -> dict:
    """``{slot: state}`` for the UI: editable, unverified, or a source role."""
    editable = set(vocabulary.editable_slots(verified))
    report = {}
    for slot in vocabulary.STANDARD_SLOTS:
        report[slot] = 'editable' if slot in editable else 'unverified'
    for slot in vocabulary.SOURCE_ROLES:
        report[slot] = 'source_role'
    return report


# ---------------------------------------------------------------------------
# small helpers
# ---------------------------------------------------------------------------
def _json_value(value):
    if isinstance(value, str):
        try:
            return json.loads(value)
        except ValueError:
            return {}
    return json.loads(json.dumps(value, ensure_ascii=False, sort_keys=True))


def _json_text(value) -> str:
    return json.dumps(_json_value(value), ensure_ascii=False, sort_keys=True)
