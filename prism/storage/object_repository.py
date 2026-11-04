"""The object repository contract: content addressed bytes, and what may be
claimed about them.

An **object repository** stores bytes by the hash of their content.  The
same bytes are one object, whatever instance, file or record pointed at
them; different bytes are never merged.  Business code never sees the
backend: it asks for a stream, a stat or a verification.

Three things this module fixes, because they are easy to get wrong:

**Unknown is not zero.**  A missing object is ``None``/an exception, never
an empty byte string that looks like "an empty file".

**A successful read is not a verified read.**  ``open_stream`` can hand
back bytes that were corrupted on disk; only ``verify`` recomputes the
content hash and says so.  ``DurableResult.verified`` is the only place
that claim may be made, and it is made by comparing the hash of the bytes
that were read back.

**Durable is a statement about the repository, not about the platter.**
  ``durable`` means: the bytes are in the container and a fresh reader
  finds them.  Whether the bytes survived a power cut is the backend's
  business and is **not** claimed here unless the backend says so.
  Consumers that need the stronger claim must ask the backend.

The interface is deliberately small so that a backend can be swapped:
``put_stream``, ``open_stream``, ``stat``, ``verify``, ``materialize``
plus the closing sequence ``stop`` / ``drain`` / ``close``.
"""

from __future__ import annotations

import abc
from dataclasses import dataclass, field
from typing import Iterator, Optional

#: The hash type the content identity uses.  SHA-256 in hex, lower case.
HASH_ALGORITHM = 'sha256'


class ObjectError(Exception):
    """Base class for the repository errors."""


class ObjectNotFound(ObjectError):
    """The requested object is not in this repository."""


class ObjectCorrupt(ObjectError):
    """The bytes read back do not match the content identity."""


class RepositoryClosed(ObjectError):
    """The repository was closed (or is draining) and takes no new work."""


class ReadOnlyViolation(ObjectError):
    """A read-only repository was asked to write."""


class ActiveWriter(ObjectError):
    """The container has a live writer, so a read-only snapshot is unsafe.

    Raised instead of reading a container whose ``packs.idx-wal`` exists:
    a reader that ignores the write ahead log would see a state that never
    existed.
    """


@dataclass(frozen=True)
class ObjectStat:
    """What the repository knows about one object."""

    hashkey: str
    length: int
    stored_length: int = 0
    compressed: bool = False
    #: True when the content hash was recomputed over the read back bytes.
    verified: bool = False
    #: The container the object lives in (for diagnostics).
    container: str = ''

    @property
    def known(self) -> bool:
        return self.length >= 0


@dataclass(frozen=True)
class DurableResult:
    """What may be claimed about an object after a write or a read.

    ``durable``
        The bytes are in the container and a fresh reader finds them.
    ``verified``
        The content hash was recomputed over the bytes that were read back
        and matched.  A plain read never sets this.
    ``fsynced``
        The backend flushed the bytes to stable storage.  The default
        repository does **not** claim this; only a backend that really
        fsyncs may set it.
    """

    hashkey: str
    length: int
    durable: bool = False
    verified: bool = False
    fsynced: bool = False
    container: str = ''
    #: Why the result is not durable, when it is not.
    reason: str = ''

    @property
    def ok(self) -> bool:
        """True when the caller may treat the object as stored and intact."""
        return self.durable and self.verified

    def text(self) -> str:
        if self.ok:
            return f'{self.hashkey} durable+verified ({self.length} bytes)'
        parts = []
        if not self.durable:
            parts.append('not durable')
        if not self.verified:
            parts.append('not verified')
        return (f'{self.hashkey} ' + '+'.join(parts or ['unknown'])
                + (f' ({self.reason})' if self.reason else ''))


class ObjectRepository(abc.ABC):
    """Content addressed bytes, with an explicit closing sequence.

    The closing sequence is part of the contract because the host has to
    be able to shut down without losing work and without calling back
    into a destroyed object:

    1. ``stop()`` - no new work is accepted;
    2. the in-flight work is cancelled or completed (the backend decides
       and says which);
    3. ``drain()`` - wait until nothing is in flight;
    4. ``close()`` - release the handles.
    """

    #: The backend name, for diagnostics ("dos", "memory", ...).
    backend = 'abstract'

    @abc.abstractmethod
    def put_stream(self, stream: Iterator[bytes], *,
                   length: Optional[int] = None) -> ObjectStat:
        """Store the bytes of ``stream`` and return their stat.

        The same bytes are one object; different bytes never merge.
        """

    @abc.abstractmethod
    def open_stream(self, hashkey: str) -> Iterator[bytes]:
        """The bytes of one object, in chunks.

        Raises :class:`ObjectNotFound` when the object is not here.  The
        bytes are **not** verified: use :meth:`verify` for that.
        """

    @abc.abstractmethod
    def stat(self, hashkey: str) -> Optional[ObjectStat]:
        """What is known about one object, or ``None`` when it is absent."""

    @abc.abstractmethod
    def verify(self, hashkey: str) -> DurableResult:
        """Read the object back and recompute its content hash."""

    def materialize(self, hashkey: str, destination) -> DurableResult:
        """Write the object to ``destination`` byte for byte."""
        raise NotImplementedError

    @abc.abstractmethod
    def stop(self) -> None:
        """Stop accepting new work.  In-flight work may still finish."""

    @abc.abstractmethod
    def drain(self, timeout: Optional[float] = None) -> bool:
        """Wait until nothing is in flight.  True when it is idle."""

    @abc.abstractmethod
    def close(self) -> None:
        """Release the handles.  Idempotent."""

    # -- convenience ----------------------------------------------------

    def has_object(self, hashkey: str) -> bool:
        return self.stat(hashkey) is not None

    def content_identity(self, data: bytes) -> str:
        """The content hash of ``data`` (the identity of an object)."""
        import hashlib
        return hashlib.new(HASH_ALGORITHM, data).hexdigest()


@dataclass
class RepositoryStats:
    """What the backend knows about itself (diagnostics only)."""

    backend: str = ''
    container: str = ''
    objects: int = 0
    total_bytes: int = 0
    stopped: bool = False
    closed: bool = False
    in_flight: int = 0
    open_sessions: int = 0
    notes: tuple = field(default_factory=tuple)
