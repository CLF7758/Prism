"""Validation and request interpretation for the multi-material state.

Two jobs, both pure (no database, no Qt, no renderer):

1. **State validation** -- a state that cannot be explained is rejected, never
   "repaired on save".  Every failure carries the error code the UI shows.
2. **Request interpretation** -- one target entry point (``working_ids`` or
   ``binding_ids``), no second copy of the targets inside ``parameters_json``,
   and the semantics that must not be the caller's choice: what a surface
   replacement keeps, what a full replacement means, and how many copies
   ``make_unique`` produces.

The rules encoded here are the ones the RFC fixes; if a caller could pass
``keep=[]`` or ``apply_defaults=false``, "preserve transparency" would become a
silent option instead of a guarantee.
"""
from __future__ import annotations

import json
from typing import Iterable, Mapping, Optional

from . import vocabulary
from .catalog import BINDING_RESOLUTIONS, binding_resolutions
from .contracts import (
    MaterialCatalog,
    MaterialEditRequest,
    MaterialOperation,
    MaterialState,
    WorkingMaterial,
)

#: Error codes.  Stable strings: the UI matches on them, so they are not prose.
E_PARAM_INVALID = 'E_PARAM_INVALID'
E_UNKNOWN_BINDING = 'E_UNKNOWN_BINDING'
E_UNKNOWN_MATERIAL = 'E_UNKNOWN_MATERIAL'
E_SLOT_READ_ONLY = 'E_SLOT_READ_ONLY'
E_RESOURCE_MISSING = 'E_RESOURCE_MISSING'
E_ACTION_INVALID = 'E_ACTION_INVALID'
E_STALE_REVISION = 'E_STALE_REVISION'
E_CATALOG_MISMATCH = 'E_CATALOG_MISMATCH'
E_TARGET_MISSING = 'E_TARGET_MISSING'
E_REQUEST_CONFLICT = 'E_REQUEST_CONFLICT'
E_STATE_INVALID = 'E_STATE_INVALID'

#: What a "replace surface" edit keeps.  Fixed by the contract, not by the
#: caller: transparency/cutout, two-sidedness, SSS, displacement and the geometry
#: assignment survive a colour/normal/roughness/metalness/AO/emissive swap.
SURFACE_REPLACEMENT_KEEPS = ('opacity', 'double_sided', 'sss', 'displacement')

#: Slots a surface replacement adopts from the library material.
SURFACE_REPLACEMENT_SLOTS = ('basecolor', 'normal', 'roughness', 'metallic',
                             'ao', 'emissive')

#: Allowed ``parameters_json`` keys per operation.  A key that is not here is
#: rejected: silently ignoring a parameter means editing something else than the
#: caller asked for.  Targets are never repeated here -- they arrive as
#: ``working_ids``/``binding_ids``.
REQUEST_PARAMETERS = {
    MaterialOperation.PATCH: frozenset({
        'slot', 'action', 'resource_id', 'value', 'texcoord', 'channels',
        'color_space', 'uv_transform', 'normal_convention'}),
    MaterialOperation.REPLACE_SURFACE: frozenset({
        'library_material_id', 'library_revision', 'source_digest'}),
    MaterialOperation.REPLACE_FULL: frozenset({
        'library_material_id', 'library_revision', 'source_digest'}),
    MaterialOperation.MAKE_UNIQUE: frozenset({'name'}),
    MaterialOperation.RESTORE_MATERIAL: frozenset(),
    MaterialOperation.RESTORE_MODEL: frozenset(),
}

#: Operations that change a material's content only, and operations that change
#: assignments.  ``restore_material`` never rebinds: restoring a binding is an
#: explicit, separate operation.
CONTENT_OPERATIONS = frozenset({MaterialOperation.PATCH,
                               MaterialOperation.REPLACE_SURFACE,
                               MaterialOperation.REPLACE_FULL,
                               MaterialOperation.RESTORE_MATERIAL})
ASSIGNMENT_OPERATIONS = frozenset({MaterialOperation.MAKE_UNIQUE,
                                   MaterialOperation.RESTORE_MODEL})


class MaterialValidationError(RuntimeError):
    """A state or a request that must be refused, with a machine-readable code."""

    def __init__(self, code: str, message: str, *, detail: Optional[Mapping] = None):
        super().__init__(f'{code}: {message}')
        self.code = code
        self.message = message
        self.detail = dict(detail or {})

    def as_dict(self) -> dict:
        return {'code': self.code, 'message': self.message, 'detail': self.detail}


