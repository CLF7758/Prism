"""Source binding <-> native render object mapping: one-to-one, no guesses.

Identity and evidence are deliberately separate things here.

* **Identity** is what the source file says: a node path, a mesh key, a primitive
  key, and (when the source really has instances) the source's own instance
  identity - ``node:3/instance:2`` for glTF EXT_mesh_gpu_instancing, a model UID for
  FBX instancing. Two parts that are geometrically identical and share a transform
  are still two different parts if the source says so.
* **Evidence** (``geometry_fingerprint``, ``content_fingerprint``) is only used to
  *check* a match, never to name something. It cannot prove that UVs, normals or
  material grouping are the same on its own, which is why a content fingerprint is
  computed separately from the position/index fingerprint.

Three directions, each with a one-to-one contract enforced up front:

* :func:`build_slots` - the catalog decides what exists; the renderer is asked to
  create exactly one object per binding. A native object cannot be handed out twice.
* :func:`reconcile` - native objects already exist. A binding and a native object
  must pair one-to-one: duplicates, shared objects, geometry changes and content
  changes are all reported, never resolved by guesswork.
* :func:`carry_over` - moves *committed assignments* onto a re-parsed revision. It
  never carries a renderer handle: a new scene needs a new native mapping. A binding
  id that still exists must also still match on content, otherwise the assignment is
  refused rather than silently pointed at the new content.

Nothing here imports a renderer.
"""
from __future__ import annotations

import hashlib
import struct
from dataclasses import dataclass
from enum import Enum
from typing import Callable, Iterable, Mapping, Optional, Sequence

#: Version of the matching rules. Bumped whenever a rule changes meaning, and
#: recorded next to any evidence produced by this module.
MATCH_RULES_VERSION = 2

#: Rounded values are hashed as text so that two loaders that widen or narrow
#: floats still produce the same fingerprint.
FINGERPRINT_DECIMALS = 5


class MappingContractError(RuntimeError):
    """The caller broke the one-to-one contract (duplicate ids in its own input).

    Raised instead of reporting a failure, because a duplicate id in the *input* is
    a programming error that must not be able to look like a successful mapping.
    """


class MatchFailure(str, Enum):
    """Why a binding or a native object could not be paired. Never silent."""

    AMBIGUOUS_NATIVE = 'ambiguous_native'
    MISSING_NATIVE = 'missing_native'
    MISSING_BINDING = 'missing_binding'
    GEOMETRY_MISMATCH = 'geometry_mismatch'
    CONTENT_MISMATCH = 'content_mismatch'
    FINGERPRINT_COLLISION = 'fingerprint_collision'
    SHARED_NATIVE_OBJECT = 'shared_native_object'
    DUPLICATE_NATIVE_ID = 'duplicate_native_id'
    CONTENT_CHANGED = 'content_changed'
    IDENTITY_CHANGED = 'identity_changed'


@dataclass(frozen=True)
class MatchEvidence:
    """What the source side can say about one binding, for checking only.

    ``geometry_fingerprint`` covers positions and indices. ``content_fingerprint``
    covers UVs, normals, the material grouping and the surface flags, because a
    position/index digest alone cannot prove those are unchanged.
    """

    geometry_fingerprint: str = ''
    content_fingerprint: str = ''


@dataclass(frozen=True)
class NativeObject:
    """One renderable object, described without touching a renderer.

    ``native_id`` is the renderer's own handle (an opaque name). ``instance_key`` is
    the *source's* instance identity, not a transform digest; the transform digest is
    evidence and lives in ``geometry_fingerprint``/``content_fingerprint`` only when
    the caller wants to use it for checking.
    """

    native_id: str
    node_path: str
    geometry_key: str
    primitive_key: str
    instance_key: str = ''
    geometry_fingerprint: str = ''
    content_fingerprint: str = ''
    material_slot: str = ''


@dataclass(frozen=True)
class NativeSlot:
    """A binding that is known to be displayed by exactly one native object."""

    binding_id: str
    native_id: str
    node_path: str
    geometry_key: str
    primitive_key: str
    instance_key: str = ''
    geometry_fingerprint: str = ''
    content_fingerprint: str = ''
    paired_by: str = 'structure'


@dataclass(frozen=True)
class Unmatched:
    """Something on either side that stays unpaired, with the reason."""

    failure: MatchFailure
    detail: str
    binding_id: str = ''
    native_id: str = ''


