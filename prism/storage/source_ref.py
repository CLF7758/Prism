"""SourceRef resolution for the storage contract.

A :class:`~prism.storage.contracts.SourceRef` says *what* the bytes are;
this module says how a backend turns one into bytes, and how the four
different "there is no content" facts are told apart.

It reuses AI-02's existing read-only accessors
(:func:`prism.fileio.sql.read_page_body` and
:func:`prism.fileio.sql.read_attachment`) instead of writing a second SQL
reader, as the batch rules require.

Pure Python: no Qt, no QApplication, no scene mutation.
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Callable, Optional

from .contracts import (
    SourceKind,
    SourceRef,
    SourceState,
    StorageError,
    StorageErrorKind,
)

#: Media roles the batch cares about.  ``unknown`` is a real answer: we must
#: not invent a role for a blob we do not understand.
MEDIA_ROLES = ("image", "video", "body", "attachment", "material", "unknown")

#: Which storage error means which source state.  The four facts stay apart.
_STATE_BY_ERROR_KIND = {
    StorageErrorKind.MISSING: SourceState.MISSING,
    StorageErrorKind.EMPTY_CONTENT: SourceState.EMPTY_CONTENT,
    StorageErrorKind.NOT_LOADED: SourceState.NOT_LOADED,
    StorageErrorKind.CORRUPT: SourceState.CORRUPT,
}


def content_revision_of(data: bytes) -> str:
    """The revision of a byte string: the full SHA256 of its content."""
    return hashlib.sha256(data).hexdigest()


@dataclass(frozen=True)
class ResolvedSource:
    """The outcome of resolving one :class:`SourceRef`."""

    ref: SourceRef
    data: Optional[bytes]
    error: Optional[StorageError]

    @property
    def ok(self) -> bool:
        return self.error is None and self.data is not None

    @property
    def state(self) -> SourceState:
        if self.error is not None:
            # The error kind already knows which of the four facts this is.
            return _STATE_BY_ERROR_KIND.get(self.error.kind, SourceState.CORRUPT)
        if self.data is None:
            return SourceState.NOT_LOADED
        if len(self.data) == 0:
            return SourceState.EMPTY_CONTENT
        return SourceState.PRESENT

    def verified_revision(self) -> Optional[str]:
        """SHA256 of what was actually read - the caller can compare it."""
        if self.data is None:
            return None
        return content_revision_of(self.data)


def legacy_page_ref(page_id: str, *, media_role: str = "body",
                    byte_length: Optional[int] = None,
                    content_revision: Optional[str] = None) -> SourceRef:
    """A page body that lives inside the current .prism archive."""
    return SourceRef(kind=SourceKind.LEGACY_ARCHIVE, source_id=str(page_id),
                     byte_length=byte_length, content_revision=content_revision,
                     media_role=media_role, state=SourceState.PRESENT)


def legacy_attachment_ref(attachment_id: str, *,
                          media_role: str = "attachment",
                          byte_length: Optional[int] = None,
                          content_revision: Optional[str] = None) -> SourceRef:
    return SourceRef(kind=SourceKind.LEGACY_ARCHIVE, source_id=str(attachment_id),
                     byte_length=byte_length, content_revision=content_revision,
                     media_role=media_role, state=SourceState.PRESENT)


def managed_object_ref(object_id: str, *, byte_length: Optional[int] = None,
                       content_revision: Optional[str] = None,
                       media_role: str = "unknown") -> SourceRef:
    return SourceRef(kind=SourceKind.MANAGED_OBJECT, source_id=str(object_id),
                     byte_length=byte_length, content_revision=content_revision,
                     media_role=media_role, state=SourceState.PRESENT)


def external_link_ref(object_id: str, uri: str, *,
                     media_role: str = "unknown",
                     byte_length: Optional[int] = None,
                     content_revision: Optional[str] = None) -> SourceRef:
    return SourceRef(kind=SourceKind.EXTERNAL_LINK, source_id=str(object_id),
                     external_uri=str(uri), byte_length=byte_length,
                     content_revision=content_revision, media_role=media_role,
                     state=SourceState.PRESENT)


def classify_stored_body(stored: object) -> SourceState:
    """Tell the four "no content" facts apart for a stored body.

    AI-02's ``StoredBody`` already separates *could not read* (``error``),
    *no such page* (``found`` false) and *here is the body*; but it uses
    ``content is None`` both for "missing" and for "the body was never
    materialised".  This function keeps the distinction the batch requires:

    * ``error`` set            -> CORRUPT
    * ``found`` false          -> MISSING
    * ``found`` and content None -> NOT_LOADED (the page exists, the body
      was never opened)
    * ``content == b''``       -> EMPTY_CONTENT (legitimately empty)
    """
    if getattr(stored, "error", None) is not None:
        return SourceState.CORRUPT
    if not getattr(stored, "found", False):
        return SourceState.MISSING
    content = getattr(stored, "content", None)
    if content is None:
        if getattr(stored, 'raw_data_available', False):
            return _classify_raw_page_data(stored.raw_data)
        return SourceState.NOT_LOADED
    if isinstance(content, str):
        content = content.encode("utf-8")
    if len(content) == 0:
        return SourceState.EMPTY_CONTENT
    return SourceState.PRESENT


def _raw_page_data(source_path: str, page_id: str):
    """The raw ``data`` column of one page, or a sentinel.

    AI-02's ``read_page_body`` reports *found* and *content*, and it uses
    ``content is None`` for two different facts: "the page exists and its
    body was never stored" and "the page exists and its stored body is
    empty".  The batch requires those to be different facts, so this reads
    the one column that distinguishes them.  It is a disambiguation of the
    existing accessor, not a second SQL reader.
    """
    from prism.fileio.sql import read_page_body
    stored = read_page_body(source_path, page_id)
    if stored.error is not None:
        return _UNREADABLE
    if not stored.found:
        return _NO_ROW
    return stored.raw_data


class _Sentinel:
    def __init__(self, name):
        self.name = name

    def __repr__(self):
        return self.name


_NO_ROW = _Sentinel("<no row>")
_UNREADABLE = _Sentinel("<unreadable>")


def _classify_raw_page_data(raw) -> SourceState:
    """Turn the raw stored column into one of the four source states."""
    import json
    from prism.fileio.sql import PAGE_PAYLOAD_KEY

    if raw is _UNREADABLE:
        return SourceState.CORRUPT
    if raw is _NO_ROW:
        return SourceState.MISSING
    if raw is None:
        # The row exists but no body was ever stored for it.
        return SourceState.NOT_LOADED
    if isinstance(raw, (bytes, bytearray)):
        raw = raw.decode("utf-8", "replace")
    if raw == "":
        return SourceState.EMPTY_CONTENT
    try:
        parsed = json.loads(raw)
    except (TypeError, ValueError):
        return SourceState.CORRUPT
    if isinstance(parsed, dict) and parsed.get(PAGE_PAYLOAD_KEY) \
            and "content" not in parsed:
        # The envelope points at a body that was never materialised.
        return SourceState.NOT_LOADED
    if parsed is None or (isinstance(parsed, dict) and parsed.get(PAGE_PAYLOAD_KEY)
                          and parsed.get('content') is None):
        return SourceState.EMPTY_CONTENT
    return SourceState.PRESENT


def _body_to_bytes(content) -> bytes:
    """Turn a stored page body into bytes.

    A stored body may be text, bytes, or a parsed JSON structure.  Text and
    bytes are returned verbatim.  A parsed structure is **re-serialised** and
    is therefore *not* byte-identical to the stored column - callers that
    need the original bytes must use
    :func:`prism.storage.legacy_backend.LegacyBackend.read_raw_page_data`.
    """
    if isinstance(content, (bytes, bytearray)):
        return bytes(content)
    if isinstance(content, str):
        return content.encode("utf-8")
    import json
    return json.dumps(content, ensure_ascii=False,
                      separators=(",", ":")).encode("utf-8")


def resolve_legacy_page(source_path: str, page_id: str) -> ResolvedSource:
    """Read one page body through AI-02's existing read-only accessor."""
    from prism.fileio.sql import read_page_body

    ref = legacy_page_ref(page_id)
    stored = read_page_body(source_path, page_id)
    if getattr(stored, "error", None) is not None:
        return ResolvedSource(ref, None, StorageError(
            StorageErrorKind.CORRUPT, f"page {page_id} could not be read",
            detail=getattr(stored, "error", None), source_id=str(page_id)))
    if not getattr(stored, "found", False):
        return ResolvedSource(ref, None, StorageError(
            StorageErrorKind.MISSING, f"page {page_id} is not in this project",
            source_id=str(page_id)))
    content = getattr(stored, "content", None)
    if content is None:
        # Empty and never-loaded look the same from the accessor alone; the
        # stored column tells them apart.
        state = classify_stored_body(stored)
        if state is SourceState.NOT_LOADED:
            return ResolvedSource(ref, None, StorageError(
                StorageErrorKind.NOT_LOADED,
                f"page {page_id} exists but its body was never loaded",
                source_id=str(page_id)))
        if state is SourceState.EMPTY_CONTENT:
            return ResolvedSource(ref, b"", StorageError(
                StorageErrorKind.EMPTY_CONTENT, f"page {page_id} body is empty",
                source_id=str(page_id)))
        if state is SourceState.MISSING:
            return ResolvedSource(ref, None, StorageError(
                StorageErrorKind.MISSING, f"page {page_id} is not in this project",
                source_id=str(page_id)))
        if state is SourceState.CORRUPT:
            return ResolvedSource(ref, None, StorageError(
                StorageErrorKind.CORRUPT, f"page {page_id} body is not readable",
                source_id=str(page_id)))
        # The accessor said "no content" but the column holds something: that
        # is a fact we do not understand, so it is unknown, not empty.
        return ResolvedSource(ref, None, StorageError(
            StorageErrorKind.UNKNOWN,
            f"page {page_id} body state could not be determined",
            source_id=str(page_id)))
    data = _body_to_bytes(content)
    if len(data) == 0:
        return ResolvedSource(ref, b"", StorageError(
            StorageErrorKind.EMPTY_CONTENT, f"page {page_id} body is empty",
            source_id=str(page_id)))
    return ResolvedSource(ref, data, None)


