"""The source-material catalog: what a model file itself declares.

This module is Qt-free and F3D-free.  It never renders, never writes a project
and never rewrites an original byte.  It only turns the fixed source revision
into an explicit, traceable list of

* definitions -- one per material the file declares,
* texture uses -- resource + channel + UV set + colour space + transform,
* binding sites -- the places a material is actually used (node instance path,
  geometry, primitive or face group),

and it keeps every field it did not understand in ``unknown_json`` so a parse,
a save and a reload cannot silently drop data.

Identity rules (see ``MaterialTarget``/``MaterialCatalog`` in ``contracts.py``):

* a definition is identified by an **explicit format locator**
  (``gltf:material:3``, ``obj:mtl:chair.mtl#wood``, ``fbx:material:2919911877216``),
  never by display name, never by a renderer actor index;
* a texture resource is identified by the SHA-256 of its bytes
  (``sha256:...``).  A file name alone is not an identity; when the bytes are
  not reachable the resource keeps an empty id and the locator is reported as
  unresolved instead of being passed off as resolved;
* a binding site keeps the instance path, so a mesh used four times has four
  sites instead of one anonymous "used somewhere".
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Iterable, Mapping, Optional

from . import vocabulary
from .contracts import (
    BindingSite,
    MaterialCatalog,
    MaterialDefinition,
    TextureUse,
)

#: The catalog payload shape this implementation writes.  It is a *payload*
#: shape inside the project record, not a new database; the disk schema is
#: decided by the persistence RFC.
CATALOG_SCHEMA_VERSION = 1

# The slot vocabulary lives in one place, :mod:`..vocabulary`.  These names are
# kept as aliases only because callers already import them from here -- and
# ``EDITABLE_SLOTS`` is a **historical** name: a slot having a standard name
# does not mean the app may edit it.  Editability is a capability question, see
# :func:`..vocabulary.editable_slots`.
STANDARD_SLOTS = vocabulary.STANDARD_SLOTS
SOURCE_ONLY_SLOTS = vocabulary.SOURCE_ROLES
ALL_SLOTS = vocabulary.CATALOG_SLOTS
EDITABLE_SLOTS = STANDARD_SLOTS

#: Colour space of a texture use.
COLOR_SRGB = 'srgb'
COLOR_LINEAR = 'linear'
COLOR_UNKNOWN = 'unknown'

#: Channel selectors, identical to the existing material-slot convention.
CHANNEL_RGB = 'RGB'
CHANNEL_R = 'R'
CHANNEL_G = 'G'
CHANNEL_B = 'B'
CHANNEL_A = 'A'

#: A stable digest of a file's bytes, prefixed so its meaning is visible.
DIGEST_PREFIX = 'sha256:'

#: How one usage point resolved.  ``assigned`` has a definition, ``unassigned``
#: means the file states no material there, ``dangling`` means the file names a
#: material that no definition of this source provides.  The last two are not
#: the same fact and must not be counted as one.
BINDING_RESOLUTIONS = ('assigned', 'unassigned', 'dangling')


class CatalogParseError(RuntimeError):
    """The file could not be read.  Never turned into an empty catalog."""


def resource_id_of(data: bytes) -> str:
    """The identity of a texture resource: the hash of its bytes."""
    return DIGEST_PREFIX + hashlib.sha256(data).hexdigest()


def digest_of_file(path) -> str:
    digest = hashlib.sha256()
    with Path(path).open('rb') as handle:
        while True:
            chunk = handle.read(1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
    return DIGEST_PREFIX + digest.hexdigest()


@dataclass(frozen=True)
class ResourceRef:
    """One resource a texture use points at.

    ``resource_id`` is empty exactly when the bytes are not reachable from this
    source revision (missing sibling file, remote URL, oversized embedded
    blob).  A caller must not treat an empty id as "no texture": the locator
    and the flags say what happened, and the three cases need different advice:

    * ``missing``  -- stated relatively and not there: 缺图;
    * ``absolute`` -- stated by an absolute path that does not exist here: a
      relocation problem, not a broken model;
    * ``remote``   -- a URL; nothing was downloaded.
    """

    resource_id: str = ''
    locator: str = ''
    embedded: bool = False
    missing: bool = False
    absolute: bool = False
    remote: bool = False
    size_bytes: int = 0

    @property
    def resolved(self) -> bool:
        return bool(self.resource_id)


def texture_use(slot: str, resource: ResourceRef, *, channels: str = CHANNEL_RGB,
                texcoord: int = 0, color_space: str = COLOR_SRGB,
                uv_transform: Iterable[float] = (), normal_convention: str = '',
                source_locator: str = '', unknown: Optional[Mapping] = None) -> TextureUse:
    """Build one :class:`TextureUse`; every argument stays explicit."""
    if slot not in ALL_SLOTS:
        raise ValueError(f'未知的贴图槽位：{slot}')
    return TextureUse(
        slot=slot,
        resource_id=resource.resource_id,
        channels=channels,
        texcoord=int(texcoord),
        color_space=color_space,
        uv_transform=tuple(float(value) for value in uv_transform),
        normal_convention=normal_convention,
        source_locator=source_locator or resource.locator,
        unknown_json=_json_text(unknown or {}),
    )


class CatalogBuilder:
    """Collect definitions and binding sites, then freeze one catalog.

    The builder is the only place that computes the catalog's source digest, so
    two inspectors cannot disagree about what "the same source" means: the
    digest covers the main file **and** every dependency whose bytes were
    actually hashed.
    """

    def __init__(self, parser_id: str, *, main_path,
                 schema_version: int = CATALOG_SCHEMA_VERSION):
        self.parser_id = parser_id
        self.main_path = Path(main_path)
        self.schema_version = int(schema_version)
        self._definitions: list[MaterialDefinition] = []
        self._bindings: list[BindingSite] = []
        self._unresolved: list[str] = []
        self._digests: dict[str, str] = {}
        self._resolutions: dict[str, str] = {}
        self._unknown: dict = {}

    # -- collection ----------------------------------------------------
    def add_definition(self, definition: MaterialDefinition) -> str:
        if any(item.material_id == definition.material_id for item in self._definitions):
            raise CatalogParseError(f'材质定位重复：{definition.material_id}')
        self._definitions.append(definition)
        return definition.material_id

    def add_binding(self, binding: BindingSite) -> str:
        if any(item.binding_id == binding.binding_id for item in self._bindings):
            raise CatalogParseError(f'使用点定位重复：{binding.binding_id}')
        self._bindings.append(binding)
        return binding.binding_id

    def note_unresolved(self, text: str) -> None:
        text = str(text)
        if text and text not in self._unresolved:
            self._unresolved.append(text)

    def note_resource_digest(self, locator: str, resource_id: str) -> None:
        if locator and resource_id:
            self._digests[locator] = resource_id

    def note_binding_resolution(self, binding_id: str, state: str) -> None:
        """Record how one usage point resolved: assigned / unassigned / dangling.

        This is the state the frozen ``BindingSite`` contract cannot carry yet.
        It is collected here so a usage point is never dropped just because the
        contract is missing a field, and so the payload already holds the answer
        for when the field lands.
        """
        if state not in BINDING_RESOLUTIONS:
            raise ValueError(f'未知的使用点解析状态：{state!r}')
        self._resolutions[str(binding_id)] = state

    def note_unknown(self, key: str, value) -> None:
        self._unknown[key] = value

    # -- inspection ----------------------------------------------------
    @property
    def definitions(self) -> tuple:
        return tuple(self._definitions)

    @property
    def bindings(self) -> tuple:
        return tuple(self._bindings)

    @property
    def unresolved(self) -> tuple:
        return tuple(self._unresolved)

    def definition(self, material_id: Optional[str]) -> Optional[MaterialDefinition]:
        for item in self._definitions:
            if item.material_id == material_id:
                return item
        return None

    # -- freezing ------------------------------------------------------
    def source_digest(self) -> str:
        main = digest_of_file(self.main_path) if self.main_path.is_file() else ''
        payload = {
            'main': main,
            'resources': [[locator, self._digests[locator]]
                          for locator in sorted(self._digests)],
        }
        return resource_id_of(json.dumps(payload, sort_keys=True).encode('utf-8'))

    def build(self, *, revision: int = 1) -> MaterialCatalog:
        known = {item.material_id for item in self._definitions}
        for binding in self._bindings:
            if binding.original_material_id and binding.original_material_id not in known:
                raise CatalogParseError(
                    '使用点引用了不存在的材质定位：'
                    f'{binding.original_material_id}（{binding.binding_id}）')
        if self._resolutions:
            self._unknown['binding_resolutions'] = dict(sorted(self._resolutions.items()))
        return MaterialCatalog(
            schema_version=self.schema_version,
            revision=int(revision),
            source_digest=self.source_digest(),
            parser_id=self.parser_id,
            definitions=tuple(self._definitions),
            binding_sites=tuple(replace(binding, resolution=self._resolutions.get(
                binding.binding_id, 'assigned' if binding.original_material_id else 'unassigned'))
                for binding in self._bindings),
            unresolved=tuple(self._unresolved),
            unknown_json=_json_text(self._unknown),
        )


# ---------------------------------------------------------------------------
# reading a catalog
# ---------------------------------------------------------------------------
def usage_counts(catalog: MaterialCatalog) -> dict:
    """``{material_id: number of binding sites}``.

    Four chairs sharing one wood material is **one** material used four times;
    a definition nobody uses is absent from this mapping and is reported by
    :func:`unused_definitions` instead of being hidden.
    """
    counts: dict[str, int] = {}
    for binding in catalog.binding_sites:
        if binding.original_material_id:
            counts[binding.original_material_id] = (
                counts.get(binding.original_material_id, 0) + 1)
    return counts


def unused_definitions(catalog: MaterialCatalog) -> tuple:
    """Definitions the file declares but never assigns."""
    used = usage_counts(catalog)
    return tuple(item.material_id for item in catalog.definitions
                 if item.material_id not in used)


def definitions_by_id(catalog: MaterialCatalog) -> dict:
    return {item.material_id: item for item in catalog.definitions}


def binding_resolutions(catalog: MaterialCatalog) -> dict:
    """``{binding_id: 'assigned'|'unassigned'|'dangling'}`` for every usage point.

    A usage point is never removed because its material is missing: the count of
    places a material *could* affect must stay honest.  Until the frozen
    ``BindingSite`` contract carries the state itself, it is read from the
    catalog payload; a binding without a recorded state falls back to what its
    assignment says.
    """
    recorded = _catalog_flags(catalog).get('binding_resolutions') or {}
    result = {}
    for binding in catalog.binding_sites:
        state = getattr(binding, 'resolution', 'unknown')
        if state not in BINDING_RESOLUTIONS:
            state = recorded.get(binding.binding_id)
        if state not in BINDING_RESOLUTIONS:
            state = 'assigned' if binding.original_material_id else 'unassigned'
        result[binding.binding_id] = state
    return result


def bindings_by_resolution(catalog: MaterialCatalog, state: str) -> tuple:
    """Every usage point in one resolution state, in source order."""
    resolutions = binding_resolutions(catalog)
    return tuple(binding for binding in catalog.binding_sites
                 if resolutions.get(binding.binding_id) == state)


def dangling_bindings(catalog: MaterialCatalog) -> dict:
    """``{binding_id: diagnostic}`` for usage points naming a missing material."""
    return dict(_catalog_flags(catalog).get('dangling_bindings') or {})


def _catalog_flags(catalog: MaterialCatalog) -> dict:
    try:
        data = json.loads(catalog.unknown_json or '{}')
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}


def texture_uses(definition: MaterialDefinition) -> dict:
    """``{slot: TextureUse}``; a definition cannot state one slot twice."""
    return {item.slot: item for item in definition.textures}


def _resource_flags(use: TextureUse) -> dict:
    try:
        data = json.loads(use.unknown_json or '{}')
    except ValueError:
        data = {}
    return data if isinstance(data, dict) else {}


def unresolved_resources(catalog: MaterialCatalog) -> tuple:
    """Textures stated **relatively** whose bytes are not there: 缺图.

    ``(slot, locator)`` per use.  A reference the file states by an absolute
    path is reported by :func:`absolute_resources` instead: "the author's disk
    moved" and "a file the model needs is missing" are different problems and
    must not be merged into one number.
    """
    result = []
    for definition in catalog.definitions:
        for use in definition.textures:
            if use.resource_id:
                continue
            flags = _resource_flags(use)
            if flags.get('absolute') or flags.get('remote') or flags.get('embedded'):
                continue
            result.append((use.slot, use.source_locator))
    return tuple(result)


def absolute_resources(catalog: MaterialCatalog) -> tuple:
    """Texture uses stated by an absolute path that does not resolve here."""
    result = []
    for definition in catalog.definitions:
        for use in definition.textures:
            if use.resource_id:
                continue
            flags = _resource_flags(use)
            if flags.get('absolute'):
                result.append((use.slot, use.source_locator))
    return tuple(result)


def remote_resources(catalog: MaterialCatalog) -> tuple:
    """Texture uses that name a URL; nothing was downloaded."""
    result = []
    for definition in catalog.definitions:
        for use in definition.textures:
            flags = _resource_flags(use)
            if flags.get('remote'):
                result.append((use.slot, use.source_locator))
    return tuple(result)


def embedded_resources(catalog: MaterialCatalog) -> tuple:
    """Texture uses whose bytes live inside the model file."""
    result = []
    for definition in catalog.definitions:
        for use in definition.textures:
            if _resource_flags(use).get('embedded'):
                result.append((use.slot, use.source_locator))
    return tuple(result)


# ---------------------------------------------------------------------------
# payload codec
# ---------------------------------------------------------------------------
CATALOG_PAYLOAD_FIELDS = frozenset({
    'schema_version', 'revision', 'source_digest', 'parser_id', 'definitions',
    'binding_sites', 'unresolved', 'unknown',
})


def catalog_to_payload(catalog: MaterialCatalog) -> dict:
    """Serialize one catalog into the project-record payload."""
    unknown = _json_value(catalog.unknown_json)
    payload = {
        'schema_version': int(catalog.schema_version),
        'revision': int(catalog.revision),
        'source_digest': catalog.source_digest,
        'parser_id': catalog.parser_id,
        'definitions': [_definition_to_payload(item) for item in catalog.definitions],
        'binding_sites': [_binding_to_payload(item) for item in catalog.binding_sites],
        'unresolved': list(catalog.unresolved),
    }
    # Fields written by a newer version stay on the envelope, untouched.
    if isinstance(unknown, dict):
        for key, value in (unknown.get('payload') or {}).items():
            if key not in CATALOG_PAYLOAD_FIELDS:
                payload.setdefault(key, value)
        remaining = {key: value for key, value in unknown.items() if key != 'payload'}
        if remaining:
            payload['unknown'] = remaining
    return payload


def catalog_from_payload(payload: Mapping) -> MaterialCatalog:
    """Rebuild a catalog, keeping envelope fields this version does not know."""
    data = dict(payload or {})
    unknown = dict(data.get('unknown') or {})
    extras = {key: value for key, value in data.items()
              if key not in CATALOG_PAYLOAD_FIELDS}
    if extras:
        unknown['payload'] = extras
    return MaterialCatalog(
        schema_version=int(data.get('schema_version', 0) or 0),
        revision=int(data.get('revision', 0) or 0),
        source_digest=str(data.get('source_digest', '') or ''),
        parser_id=str(data.get('parser_id', '') or ''),
        definitions=tuple(_definition_from_payload(item)
                          for item in data.get('definitions') or ()),
        binding_sites=tuple(_binding_from_payload(item)
                            for item in data.get('binding_sites') or ()),
        unresolved=tuple(str(item) for item in data.get('unresolved') or ()),
        unknown_json=_json_text(unknown),
    )


def _definition_to_payload(definition: MaterialDefinition) -> dict:
    return {
        'material_id': definition.material_id,
        'source_key': definition.source_key,
        'name': definition.name,
        'workflow': definition.workflow,
        'textures': [_texture_to_payload(item) for item in definition.textures],
        'properties': _json_value(definition.properties_json),
        'unsupported': list(definition.unsupported),
        'unknown': _json_value(definition.unknown_json),
    }


def _definition_from_payload(payload: Mapping) -> MaterialDefinition:
    data = dict(payload or {})
    return MaterialDefinition(
        material_id=str(data.get('material_id', '') or ''),
        source_key=str(data.get('source_key', '') or ''),
        name=str(data.get('name', '') or ''),
        workflow=str(data.get('workflow', '') or ''),
        textures=tuple(_texture_from_payload(item)
                       for item in data.get('textures') or ()),
        properties_json=_json_text(data.get('properties') or {}),
        unsupported=tuple(str(item) for item in data.get('unsupported') or ()),
        unknown_json=_json_text(data.get('unknown') or {}),
    )


def _texture_to_payload(use: TextureUse) -> dict:
    return {
        'slot': use.slot,
        'resource_id': use.resource_id,
        'channels': use.channels,
        'texcoord': int(use.texcoord),
        'color_space': use.color_space,
        'uv_transform': list(use.uv_transform),
        'normal_convention': use.normal_convention,
        'source_locator': use.source_locator,
        'unknown': _json_value(use.unknown_json),
    }


def _texture_from_payload(payload: Mapping) -> TextureUse:
    data = dict(payload or {})
    return TextureUse(
        slot=str(data.get('slot', '') or ''),
        resource_id=str(data.get('resource_id', '') or ''),
        channels=str(data.get('channels', CHANNEL_RGB) or CHANNEL_RGB),
        texcoord=int(data.get('texcoord', 0) or 0),
        color_space=str(data.get('color_space', COLOR_UNKNOWN) or COLOR_UNKNOWN),
        uv_transform=tuple(float(value) for value in data.get('uv_transform') or ()),
        normal_convention=str(data.get('normal_convention', '') or ''),
        source_locator=str(data.get('source_locator', '') or ''),
        unknown_json=_json_text(data.get('unknown') or {}),
    )


def _binding_to_payload(binding: BindingSite) -> dict:
    return {
        'binding_id': binding.binding_id,
        'source_key': binding.source_key,
        'node_path': binding.node_path,
        'geometry_key': binding.geometry_key,
        'primitive_key': binding.primitive_key,
        'original_material_id': binding.original_material_id,
        'resolution': binding.resolution,
        'instance_key': binding.instance_key,
        'geometry_fingerprint': binding.geometry_fingerprint,
        'content_fingerprint': binding.content_fingerprint,
        'match_rules_version': binding.match_rules_version,
    }


def _binding_from_payload(payload: Mapping) -> BindingSite:
    data = dict(payload or {})
    original = data.get('original_material_id')
    return BindingSite(
        binding_id=str(data.get('binding_id', '') or ''),
        source_key=str(data.get('source_key', '') or ''),
        node_path=str(data.get('node_path', '') or ''),
        geometry_key=str(data.get('geometry_key', '') or ''),
        primitive_key=str(data.get('primitive_key', '') or ''),
        original_material_id=None if not original else str(original),
        resolution=str(data.get('resolution', 'unknown')),
        instance_key=str(data.get('instance_key', '')),
        geometry_fingerprint=str(data.get('geometry_fingerprint', '')),
        content_fingerprint=str(data.get('content_fingerprint', '')),
        match_rules_version=int(data.get('match_rules_version', 2)),
    )


# ---------------------------------------------------------------------------
# small JSON helpers (a catalog must never hand out a live dict)
# ---------------------------------------------------------------------------
def _json_text(value) -> str:
    return json.dumps(_json_value(value), ensure_ascii=False, sort_keys=True)


def _json_value(value):
    if isinstance(value, str):
        try:
            return json.loads(value)
        except ValueError:
            return {}
    return json.loads(json.dumps(value, ensure_ascii=False, sort_keys=True))
