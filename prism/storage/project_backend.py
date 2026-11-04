"""ProjectBackend — the routing seam between "what a project is" and
"where its bytes live".

D02 requires a backend that can probe, snapshot, read header/body, query,
commit and close, and that does **not** treat the full scene as the only
source of truth.  D03 requires that the new routing layer calls the
existing SQLite backend first, so that today's ``.prism`` files keep their
current open/save behaviour.

This module defines the protocol and the small value objects it needs.  It
contains no backend implementation: that is
:mod:`prism.storage.legacy_backend` for the current format.

Pure Python: no Qt, no QApplication, no database.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Mapping, Optional, Protocol, Sequence, Tuple, runtime_checkable

from .contracts import (
    ProjectIdentity,
    SaveReceipt,
    SourceRef,
    StorageError,
    StorageErrorKind,
)


class BackendKind(str, Enum):
    LEGACY_SQLITE = "legacy_sqlite"
    MANAGED_OBJECT_STORE = "managed_object_store"
    UNKNOWN = "unknown"


class Capability(str, Enum):
    """What a backend may be asked to do.

    A caller asks for a capability; the backend answers yes or no.  Nobody
    infers "it will probably work" from a file extension.
    """

    READ = "read"
    WRITE = "write"
    PAGE_BODY = "page_body"
    ATTACHMENT = "attachment"
    QUERY = "query"
    INCREMENTAL_COMMIT = "incremental_commit"
    COMPATIBLE_EXPORT = "compatible_export"


@dataclass(frozen=True)
class BackendProbe:
    """What a probe found out about a path — without opening it for writing."""

    path: str
    kind: BackendKind
    readable: bool
    writable: bool
    application_id: Optional[int] = None
    user_version: Optional[int] = None
    supported: bool = True
    unsupported_reason: Optional[str] = None
    required_features: Tuple[str, ...] = ()
    capabilities: Tuple[Capability, ...] = ()

    def supports(self, capability: Capability) -> bool:
        return capability in self.capabilities

    def as_dict(self) -> dict:
        return {"path": self.path, "kind": self.kind.value,
                "readable": self.readable, "writable": self.writable,
                "application_id": self.application_id,
                "user_version": self.user_version,
                "supported": self.supported,
                "unsupported_reason": self.unsupported_reason,
                "required_features": list(self.required_features),
                "capabilities": [c.value for c in self.capabilities]}


@dataclass(frozen=True)
class ProjectSnapshot:
    """A read-only view of what a project contains at one moment.

    ``source_revision`` is backend-specific. Managed storage should provide
    a committed logical revision. Legacy storage exposes a conservative
    file/WAL/header validation token and declares that basis in ``notes``;
    it must not pretend that token is a per-content digest.
    """

    identity: ProjectIdentity
    source_revision: str
    page_ids: Tuple[str, ...] = ()
    attachment_ids: Tuple[str, ...] = ()
    entity_count: int = 0
    read_only: bool = True
    notes: Mapping[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict:
        return {"identity": self.identity.as_dict(),
                "source_revision": self.source_revision,
                "page_ids": list(self.page_ids),
                "attachment_ids": list(self.attachment_ids),
                "entity_count": self.entity_count,
                "read_only": self.read_only, "notes": dict(self.notes)}


@runtime_checkable
class ProjectBackend(Protocol):
    """The seam every backend implements.

    Implementations must not mutate the live scene while reading: reading a
    page body that the user never opened is a *read* of the file, not an
    edit of the project.
    """

    def probe(self, path: str) -> BackendProbe:
        """Find out what is at ``path`` without writing anything."""

    def snapshot(self) -> ProjectSnapshot:
        """A consistent read-only view of the project."""

    def read_source(self, ref: SourceRef) -> bytes:
        """The bytes of one source, or raise a typed :class:`StorageError`."""

    def query(self, **criteria) -> Sequence[str]:
        """Candidate ids for the given criteria."""

    def commit(self, **kwargs) -> SaveReceipt:
        """Commit a change and say exactly what reached the disk."""

    def close(self) -> None:
        """Stop accepting work, drain, then release resources."""


class BackendRouter:
    """Chooses a backend for a path.

    The router is the only place that knows which backend exists.  Business
    code asks the router for a backend and then talks to the protocol, so
    no module has to scatter ``if <directory layout>`` checks around.
    """

    def __init__(self, backends: Optional[Sequence[Any]] = None):
        self._backends = list(backends or [])

    def register(self, backend: Any) -> None:
        self._backends.append(backend)

    def probe(self, path: str) -> BackendProbe:
        for backend in self._backends:
            probe = backend.probe(path)
            if probe.kind is not BackendKind.UNKNOWN:
                return probe
        raise StorageError(StorageErrorKind.UNKNOWN,
                           f"no backend recognises {path!r}")

    def open(self, path: str, **kwargs) -> Any:
        """Return the backend that claims ``path``."""
        for backend in self._backends:
            probe = backend.probe(path)
            if probe.kind is not BackendKind.UNKNOWN:
                if not probe.readable:
                    raise StorageError(StorageErrorKind.MISSING,
                                       f"{path!r} is not readable",
                                       source_id=path)
                if not probe.supported:
                    raise StorageError(StorageErrorKind.UNSUPPORTED_VERSION,
                                       probe.unsupported_reason or
                                       f"{path!r} is not supported",
                                       source_id=path)
                opener = getattr(backend, 'open', None)
                if opener is None:
                    if kwargs or str(getattr(backend, 'path', path)) != str(path):
                        raise StorageError(StorageErrorKind.UNKNOWN,
                                           'backend cannot bind the requested path/options')
                    return backend
                return opener(path, **kwargs)
        raise StorageError(StorageErrorKind.UNKNOWN,
                           f"no backend recognises {path!r}", source_id=path)


def unknown_probe(path: str, reason: str) -> BackendProbe:
    """The probe used when nothing recognises a path.

    It is *not* an error to not recognise a path — it is an error to guess.
    """
    return BackendProbe(path=path, kind=BackendKind.UNKNOWN, readable=False,
                        writable=False, supported=False,
                        unsupported_reason=reason)
