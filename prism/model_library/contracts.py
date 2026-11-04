"""Qt-free contracts shared by the UI, project repository and future workers.

These are integration contracts, not a second persistence format. The project
storage owner must implement migration, partial saves and unknown-field retention
before enabling any mutating action.
"""
from dataclasses import dataclass, field
from enum import Enum
from typing import Mapping

PROTOCOL_VERSION = 1
MODEL_EXTENSIONS = frozenset({".fbx", ".obj", ".glb", ".gltf"})


class ModelFeatureUnavailable(RuntimeError):
    """A missing capability must fail explicitly, never return fake success."""


class ModelJobKind(str, Enum):
    IMPORT = "import"
    THUMBNAIL = "thumbnail"
    INSPECT = "inspect"
    PREVIEW = "preview"
    EXPORT_ORIGINALS = "export_originals"


class ModelJobState(str, Enum):
    QUEUED = "queued"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"
    STALE = "stale"


class ModelViewMode(str, Enum):
    MATERIAL = "material"
    MATERIAL_EDGES = "material_edges"
    WIREFRAME = "wireframe"
    BASECOLOR = "basecolor"
    NORMAL = "normal"
    ROUGHNESS = "roughness"
    METALLIC = "metallic"
    AO = "ao"
    OPACITY = "opacity"
    EMISSIVE = "emissive"
    SSS = "sss"
    DISPLACEMENT = "displacement"


@dataclass(frozen=True)
class ModelQuery:
    folder_id: str | None = None
    text: str = ""
    offset: int = 0
    limit: int = 128
    sort: str = "name"
    focus_id: str | None = None  # Locate its page without loading preceding covers.


@dataclass(frozen=True)
class ModelStatistics:
    """None means unknown. Counts refer to loaded geometry, not DCC topology."""
    vertices: int | None = None
    triangles: int | None = None
    polygons: int | None = None
    instances: int | None = None
    basis: str = "unknown"
    backend_version: str = ""


@dataclass(frozen=True)
class ModelFileRef:
    file_id: str
    relative_path: str
    role: str
    size_bytes: int
    sha256: str
    source_ref: str
    missing: bool = False
    original_reference: str = ""
    attachment_id: int | None = None
    origin: str = ""
    absolute_reference: bool = False


@dataclass(frozen=True)
class ModelAppearance:
    revision: int = 0
    # Values use the EXISTING material parameter conventions.
    # The adapter must capture a detached configuration, not live material state.
    bindings: Mapping[str, object] = field(default_factory=dict)
    properties: Mapping[str, object] = field(default_factory=dict)


@dataclass(frozen=True)
class ModelCardSummary:
    model_id: str
    name: str
    format: str
    tags: tuple[str, ...] = ()
    size_bytes: int | None = None
    statistics: ModelStatistics = field(default_factory=ModelStatistics)
    # UI receives a cached data URL, never reads original files to draw a card.
    thumbnail_data_url: str | None = None
    thumbnail_state: str = "missing"
    thumbnail_renderer: str = ""


@dataclass(frozen=True)
class ModelPage:
    items: tuple[ModelCardSummary, ...]
    total: int
    offset: int = 0


@dataclass(frozen=True)
class ModelRecord:
    model_id: str
    project_id: str
    name: str
    main_file_id: str
    format: str
    source_revision: int = 0
    record_revision: int = 0
    folder_id: str | None = None
    tags: tuple[str, ...] = ()
    notes: str = ""
    files: tuple[ModelFileRef, ...] = ()
    appearance: ModelAppearance = field(default_factory=ModelAppearance)
    statistics: ModelStatistics = field(default_factory=ModelStatistics)
    unknown_fields: Mapping[str, object] = field(default_factory=dict)


@dataclass(frozen=True)
class ModelRequestIdentity:
    """All fields must match at publication; project_id alone is insufficient."""
    project_session: str
    open_epoch: int
    model_id: str
    request_id: str
    source_revision: int
    appearance_revision: int
    camera_revision: int
    protocol_version: int = PROTOCOL_VERSION


@dataclass(frozen=True)
class ModelJobRequest:
    identity: ModelRequestIdentity
    kind: ModelJobKind
    manifest_ref: str
    output_ref: str | None = None


@dataclass(frozen=True)
class ModelJobResult:
    identity: ModelRequestIdentity
    state: ModelJobState
    output_ref: str | None = None
    error_code: str | None = None
    error_message: str | None = None


@dataclass(frozen=True)
class ModelCapabilities:
    """Only the composition root may enable capabilities after implementation."""
    browse: bool = False
    import_models: bool = False
    export_originals: bool = False
    edit_metadata: bool = False
    edit_appearance: bool = False
    preview: bool = False
    wireframe: bool = False
    create_folders: bool = False
    backend_version: str = ""
