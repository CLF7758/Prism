"""Binding between the object repository and the storage contract.

The contract (``storage-contract-v1``, owner AI-01) defines **one**
vocabulary for where bytes come from: :class:`SourceRef` with three kinds
(legacy archive / managed object / external link) and five states that must
not be merged (present / empty content / not loaded / missing / corrupt).

This module is the adapter between that vocabulary and the object
repository.  It is deliberately thin: it **reads** the contract's types and
never replaces them.  The rules of the batch forbid a second, parallel
state machine for the same thing.

Two rules from the contract are load bearing here:

* ``byte_length`` and ``content_revision`` describe the **content**, not
  the container.  Two containers holding the same bytes share them - so
  ``content_revision`` is the content hash of the object, not a path, not
  an mtime and not a row id.
* A fact that is not known must not be written as a zero.  A missing
  object is ``SourceState.MISSING``; an unreadable one is ``CORRUPT``;
  the difference is kept.

The import is **strict and late**: the contract module is imported when a
binding is actually made, and a missing module raises a clear error.  A
missing interface is never dressed up as an empty result.
"""

from __future__ import annotations

from typing import Optional

from prism.storage.object_repository import (
    ActiveWriter, DurableResult, ObjectCorrupt, ObjectError, ObjectNotFound,
    ObjectStat, ReadOnlyViolation, RepositoryClosed)


def _contract():
    """Import the published contract, or say clearly that it is missing."""
    try:
        from prism.storage import contracts
    except ImportError as exc:                           # pragma: no cover
        raise ObjectError(
            'the storage contract (prism/storage/contracts.py) is not '
            'available in this tree; storage-contract-v1 is still PROPOSED, '
            'so the binding cannot be made yet') from exc
    return contracts


#: Which error kind a repository failure maps to.  ``None`` means "the
#: contract has no counterpart" - those are listed in the sign-off.
_ERROR_KINDS = {
    'ObjectNotFound': 'MISSING',
    'ObjectCorrupt': 'CORRUPT',
    'ReadOnlyViolation': 'READ_ONLY',
    'RepositoryClosed': 'CANCELLED',
}


def error_kind(exc: BaseException) -> Optional[str]:
    """The contract's error kind for a repository failure.

    v1.1 names the live-writer condition SOURCE_BUSY; it is neither missing
    content nor an unspecified error. None is reserved for unrecognised types.
    """
    name = type(exc).__name__
    if name == 'ActiveWriter':
        return 'SOURCE_BUSY'
    return _ERROR_KINDS.get(name)


def source_ref_for_object(stat: ObjectStat, *, media_role: Optional[str] = None,
                          state: Optional[str] = None):
    """A :class:`SourceRef` for an object in the repository.

    ``content_revision`` is the content hash: the same bytes in another
    container share it.
    """
    contracts = _contract()
    kind = contracts.SourceKind.MANAGED_OBJECT
    source_state = contracts.SourceState.PRESENT
    if state is not None:
        source_state = getattr(contracts.SourceState, state)
    return contracts.SourceRef(
        kind=kind,
        source_id=stat.hashkey,
        byte_length=stat.length,
        content_revision=stat.hashkey,
        media_role=media_role,
        state=source_state,
        external_uri=None)


def source_ref_for_legacy(source_id: str, *, byte_length: Optional[int] = None,
                           content_revision: Optional[str] = None,
                           media_role: Optional[str] = None,
                           state: Optional[str] = None):
    """A :class:`SourceRef` for the bytes that still live in the old archive."""
    contracts = _contract()
    source_state = contracts.SourceState.PRESENT
    if state is not None:
        source_state = getattr(contracts.SourceState, state)
    return contracts.SourceRef(
        kind=contracts.SourceKind.LEGACY_ARCHIVE,
        source_id=source_id,
        byte_length=byte_length,
        content_revision=content_revision,
        media_role=media_role,
        state=source_state,
        external_uri=None)


def source_ref_for_external(source_id: str, external_uri: str, *,
                             byte_length: Optional[int] = None,
                             media_role: Optional[str] = None):
    """A :class:`SourceRef` for a file we do not own."""
    contracts = _contract()
    return contracts.SourceRef(
        kind=contracts.SourceKind.EXTERNAL_LINK,
        source_id=source_id,
        byte_length=byte_length,
        content_revision=None,
        media_role=media_role,
        state=contracts.SourceState.PRESENT,
        external_uri=external_uri)


def durable_state(result: DurableResult) -> str:
    """The contract's durable state for a repository result.

    The mapping is the one the acceptance criteria ask for:

    * the bytes are there **and** their hash was recomputed -> committed;
    * the bytes are there but nobody checked them -> **committed, not
      confirmed**.  That is not "success": the content identity of the
      object is still unproven.
    * the bytes are not there -> not started.
    """
    contracts = _contract()
    if result.durable and result.verified:
        return contracts.DurableState.COMMITTED.value
    if result.durable:
        # NOTE: the contract spells this fact two ways -
        # `StorageErrorKind.COMMITTED_NOT_CONFIRMED` ('committed_not_confirmed')
        # and `DurableState.COMMITTED_UNCONFIRMED` ('committed_unconfirmed').
        # The enum member is used here; the spelling difference is reported
        # in the AI-03 sign-off (it is a defect, not a choice).
        return contracts.DurableState.COMMITTED_UNCONFIRMED.value
    return contracts.DurableState.NOT_STARTED.value


def state_for_exception(exc: BaseException) -> str:
    """The contract's source state for a repository failure."""
    contracts = _contract()
    name = type(exc).__name__
    if name == 'ObjectNotFound':
        return contracts.SourceState.MISSING.value
    if name == 'ObjectCorrupt':
        return contracts.SourceState.CORRUPT.value
    if name == 'RepositoryClosed':
        return contracts.SourceState.NOT_LOADED.value
    if name == 'ActiveWriter':
        raise contracts.StorageError(contracts.StorageErrorKind.SOURCE_BUSY, str(exc)) from exc
    raise contracts.StorageError(contracts.StorageErrorKind.UNKNOWN, str(exc)) from exc


def open_object(repository, source_ref):
    """Open the object a :class:`SourceRef` points at.

    Only a **managed object** can be opened this way: the legacy archive and
    the external link are read by their own readers (AI-02's
    ``read_page_body`` / ``read_attachment``), not through the object
    repository.  A mismatch is an error, not a silent fallback.
    """
    contracts = _contract()
    if source_ref.kind is not contracts.SourceKind.MANAGED_OBJECT:
        raise ObjectError(
            f'source kind {source_ref.kind.value} is not read through the '
            'object repository')
    return repository.open_stream(source_ref.source_id)
