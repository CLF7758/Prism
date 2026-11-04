"""Worker sessions for the object repository, and the closing protocol.

Two problems, one place.

**Sessions.**  A repository handle owns a database connection and file
handles.  Workers must not share one: the backend is not documented as
thread safe, and a shared handle would mix the connections of unrelated
jobs.  So every worker gets its **own** session, and the pool keeps the
number of open sessions (and with it the number of connections and file
handles) inside a budget.

**Closing.**  The host has to be able to shut down without losing work
and without calling back into something that is already gone.  The
sequence is fixed:

1. ``stop()`` - no new work is accepted;
2. the in-flight work is cancelled or completed;
3. the **epoch** moves on - every callback that was created before the
   move is now stale and must not install anything;
4. ``drain()`` - wait until nothing is in flight;
5. ``close()`` - release the handles.

Step 3 is the one that is easy to forget and expensive to get wrong: a
result that arrives after the close was started belongs to the old epoch
and must be dropped, not installed into the new project.
"""

from __future__ import annotations

import itertools
import threading
from dataclasses import dataclass, field
from typing import Callable, Iterator, Optional

from prism.storage.object_repository import (
    ObjectRepository, RepositoryClosed, RepositoryStats)


class SessionBudgetExceeded(RuntimeError):
    """More sessions were asked for than the budget allows."""


class StaleEpoch(RuntimeError):
    """A callback from an older epoch tried to install its result."""


@dataclass
class ObjectSession:
    """One repository handle, owned by one worker."""

    repository: ObjectRepository
    owner: str
    epoch: int
    session_id: int = 0
    closed: bool = False

    def check_epoch(self, epoch: int) -> None:
        """Raise when the caller belongs to an epoch that has moved on."""
        if epoch != self.epoch:
            raise StaleEpoch(
                f'session {self.session_id} belongs to epoch {self.epoch}, '
                f'the callback is from epoch {epoch}')

    def close(self) -> None:
        if self.closed:
            return
        self.closed = True
        self.repository.close()


class ObjectSessionPool:
    """Hands out one session per worker and keeps the handles bounded."""

    def __init__(self, factory: Callable[[], ObjectRepository], *,
                 max_sessions: int = 4):
        if max_sessions < 1:
            raise ValueError('max_sessions must be at least one')
        self._factory = factory
        self._max_sessions = max_sessions
        self._lock = threading.Lock()
        self._sessions: dict = {}
        self._ids = itertools.count(1)
        self._epoch = 0
        self._stopped = False
        self._closed = False

    @property
    def epoch(self) -> int:
        """The current epoch.  It moves when the pool starts closing."""
        with self._lock:
            return self._epoch

    def acquire(self, owner: str) -> ObjectSession:
        """A session for ``owner``.  One session per owner at a time."""
        with self._lock:
            if self._closed:
                raise RepositoryClosed('the pool is closed')
            if self._stopped:
                raise RepositoryClosed('the pool stopped accepting work')
            existing = self._sessions.get(owner)
            if existing is not None:
                return existing
            if len(self._sessions) >= self._max_sessions:
                raise SessionBudgetExceeded(
                    f'{len(self._sessions)} sessions are open, the budget '
                    f'is {self._max_sessions}')
            repository = self._factory()
            session = ObjectSession(repository=repository, owner=owner,
                                   epoch=self._epoch,
                                   session_id=next(self._ids))
            self._sessions[owner] = session
            return session

    def release(self, session: ObjectSession) -> None:
        """Give a session back and close its handle."""
        with self._lock:
            current = self._sessions.get(session.owner)
            if current is not session:
                return
            self._sessions.pop(session.owner, None)
        session.close()

    def stop(self) -> int:
        """Stop accepting new work and move the epoch on.

        Returns the new epoch.  Every callback created before this call
        belongs to the old epoch and must not install anything.
        """
        with self._lock:
            self._stopped = True
            self._epoch += 1
            return self._epoch

    def drain(self, timeout: Optional[float] = None) -> bool:
        """Wait until no session is busy."""
        import time
        deadline = None if timeout is None else time.monotonic() + timeout
        while True:
            with self._lock:
                sessions = list(self._sessions.values())
            if all(session.repository.drain(0) for session in sessions):
                return True
            if deadline is not None and time.monotonic() >= deadline:
                return False
            time.sleep(0.01)

    def close(self) -> None:
        """Release every session.  Idempotent."""
        with self._lock:
            if self._closed:
                return
            self._closed = True
            self._stopped = True
            self._epoch += 1
            sessions = list(self._sessions.values())
            self._sessions.clear()
        for session in sessions:
            session.close()

    def stats(self) -> RepositoryStats:
        with self._lock:
            return RepositoryStats(backend='session-pool',
                                   container='',
                                   stopped=self._stopped,
                                   closed=self._closed,
                                   open_sessions=len(self._sessions),
                                   notes=(f'epoch {self._epoch}',))


@dataclass
class CloseProtocol:
    """The closing sequence, and the guard for late callbacks.

    Usage::

        protocol = CloseProtocol()
        epoch = protocol.begin_close()      # stop + epoch moves on
        protocol.is_current(epoch)          # False for the old callbacks
        protocol.drain(timeout=5.0)
        protocol.close()
    """

    _lock: threading.Lock = field(default_factory=threading.Lock)
    _epoch: int = 0
    _closing: bool = False
    _closed: bool = False
    _callbacks_seen: int = 0
    _callbacks_dropped: int = 0

    @property
    def epoch(self) -> int:
        with self._lock:
            return self._epoch

    @property
    def closing(self) -> bool:
        with self._lock:
            return self._closing

    def begin_close(self) -> int:
        """Stop accepting work and move the epoch on."""
        with self._lock:
            self._closing = True
            self._epoch += 1
            return self._epoch

    def is_current(self, epoch: int) -> bool:
        """Whether a callback from ``epoch`` may still install a result."""
        with self._lock:
            if epoch != self._epoch:
                self._callbacks_dropped += 1
                return False
            self._callbacks_seen += 1
            return True

    def drain(self, timeout: Optional[float] = None) -> bool:
        """Wait for the caller's own in-flight work (a hook, not a guess)."""
        import time
        deadline = None if timeout is None else time.monotonic() + timeout
        while True:
            if self._in_flight() == 0:
                return True
            if deadline is not None and time.monotonic() >= deadline:
                return False
            time.sleep(0.01)

    def _in_flight(self) -> int:
        return 0

    def close(self) -> None:
        with self._lock:
            self._closed = True
            self._closing = True

    def stats(self) -> RepositoryStats:
        with self._lock:
            return RepositoryStats(backend='close-protocol',
                                   stopped=self._closing, closed=self._closed,
                                   notes=(f'epoch {self._epoch}',
                                          f'callbacks seen {self._callbacks_seen}',
                                          f'callbacks dropped '
                                          f'{self._callbacks_dropped}'))
