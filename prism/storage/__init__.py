"""Storage contract layer for the storage-20261025 batch.

Importing this package must stay cheap and side-effect free: it is imported
by code that runs before Qt is set up, and by tooling that never opens a
window.  Nothing here imports Qt, creates a QApplication, or opens a
database.

See ``prism/storage/contracts.py`` for the vocabulary and
``docs/dispatch-storage-20261025/01-强制协作规则.md`` for the rules this
package has to satisfy.
"""
from __future__ import annotations

from .contracts import (
    CONTRACT_OWNER,
    CONTRACT_STATUS,
    CONTRACT_VERSION,
    AckState,
    CommitState,
    DurableState,
    NEW_TYPES,
    REUSED_TYPES,
    ProjectIdentity,
    SaveReceipt,
    SourceKind,
    SourceRef,
    SourceState,
    StorageError,
    StorageErrorKind,
    Terminal,
    contract_manifest,
    identity_of,
    receipt_of,
)
from .project_backend import (
    BackendKind,
    BackendProbe,
    BackendRouter,
    Capability,
    ProjectBackend,
    ProjectSnapshot,
)
from .source_ref import (
    MEDIA_ROLES,
    ResolvedSource,
    content_revision_of,
    external_link_ref,
    legacy_attachment_ref,
    legacy_page_ref,
    managed_object_ref,
    resolve_legacy_attachment,
    resolve_legacy_page,
)

__all__ = [
    "CONTRACT_OWNER", "CONTRACT_STATUS", "CONTRACT_VERSION",
    "AckState", "CommitState", "DurableState", "NEW_TYPES", "REUSED_TYPES",
    "ProjectIdentity", "SaveReceipt", "SourceKind", "SourceRef", "SourceState",
    "StorageError", "StorageErrorKind", "Terminal", "contract_manifest",
    "identity_of", "receipt_of",
    "BackendKind", "BackendProbe", "BackendRouter", "Capability",
    "ProjectBackend", "ProjectSnapshot",
    "MEDIA_ROLES", "ResolvedSource", "content_revision_of",
    "external_link_ref", "legacy_attachment_ref", "legacy_page_ref",
    "managed_object_ref", "resolve_legacy_attachment", "resolve_legacy_page",
]