@dataclass(frozen=True)
class BindingMap:
    """Result of pairing one catalog revision with one loaded scene."""

    slots: tuple[NativeSlot, ...] = ()
    unmatched: tuple[Unmatched, ...] = ()
    rules_version: int = MATCH_RULES_VERSION

    @property
    def complete(self) -> bool:
        """True only when every binding and every native object was paired once."""
        return not self.unmatched

    def slot_for(self, binding_id: str) -> Optional[NativeSlot]:
        for slot in self.slots:
            if slot.binding_id == binding_id:
                return slot
        return None

    def native_ids_for(self, binding_ids: Iterable[str]) -> tuple[str, ...]:
        wanted = set(binding_ids)
        return tuple(slot.native_id for slot in self.slots if slot.binding_id in wanted)

    def failures(self, failure: MatchFailure) -> tuple[Unmatched, ...]:
        return tuple(item for item in self.unmatched if item.failure is failure)


@dataclass(frozen=True)
class AssignmentTransfer:
    """A committed working material moved onto a new revision.

    Deliberately carries no ``native_id``: the new scene has its own objects, and
    reusing a handle from the previous scene is exactly the bug this type prevents.
    """

    binding_id: str
    previous_binding_id: str
    working_id: str
    matched_by: str


@dataclass(frozen=True)
class CarryResult:
    """Outcome of moving assignments across revisions."""

    transfers: tuple[AssignmentTransfer, ...] = ()
    unmatched: tuple[Unmatched, ...] = ()
    unassigned: tuple[str, ...] = ()
    rules_version: int = MATCH_RULES_VERSION

    @property
    def complete(self) -> bool:
        return not self.unmatched

    def working_id_for(self, binding_id: str) -> Optional[str]:
        for transfer in self.transfers:
            if transfer.binding_id == binding_id:
                return transfer.working_id
        return None

    def failures(self, failure: MatchFailure) -> tuple[Unmatched, ...]:
        return tuple(item for item in self.unmatched if item.failure is failure)


# ------------------------------------------------------------------ fingerprints
def fingerprint_geometry(points: Sequence[float], indices: Sequence[int] = (),
                         *, decimals: int = FINGERPRINT_DECIMALS) -> str:
    """Fingerprint of positions and indices. Matching evidence, not identity."""
    digest = hashlib.sha256()
    flat = [round(float(value), decimals) + 0.0 for value in points]
    digest.update(struct.pack('<I', len(flat)))
    for value in flat:
        digest.update(('%.*f,' % (decimals, value)).encode('ascii'))
    digest.update(struct.pack('<I', len(indices)))
    for index in indices:
        digest.update(struct.pack('<i', int(index)))
    return digest.hexdigest()[:32]


def fingerprint_content(*, uvs: Sequence[float] = (), normals: Sequence[float] = (),
                        material_groups: Sequence[str] = (),
                        two_sided: Optional[bool] = None, alpha_mode: str = '',
                        decimals: int = FINGERPRINT_DECIMALS) -> str:
    """Fingerprint of everything a position/index digest cannot prove.

    UV sets and their transforms, normals, the material grouping of the faces,
    two-sidedness and the alpha mode all change how a part looks, so a cross-revision
    or payload comparison that ignores them would accept a wrong match.
    """
    digest = hashlib.sha256()
    digest.update(struct.pack('<I', len(uvs)))
    for value in uvs:
        digest.update(('%.*f,' % (decimals, round(float(value), decimals))).encode('ascii'))
    digest.update(struct.pack('<I', len(normals)))
    for value in normals:
        digest.update(('%.*f,' % (decimals, round(float(value), decimals))).encode('ascii'))
    digest.update(struct.pack('<I', len(material_groups)))
    for group in material_groups:
        text = str(group).encode('utf-8')
        digest.update(struct.pack('<I', len(text)))
        digest.update(text)
    digest.update(b'2' if two_sided else b'1')
    digest.update(alpha_mode.encode('utf-8'))
    return digest.hexdigest()[:32]


def fingerprint_transform(matrix: Sequence[float],
                          *, decimals: int = FINGERPRINT_DECIMALS) -> str:
    """Fingerprint of an instance transform. Evidence only, never an identity."""
    digest = hashlib.sha256()
    for value in matrix:
        digest.update(('%.*f,' % (decimals, round(float(value), decimals))).encode('ascii'))
    return digest.hexdigest()[:16]


