"""Proposed v1 handoff contracts, NOT a new on-disk schema.

Tuples and JSON strings keep requests detached from GUI-owned dictionaries.
Serialization, schema validation, migration and capability verification remain
implementation tasks. IDs must never be derived from a renderer actor index.
"""
from dataclasses import dataclass
from enum import Enum

CONTRACT_VERSION = 1


@dataclass(frozen=True)
class MaterialTarget:
    project_session: str
    open_epoch: int
    model_id: str
    source_revision: int
    catalog_revision: int
    state_revision: int


@dataclass(frozen=True)
class TextureUse:
    slot: str
    resource_id: str
    channels: str
    texcoord: int
    color_space: str
    uv_transform: tuple[float, ...] = ()
    normal_convention: str = ''
    source_locator: str = ''
    unknown_json: str = '{}'


@dataclass(frozen=True)
class MaterialDefinition:
    material_id: str
    source_key: str
    name: str
    workflow: str
    textures: tuple[TextureUse, ...] = ()
    properties_json: str = '{}'
    unsupported: tuple[str, ...] = ()
    unknown_json: str = '{}'


@dataclass(frozen=True)
class BindingSite:
    binding_id: str
    source_key: str
    node_path: str
    geometry_key: str
    primitive_key: str
    original_material_id: str | None
    resolution: str = 'unknown'
    instance_key: str = ''
    geometry_fingerprint: str = ''
    content_fingerprint: str = ''
    match_rules_version: int = 2


@dataclass(frozen=True)
class MaterialCatalog:
    schema_version: int
    revision: int
    source_digest: str
    parser_id: str
    definitions: tuple[MaterialDefinition, ...]
    binding_sites: tuple[BindingSite, ...]
    unresolved: tuple[str, ...] = ()
    unknown_json: str = '{}'


class PatchAction(str, Enum):
    INHERIT = 'inherit'
    SET = 'set'
    CLEAR = 'clear'


@dataclass(frozen=True)
class TexturePatch:
    slot: str
    action: PatchAction
    value: TextureUse | None = None


@dataclass(frozen=True)
class WorkingMaterial:
    working_id: str
    source_material_id: str | None
    display_name: str
    # A self-contained library snapshot takes precedence over the original
    # source definition as the inheritance base. Provenance is not that base.
    base_snapshot_ref: str | None = None
    texture_patches: tuple[TexturePatch, ...] = ()
    property_patch_json: str = '{}'
    provenance_json: str = '{}'
    unknown_json: str = '{}'


@dataclass(frozen=True)
class MaterialAssignment:
    binding_id: str
    working_id: str


@dataclass(frozen=True)
class MaterialState:
    target: MaterialTarget
    materials: tuple[WorkingMaterial, ...]
    assignments: tuple[MaterialAssignment, ...]
    legacy_override_json: str = '{}'
    unknown_json: str = '{}'


class MaterialOperation(str, Enum):
    PATCH = 'patch'
    REPLACE_SURFACE = 'replace_surface'
    REPLACE_FULL = 'replace_full'
    MAKE_UNIQUE = 'make_unique'
    RESTORE_MATERIAL = 'restore_material'
    RESTORE_MODEL = 'restore_model'
    RESTORE_PREVIEW = 'restore_preview'
    PASTE = 'paste'
    UNDO = 'undo'
    REDO = 'redo'


@dataclass(frozen=True)
class MaterialEditRequest:
    request_id: str
    gesture_id: str
    target: MaterialTarget
    operation: MaterialOperation
    working_ids: tuple[str, ...] = ()
    binding_ids: tuple[str, ...] = ()
    parameters_json: str = '{}'
    contract_version: int = CONTRACT_VERSION


@dataclass(frozen=True)
class MaterialCapabilityReport:
    read_catalog: bool = False
    edit_material: bool = False
    assign_material: bool = False
    pick_material: bool = False
    normal_override: bool = False
    sss: bool = False
    displacement: bool = False
    wireframe: bool = False
    reason: str = '模型多材质服务尚未接入'


@dataclass(frozen=True)
class MaterialCommitReceipt:
    request_id: str
    before: MaterialTarget
    after: MaterialTarget
    command_id: str


@dataclass(frozen=True)
class PreparedMaterialView:
    request_id: str
    target: MaterialTarget
    draft_generation: int
    artifact_ref: str
    lease_id: str


@dataclass(frozen=True)
class MaterialRenderReceipt:
    request_id: str
    target: MaterialTarget
    draft_generation: int
    frame_id: str
