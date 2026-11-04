"""Dependency-injection ports. Implement these without putting IO in widgets."""
from typing import Protocol

from .contracts import (
    ModelAppearance, ModelCapabilities, ModelJobRequest, ModelPage, ModelQuery,
    ModelRecord,
)


class ModelRepository(Protocol):
    """Project-owned metadata; integrate existing full AND partial saves."""

    def query(self, query: ModelQuery) -> ModelPage: ...

    def get(self, model_id: str) -> ModelRecord | None: ...

    def save(self, record: ModelRecord, *, expected_revision: int) -> None: ...


class ModelJobController(Protocol):
    """Owner of subprocess lifetime, queue budgets and stale-result rejection."""

    def submit(self, request: ModelJobRequest) -> str: ...

    def cancel(self, job_id: str) -> None: ...

    def close_project(self, project_session: str) -> None: ...


class ModelImportPort(Protocol):
    """Collect and copy dependencies in the background; publish atomically."""

    def import_paths(self, paths: tuple[str, ...], *, folder_id: str | None) -> str: ...


class ModelExportPort(Protocol):
    """Export immutable originals and dependencies; NEVER export the preview."""

    def export_originals(self, model_ids: tuple[str, ...], destination: str) -> str: ...


class ModelPreviewPort(Protocol):
    """Independent F3D host. No QWidget, scene or DB objects across this boundary."""

    @property
    def capabilities(self) -> ModelCapabilities: ...

    def open(self, request: ModelJobRequest) -> str: ...

    def update_appearance(self, request: ModelJobRequest,
                          appearance: ModelAppearance) -> str: ...