# --------------------------------------------------------------------- helpers
def _structure_key(node_path: str, geometry_key: str, primitive_key: str,
                   instance_key: str) -> tuple:
    return (node_path, geometry_key, primitive_key, instance_key)


def _content_key(node_path: str, geometry_fingerprint: str, primitive_key: str,
                 instance_key: str) -> tuple:
    return (node_path, geometry_fingerprint, primitive_key, instance_key)


def _index_unique(items: Iterable, field: str, what: str) -> dict:
    """Index by id and refuse duplicates: the one-to-one contract starts here."""
    index = {}
    for item in items:
        key = getattr(item, field)
        if not key:
            raise MappingContractError(f'{what} 缺少 {field}')
        if key in index:
            raise MappingContractError(f'{what} 的 {field} 重复：{key}')
        index[key] = item
    return index


def _evidence_of(binding, bindings_evidence: Mapping[str, MatchEvidence]) -> MatchEvidence:
    supplied = bindings_evidence.get(getattr(binding, 'binding_id'))
    if supplied is not None:
        return supplied
    return MatchEvidence(
        geometry_fingerprint=getattr(binding, 'geometry_fingerprint', '') or '',
        content_fingerprint=getattr(binding, 'content_fingerprint', '') or '',
    )


# --------------------------------------------------------------- build_slots
def build_slots(bindings: Iterable, make_native: Callable[[object], str | None], *,
                node_path_of: Optional[Callable[[object], str]] = None,
                evidence_of: Optional[Callable[[object], MatchEvidence]] = None,
                fingerprint_of: Optional[Callable[[object], str]] = None) -> BindingMap:
    """Create exactly one native object per binding and record the pairing.

    ``make_native`` returning an id that was already handed out is a failure, not a
    silent alias: two bindings must never share one render object.
    """
    node_path_of = node_path_of or (lambda binding: getattr(binding, 'node_path', ''))
    if evidence_of is None:
        if fingerprint_of is not None:
            evidence_of = lambda binding: MatchEvidence(  # noqa: E731
                geometry_fingerprint=fingerprint_of(binding))
        else:
            evidence_of = lambda binding: MatchEvidence()  # noqa: E731

    listed = list(bindings)
    _index_unique(listed, 'binding_id', '使用点')

    slots = []
    unmatched = []
    used: set[str] = set()
    for binding in listed:
        binding_id = binding.binding_id
        try:
            native_id = make_native(binding)
        except Exception as error:  # noqa: BLE001 - reported, never silently swallowed
            unmatched.append(Unmatched(MatchFailure.MISSING_NATIVE,
                                       f'原生对象创建失败：{error}', binding_id=binding_id))
            continue
        if not native_id:
            unmatched.append(Unmatched(MatchFailure.MISSING_NATIVE,
                                       '原生对象未创建', binding_id=binding_id))
            continue
        native_id = str(native_id)
        if native_id in used:
            unmatched.append(Unmatched(
                MatchFailure.DUPLICATE_NATIVE_ID,
                f'该原生句柄已被其他使用点占用：{native_id}', binding_id=binding_id,
                native_id=native_id))
            continue
        used.add(native_id)
        evidence = evidence_of(binding)
        slots.append(NativeSlot(
            binding_id=binding_id,
            native_id=native_id,
            node_path=node_path_of(binding),
            geometry_key=getattr(binding, 'geometry_key', ''),
            primitive_key=getattr(binding, 'primitive_key', ''),
            instance_key=getattr(binding, 'instance_key', ''),
            geometry_fingerprint=evidence.geometry_fingerprint,
            content_fingerprint=evidence.content_fingerprint,
            paired_by='constructed',
        ))
    return BindingMap(slots=tuple(slots), unmatched=tuple(unmatched))