# ---------------------------------------------------------------------------
# catalog / state validation
# ---------------------------------------------------------------------------
def validate_catalog(catalog: MaterialCatalog) -> None:
    """A catalog that a consumer can address: unique ids, resolvable references."""
    seen = set()
    for definition in catalog.definitions:
        if not definition.material_id:
            raise MaterialValidationError(E_STATE_INVALID, '材质缺少定位')
        if definition.material_id in seen:
            raise MaterialValidationError(
                E_STATE_INVALID, f'材质定位重复：{definition.material_id}')
        seen.add(definition.material_id)
        slots = set()
        for use in definition.textures:
            if vocabulary.classify(use.slot) == 'unknown':
                raise MaterialValidationError(
                    E_STATE_INVALID, f'未知的贴图槽位：{use.slot!r}',
                    detail={'material_id': definition.material_id})
            if use.slot in slots:
                raise MaterialValidationError(
                    E_STATE_INVALID, f'材质 {definition.material_id} 重复声明槽位 {use.slot}')
            slots.add(use.slot)
    bindings = set()
    for binding in catalog.binding_sites:
        if not binding.binding_id:
            raise MaterialValidationError(E_STATE_INVALID, '使用点缺少定位')
        if binding.binding_id in bindings:
            raise MaterialValidationError(
                E_STATE_INVALID, f'使用点定位重复：{binding.binding_id}')
        bindings.add(binding.binding_id)
        if binding.original_material_id and binding.original_material_id not in seen:
            raise MaterialValidationError(
                E_UNKNOWN_MATERIAL, f'使用点引用了不存在的材质：{binding.original_material_id}',
                detail={'binding_id': binding.binding_id})


def validate_state(state: MaterialState, catalog: MaterialCatalog, *,
                   resources: Optional[Iterable[str]] = None,
                   snapshots: Optional[Iterable[str]] = None,
                   verified_slots: Optional[Iterable[str]] = None) -> None:
    """Every field of a state must be explainable from the catalog.

    ``resources`` is the set of resource ids the project actually holds; when it
    is given, a texture that points at a resource nobody stores is refused
    instead of being written as a broken link.

    ``verified_slots`` is what the native runtime has proven it can edit.
    ``None`` means "this check does not apply here" (a state that is being
    re-validated after reload); an **empty** collection means "nothing is
    editable yet" and refuses every ``set`` patch, which is the honest default
    on a backend that has proven nothing.
    """
    validate_catalog(catalog)
    definitions = {item.material_id: item for item in catalog.definitions}
    binding_ids = {item.binding_id for item in catalog.binding_sites}
    known_resources = set(resources) if resources is not None else None
    known_snapshots = set(snapshots) if snapshots is not None else None

    working_ids = set()
    for material in state.materials:
        if not material.working_id:
            raise MaterialValidationError(E_STATE_INVALID, '工作材质缺少 id')
        if material.working_id in working_ids:
            raise MaterialValidationError(
                E_STATE_INVALID, f'工作材质 id 重复：{material.working_id}')
        working_ids.add(material.working_id)
        if material.source_material_id and material.source_material_id not in definitions:
            raise MaterialValidationError(
                E_UNKNOWN_MATERIAL,
                f'工作材质引用了不存在的源材质：{material.source_material_id}',
                detail={'working_id': material.working_id})
        if material.base_snapshot_ref and known_snapshots is not None \
                and material.base_snapshot_ref not in known_snapshots:
            raise MaterialValidationError(
                E_RESOURCE_MISSING,
                f'继承基础快照不存在：{material.base_snapshot_ref}',
                detail={'working_id': material.working_id})
        _validate_patches(material, known_resources, verified_slots)

    assigned = set()
    for assignment in state.assignments:
        if assignment.binding_id not in binding_ids:
            raise MaterialValidationError(
                E_UNKNOWN_BINDING, f'分配指向不存在的使用点：{assignment.binding_id}')
        if assignment.binding_id in assigned:
            raise MaterialValidationError(
                E_STATE_INVALID, f'使用点被分配了两次：{assignment.binding_id}')
        assigned.add(assignment.binding_id)
        if assignment.working_id not in working_ids:
            raise MaterialValidationError(
                E_UNKNOWN_MATERIAL,
                f'分配指向不存在的工作材质：{assignment.working_id}',
                detail={'binding_id': assignment.binding_id})
    resolutions = binding_resolutions(catalog)
    for binding_id, resolution in resolutions.items():
        if resolution == 'dangling' and binding_id in assigned:
            raise MaterialValidationError(
                E_UNKNOWN_MATERIAL,
                f'悬空的使用点不能被分配：{binding_id}',
                detail={'binding_id': binding_id})


