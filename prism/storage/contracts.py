"""STORAGE-CONTRACT v1 — the shared vocabulary of the storage batch.

Pure Python.  This module must never import Qt, must never create a
QApplication, and must never open a database.  It only says what a *value*
means; the backends in this package decide how to obtain it.

The batch rule is explicit: ``SourceRef`` / ``SaveReceipt`` /
``ResultPublication`` / ``CountPublication`` / ``GeometryKey`` must reuse the
structures that already exist in the tree.  This module therefore does not
define a second counting, saving or source state machine.  It defines the
three identities that have **no** equivalent today
(:class:`ProjectIdentity`, :class:`SourceRef`, the backend protocol) and
adapters that read the existing structures instead of replacing them.

Existing structures reused as-is (see ``evidence/interface-inventory.json``):

===========================  ===============================================
``prism.count_contract``     ``CountSnapshot``, ``CountPublication``
``prism.catalog_browser``    ``QueryKey``, ``ResultPublication``
``prism.fileio.save_preparation``  ``SaveOutcome``
``prism.compact_layout``     ``layout_key`` (the geometry identity)
===========================  ===============================================

Nothing here imports those modules at module import time: ``contracts`` is
imported by backends that must stay Qt-free, and importing the owners would
drag their dependencies along.  The reuse is expressed as *structural*
adapters (:func:`identity_of`, :func:`receipt_of`) that work on any object
carrying the required attributes, plus the exact class-to-module mapping in
:data:`REUSED_TYPES` for the published manifest.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Mapping, Optional, Sequence, Tuple

CONTRACT_VERSION = "storage-contract-v1.1"
CONTRACT_STATUS = "ACCEPTED"  # integrated locally; independent v1.1 acceptance pending
CONTRACT_OWNER = "AI-01"


# --------------------------------------------------------------------- enums
class SourceKind(str, Enum):
    """The three kinds of source a stored byte can come from."""

    LEGACY_ARCHIVE = "legacy_archive"   # a blob inside the current .prism
    MANAGED_OBJECT = "managed_object"   # a byte object in a managed store
    EXTERNAL_LINK = "external_link"     # a reference to a file we do not own


class SourceState(str, Enum):
    """What is actually known about one source.

    These are different facts and must never be collapsed into one another:
    ``MISSING`` means there is no such thing, ``NOT_LOADED`` means the thing
    exists but its body was never materialised, ``EMPTY_CONTENT`` means the
    body was materialised and is legitimately empty.
    """

    PRESENT = "present"
    EMPTY_CONTENT = "empty_content"
    NOT_LOADED = "not_loaded"
    MISSING = "missing"
    CORRUPT = "corrupt"


class StorageErrorKind(str, Enum):
    """Every failure the batch requires to be distinguishable."""

    MISSING = "missing"                        # 缺失
    EMPTY_CONTENT = "empty_content"            # 空内容
    NOT_LOADED = "not_loaded"                  # 未加载
    CORRUPT = "corrupt"                        # 损坏
    UNSUPPORTED_VERSION = "unsupported_version"  # 版本不支持
    READ_ONLY = "read_only"                    # 只读
    CANCELLED = "cancelled"                    # 取消
    SOURCE_BUSY = "source_busy"                # active writer / unsafe snapshot
    COMMITTED_NOT_CONFIRMED = "committed_not_confirmed"  # 提交但未确认
    INSUFFICIENT_SPACE = "insufficient_space"  # 空间不足
    UNKNOWN = "unknown"                        # unknown 不写 0, 不写成功


class DurableState(str, Enum):
    """What is true about the bytes on disk after a save attempt."""

    NOT_STARTED = "not_started"
    ROLLED_BACK = "rolled_back"
    COMMITTED = "committed"
    COMMITTED_UNCONFIRMED = "committed_unconfirmed"
    INCOMPLETE = "incomplete"
    UNKNOWN = "unknown"


class CommitState(str, Enum):
    """Mirrors the vocabulary AI-02 already uses in ``SaveOutcome``."""

    NOT_STARTED = "not_started"
    ROLLED_BACK = "rolled_back"
    COMPLETE = "complete"
    INCOMPLETE = "incomplete"
    UNKNOWN = "unknown"


class AckState(str, Enum):
    """Mirrors the vocabulary AI-02 already uses in ``SaveOutcome``."""

    NOT_APPLICABLE = "not_applicable"
    CONFIRMED = "confirmed"
    FAILED = "failed"
    UNKNOWN = "unknown"


class Terminal(str, Enum):
    """How a save attempt ended."""

    CONFIRMED = "confirmed"
    CANCELLED = "cancelled"
    REFUSED = "refused"
    FAILED = "failed"
    COMMITTED_NOT_CONFIRMED = "committed_not_confirmed"


# -------------------------------------------------------------------- errors
class StorageError(Exception):
    """A storage failure that carries its *kind*.

    ``kind`` is never dropped: a caller must be able to tell "the page is
    missing" from "the page exists but was never loaded" from "the file is
    corrupt" from "the write was cancelled".
    """

    def __init__(self, kind: StorageErrorKind, message: str = "",
                 *, detail: Any = None, source_id: Optional[str] = None):
        super().__init__(message or kind.value)
        self.kind = kind
        self.detail = detail
        self.source_id = source_id

    @property
    def is_unknown(self) -> bool:
        return self.kind is StorageErrorKind.UNKNOWN

    def as_dict(self) -> dict:
        return {"kind": self.kind.value, "message": str(self),
                "source_id": self.source_id,
                "detail": repr(self.detail) if self.detail is not None else None}


# ---------------------------------------------------------------- identities
@dataclass(frozen=True)
class ProjectIdentity:
    """Three different identities, deliberately kept apart.

    ``project_id``
        The *business* identity of a project.  It survives moving the
        directory: a renamed folder is the same project.
    ``storage_instance_id``
        Which physical container holds it.  A migrated project gets a new
        instance id while keeping its project id.
    ``open_epoch``
        Which *opening* of that instance this is.  Callbacks and receipts
        from an older epoch must not be applied to the current one.
    """

    project_id: str
    storage_instance_id: str
    open_epoch: str

    def __post_init__(self) -> None:
        if not self.project_id:
            raise ValueError("project_id is required")
        if not self.storage_instance_id:
            raise ValueError("storage_instance_id is required")
        if not self.open_epoch:
            raise ValueError("open_epoch is required")

    def with_epoch(self, open_epoch: str) -> "ProjectIdentity":
        return ProjectIdentity(self.project_id, self.storage_instance_id, open_epoch)

    def same_project(self, other: "ProjectIdentity") -> bool:
        return self.project_id == other.project_id

    def as_dict(self) -> dict:
        return {"project_id": self.project_id,
                "storage_instance_id": self.storage_instance_id,
                "open_epoch": self.open_epoch}


@dataclass(frozen=True)
class SourceRef:
    """Where the bytes of one original come from.

    ``byte_length`` and ``content_revision`` describe the *content*, not the
    container: two different containers holding the same bytes share them.
    ``media_role`` says what the bytes are for (image / video / body /
    attachment / unknown).
    """

    kind: SourceKind
    source_id: str
    byte_length: Optional[int] = None
    content_revision: Optional[str] = None
    media_role: Optional[str] = None
    state: SourceState = SourceState.PRESENT
    external_uri: Optional[str] = None

    def __post_init__(self) -> None:
        if not self.source_id:
            raise ValueError("source_id is required")
        if self.kind is SourceKind.EXTERNAL_LINK and not self.external_uri:
            raise ValueError("external_link requires external_uri")
        if self.kind is SourceKind.EXTERNAL_LINK and self.state is SourceState.PRESENT \
                and self.byte_length is None:
            # an external link we do not own may legitimately have unknown size
            pass

    @property
    def is_managed(self) -> bool:
        return self.kind is SourceKind.MANAGED_OBJECT

    @property
    def is_resolvable(self) -> bool:
        return self.state is SourceState.PRESENT

    def as_dict(self) -> dict:
        return {"kind": self.kind.value, "source_id": self.source_id,
                "byte_length": self.byte_length,
                "content_revision": self.content_revision,
                "media_role": self.media_role, "state": self.state.value,
                "external_uri": self.external_uri}


@dataclass(frozen=True)
class SaveReceipt:
    """The fact that a save attempt produced (or did not produce) a commit.

    Built from AI-02's existing :class:`SaveOutcome` rather than replacing
    it.  Two things are added, because the acceptance criteria need them:

    ``committed_revision``
        the revision that is actually on disk.  ``captured_revision`` is what
        was captured; they are equal only when nothing changed in between.
    ``durable_state``
        what is true about the bytes.  ``COMMITTED_NOT_CONFIRMED`` is a
        distinct outcome: the data is written but the UI could not confirm
        it, and the user must be told that - not "file not accessible".
    """

    operation_id: str
    identity: ProjectIdentity
    target: str
    captured_revision: Any
    committed_revision: Optional[str]
    changed_entities: Optional[tuple]
    complete: bool
    durable_state: DurableState
    commit_state: CommitState
    ack_state: AckState
    terminal: Terminal
    error: Optional[StorageError] = None
    source_before: Optional[str] = None
    source_after: Optional[str] = None
    changed_entities_complete: bool = False
    live_revision_after_commit: Any = None

    @property
    def is_durable(self) -> bool:
        return self.durable_state in (DurableState.COMMITTED,
                                      DurableState.COMMITTED_UNCONFIRMED)

    @property
    def changed_after_capture(self) -> Optional[bool]:
        """Only an observed live revision can prove edits during a save.

        A storage commit revision and a UI capture revision are different
        domains. Comparing those would label every successful save as dirty.
        """
        if self.live_revision_after_commit is None:
            return None
        return self.live_revision_after_commit != self.captured_revision

    def as_dict(self) -> dict:
        return {
            "operation_id": self.operation_id,
            "identity": self.identity.as_dict(), "target": self.target,
            "captured_revision": self.captured_revision,
            "committed_revision": self.committed_revision,
            "changed_entities": (list(self.changed_entities)
                                 if self.changed_entities is not None else None),
            "changed_entities_complete": self.changed_entities_complete,
            "live_revision_after_commit": self.live_revision_after_commit,
            "complete": self.complete,
            "durable_state": self.durable_state.value,
            "commit_state": self.commit_state.value,
            "ack_state": self.ack_state.value,
            "terminal": self.terminal.value,
            "error": self.error.as_dict() if self.error else None,
            "source_before": self.source_before, "source_after": self.source_after,
        }


# ------------------------------------------------------------- reuse adapters
def _get(obj: Any, name: str, default: Any = None) -> Any:
    return getattr(obj, name, default)


def identity_of(obj: Any, *, project_id: Optional[str] = None,
                storage_instance_id: Optional[str] = None,
                open_epoch: Optional[str] = None) -> ProjectIdentity:
    """Read a :class:`ProjectIdentity` out of an existing structure.

    AI-05's ``QueryKey``/``ResultPublication`` and AI-03's
    ``CountPublication`` each already carry a project id and an open epoch.
    This adapter lifts those into the identity type instead of asking every
    owner to invent a second one.
    """
    pid = project_id or _get(obj, "project", None) or _get(obj, "project_id", None)
    epoch = open_epoch or _get(obj, "open_epoch", None)
    instance = (storage_instance_id
                or _get(obj, "storage_instance_id", None)
                or _get(obj, "source_revision", None)
                or _get(obj, "target", None)
                or _get(obj, "source_before", None))
    if not pid:
        raise StorageError(StorageErrorKind.UNKNOWN,
                           "no project identity available", detail=repr(obj))
    if not instance:
        # A missing storage instance is *unknown*, not a crash: the caller must
        # not be handed an identity that was invented here.
        raise StorageError(StorageErrorKind.UNKNOWN,
                           "no storage instance identity available", detail=repr(obj))
    if not epoch:
        raise StorageError(StorageErrorKind.UNKNOWN,
                           "no open epoch available", detail=repr(obj))
    return ProjectIdentity(project_id=str(pid),
                           storage_instance_id=str(instance),
                           open_epoch=str(epoch))


def receipt_of(outcome: Any, *, identity: Optional[ProjectIdentity] = None,
               committed_revision: Optional[str] = None,
               durable_state: Optional[DurableState] = None) -> SaveReceipt:
    """Build a :class:`SaveReceipt` from AI-02's existing ``SaveOutcome``.

    The outcome already carries operation id, project id, open epoch,
    captured revision, commit/ack state, terminal and changed entities.
    Only ``committed_revision`` and ``durable_state`` are added, and both
    default to the conservative reading: *unknown* is never reported as a
    successful commit.
    """
    op_id = str(_get(outcome, "operation_id", "") or "")
    if not op_id:
        raise StorageError(StorageErrorKind.UNKNOWN,
                           "outcome carries no operation_id", detail=repr(outcome))
    ident = identity or identity_of(outcome,
                                    project_id=_get(outcome, "project_id", None),
                                    open_epoch=_get(outcome, "open_epoch", None),
                                    storage_instance_id=(
                                        _get(outcome, "target", None)
                                        or _get(outcome, "source_before", None)))
    commit_raw = _enum_value(_get(outcome, "commit_state", ""))
    ack_raw = _enum_value(_get(outcome, "ack_state", ""))
    terminal_raw = _enum_value(_get(outcome, "terminal", ""))
    commit = _as_commit(commit_raw)
    ack = _as_ack(ack_raw)
    terminal = _as_terminal(terminal_raw)
    durable = durable_state or _derive_durable(commit, ack, terminal)
    committed = committed_revision if committed_revision is not None \
        else _get(outcome, "committed_revision", None)
    changed_raw = _get(outcome, "changed_entities", None)
    changed = tuple(changed_raw) if changed_raw is not None else None
    return SaveReceipt(
        operation_id=op_id, identity=ident,
        target=str(_get(outcome, "target", "") or ""),
        captured_revision=_get(outcome, "captured_revision", None),
        committed_revision=(str(committed) if committed is not None else None),
        changed_entities=changed,
        # Disk commit and the completeness of a change enumeration are
        # independent facts. Unknown is not an empty, complete change set.
        complete=commit is CommitState.COMPLETE,
        durable_state=durable, commit_state=commit, ack_state=ack, terminal=terminal,
        source_before=_get(outcome, "source_before", None),
        source_after=_get(outcome, "source_after", None),
        changed_entities_complete=(changed is not None and
            bool(_get(outcome, "changed_entities_complete", False))))


_COMMIT_MAP = {c.value: c for c in CommitState}
_ACK_MAP = {a.value: a for a in AckState}
_TERMINAL_MAP = {t.value: t for t in Terminal}
_TERMINAL_MAP["committed_unconfirmed"] = Terminal.COMMITTED_NOT_CONFIRMED


def _enum_value(value: Any) -> str:
    return str(getattr(value, "value", value) or "")


def _as_commit(raw: str) -> CommitState:
    return _COMMIT_MAP.get(raw, CommitState.UNKNOWN)


def _as_ack(raw: str) -> AckState:
    return _ACK_MAP.get(raw, AckState.UNKNOWN)


def _as_terminal(raw: str) -> Terminal:
    return _TERMINAL_MAP.get(raw, Terminal.FAILED)


def _derive_durable(commit: CommitState, ack: AckState,
                     terminal: Terminal) -> DurableState:
    if commit is CommitState.NOT_STARTED:
        return DurableState.NOT_STARTED
    if commit is CommitState.ROLLED_BACK:
        return DurableState.ROLLED_BACK
    if commit is CommitState.COMPLETE:
        if ack is AckState.CONFIRMED:
            return DurableState.COMMITTED
        return DurableState.COMMITTED_UNCONFIRMED
    # UI cancellation is not proof of a database rollback. Preserve the
    # writer's evidence independently from the presentation terminal.
    if commit is CommitState.INCOMPLETE:
        return DurableState.INCOMPLETE
    return DurableState.UNKNOWN


# ------------------------------------------------------------- reuse registry
#: Existing structures this contract reuses.  Published in the machine
#: readable manifest so a consumer can check the real class, not a guess.
REUSED_TYPES = {
    "CountSnapshot": {"module": "prism.count_contract", "owner": "AI-05",
                      "role": "one count value with its basis and revisions"},
    "CountPublication": {"module": "prism.count_contract", "owner": "AI-05",
                         "role": "published count set with basis and pending flag"},
    "QueryKey": {"module": "prism.catalog_browser", "owner": "AI-05",
                 "role": "query identity (source/overlay/epoch/scope/rules)"},
    "ResultPublication": {"module": "prism.catalog_browser", "owner": "AI-05",
                          "role": "what one model publication actually did"},
    "SaveOutcome": {"module": "prism.fileio.save_preparation", "owner": "AI-02",
                    "role": "save attempt result; source of SaveReceipt"},
    "layout_key": {"module": "prism.compact_layout", "owner": "AI-05",
                   "role": "geometry identity (project epoch/range/sort/geometry/viewport)"},
}

#: Things this contract introduces because nothing equivalent exists today.
NEW_TYPES = {
    "ProjectIdentity": "project_id / storage_instance_id / open_epoch in one value",
    "SourceRef": "legacy archive / managed object / external link + state",
    "ProjectBackend": "probe / snapshot / read / query / commit / close routing",
    "SaveReceipt": "SaveOutcome + committed_revision + durable_state",
}


def contract_manifest() -> dict:
    """Machine readable description of this contract version."""
    return {
        "contract": CONTRACT_VERSION,
        "status": CONTRACT_STATUS,
        "owner": CONTRACT_OWNER,
        "pure_python": True,
        "qt_free": True,
        "new_types": NEW_TYPES,
        "reused_types": REUSED_TYPES,
        "error_kinds": [k.value for k in StorageErrorKind],
        "durable_states": [s.value for s in DurableState],
        "source_kinds": [k.value for k in SourceKind],
        "source_states": [s.value for s in SourceState],
        "rule": "published 契约不得原地改签名; 升版本并说明受影响消费者与复验清单",
    }