# ------------------------------------------------------------------ reconcile
def reconcile(bindings: Iterable, natives: Iterable[NativeObject], *,
              bindings_evidence: Optional[Mapping[str, MatchEvidence]] = None,
              fingerprints: Optional[Mapping[str, str]] = None) -> BindingMap:
    """Pair existing native objects with bindings, one to one.

    ``fingerprints`` is a convenience for the geometry digest only; pass
    ``bindings_evidence`` when the content digest and instance identity are known.
    """
    listed = list(bindings)
    objects = list(natives)
    _index_unique(listed, 'binding_id', '使用点')
    _index_unique(objects, 'native_id', '原生对象')

    evidence_map: dict[str, MatchEvidence] = dict(bindings_evidence or {})
    for binding_id, value in (fingerprints or {}).items():
        evidence_map[binding_id] = MatchEvidence(
            geometry_fingerprint=value,
            content_fingerprint=evidence_map.get(binding_id, MatchEvidence()).content_fingerprint)

    by_structure: dict[tuple, list[NativeObject]] = {}
    for native in objects:
        by_structure.setdefault(
            _structure_key(native.node_path, native.geometry_key, native.primitive_key,
                           native.instance_key), []).append(native)

    slots = []
    unmatched = []
    claimed: dict[str, str] = {}
    for binding in listed:
        binding_id = binding.binding_id
        instance_key = getattr(binding, 'instance_key', '') or ''
        key = _structure_key(getattr(binding, 'node_path', '') or '',
                             getattr(binding, 'geometry_key', '') or '',
                             getattr(binding, 'primitive_key', '') or '', instance_key)
        candidates = by_structure.get(key, [])
        if not candidates:
            unmatched.append(Unmatched(MatchFailure.MISSING_NATIVE,
                                       '该使用点在渲染侧没有对象', binding_id=binding_id))
            continue
        if len(candidates) > 1:
            unmatched.append(Unmatched(
                MatchFailure.AMBIGUOUS_NATIVE,
                f'结构键对应 {len(candidates)} 个原生对象，拒绝猜测',
                binding_id=binding_id))
            continue
        native = candidates[0]
        if native.native_id in claimed:
            unmatched.append(Unmatched(
                MatchFailure.SHARED_NATIVE_OBJECT,
                f'同一个原生对象被两个使用点争用：{claimed[native.native_id]} / {binding_id}',
                binding_id=binding_id, native_id=native.native_id))
            continue
        expected = _evidence_of(binding, evidence_map)
        if (expected.geometry_fingerprint and native.geometry_fingerprint
                and expected.geometry_fingerprint != native.geometry_fingerprint):
            unmatched.append(Unmatched(
                MatchFailure.GEOMETRY_MISMATCH,
                f'几何内容与源清单不一致：{expected.geometry_fingerprint[:8]}'
                f' != {native.geometry_fingerprint[:8]}',
                binding_id=binding_id, native_id=native.native_id))
            continue
        if (expected.content_fingerprint and native.content_fingerprint
                and expected.content_fingerprint != native.content_fingerprint):
            unmatched.append(Unmatched(
                MatchFailure.CONTENT_MISMATCH,
                f'UV/法线/材质分组与源清单不一致：{expected.content_fingerprint[:8]}'
                f' != {native.content_fingerprint[:8]}',
                binding_id=binding_id, native_id=native.native_id))
            continue
        claimed[native.native_id] = binding_id
        slots.append(NativeSlot(
            binding_id=binding_id,
            native_id=native.native_id,
            node_path=native.node_path,
            geometry_key=native.geometry_key,
            primitive_key=native.primitive_key,
            instance_key=native.instance_key,
            geometry_fingerprint=native.geometry_fingerprint,
            content_fingerprint=native.content_fingerprint,
            paired_by='structure',
        ))

    for native in objects:
        if native.native_id not in claimed:
            unmatched.append(Unmatched(MatchFailure.MISSING_BINDING,
                                       '渲染对象在源清单里没有对应使用点',
                                       native_id=native.native_id))
    return BindingMap(slots=tuple(slots), unmatched=tuple(unmatched))