def resolve_legacy_attachment(source_path: str,
                               attachment_id: str) -> ResolvedSource:
    """Read one attachment through AI-02's existing read-only accessor."""
    from prism.fileio.sql import read_attachment

    ref = legacy_attachment_ref(attachment_id)
    stored = read_attachment(source_path, attachment_id)
    if getattr(stored, "error", None) is not None:
        return ResolvedSource(ref, None, StorageError(
            StorageErrorKind.CORRUPT,
            f"attachment {attachment_id} could not be read",
            detail=getattr(stored, "error", None), source_id=str(attachment_id)))
    if not getattr(stored, "found", False):
        return ResolvedSource(ref, None, StorageError(
            StorageErrorKind.MISSING,
            f"attachment {attachment_id} is not in this project",
            source_id=str(attachment_id)))
    blob = getattr(stored, "blob", None)
    if blob is None:
        return ResolvedSource(ref, None, StorageError(
            StorageErrorKind.NOT_LOADED,
            f"attachment {attachment_id} exists but has no stored bytes",
            source_id=str(attachment_id)))
    blob = bytes(blob)
    if len(blob) == 0:
        return ResolvedSource(ref, b"", StorageError(
            StorageErrorKind.EMPTY_CONTENT,
            f"attachment {attachment_id} is legitimately empty",
            source_id=str(attachment_id)))
    return ResolvedSource(ref, blob, None)
