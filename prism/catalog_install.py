"""Query installation lifetime identity; detached from widgets and streams."""
from dataclasses import dataclass

@dataclass(frozen=True)
class InstallIdentity:
    token: object
    project: str
    open_epoch: object
    query_generation: object

    @classmethod
    def capture(cls, pending):
        return cls(pending['token'], pending.get('project'),
                   pending.get('open_epoch'), pending.get('query_generation'))

    def matches(self, *, token, project, open_epoch, query_generation):
        return self == type(self)(token, str(project), open_epoch, query_generation)


@dataclass(frozen=True)
class OverlayProgress:
    """Immutable continuation; each install owns its position independently."""
    entries: tuple
    offset: int = 0

    @classmethod
    def begin(cls, overlay, new):
        return cls(tuple((item, 'overlay_rows') for item in overlay.values()) +
                   tuple((item, 'new_rows') for item in new))

    def take(self):
        if self.offset >= len(self.entries):
            return None, self
        return self.entries[self.offset], type(self)(self.entries, self.offset + 1)


def _copy_containers(value):
    # Copy query predicates/sets, not scene-owned proxies or the stream itself.
    if isinstance(value, dict):
        return {k: _copy_containers(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_copy_containers(v) for v in value]
    if isinstance(value, set):
        return set(value)
    if isinstance(value, tuple):
        return tuple(_copy_containers(v) for v in value)
    return value


def checkpoint_install(pending):
    """Detach owned containers; stream, evidence and proxies are borrowed.

    UI preview widgets and their mutable indexes never enter a checkpoint.
    An OverlayProgress continuation is immutable, unlike a Python iterator.
    """
    excluded = {'grid_preview', 'grid_preview_view', 'grid_data_index',
                'grid_seed', 'grid_preview_offset', 'grid_preview_at',
                'install_queued'}
    snapshot = {k: v for k, v in pending.items() if k not in excluded}
    snapshot['items'] = tuple(pending.get('items') or ())
    snapshot['overlay'] = dict(pending.get('overlay') or {})
    snapshot['new'] = tuple(pending.get('new') or ())
    if 'match_context' in pending:
        snapshot['match_context'] = _copy_containers(pending['match_context'])
    return snapshot


def resume_install(checkpoint, **identity):
    """Create writable install containers without modifying the saved prefix."""
    pending = checkpoint_install(checkpoint)
    pending['items'] = list(pending['items'])
    pending.update(identity)
    pending['inherited_offset'] = pending['offset']
    pending.pop('published_items', None)
    return pending


@dataclass(frozen=True)
class OverlayBatch:
    progress: OverlayProgress
    accepted: tuple
    overlay_rows: int
    new_rows: int
    filtered_rows: int
    stop: str


def consume_overlay(progress, *, matches, valid, expired, limit=256):
    """Consume a bounded batch without installing widgets or mutating input.

    The caller owns cancellation and scheduling. Returned counters describe
    this batch alone, and the immutable continuation can be checkpointed.
    """
    accepted = []
    overlay = new = filtered = 0
    for _ in range(limit):
        if not valid():
            return OverlayBatch(progress, tuple(accepted), overlay, new, filtered, 'invalid')
        entry, next_progress = progress.take()
        if entry is None:
            return OverlayBatch(progress, tuple(accepted), overlay, new, filtered, 'exhausted')
        item, category = entry
        if matches(item):
            accepted.append(item)
        else:
            filtered += 1
        progress = next_progress
        if category == 'overlay_rows':
            overlay += 1
        else:
            new += 1
        if expired():
            return OverlayBatch(progress, tuple(accepted), overlay, new, filtered, 'budget')
    return OverlayBatch(progress, tuple(accepted), overlay, new, filtered, 'limit')