# ------------------------------------------------------------------ carry_over
def carry_over(previous: BindingMap, bindings: Iterable,
               assignments: Mapping[str, str], *,
               bindings_evidence: Optional[Mapping[str, MatchEvidence]] = None,
               fingerprints: Optional[Mapping[str, str]] = None) -> CarryResult:
    """Move committed assignments onto a re-parsed revision.

    What travels is the working material, not the render object. The same
    ``binding_id`` is only trusted after its content is checked again; when the id
    survived but the geometry, content or instance identity changed, the assignment
    is refused with ``CONTENT_CHANGED``/``IDENTITY_CHANGED`` instead of being pointed
    at whatever now sits at that id.
    """
    listed = list(bindings)
    _index_unique(listed, 'binding_id', '使用点')

    evidence_map: dict[str, MatchEvidence] = dict(bindings_evidence or {})
    for binding_id, value in (fingerprints or {}).items():
        evidence_map[binding_id] = MatchEvidence(
            geometry_fingerprint=value,
            content_fingerprint=evidence_map.get(binding_id, MatchEvidence()).content_fingerprint)

    by_id = {slot.binding_id: slot for slot in previous.slots}
    by_content: dict[tuple, list[NativeSlot]] = {}
    for slot in previous.slots:
        by_content.setdefault(
            _content_key(slot.node_path, slot.geometry_fingerprint, slot.primitive_key,
                         slot.instance_key), []).append(slot)

    transfers = []
    unmatched = []
    unassigned = []
    for binding in listed:
        binding_id = binding.binding_id
        evidence = _evidence_of(binding, evidence_map)
        instance_key = getattr(binding, 'instance_key', '') or ''
        node_path = getattr(binding, 'node_path', '') or ''
        primitive_key = getattr(binding, 'primitive_key', '') or ''

        previous_slot = by_id.get(binding_id)
        if previous_slot is not None:
            changed = _identity_or_content_change(previous_slot, node_path, primitive_key,
                                                 instance_key, evidence)
            if changed is not None:
                unmatched.append(Unmatched(changed,
                                           _change_detail(changed, binding_id),
                                           binding_id=binding_id))
                continue
            working_id = assignments.get(binding_id)
            if working_id is None:
                unassigned.append(binding_id)
                continue
            transfers.append(AssignmentTransfer(
                binding_id=binding_id, previous_binding_id=binding_id,
                working_id=working_id, matched_by='identity+content'))
            continue

        key = _content_key(node_path, evidence.geometry_fingerprint, primitive_key,
                           instance_key)
        candidates = by_content.get(key, [])
        if len(candidates) == 1:
            found = candidates[0]
            working_id = assignments.get(found.binding_id)
            if working_id is None:
                unassigned.append(binding_id)
                continue
            transfers.append(AssignmentTransfer(
                binding_id=binding_id, previous_binding_id=found.binding_id,
                working_id=working_id, matched_by='content'))
            continue
        if len(candidates) > 1:
            unmatched.append(Unmatched(
                MatchFailure.FINGERPRINT_COLLISION,
                f'内容键命中 {len(candidates)} 个旧使用点，拒绝自动承接',
                binding_id=binding_id))
            continue
        unmatched.append(Unmatched(MatchFailure.MISSING_BINDING,
                                   '新修订里找不到对应旧使用点', binding_id=binding_id))
    return CarryResult(transfers=tuple(transfers), unmatched=tuple(unmatched),
                       unassigned=tuple(unassigned))


def _identity_or_content_change(previous_slot: NativeSlot, node_path: str,
                               primitive_key: str, instance_key: str,
                               evidence: MatchEvidence) -> Optional[MatchFailure]:
    if node_path and previous_slot.node_path and node_path != previous_slot.node_path:
        return MatchFailure.IDENTITY_CHANGED
    if previous_slot.primitive_key and primitive_key != previous_slot.primitive_key:
        return MatchFailure.IDENTITY_CHANGED
    if previous_slot.instance_key and instance_key != previous_slot.instance_key:
        return MatchFailure.IDENTITY_CHANGED
    if (evidence.geometry_fingerprint and previous_slot.geometry_fingerprint
            and evidence.geometry_fingerprint != previous_slot.geometry_fingerprint):
        return MatchFailure.CONTENT_CHANGED
    if (evidence.content_fingerprint and previous_slot.content_fingerprint
            and evidence.content_fingerprint != previous_slot.content_fingerprint):
        return MatchFailure.CONTENT_CHANGED
    return None


def _change_detail(failure: MatchFailure, binding_id: str) -> str:
    if failure is MatchFailure.IDENTITY_CHANGED:
        return f'同 ID 但节点/primitive/实例身份已变，拒绝沿用旧分配：{binding_id}'
    return f'同 ID 但几何或内容已变，拒绝沿用旧分配：{binding_id}'


def native_objects_from_rows(rows: Iterable[Mapping]) -> tuple[NativeObject, ...]:
    """Helper for evidence scripts: build :class:`NativeObject` from plain dicts."""
    return tuple(NativeObject(
        native_id=str(row['native_id']),
        node_path=str(row.get('node_path', '')),
        geometry_key=str(row.get('geometry_key', '')),
        primitive_key=str(row.get('primitive_key', '')),
        instance_key=str(row.get('instance_key', '')),
        geometry_fingerprint=str(row.get('geometry_fingerprint', '')),
        content_fingerprint=str(row.get('content_fingerprint', '')),
        material_slot=str(row.get('material_slot', '')),
    ) for row in rows)
