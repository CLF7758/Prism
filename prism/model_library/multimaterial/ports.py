"""Dependency directions only; Protocol bodies are intentionally unimplemented.

inspect/load/prepare run on background workers. install runs only on the
isolated renderer's owning thread. commit must use the project's transaction
and resource-reference lifecycle, never the GUI's mutable record directly.
"""
from typing import Protocol

from .contracts import (MaterialCatalog, MaterialCapabilityReport,
    MaterialCommitReceipt, MaterialEditRequest, MaterialRenderReceipt,
    MaterialState, MaterialTarget, PreparedMaterialView)


class MaterialCatalogPort(Protocol):
    def inspect(self, target: MaterialTarget, bundle_ref: str) -> MaterialCatalog: ...


class MaterialStatePort(Protocol):
    def load(self, target: MaterialTarget) -> MaterialState: ...

    def commit(self, request: MaterialEditRequest) -> MaterialCommitReceipt: ...


class MaterialPreviewPort(Protocol):
    def capabilities(self, target: MaterialTarget,
                     working_ids: tuple[str, ...]) -> MaterialCapabilityReport: ...

    def prepare(self, request: MaterialEditRequest,
                draft_generation: int) -> PreparedMaterialView: ...

    def install(self, prepared: PreparedMaterialView) -> MaterialRenderReceipt: ...

    def release(self, lease_id: str) -> None: ...


class MaterialJobPort(Protocol):
    """Main-process asynchronous facade. Returning an ID never means success."""
    def inspect(self, target: MaterialTarget) -> str: ...

    def preview(self, request: MaterialEditRequest, draft_generation: int) -> str: ...

    def commit(self, request: MaterialEditRequest) -> str: ...

    def cancel(self, job_id: str) -> None: ...