def _validate_patches(material: WorkingMaterial, known_resources,
                      verified_slots) -> None:
    seen_slots = set()
    for patch in material.texture_patches:
        slot = patch.slot
        kind = vocabulary.classify(slot)
        if kind == 'unknown':
            raise MaterialValidationError(
                E_STATE_INVALID, f'未知的贴图槽位：{slot!r}',
                detail={'working_id': material.working_id})
        if slot in seen_slots:
            raise MaterialValidationError(
                E_STATE_INVALID, f'同一槽位被修改两次：{slot}',
                detail={'working_id': material.working_id})
        seen_slots.add(slot)
        action = getattr(patch.action, 'value', patch.action)
        if action not in ('set', 'inherit', 'clear'):
            raise MaterialValidationError(
                E_ACTION_INVALID, f'未知的三态动作：{action!r}',
                detail={'slot': slot})
        if action == 'set':
            if kind == 'source_role':
                raise MaterialValidationError(
                    E_SLOT_READ_ONLY,
                    f'{slot} 是源文件角色，不能作为可编辑目标',
                    detail={'slot': slot})
            if verified_slots is not None and slot not in set(verified_slots):
                raise MaterialValidationError(
                    E_SLOT_READ_ONLY,
                    f'槽位 {slot} 尚未被原生运行时验证为可编辑',
                    detail={'slot': slot})
            if patch.value is None:
                raise MaterialValidationError(
                    E_PARAM_INVALID, f'槽位 {slot} 的 set 动作缺少 value',
                    detail={'slot': slot})
            resource_id = getattr(patch.value, 'resource_id', '') or ''
            if known_resources is not None and resource_id \
                    and resource_id not in known_resources:
                raise MaterialValidationError(
                    E_RESOURCE_MISSING, f'材质引用的资源不在工程里：{resource_id}',
                    detail={'slot': slot, 'resource_id': resource_id})
        elif patch.value is not None:
            raise MaterialValidationError(
                E_PARAM_INVALID,
                f'槽位 {slot} 的 {action} 动作不能带 value',
                detail={'slot': slot})


def validate_transition(before: MaterialState, after: MaterialState) -> None:
    """A commit may add or change, but the target identity and revision must hold."""
    if before.target != after.target:
        raise MaterialValidationError(
            E_STALE_REVISION, '提交前后的目标身份不一致',
            detail={'before': repr(before.target), 'after': repr(after.target)})
    if int(after.target.state_revision) <= int(before.target.state_revision):
        raise MaterialValidationError(
            E_STALE_REVISION,
            '提交后的状态修订必须递增',
            detail={'before': before.target.state_revision,
                    'after': after.target.state_revision})


# ---------------------------------------------------------------------------
# requests
# ---------------------------------------------------------------------------
def parse_parameters(request: MaterialEditRequest) -> dict:
    """The request's parameters, with unknown or duplicated targets refused."""
    raw = request.parameters_json or '{}'
    try:
        parameters = json.loads(raw)
    except ValueError as error:
        raise MaterialValidationError(
            E_PARAM_INVALID, f'parameters_json 不是合法 JSON：{error}') from error
    if not isinstance(parameters, dict):
        raise MaterialValidationError(E_PARAM_INVALID, 'parameters_json 必须是对象')
    allowed = REQUEST_PARAMETERS.get(request.operation)
    if allowed is None:
        raise MaterialValidationError(
            E_PARAM_INVALID, f'未知的操作：{request.operation!r}')
    extra = sorted(set(parameters) - set(allowed))
    if extra:
        raise MaterialValidationError(
            E_PARAM_INVALID,
            '参数不被该操作接受（目标请用 working_ids/binding_ids）：'
            + ', '.join(extra),
            detail={'operation': str(request.operation), 'rejected': extra})
    return dict(parameters)


