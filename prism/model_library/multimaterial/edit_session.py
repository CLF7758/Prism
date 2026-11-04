"""Qt-free pending command records and preview lifetime ownership."""
from dataclasses import dataclass

@dataclass(frozen=True)
class PendingPreview:
    """The one preview request whose results are currently acceptable."""

    request_id: str
    draft_generation: int
    job_id: str | None = None
    working_id: str | None = None
    kind: str = ''
    lease_id: str | None = None


@dataclass(frozen=True)
class PendingCommit:
    """One submitted commit awaiting its receipt, keyed by request id."""

    request: object
    note: str = ''
    job_id: str | None = None
    failed_reason: str = ''

    @property
    def request_id(self):
        return self.request.request_id

    @property
    def failed(self):
        return bool(self.failed_reason)



class PreviewSession:
    def __init__(self):
        self.current = None
        self.lease_id = None

    def take_current(self):
        current, self.current = self.current, None
        return current

    def take_lease(self):
        lease, self.lease_id = self.lease_id, None
        return lease

    def rejection_reason(self, prepared, target):
        received = getattr(prepared, 'target', None)
        if received is None or target is None or any(
                getattr(received, key, None) != getattr(target, key, None)
                for key in ('project_session', 'open_epoch', 'model_id')):
            return 'model_or_project_changed'
        current = self.current
        if current is None:
            return 'no_pending_preview'
        if (getattr(prepared, 'request_id', None) != current.request_id or
                getattr(prepared, 'draft_generation', None) != current.draft_generation):
            return 'request_or_generation_mismatch'
        return None


class CommitLedger:
    """Own receipt ordering/idempotence without widgets or backend calls."""
    def __init__(self):
        self.pending = {}
        self.order = []
        self.confirmed = set()

    def register(self, pending):
        self.pending[pending.request_id] = pending
        if pending.request_id not in self.order:
            self.order.append(pending.request_id)

    def acknowledge(self, request_id):
        if request_id in self.confirmed:
            return 'duplicate', None
        pending = self.pending.pop(request_id, None)
        if pending is None:
            return 'unknown', None
        self.confirmed.add(request_id)
        if request_id in self.order:
            self.order.remove(request_id)
        return 'accepted', pending

    def newest_waiting(self):
        return next((self.pending[key] for key in reversed(self.order)
                     if key in self.pending and not self.pending[key].failed), None)