def resolve_targets(request: MaterialEditRequest, catalog: MaterialCatalog,
                    state: MaterialState) -> tuple:
    """The targets of one request, from **one** place: the request's id lists.

    ``working_ids`` and ``binding_ids`` are not interchangeable for every
    operation: a content edit addresses materials, an assignment edit addresses
    usage points.  Using the wrong list is an error, not something to guess.
    """
    bindings = {item.binding_id for item in catalog.binding_sites}
    materials = {item.working_id for item in state.materials}
    resolutions = binding_resolutions(catalog)

    if request.operation in CONTENT_OPERATIONS:
        if request.binding_ids:
            raise MaterialValidationError(
                E_PARAM_INVALID,
                f'{getattr(request.operation, "value", request.operation)} 只接受 working_ids',
                detail={'binding_ids': list(request.binding_ids)})
        targets = tuple(request.working_ids)
        if not targets:
            raise MaterialValidationError(E_TARGET_MISSING, '请求没有目标材质')
        for working_id in targets:
            if working_id not in materials:
                raise MaterialValidationError(
                    E_UNKNOWN_MATERIAL, f'未知的工作材质：{working_id}')
        return targets

    targets = tuple(request.binding_ids or request.working_ids)
    if not targets:
        raise MaterialValidationError(E_TARGET_MISSING, '请求没有目标使用点')
    for target in targets:
        if target not in bindings:
            raise MaterialValidationError(E_UNKNOWN_BINDING, f'未知的使用点：{target}')
        if resolutions.get(target) == 'dangling':
            raise MaterialValidationError(
                E_UNKNOWN_MATERIAL,
                f'使用点 {target} 指向文件里不存在的材质，不能作为编辑目标',
                detail={'binding_id': target})
    return targets


def request_signature(request: MaterialEditRequest,
                      parameters: Optional[Mapping] = None) -> str:
    """A content digest for idempotency: same id + same content = one effect.

    A retry after a lost receipt must not apply twice, and a *different* request
    that happens to reuse an id must not be mistaken for that retry.  The digest
    covers the operation, the target identity and the interpreted parameters.
    """
    values = parameters if parameters is not None else parse_parameters(request)
    payload = {
        'contract_version': int(request.contract_version),
        'operation': str(getattr(request.operation, 'value', request.operation)),
        'target': {
            'project_session': request.target.project_session,
            'open_epoch': int(request.target.open_epoch),
            'model_id': request.target.model_id,
            'source_revision': int(request.target.source_revision),
            'catalog_revision': int(request.target.catalog_revision),
            'state_revision': int(request.target.state_revision),
        },
        'working_ids': sorted(request.working_ids),
        'binding_ids': sorted(request.binding_ids),
        'parameters': _canonical(values),
    }
    import hashlib
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, ensure_ascii=False).encode('utf-8')
    ).hexdigest()


def _canonical(value):
    if isinstance(value, Mapping):
        return {str(key): _canonical(item) for key, item in sorted(value.items())}
    if isinstance(value, (list, tuple)):
        return [_canonical(item) for item in value]
    if isinstance(value, float) and value.is_integer():
        return int(value)
    return value


def check_idempotent(request_id: str, signature: str, recorded: Mapping) -> None:
    """Refuse a reused request id whose content changed."""
    previous = (recorded or {}).get(request_id)
    if previous is None:
        return
    if previous != signature:
        raise MaterialValidationError(
            E_REQUEST_CONFLICT,
            f'请求 {request_id} 已用于不同内容，拒绝把它当成重试',
            detail={'request_id': request_id, 'recorded': previous,
                    'received': signature})


# ---------------------------------------------------------------------------
# make_unique: how many copies, and who shares them
# ---------------------------------------------------------------------------
def plan_make_unique(binding_ids: Iterable[str], state: MaterialState,
                     *, new_id_factory) -> dict:
    """``{binding_id: new working id}`` for "make this part independent".

    Two rules the caller cannot change:

    * targets that already share one material get **one** copy, so "these four
      legs are now one independent material" stays true;
    * targets whose materials differ never merge into one copy -- each distinct
      source material gets its own new material, and the copy count equals the
      number of distinct source materials, not the number of targets.
    """
    assignments = {item.binding_id: item.working_id for item in state.assignments}
    materials = {item.working_id: item for item in state.materials}
    copies: dict = {}
    plan: dict = {}
    for binding_id in binding_ids:
        source_id = assignments.get(binding_id)
        if source_id is None:
            raise MaterialValidationError(
                E_UNKNOWN_BINDING,
                f'使用点没有已提交的材质分配：{binding_id}',
                detail={'binding_id': binding_id})
        source = materials.get(source_id)
        if source is None:
            raise MaterialValidationError(
                E_UNKNOWN_MATERIAL,
                f'分配指向未知材质：{source_id}',
                detail={'binding_id': binding_id})
        if source_id not in copies:
            copies[source_id] = new_id_factory(source)
        plan[binding_id] = copies[source_id]
    return plan
