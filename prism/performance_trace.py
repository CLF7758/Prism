"""Low-overhead interaction trace and GUI stall diagnostics (local JSONL).

Schema version 2 adds, **without removing or renaming anything from v1**:
a real span/trace chain (``trace_id`` / ``span_id`` / ``parent_span_id``),
an immutable context that survives being carried to a worker thread, an
explicit origin action id instead of guessing from ``last_action``, three
modes (``off`` / ``normal`` / ``diagnostic``), an honest loss report for the
bounded queue, and a version snapshot of the source that produced the log.

Every existing call site -- ``record(kind, **fields)``, ``measured``,
``operation``, ``open_phase`` -- keeps working untouched; the new fields are
additive and may be absent from older logs.
"""
from collections import Counter, deque
from contextlib import contextmanager
from dataclasses import dataclass, replace
from functools import wraps
from logging.handlers import RotatingFileHandler
import contextvars
import gc
import hashlib
import json
import itertools
import os
from pathlib import Path
import queue
import re
import subprocess
import sys
import threading
import time
import traceback
import uuid
from PyQt6 import QtCore, QtGui, QtWidgets

_active = None

#: Bumped whenever the event contract gains fields. v1 readers keep working:
#: they simply ignore what they do not know.
SCHEMA_VERSION = 2

#: The *recording contract* version. ``SCHEMA_VERSION`` describes the shape
#: of one event line; this describes the *meaning* of the counters and gauges
#: inside it. It changes when a field keeps its name but changes its
#: semantics. A report that compares two logs must be able to say that the
#: two logs do not speak the same dialect.
RECORDING_CONTRACT_VERSION = 2

#: The size at which one log file is rotated, and how many rotated parts of
#: one file are kept next to it.
ROTATION_MAX_BYTES = 4*1024*1024
ROTATION_BACKUP_COUNT = 2

# Preserve significant work/failures in the existing session archive even
# after ordinary high-frequency records rotate. This is an additional per-run
# budget, not an unbounded second trace or a larger GUI-side queue.
DIAGNOSTIC_ARCHIVE_MAX_BYTES = 16*1024*1024


def _diagnostic_archive_candidate(value):
    event = value.get('event', '')
    if event in ('capture.helper.start', 'capture.helper.startup',
                 'capture.helper.ready', 'capture.helper.exit', 'capture.start',
                 'capture.captured', 'capture.cancelled', 'capture.pinned',
                 'model_feature_unavailable') or event.endswith('.rejected'):
        return True
    if event in ('gui_stall', 'gc'):
        return True
    if event == 'capture.inbox.cleanup':
        return bool(value.get('removed_files') or value.get('failed_files') or value.get('over_budget'))
    if value.get('error') or value.get('error_message') or event.endswith(('_failed', '.failed')):
        return True
    if event == 'catalog_install_slice_overrun':
        duration = value.get('elapsed_ms')
        return isinstance(duration, (int, float)) and duration >= 16
    if event in ('operation', 'library.stage.duration', 'slow_dispatch'):
        duration = value.get('duration_ms')
        return isinstance(duration, (int, float)) and duration >= 100
    return False

#: How many process logs (one per run) are kept side by side. Older runs are
#: removed at startup -- the *current* run's evidence is never deleted to
#: make room for itself.
RETAINED_LOG_FILES = 3
LOG_CLEANUP_INTERVAL_S = 15*60
LOG_RETENTION_AGE_S = 7*24*60*60
LOG_RETENTION_MAX_BYTES = 64*1024*1024


def _log_process_running(pid):
    """Fail closed: a process we cannot inspect protects its log family."""
    if pid == os.getpid():
        return True
    if sys.platform == 'win32':
        try:
            ctypes, kernel, _psapi, _counters = _memory_api()
            handle = kernel.OpenProcess(0x00100000, False, pid)  # SYNCHRONIZE, read only
            if not handle:
                return ctypes.get_last_error() != 87  # ERROR_INVALID_PARAMETER: no PID
            try:
                from ctypes import wintypes
                kernel.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
                kernel.WaitForSingleObject.restype = wintypes.DWORD
                return kernel.WaitForSingleObject(handle, 0) != 0
            finally:
                kernel.CloseHandle(handle)
        except Exception:
            return True
    try:
        os.kill(pid, 0)  # POSIX liveness probe only; never called on Windows.
    except ProcessLookupError:
        return False
    except OSError:
        return True
    return True


def _prune_trace_logs(folder, *, current_pid=None, now=None):
    """Clean exact owned log names by PID family, away from the GUI thread."""
    current_pid = os.getpid() if current_pid is None else current_pid
    now = time.time() if now is None else now
    pattern = re.compile(r'Prism-(?:operations-(\d+)\.jsonl(?:\.\d+)?|'
                         r'startup-(\d+)\.jsonl|crash-(\d+)\.json|native-crash-(\d+)\.log)$')
    groups = {}
    failures = 0
    for path in folder.iterdir():
        match = pattern.fullmatch(path.name)
        if not match or path.is_symlink() or not path.is_file():
            continue
        try:
            stat = path.stat()
        except OSError:
            failures += 1
            continue
        pid = int(next(value for value in match.groups() if value is not None))
        group = groups.setdefault(pid, dict(files=[], modified=0, size=0))
        group['files'].append((path, stat))
        group['modified'] = max(group['modified'], stat.st_mtime)
        group['size'] += stat.st_size
    newest = sorted(groups, key=lambda pid: groups[pid]['modified'], reverse=True)
    total = sum(group['size'] for group in groups.values())
    removed = released = protected = 0
    for rank in range(len(newest)-1, -1, -1):
        pid = newest[rank]
        group = groups[pid]
        expired = now-group['modified'] >= LOG_RETENTION_AGE_S
        if not (expired or rank >= RETAINED_LOG_FILES or total > LOG_RETENTION_MAX_BYTES):
            continue
        if pid == current_pid or _log_process_running(pid):
            protected += 1
            continue
        for path, before in group['files']:
            try:
                # Do not remove a file replaced or written since inspection.
                after = path.stat()
                if (after.st_size, after.st_mtime_ns, after.st_ino) != (
                        before.st_size, before.st_mtime_ns, before.st_ino):
                    continue
                path.unlink()
                removed += 1
                released += before.st_size
                total -= before.st_size
            except OSError:
                failures += 1
    return dict(removed_files=removed, released_bytes=released, failed_files=failures,
                protected_processes=protected, remaining_bytes=total,
                over_budget=total > LOG_RETENTION_MAX_BYTES,
                max_age_seconds=LOG_RETENTION_AGE_S, max_bytes=LOG_RETENTION_MAX_BYTES)

#: A collection shorter than this is not worth a line of its own.
GC_MIN_DURATION_MS = 10

# ── modes ────────────────────────────────────────────────────────────────
#: ``off``         no stacks, no scans, no formatting
#: ``normal``      what a user session should pay for by default
#: ``diagnostic``  per-session, time-limited; visibly more expensive
MODE_OFF = 'off'
MODE_NORMAL = 'normal'
MODE_DIAGNOSTIC = 'diagnostic'
_MODES = (MODE_OFF, MODE_NORMAL, MODE_DIAGNOSTIC)
_mode = MODE_NORMAL
_mode_deadline = None
_mode_lock = threading.Lock()


def mode():
    """The active mode. ``diagnostic`` expires on its own when it has a deadline."""
    if _mode == MODE_DIAGNOSTIC and _mode_deadline is not None:
        if time.monotonic() >= _mode_deadline:
            set_mode(MODE_NORMAL)
    return _mode


def set_mode(value, duration_s=None):
    """Switch mode for this session. ``duration_s`` bounds ``diagnostic``.

    Returns the mode actually in force afterwards. Unknown values raise, so a
    typo cannot silently disable the log the user was trying to turn on.
    """
    global _mode, _mode_deadline
    if value not in _MODES:
        raise ValueError('unknown trace mode %r; expected one of %r' % (value, _MODES))
    with _mode_lock:
        _mode = value
        _mode_deadline = (time.monotonic() + float(duration_s)
                          if value == MODE_DIAGNOSTIC and duration_s else None)
    trace = _active
    if trace is not None:
        trace.record('trace_mode', requested=value, effective=mode(),
                     priority=PRIORITY_STATE)
    # The counters describe the *normal* mode strategy. When the strategy
    # changes, the tail is written and the cumulative epoch starts over, so
    # a rollup is never read as covering both strategies.
    flush_hotpath_rollups(reason='mode_change', trace=trace)
    return _mode


# ── event priority ───────────────────────────────────────────────────────
#: Session boundary: never dropped, it is what makes a log readable at all.
PRIORITY_SESSION = 'session'
#: Something failed; includes terminal states of spans.
PRIORITY_ERROR = 'error'
#: The closing half of a span/trace.
PRIORITY_TERMINAL = 'terminal'
#: A state change worth keeping (catalog ready, mode switch, snapshot).
PRIORITY_STATE = 'state'
#: A sampled or per-item event; the first thing to go when the queue is full.
PRIORITY_SAMPLE = 'sample'
#: User input and the actions it triggered.
PRIORITY_INPUT = 'input'

_TERMINAL_KINDS = frozenset({
    'session_start', 'session_end', 'runtime', 'trace_mode', 'crash',
})
_ERROR_KINDS = frozenset({
    'gui_stall', 'gui_recovered', 'video.materialize_failed',
    'video.preview.failed', 'project_open_failed', 'project_save_failed',
    'material.render.failed', 'decode.failed',
})


def _priority_for(kind, explicit=None):
    if explicit:
        return explicit
    if kind in _TERMINAL_KINDS:
        return PRIORITY_SESSION
    if kind in _ERROR_KINDS or kind.endswith('_failed') or kind.endswith('.failed'):
        return PRIORITY_ERROR
    if kind in ('operation_start', 'open_phase'):
        # Both lifecycle halves use the same FIFO lane. Otherwise a terminal
        # can be written (and rotated away) before its older opening half.
        return PRIORITY_TERMINAL
    if kind in ('input', 'action') or kind.startswith('project_') or kind.startswith('catalog_'):
        return PRIORITY_INPUT
    if kind in CALLER_EVENTS:
        return PRIORITY_TERMINAL
    return PRIORITY_SAMPLE


# ── immutable trace context ───────────────────────────────────────────────
@dataclass(frozen=True)
class TraceContext:
    """Immutable. Safe to hand to a worker; never holds a Qt object."""

    trace_id: str = None
    span_id: str = None
    parent_span_id: str = None
    origin_action_id: int = None
    project_token: str = None
    open_epoch: int = None
    generation: int = None
    source_revision: str = None


_context: 'contextvars.ContextVar[TraceContext | None]' = contextvars.ContextVar(
    'prism_trace_context', default=None)
_context_restored = contextvars.ContextVar('prism_trace_context_restored', default=False)
_project_token = None
_open_epoch = None
_generation = None
_source_revision = None


def current_context():
    """This thread/task's context. Cheap: a ``ContextVar`` read."""
    return _context.get()


def current_context():
    """The caller's context, without changing it.

    ``capture_context`` freezes **and** installs a context; a caller that
    only wants to carry the context to a worker needs a copy it can hand
    over without touching the caller's own context.
    """
    return _context.get() or TraceContext()


def origin_action_context(origin_action_id):
    """A context that attributes spans to one action and to nothing else.

    Only the action id travels.  A cross-thread parent span id would be a
    parent chain the log cannot prove -- the worker is not a child of the
    GUI thread's span -- while the action id is the one fact the worker
    legitimately inherits from the thread that offered the work.
    """
    context = _context.get() or TraceContext()
    return replace(context, span_id=None, parent_span_id=None,
                   origin_action_id=origin_action_id)


def capture_context(**overrides):
    """Freeze the current context. Call this where a task is *created*."""
    current = _context.get() or TraceContext()
    bound = {'project_token': _project_token, 'open_epoch': _open_epoch,
             'generation': _generation, 'source_revision': _source_revision}
    updates = {}
    for key, value in bound.items():
        # A restored worker context describes the originating project, even
        # after the GUI has bound another project. Never rebase it on globals.
        inherited = getattr(current, key)
        updates[key] = (overrides[key] if key in overrides else
                        inherited if _context_restored.get() or inherited is not None else value)
    for key in ('trace_id', 'span_id', 'parent_span_id', 'origin_action_id'):
        if key in overrides and overrides[key] is not None:
            updates[key] = overrides[key]
    frozen = replace(current, **updates)
    _context.set(frozen)
    return frozen


@contextmanager
def restore_context(ctx):
    """Install a captured context for the duration of a block.

    QRunnable, QThreadPool, ThreadPoolExecutor and queued Qt callbacks do not
    inherit the creating task's context; the worker has to restore it by hand
    and the previous context is always put back in ``finally``.
    """
    token = _context.set(ctx)
    restored_token = _context_restored.set(True)
    try:
        yield ctx
    finally:
        _context.reset(token)
        _context_restored.reset(restored_token)


def bind_project(token, *, open_epoch=None, generation=None, source_revision=None):
    """Record which project this session is working on.

    ``token`` must be a *session identity*, not a bare file name: the same
    project reopened twice is two different sessions.
    """
    global _project_token, _open_epoch, _generation, _source_revision
    _project_token, _open_epoch = token, open_epoch
    _generation, _source_revision = generation, source_revision
    current = _context.get() or TraceContext()
    _context.set(replace(current, project_token=token, open_epoch=open_epoch,
                         generation=generation, source_revision=source_revision))


def new_origin_action_id():
    """Mint the id that a following span will use as its origin."""
    return next(_origin_ids)


_origin_ids = itertools.count(1)


def thread_identity():
    return threading.get_ident(), threading.current_thread().name


# ── recording helpers (pure; unit-testable without Qt) ───────────────────


#: Fallback lock for objects that do not carry one (a minimal test double).
#: The collection must keep working for them.
_FALLBACK_LOCK = threading.Lock()


def _lock_of(obj, attribute):
    """The named lock of ``obj``, or a shared fallback lock.

    A minimal test double may not carry the lock; the collection must still
    work for it, and the real instances keep their own.
    """
    lock = getattr(obj, attribute, None)
    return _FALLBACK_LOCK if lock is None else lock


def _gc_interval_store(obj):
    """The bounded store of GC intervals of ``obj``.

    A minimal test double may not carry one. It gets an empty store, so the
    collection keeps working for it instead of failing silently and
    swallowing the event it was about to write.
    """
    store = getattr(obj, '_gc_intervals', None)
    if store is None:
        store = deque(maxlen=32)
        try:
            obj._gc_intervals = store
        except Exception:
            pass
    return store


def union_intervals(intervals):
    """Merge overlapping ``[start, end)`` spans and return the merged list.

    Overlapping spans are **not** added up. Two collections that overlap
    cover the wall clock once, not twice: a GC pause inside a dispatch
    inside a stall is one pause, and counting it twice invents time that
    never elapsed.
    """
    spans = sorted((float(start), float(end)) for start, end in intervals
                   if start is not None and end is not None and end > start)
    merged = []
    for start, end in spans:
        if merged and start <= merged[-1][1]:
            if end > merged[-1][1]:
                merged[-1][1] = end
        else:
            merged.append([start, end])
    return [(start, end) for start, end in merged]


def _recording_owner():
    """The identity the work being recorded belongs to.

    ``(project_token, open_epoch, generation)``. A reopened project is a
    different session identity even when the file name is the same, so
    cumulative counters can be attributed to the right owner.
    """
    ctx = _context.get()
    token = getattr(ctx, 'project_token', None) if ctx is not None else None
    epoch = getattr(ctx, 'open_epoch', None) if ctx is not None else None
    generation = getattr(ctx, 'generation', None) if ctx is not None else None
    if token is None:
        token, epoch, generation = _project_token, _open_epoch, _generation
    return (token, epoch, generation)


# ── hot-path sampling contract ───────────────────────────────────────────

#: How often a hot-path rollup is written. The rollup carries the window
#: delta *and* the cumulative counters, so nothing that happened between
#: two rollups is lost.
HOTPATH_ROLLUP_INTERVAL_S = 2.0

#: ``name -> _HotpathRollup``. The registry is module level because the
#: decorator is applied at import time, long before a trace exists.
_HOTPATH_ROLLUPS = {}
_HOTPATH_LOCK = threading.RLock()


class _HotpathRollup:
    """The cumulative counters of one hot path and the owner they belong to.

    Cumulative counters are only meaningful together with their owner. When
    the owner changes -- another project, the same project reopened -- the
    previous owner's tail is flushed first and the counters start over, so a
    new project never inherits the previous project's work as its own.
    """

    __slots__ = ('name', 'minimum_ms', 'calls', 'total_ms', 'max_ms', 'failed',
                 'owner', 'owner_key', 'reset_reason', 'window_calls',
                 'window_total_ms', 'window_max_ms', 'window_failed_calls',
                 'last_emit', 'lock')

    def __init__(self, name, minimum_ms, owner=(None, None, None)):
        self.name = name
        self.minimum_ms = minimum_ms
        self.calls = 0
        self.total_ms = 0.0
        self.max_ms = 0.0
        self.failed = 0
        self.owner = tuple(owner)
        #: ``(token, epoch)`` is what the hot path compares on every call.
        #: The generation is only reported, never compared.
        self.owner_key = (self.owner[0], self.owner[1])
        self.reset_reason = 'initial'
        self.window_calls = 0
        self.window_total_ms = 0.0
        self.window_max_ms = 0.0
        self.window_failed_calls = 0
        self.last_emit = 0.0
        self.lock = threading.RLock()

    def add(self, elapsed_ms, failed):
        """Accumulate one call.

        This is the only place the counters are updated, so the fast path
        and the tests can never drift apart.
        """
        self.calls += 1
        self.total_ms += elapsed_ms
        if elapsed_ms > self.max_ms:
            self.max_ms = elapsed_ms
        if failed:
            self.failed += 1
        self.window_calls += 1
        self.window_total_ms += elapsed_ms
        if elapsed_ms > self.window_max_ms:
            self.window_max_ms = elapsed_ms
        if failed:
            self.window_failed_calls += 1

    def reset_window(self):
        self.window_calls = 0
        self.window_total_ms = 0.0
        self.window_max_ms = 0.0
        self.window_failed_calls = 0

    def reset_all(self, reason):
        """Start a new cumulative epoch for a new owner."""
        self.calls = 0
        self.total_ms = 0.0
        self.max_ms = 0.0
        self.failed = 0
        self.reset_window()
        self.reset_reason = reason


def _hotpath_payload(state, *, reset_reason):
    """The fields of one hot-path rollup line.

    ``calls``/``total_ms``/``max_ms`` stay **cumulative** since the last
    reset; the ``window_*`` fields are the delta since the previous rollup.
    Both are published so neither has to be re-derived by the reader.
    """
    token, epoch, generation = state.owner
    return {
        'name': state.name,
        'calls': state.calls,
        'total_ms': round(state.total_ms, 3),
        'max_ms': round(state.max_ms, 3),
        'failed_calls': state.failed,
        'window_calls': state.window_calls,
        'window_total_ms': round(state.window_total_ms, 3),
        'window_max_ms': round(state.window_max_ms, 3),
        'window_failed_calls': state.window_failed_calls,
        'counters_cumulative': True,
        'cumulative_scope': 'since_last_reset',
        'reset_reason': reset_reason,
        'owner_project_token': token,
        'owner_open_epoch': epoch,
        'owner_generation': generation,
        'minimum_sample_ms': state.minimum_ms,
        'sampling_strategy': 'threshold_rollup',
        # Snapshot only. Resolving an expired mode here can recursively flush
        # every rollup while this state lock is held.
        'trace_mode': _mode,
        'recording_contract_version': RECORDING_CONTRACT_VERSION,
    }


def _take_hotpath_state(state, *, reason):
    """Snapshot/reset under one state lock; no recorder call or mode change."""
    with state.lock:
        if state.calls == 0 and state.window_calls == 0:
            return None
        payload = _hotpath_payload(state, reset_reason=state.reset_reason)
        payload['flush_reason'] = reason
        state.reset_all(reason)
        state.reset_reason = reason
        state.last_emit = time.perf_counter()
        return payload


def _flush_hotpath_state(state, *, reason, trace=None):
    payload = _take_hotpath_state(state, reason=reason)
    target = trace or _active
    if payload is not None and target is not None:
        try:
            target.record('hotpath_rollup', **payload)
        except Exception:
            pass
    return payload


def flush_hotpath_rollups(*, reason='manual', trace=None):
    """Flush every hot-path counter that has not been written yet.

    Called when the recording ends and when the mode changes, so the tail of
    the counters is never lost and never carried into the next owner.
    """
    with _HOTPATH_LOCK:
        states = list(_HOTPATH_ROLLUPS.values())
    written = []
    for state in states:
        payload = _flush_hotpath_state(state, reason=reason, trace=trace)
        if payload is not None:
            written.append(payload)
    return written


def _hotpath_registry_snapshot():
    """The declared state of every hot path that has been used so far."""
    with _HOTPATH_LOCK:
        return {name: {'minimum_sample_ms': state.minimum_ms,
                       'calls': state.calls,
                       'owner_project_token': state.owner[0],
                       'reset_reason': state.reset_reason}
                for name, state in _HOTPATH_ROLLUPS.items()}


def _hotpath_state(name, minimum_ms):
    """The counter object of one hot path, created once per name."""
    with _HOTPATH_LOCK:
        state = _HOTPATH_ROLLUPS.get(name)
        if state is None:
            state = _HotpathRollup(name, minimum_ms)
            _HOTPATH_ROLLUPS[name] = state
        return state


# ── version snapshot ─────────────────────────────────────────────────────
#: The files whose on-disk state decides what "the code that produced this
#: log" meant. Hashed once in the background: a stat/hash per event on the
#: GUI thread would cost more than the log is worth.
VERSION_KEY_FILES = (
    'config/settings.py', 'asset_records.py', 'catalog_match_record.py',
    'performance_trace.py', 'fileio/sql.py', 'view.py', 'window_workspace.py',
    'project_catalog.py', 'project_catalog_index.py', 'catalog_browser.py',
    'widgets/asset_canvas.py', 'image_decode.py', 'scene.py', 'items.py',
    'widgets/asset_browser.py', 'aggregate_save_delta.py', 'resource_budget.py',
    'actions/storage_workflow.py', 'storage/migration.py', 'storage/leases.py',
    'upgrade_preview.py', 'source_read_status.py', 'widgets/detail_panel.py',
)


def _git_revision():
    """commit + dirty, or ``unknown``. Never initialises a repository."""
    def run(args):
        try:
            completed = subprocess.run(args, capture_output=True, text=True,
                                       timeout=5, cwd=str(Path(__file__).parent))
        except (OSError, subprocess.SubprocessError):
            return None
        return completed.stdout.strip() if completed.returncode == 0 else None

    commit = run(['git', 'rev-parse', 'HEAD'])
    if not commit:
        return {'available': False, 'commit': 'unknown', 'dirty': 'unknown',
                'note': 'git not available or not a repository; not initialised'}
    status = run(['git', 'status', '--porcelain'])
    return {'available': True, 'commit': commit,
            'dirty': bool(status) if status is not None else 'unknown',
            'changed_files': len(status.splitlines()) if status else 0}


def build_version_snapshot():
    """Hash the key files once. Cheap enough for a background thread only."""
    root = Path(__file__).parent
    files = {}
    inconsistent = False
    for name in VERSION_KEY_FILES:
        target = root / name
        entry = {'exists': target.is_file()}
        if entry['exists']:
            try:
                before = target.stat()
                digest = hashlib.sha256()
                with target.open('rb') as handle:
                    for chunk in iter(lambda: handle.read(1 << 20), b''):
                        digest.update(chunk)
                after = target.stat()
                entry.update(size_bytes=after.st_size,
                             mtime=round(after.st_mtime, 3),
                             sha256=digest.hexdigest()[:32])
                # A file edited while we hashed it must not be presented as
                # evidence for the bytes that actually ran.
                if (before.st_size, before.st_mtime) != (after.st_size, after.st_mtime):
                    inconsistent = True
                    entry['changed_during_hash'] = True
            except OSError as error:
                entry['error'] = str(error)
        files[name] = entry
    return {
        'schema_version': SCHEMA_VERSION,
        'source_mode': 'frozen' if getattr(sys, 'frozen', False) else 'source',
        'entry_point': ' '.join(sys.argv[:3]) if sys.argv else '',
        'python': sys.version,
        'executable': sys.executable,
        'cwd': str(Path.cwd()),
        'files': files,
        'version_snapshot_inconsistent': inconsistent,
        'note': 'the module set loaded at start may differ from what is on '
                'disk now; this snapshot states the state of these files at '
                'collection time, not a proof of every loaded byte code',
        'git': _git_revision(),
        'gpu_info': 'unknown',
    }


def trace_stats():
    """Queue/loss self-report. Bounded queues cannot promise never to drop."""
    trace = _active
    if trace is None:
        return {'active': False}
    return {
        'active': True,
        'mode': mode(),
        'queued': trace.events.qsize() + trace.important.qsize(),
        'important_queued': trace.important.qsize(),
        'capacity': trace.events.maxsize + trace.important.maxsize,
        'queue_peak': trace.queue_peak,
        'dropped_total': trace.dropped,
        'dropped_by_kind': dict(trace.dropped_by_kind),
        'seq_gap_count': trace.seq_gaps,
        'seq_gap_basis': 'known_dropped_events',
        'sequence_order_guaranteed': False,
        'trace_incomplete': trace.trace_incomplete,
        'writer_errors': trace.writer_errors,
        'writer_reopens': trace.writer_reopens,
        'record_self_ms_total': round(trace.self_ms_total, 3),
        'record_self_ms_max': round(trace.self_ms_max, 3),
        'last_written_sequence': trace._last_written_sequence,
    }


#: The access rights needed to read the memory counters of another process.
_PROCESS_QUERY_INFORMATION = 0x0400
_PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
_PROCESS_VM_READ = 0x0010

#: The Win32 memory counter API, built on first use and reused afterwards.
#:
#: ``psapi.GetProcessMemoryInfo`` fills one structure with the working set,
#: the private bytes and the page file usage of a process.  This process and
#: its child processes are read with the same counter, so their numbers are
#: the same unit.
_MEMORY_API = None
_MEMORY_API_LOCK = threading.Lock()


def _memory_api():
    """The Win32 memory counter API: the counter structure and its entry points."""
    global _MEMORY_API
    with _MEMORY_API_LOCK:
        if _MEMORY_API is not None:
            return _MEMORY_API
        import ctypes
        from ctypes import wintypes
        class Counters(ctypes.Structure):
            _fields_ = [('cb', wintypes.DWORD), ('faults', wintypes.DWORD)] + [
                (name, ctypes.c_size_t) for name in ('peak', 'working', 'peak_paged', 'paged',
                                                    'peak_nonpaged', 'nonpaged', 'pagefile', 'peak_pagefile')]
        kernel = ctypes.WinDLL('kernel32', use_last_error=True)
        kernel.GetCurrentProcess.restype = wintypes.HANDLE
        kernel.OpenProcess.restype = wintypes.HANDLE
        kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel.CloseHandle.restype = wintypes.BOOL
        kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        psapi = ctypes.WinDLL('psapi', use_last_error=True)
        psapi.GetProcessMemoryInfo.restype = wintypes.BOOL
        psapi.GetProcessMemoryInfo.argtypes = [wintypes.HANDLE, ctypes.POINTER(Counters),
                                                wintypes.DWORD]
        _MEMORY_API = (ctypes, kernel, psapi, Counters)
        return _MEMORY_API


def process_memory_bytes():
    """Current process resident bytes from the OS, without decoding any assets."""
    if sys.platform != 'win32':
        return None
    try:
        ctypes, kernel, psapi, Counters = _memory_api()
        counters = Counters()
        counters.cb = ctypes.sizeof(counters)
        if psapi.GetProcessMemoryInfo(kernel.GetCurrentProcess(),
                                      ctypes.byref(counters), counters.cb):
            return int(counters.working)
    except Exception:  # pragma: no cover - a diagnostic must never break the app
        pass
    return None


def _working_set_bytes(pid):
    """The working set of one other process in bytes, or ``None``.

    The same counter this process is measured with, so a child process and
    this process are the same unit.
    """
    if sys.platform != 'win32':
        return None
    try:
        ctypes, kernel, psapi, Counters = _memory_api()
        for rights in (_PROCESS_QUERY_INFORMATION | _PROCESS_VM_READ,
                       _PROCESS_QUERY_LIMITED_INFORMATION):
            handle = kernel.OpenProcess(rights, False, int(pid))
            if not handle:
                continue
            try:
                counters = Counters()
                counters.cb = ctypes.sizeof(counters)
                if psapi.GetProcessMemoryInfo(handle, ctypes.byref(counters), counters.cb):
                    return int(counters.working)
                return None
            finally:
                kernel.CloseHandle(handle)
    except Exception:  # pragma: no cover - defensive only
        return None
    return None


def process_io_bytes():
    """OS process transfer counters, not disk bytes or per-stage attribution."""
    if sys.platform != 'win32':
        return dict(read_bytes=None, write_bytes=None, source='unknown',
                    reason='process I/O backend unavailable on this platform')
    try:
        import ctypes
        from ctypes import wintypes
        class Counters(ctypes.Structure):
            _fields_ = [(name, ctypes.c_ulonglong) for name in (
                'read_ops', 'write_ops', 'other_ops', 'read', 'write', 'other')]
        kernel = ctypes.WinDLL('kernel32', use_last_error=True)
        kernel.GetCurrentProcess.restype = wintypes.HANDLE
        function = kernel.GetProcessIoCounters
        function.argtypes = [wintypes.HANDLE, ctypes.POINTER(Counters)]
        function.restype = wintypes.BOOL
        counters = Counters()
        if function(kernel.GetCurrentProcess(), ctypes.byref(counters)):
            return dict(read_bytes=counters.read, write_bytes=counters.write,
                        other_bytes=counters.other, source='GetProcessIoCounters',
                        scope='current_process_lifetime', physical_disk_bytes=False,
                        per_operation_attribution=False, includes_child_processes=False)
    except (OSError, AttributeError):
        pass
    return dict(read_bytes=None, write_bytes=None, source='unknown',
                reason='GetProcessIoCounters unavailable or failed')


# ── child processes ──────────────────────────────────────────────────────

#: ``CreateToolhelp32Snapshot``: take a snapshot of the process list.
_TH32CS_SNAPPROCESS = 0x00000002

#: How many children one sample names individually.
CHILD_SAMPLE_MAX_LISTED = 16

#: How often the background sampler reads the child processes.
CHILD_SAMPLE_INTERVAL = 5.0


def _toolhelp_children(parent_pid):
    """The direct child processes of ``parent_pid``: ``(pid, name)`` pairs.

    Enumerated with the documented Toolhelp32 API and nothing else, so no
    third-party dependency is needed.  The walk costs tens of milliseconds
    (measured 25-35 ms on this machine), so it must never run on the GUI
    thread: the caller samples in the background and reads the cached result.
    """
    import ctypes
    from ctypes import wintypes
    class ProcessEntry32W(ctypes.Structure):
        _fields_ = [('dwSize', wintypes.DWORD),
                    ('cntUsage', wintypes.DWORD),
                    ('th32ProcessID', wintypes.DWORD),
                    ('th32DefaultHeapID', ctypes.c_size_t),
                    ('th32ModuleID', wintypes.DWORD),
                    ('cntThreads', wintypes.DWORD),
                    ('th32ParentProcessID', wintypes.DWORD),
                    ('pcPriClassBase', wintypes.LONG),
                    ('dwFlags', wintypes.DWORD),
                    ('szExeFile', wintypes.WCHAR * 260)]
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    kernel.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
    kernel.Process32FirstW.restype = wintypes.BOOL
    kernel.Process32FirstW.argtypes = [wintypes.HANDLE, ctypes.POINTER(ProcessEntry32W)]
    kernel.Process32NextW.restype = wintypes.BOOL
    kernel.Process32NextW.argtypes = [wintypes.HANDLE, ctypes.POINTER(ProcessEntry32W)]
    kernel.CloseHandle.restype = wintypes.BOOL
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    invalid = (1 << (8 * ctypes.sizeof(ctypes.c_void_p))) - 1
    snapshot = kernel.CreateToolhelp32Snapshot(_TH32CS_SNAPPROCESS, 0)
    if not snapshot or snapshot == invalid:
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        entry = ProcessEntry32W()
        entry.dwSize = ctypes.sizeof(entry)
        children = []
        if not kernel.Process32FirstW(snapshot, ctypes.byref(entry)):
            return children
        while True:
            if int(entry.th32ParentProcessID) == int(parent_pid):
                children.append((int(entry.th32ProcessID), str(entry.szExeFile)))
            if not kernel.Process32NextW(snapshot, ctypes.byref(entry)):
                break
        return children
    finally:
        kernel.CloseHandle(snapshot)


class _ChildProcessSampler:
    """The child-process sample, taken in the background.

    The enumeration costs tens of milliseconds, so it never runs on the GUI
    thread: this thread takes the sample and every reader only reads the
    cached result.  The writer thread is not used for it either -- a writer
    that falls behind loses the events that explain a stall, which is worse
    than the gap this sample closes.
    """

    INTERVAL = CHILD_SAMPLE_INTERVAL

    def __init__(self):
        self._condition = threading.Condition()
        self._sample = None
        self._sequence = 0
        self._requested = False
        self._thread = None

    def start(self):
        """Start the sampler thread.  It takes its first sample at once."""
        with self._condition:
            if self._thread is not None and self._thread.is_alive():
                return self._thread
            self._thread = threading.Thread(target=self._run, daemon=True,
                                            name='PrismChildSampler')
            self._thread.start()
            return self._thread

    def _run(self):
        while True:
            self._take_sample()
            with self._condition:
                self._sequence += 1
                self._condition.notify_all()
                if not self._requested:
                    self._condition.wait(self.INTERVAL)
                self._requested = False

    def _take_sample(self):
        started = time.perf_counter()
        try:
            sample = self._measure()
        except Exception as error:
            sample = {'bytes': None, 'count': None, 'children': [],
                      'source': 'unknown',
                      'error': '%s: %s' % (type(error).__name__, error)}
        sample['duration_ms'] = round((time.perf_counter() - started) * 1000, 3)
        sample['sampled_at'] = time.time()
        sample['monotonic'] = time.perf_counter()
        sample['thread'] = threading.current_thread().name
        with self._condition:
            self._sample = sample

    def _measure(self):
        children = _toolhelp_children(os.getpid())
        rows = []
        total = 0
        unreadable = 0
        for pid, name in children:
            size = _working_set_bytes(pid)
            if size is None:
                unreadable += 1
                continue
            total += size
            rows.append({'pid': pid, 'name': name, 'bytes': size})
        rows.sort(key=lambda row: -row['bytes'])
        return {'bytes': total, 'count': len(rows),
                'children': rows[:CHILD_SAMPLE_MAX_LISTED],
                'children_truncated': len(rows) > CHILD_SAMPLE_MAX_LISTED,
                'children_unreadable': unreadable,
                'source': 'CreateToolhelp32Snapshot+GetProcessMemoryInfo',
                'rss_bytes': process_memory_bytes()}

    def sample(self):
        """The cached sample, or ``None`` when there is none yet."""
        with self._condition:
            return dict(self._sample) if self._sample is not None else None

    def request(self, timeout=5.0):
        """Take a fresh sample and wait for it.

        The caller blocks until the sample is there, so it must not be the
        GUI thread.
        """
        self.start()
        with self._condition:
            target = self._sequence + 1
            self._requested = True
            self._condition.notify_all()
            deadline = time.monotonic() + timeout
            while self._sequence < target:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return False
                self._condition.wait(remaining)
            return True


#: The one sampler of this process.
_child_sampler = _ChildProcessSampler()


def child_process_sample(*, refresh=False, timeout=5.0):
    """The cached sample of this process' child processes.

    The enumeration never runs on the calling thread: the sample is taken
    by a background thread and this only reads the cached result.  With
    ``refresh=True`` that thread is asked for a fresh sample and the caller
    waits for it -- **never on the GUI thread**.
    """
    _child_sampler.start()
    if refresh:
        _child_sampler.request(timeout=timeout)
    return _child_sampler.sample()


#: Events whose callers must be recorded, with how many frames to keep.
#:
#: A miss without a caller answers "the directory was unavailable" but not
#: "who asked while it was unavailable" -- which is the only question that
#: turns a number into a fix. The open phase had 25 of these in one run and
#: the owner had to read the source by hand to find out.
CALLER_EVENTS = frozenset({
    'catalog_scope_miss', 'catalog_scope_hit',
    'project_open_start', 'project_open_finished', 'project_open_scope_ready',
    'project_catalog_build_started', 'project_catalog_build_finished',
    'project_catalog_build_retried',
    'gui_stall', 'gui_recovered',
    'open_phase',
    # Diagnostics that must survive a saturated queue: a slow handler and a
    # GC pause are exactly the events that explain the stall they arrived in.
    'slow_dispatch', 'nested_dispatch', 'gc',
    # Slow SQL names the code that asked for it -- the one attribution the
    # stall histogram cannot give for time spent inside sqlite's C code.
    'sql.execute', 'sql.fetch',
    # Click -> screen-update latency over the threshold: keep it even when
    # the queue is saturated, it is the closest proxy to "felt lag".
    'slow_input_to_paint',
})
CALLER_FRAMES = 6


def _frame_signature(frame, max_frames=8):
    """A compact, deduplicated summary of a Python frame chain.

    Used by the sampler histograms: repeated samples of a busy main thread
    are folded into one bucket per code path, so a stall report stays small
    no matter how many samples were taken. Consecutive duplicate frames
    (e.g. recursive ``notify``) are collapsed.
    """
    parts = []
    last = None
    while frame is not None and len(parts) < max_frames:
        code = frame.f_code
        name = '%s:%d %s' % (code.co_filename.rsplit(os.sep, 1)[-1],
                             frame.f_lineno, code.co_name)
        if name != last:
            parts.append(name)
            last = name
        frame = frame.f_back
    return ' <- '.join(parts)


def _caller_stack():
    """The frames that asked, excluding this module's own plumbing.

    Walks outward from the caller and drops frames that belong to
    ``performance_trace`` itself, so the top frame is the business code that
    actually made the call.
    """
    frames = sys._current_frames()
    mine = threading.get_ident()
    here = globals()
    collected = []
    # Start one level above this function.
    frame = sys._getframe(1) if hasattr(sys, '_getframe') else None
    if frame is not None:
        while frame is not None and len(collected) < CALLER_FRAMES:
            code = frame.f_code
            if code.co_filename != here.get('__file__'):
                collected.append('%s:%d in %s' % (
                    code.co_filename.rsplit(os.sep, 1)[-1],
                    frame.f_lineno, code.co_name))
            frame = frame.f_back
    del frames, mine
    return collected


#: Field caps. Applied while the event is still a dict on the calling
#: thread, *before* anything is encoded -- constructing a 100 MB payload and
#: truncating it at the far end costs exactly what we are trying to avoid.
MAX_FIELDS = 96
MAX_STRING = 2000
MAX_SEQUENCE = 24
MAX_DICT = 24
MAX_STACK = 32

# Bounded payloads may lose observations, never the lifecycle identity/status.
_LIFECYCLE_FIELDS = (
    'event', 'sequence', 'session', 'time', 'monotonic', 'schema_version',
    'pid', 'thread_id', 'thread_name', 'priority', 'trace_id', 'span_id',
    'parent_span_id', 'name', 'stage', 'role', 'status', 'phase',
    'duration_ms', 'failed', 'cross_thread_handoff', 'handoff_id',
    'origin_action_id', 'project_token', 'open_epoch', 'generation',
    'source_revision', 'dropped_events', 'trace_incomplete', 'seq_gap_count',
    'writer_lifecycle_version', 'startup_archive_errors', 'end_scope',
    'stall_id', 'window_start_monotonic', 'window_end_monotonic',
    'dispatch_batch_version', 'dispatch_batch_count', 'batch_index', 'batch_count',
    'dispatch_interval_count', 'dispatch_intervals_retained', 'dispatch_intervals_omitted')


def _stall_dispatch_payloads(intervals):
    """Keep frozen scalar intervals, never lift generic payload/byte limits.

    Batches use the existing 24-item field cap; the 512-row ceiling matches
    dispatch history. Omitted history remains explicit, not complete evidence.
    """
    keys = ('dispatch_id', 'parent_dispatch_id', 'root_dispatch_id', 'depth',
            'target', 'event_type', 'start_monotonic', 'end_monotonic',
            'interval_open_at_stall_end', 'synthetic_start')
    rows = []
    for entry in intervals[:512]:
        row = {}
        for key in keys:
            value = entry.get(key)
            if isinstance(value, str):
                # Recorder-owned IDs/class names are short; cap defensive input.
                value = value[:120]
            if value is None or type(value) in (str, bool, int, float):
                row[key] = value
        rows.append(row)
    batches = ([rows[start:start+MAX_SEQUENCE] for start in range(0, len(rows), MAX_SEQUENCE)]
               if len(rows) > MAX_SEQUENCE else [])
    metadata = dict(dispatch_batch_version=1, dispatch_batch_count=len(batches),
                    dispatch_interval_count=len(intervals),
                    dispatch_intervals_retained=len(rows),
                    dispatch_intervals_omitted=len(intervals)-len(rows))
    return rows[:MAX_SEQUENCE], batches, metadata


def _cap(value, depth=0):
    """Bound one field without walking into Qt objects or blobs."""
    if isinstance(value, str):
        return value if len(value) <= MAX_STRING else value[:MAX_STRING] + '…[cut]'
    if isinstance(value, (bytes, bytearray, memoryview)):
        return '<%d bytes>' % len(value)
    if isinstance(value, dict):
        if depth >= 2 or len(value) > MAX_DICT:
            return {'_capped': 'dict(%d)' % len(value)}
        return {str(key)[:120]: _cap(item, depth+1)
                for index, (key, item) in enumerate(value.items())
                if index < MAX_DICT}
    if isinstance(value, (list, tuple, set, frozenset)):
        if depth >= 2 or len(value) > MAX_SEQUENCE:
            return ['…[capped %d items]' % len(value)]
        return [_cap(item, depth+1) for item in list(value)[:MAX_SEQUENCE]]
    if isinstance(value, (int, float, bool, type(None))):
        return value
    if isinstance(value, QtCore.QObject) or isinstance(value, QtGui.QPixmap):
        return '<%s>' % type(value).__name__
    text = str(value)
    return text if len(text) <= MAX_STRING else text[:MAX_STRING] + '…[cut]'


#: Bounded accounting of diagnostic failures. The counter is updated
#: **without** going through ``record()``: a failing log channel must never
#: be asked to report its own failure. The number of distinct kinds is
#: capped so a broken channel cannot flood the report.
_DIAGNOSTIC_FAILURES = Counter()
_DIAGNOSTIC_FAILURE_TOTAL = 0
_DIAGNOSTIC_FAILURE_KINDS_MAX = 8
_DIAGNOSTIC_FAILURE_LOCK = threading.Lock()


def _note_diagnostic_failure(where, error=None):
    """Count one diagnostic failure. Never raises, never recurses."""
    global _DIAGNOSTIC_FAILURE_TOTAL
    try:
        with _DIAGNOSTIC_FAILURE_LOCK:
            _DIAGNOSTIC_FAILURE_TOTAL += 1
            if (where in _DIAGNOSTIC_FAILURES
                    or len(_DIAGNOSTIC_FAILURES) < _DIAGNOSTIC_FAILURE_KINDS_MAX):
                _DIAGNOSTIC_FAILURES[where] += 1
    except Exception:
        pass


def diagnostic_failures():
    """The bounded diagnostic failure report.

    It is published with the session summary and in the next healthy
    snapshot. When the writer itself is unavailable the state says
    ``not_persisted`` rather than pretending the evidence was stored.
    """
    with _DIAGNOSTIC_FAILURE_LOCK:
        return {
            'total': _DIAGNOSTIC_FAILURE_TOTAL,
            'by_kind': dict(_DIAGNOSTIC_FAILURES),
            'kinds_capped': len(_DIAGNOSTIC_FAILURES) >= _DIAGNOSTIC_FAILURE_KINDS_MAX,
            'note': ('诊断失败被计数而不是被吞掉；该计数器自身不经过 record()，'
                     '因此失败的日志通道不会被要求报告自己的失败。'),
        }


def record(kind, /, *, priority=None, **fields):
    """Append one event. Existing call sites need no change.

    A diagnostic failure is **counted**, never raised: the log must not be
    able to break the business it observes. Only ``Exception`` is isolated;
    ``KeyboardInterrupt``/``SystemExit`` and a user cancellation still
    propagate untouched.
    """
    trace = _active
    if trace is None:
        return
    try:
        trace.record(kind, priority=priority, **fields)
    except Exception as error:
        _note_diagnostic_failure('record', error)


def record_catalog_build_progress(*, build_id, processed, processed_this_run,
                                  total_records, total_known, final, _record=record):
    """Publish one catalog-build fact plus its explicit legacy mirror."""
    fields = dict(build_id=build_id, processed=int(processed),
                  processed_this_run=processed_this_run,
                  total_records=total_records, total_known=bool(total_known),
                  final=bool(final))
    _record('paged_catalog_build_progress', **fields)
    _record('paged_prepare_processed', phase='catalog_build', legacy=True,
            **fields)


def record_scope_prepared(*, open_id, canvas, scope_ids, pixmap_count,
                          non_pixmap_count, kinds, catalog_ready=True,
                          _record=record):
    """Publish one bounded scope fact without claiming a catalog-build total."""
    fields = dict(open_id=open_id, canvas=canvas, scope_ids=int(scope_ids),
                  pixmap_count=int(pixmap_count),
                  non_pixmap_count=int(non_pixmap_count), kinds=list(kinds),
                  processed=int(pixmap_count),
                  total_records=int(scope_ids) if catalog_ready else None,
                  total_known=bool(catalog_ready), catalog_ready=bool(catalog_ready),
                  total_records_scope='scope', final=True)
    _record('paged_scope_prepared', **fields)
    _record('paged_prepare_processed', phase='scope', legacy=True, **fields)


def _note_span_activity(trace, opening):
    """Keep the existing record-only adapter contract usable by embedders."""
    callback = getattr(trace, 'note_span_open' if opening else 'note_span_close', None)
    if callable(callback):
        callback()


class SpanHandle:
    """What ``span()`` yields, so a caller can add work counters as it goes."""

    __slots__ = ('operation', 'stage', 'span_id', 'parent_span_id', 'trace_id',
                 'extra', 'ended', 'begin_failed', 'activity_counted', 'recorder')

    def __init__(self, operation, stage, span_id, parent_span_id, trace_id, recorder=None):
        self.recorder = recorder
        self.operation = operation
        self.stage = stage
        self.span_id = span_id
        self.parent_span_id = parent_span_id
        self.trace_id = trace_id
        self.extra = {}
        self.ended = False
        #: The opening half could not be written. The span therefore has no
        #: lifecycle in the log and must not get a terminal half either.
        self.begin_failed = False
        #: The activity counter was incremented, so it must be decremented.
        self.activity_counted = False

    def set(self, **fields):
        """Attach work counters without allowing them to replace lifecycle identity."""
        if 'stage' in fields:
            self.stage = fields.pop('stage')
        reserved = {'name', 'span_id', 'parent_span_id', 'trace_id',
                    'role', 'status', 'priority', '_started'}
        self.extra.update({key: value for key, value in fields.items()
                           if key not in reserved})

    def end(self, *, status=None, **fields):
        if self.ended:
            return
        _end_span(self, status=status, **fields)


@contextmanager
def span(operation, *, stage=None, status=None, **fields):
    """Time a bounded stage *and* record a real parent/child chain.

    Synchronous nesting is handled by the ``ContextVar``. Asynchronous children
    do **not** inherit automatically: capture at task creation and restore in
    the worker. Cancellation and exceptions still produce a terminal status --
    the span is never left open.
    """
    trace = _active
    if trace is None or mode() == MODE_OFF:
        handle = SpanHandle(operation, stage, None, None, None)
        handle.extra.update(fields)
        try:
            yield handle
        finally:
            handle.ended = True
        return
    fields = {key: value for key, value in fields.items()
              if key not in ('priority', 'name', 'span_id', 'parent_span_id',
                             'trace_id', 'role', '_started')}
    parent = _context.get() or TraceContext()
    trace_id = parent.trace_id or uuid.uuid4().hex[:12]
    span_id = uuid.uuid4().hex[:16]
    handle = SpanHandle(operation, stage, span_id, parent.span_id, trace_id, recorder=trace)
    handle.extra.update(fields)
    handle.extra['_started'] = time.perf_counter()
    # The context is restored with its **token**, so the previous value is
    # restored exactly -- including "no context at all".
    context_token = _context.set(replace(parent, trace_id=trace_id, span_id=span_id,
                                         parent_span_id=parent.span_id))
    try:
        accepted = trace.record('operation_start', name=operation, stage=stage,
                     span_id=span_id, parent_span_id=parent.span_id, trace_id=trace_id,
                     status='start', role='start',
                     thread=threading.current_thread().name, **fields)
        # Record-only adapters historically returned None. Only an explicit
        # rejection means no opening lifecycle was accepted.
        if accepted is False:
            handle.begin_failed = True
    except Exception as error:
        # The diagnostic entry failed. The business must still run, exactly
        # once, with its original semantics: no retry, no fake return value.
        # The span is marked as having no opening half.
        _note_diagnostic_failure('span_begin', error)
        handle.begin_failed = True
    else:
        if not handle.begin_failed:
            try:
                _note_span_activity(trace, True)
            except Exception as error:
                _note_diagnostic_failure('span_activity', error)
            else:
                handle.activity_counted = True
    try:
        yield handle
    except BaseException:
        _end_span(handle, status=status if type(status) is str and status else 'error', failed=True)
        raise
    else:
        _end_span(handle, status=status if type(status) is str and status else 'end')
    finally:
        _context.reset(context_token)


def _end_span(handle, *, status=None, **fields):
    """Emit the closing half of a span once. Never raises into business code.

    The closing half is only written when the opening half was written. A
    span whose begin failed gets no terminal half: that would be a
    fabricated lifecycle, and the card is explicit that a missing opening
    must never be answered with a matching-looking closing record.
    """
    if handle.ended:
        return
    handle.ended = True
    trace = handle.recorder
    handle.recorder = None  # A retained handle must not keep a closed recorder alive.
    if trace is None or handle.span_id is None:
        return
    started = handle.extra.pop('_started', None)
    extra = dict(handle.extra)
    extra.update(fields)
    extra.pop('priority', None)
    final_status = status if type(status) is str and status else 'end'
    failure = extra.get('failed', final_status == 'error')
    # Instrumentation metadata must never invoke arbitrary __bool__ while a
    # business exception is unwinding.
    failed = failure if type(failure) is bool else final_status == 'error'
    payload = dict(extra)
    payload.update(name=handle.operation, stage=extra.get('stage', handle.stage),
                   span_id=handle.span_id, parent_span_id=handle.parent_span_id,
                   trace_id=handle.trace_id, status=final_status, role='end',
                   failed=failed,
                   thread=threading.current_thread().name)
    if started is not None:
        payload['duration_ms'] = round((time.perf_counter()-started)*1000, 3)
    if handle.activity_counted:
        try:
            _note_span_activity(trace, False)
        except Exception as error:
            _note_diagnostic_failure('span_activity', error)
    if handle.begin_failed:
        try:
            trace.record('span_diagnostic_failed', name=handle.operation,
                         span_id=handle.span_id,
                         parent_span_id=handle.parent_span_id,
                         trace_id=handle.trace_id, role='attachment',
                         status='diagnostic_failed',
                         duration_ms=payload.get('duration_ms'),
                         reason='begin_record_failed')
        except Exception as error:
            _note_diagnostic_failure('span_end', error)
        return
    try:
        trace.record('operation', priority=PRIORITY_TERMINAL, **payload)
    except Exception as error:
        # A failing diagnostic must never overwrite the business exception
        # that is on its way out of the caller's ``except`` branch.
        _note_diagnostic_failure('span_end', error)


def measured(name, minimum_ms=0, **extra):
    """Time one call and record it as a real span.

    ``extra`` adds fields to the recorded span (the sampling strategy and
    the mode, for example) without changing what the function does.
    """
    def decorate(function):
        @wraps(function)
        def run(*args, **kwargs):
            if _active is None or mode() == MODE_OFF:
                return function(*args, **kwargs)
            # A measured invocation is a real child span.  The historical
            # ``minimum_ms`` argument remains accepted; lifecycle integrity
            # takes precedence over dropping the opening half after it exists.
            with span(name, minimum_ms=minimum_ms, **extra):
                return function(*args, **kwargs)
        return run
    return decorate


def measured_hotpath(name, minimum_ms=16):
    """Normal mode aggregates fast calls; diagnostic mode keeps full spans.

    Slow samples are standalone observations, never a terminal half of a
    span whose opening was dropped. Nested work keeps its existing parent.

    The counters live in a module level registry keyed by ``name``, so they
    carry the owner they belong to and are reset when the owner changes.
    """
    def decorate(function):
        full = measured(name, minimum_ms, sampling_strategy='full_span_diagnostic',
                        trace_mode=MODE_DIAGNOSTIC)(function)
        # The counter object is created once per name and captured here, so
        # the per-call path never looks it up.
        state = _hotpath_state(name, minimum_ms)
        @wraps(function)
        def run(*args, **kwargs):
            trace = _active
            if trace is None or mode() == MODE_OFF:
                return function(*args, **kwargs)
            if mode() == MODE_DIAGNOSTIC:
                return full(*args, **kwargs)
            started = time.perf_counter()
            failed = False
            try:
                return function(*args, **kwargs)
            except BaseException:
                failed = True
                raise
            finally:
                ended = time.perf_counter()
                elapsed = (ended-started)*1000
                if failed or elapsed >= minimum_ms:
                    try:
                        trace.record('hotpath_sample', name=name,
                                     duration_ms=round(elapsed, 3),
                                     start_mono=started, role='attachment',
                                     failed=failed,
                                     observation='threshold_sample_not_complete_span',
                                     sampling_strategy='threshold_sample',
                                     minimum_sample_ms=minimum_ms,
                                     trace_mode=mode(),
                                     recording_contract_version=RECORDING_CONTRACT_VERSION)
                    except Exception:
                        # A diagnostic must never replace the exception the
                        # business code raised.
                        pass
                # Everything below runs on **every** fast-path call, so it
                # stays as short as possible: one context read, one lock,
                # plain arithmetic. The owner is compared, not rebuilt.
                ctx = _context.get()
                if ctx is None:
                    owner_key = (_project_token, _open_epoch)
                else:
                    owner_key = (ctx.project_token, ctx.open_epoch)
                    if owner_key[0] is None:
                        owner_key = (_project_token, _open_epoch)
                payload = None
                owner_payload = None
                with state.lock:
                    if owner_key != state.owner_key:
                        # Capture/reset under this lock, but never record while
                        # holding it: record() may resolve an expired mode.
                        owner_payload = _take_hotpath_state(state, reason='owner_changed')
                        state.owner = (owner_key[0], owner_key[1],
                                       ctx.generation if ctx is not None
                                       else _generation)
                        state.owner_key = owner_key
                    state.add(elapsed, failed)
                    if ended - state.last_emit >= HOTPATH_ROLLUP_INTERVAL_S:
                        state.last_emit = ended
                        payload = _hotpath_payload(state,
                                                  reset_reason=state.reset_reason)
                        state.reset_window()
                for outgoing in (owner_payload, payload):
                    if outgoing is not None:
                        try:
                            trace.record('hotpath_rollup', **outgoing)
                        except Exception:
                            pass
        return run
    return decorate


@contextmanager
def operation(name, **fields):
    """Time a bounded stage and yield its historical mutable field dictionary."""
    fields = {key: value for key, value in fields.items() if key != 'name'}
    trace = _active
    if trace is None or mode() == MODE_OFF:
        yield fields
        return
    origin_action = trace.last_action
    mutable = dict(fields)
    with span(name, origin_action=origin_action, **fields) as observed:
        try:
            yield mutable
        finally:
            # Copy the caller's final counters without exposing the span's
            # private timing key in the mutable dictionary.
            observed.set(**mutable)


@contextmanager
def open_phase(stage, **fields):
    """Time one named project-open stage using the existing event name."""
    trace = _active
    if trace is None or mode() == MODE_OFF:
        yield fields
        return
    fields = {key: value for key, value in fields.items()
              if key not in ('stage', 'phase', 'priority', 'role', 'status',
                             'trace_id', 'span_id', 'parent_span_id',
                             'thread', 'origin_action', 'failed', 'duration_ms')}
    parent = _context.get() or TraceContext()
    trace_id = parent.trace_id or uuid.uuid4().hex[:12]
    span_id = uuid.uuid4().hex[:16]
    started = time.perf_counter()
    mutable = dict(fields)
    # The context is restored with its **token**, so the previous value is
    # restored exactly -- including "no context at all".
    context_token = _context.set(replace(parent, trace_id=trace_id, span_id=span_id,
                                         parent_span_id=parent.span_id))
    try:
        accepted = trace.record('open_phase', stage=stage, phase='start', status='start',
                     role='start', trace_id=trace_id, span_id=span_id,
                     parent_span_id=parent.span_id,
                     thread=threading.current_thread().name,
                     origin_action=trace.last_action, **fields)
    except Exception as error:
        # The diagnostic entry failed: the stage must still run, exactly once.
        _note_diagnostic_failure('open_phase_begin', error)
        begin_accepted = False
        activity_counted = False
    else:
        begin_accepted = accepted is not False
        activity_counted = False
        if begin_accepted:
            try:
                _note_span_activity(trace, True)
            except Exception as error:
                _note_diagnostic_failure('span_activity', error)
            else:
                activity_counted = True
    failed = False
    try:
        yield mutable
    except BaseException:
        failed = True
        raise
    finally:
        if activity_counted:
            try:
                _note_span_activity(trace, False)
            except Exception as error:
                _note_diagnostic_failure('span_activity', error)
        try:
            if begin_accepted:
                payload = {key: value for key, value in mutable.items()
                           if key not in ('priority', 'origin_action')}
                payload.update(stage=stage, phase='end',
                               status='error' if failed else 'end', role='end',
                               trace_id=trace_id, span_id=span_id,
                               parent_span_id=parent.span_id,
                               duration_ms=round((time.perf_counter()-started)*1000, 3),
                               failed=failed, thread=threading.current_thread().name)
                trace.record('open_phase', priority=PRIORITY_TERMINAL, **payload)
        except Exception as error:
            # A failing diagnostic must never overwrite the business
            # exception that is on its way out.
            _note_diagnostic_failure('open_phase_end', error)
        finally:
            # The context is restored **last**, and always: a diagnostic
            # failure must not leak a wrong parent/span context.
            _context.reset(context_token)


# ── queue accounting (the contract every work queue reports through) ────

#: The counters every work queue reports. ``submitted`` is what was offered,
#: ``started`` what a worker actually began, ``completed`` what the worker
#: finished, ``installed`` what reached the GUI. The rest is the honest
#: bookkeeping of what never made it: cancelled, stale, rejected, coalesced
#: (merged into a request that was already running) or retried.
QUEUE_COUNTERS = ('submitted', 'started', 'completed', 'installed', 'cancelled',
                   'cancel_after_start', 'stale_discarded', 'rejected', 'retried',
                   'coalesced', 'failed')

#: The queue categories the diagnosis distinguishes.
QUEUE_CATEGORIES = ('image_decode', 'catalog', 'gui_install', 'layout',
                    'material_texture', 'material_batch', 'video_materialize')


#: The lifecycle states of one task. A task reaches **exactly one** terminal
#: state; a second terminal transition is ignored and counted, so one task can
#: never be counted as both failed and installed.
TASK_PENDING = 'pending'
TASK_RUNNING = 'running'
TASK_READY = 'ready'
TASK_INSTALLED = 'installed'
TASK_FAILED = 'failed'
TASK_CANCELLED = 'cancelled'
TASK_CANCELLED_RUNNING = 'cancelled_running'
TASK_STALE = 'stale'

#: The states a task can leave the queue in. Exactly one of them is reached.
TASK_TERMINAL_STATES = frozenset({
    TASK_INSTALLED, TASK_FAILED, TASK_CANCELLED,
    TASK_CANCELLED_RUNNING, TASK_STALE,
})


class _TaskRecord:
    """The registration of one task, and its live state.

    The registration part -- ``owner``, ``epoch``, ``enqueued_at`` -- is
    **frozen**: every later transition of this task is counted for the owner
    it was registered under, even after the queue moved on to another
    project. That is what stops an old project's in-flight work from being
    counted as the new project's work.
    """

    __slots__ = ('task_id', 'owner', 'owner_key', 'epoch', 'enqueued_at',
                 'state', 'wait_ms', 'service_ms', 'terminal_reason')

    def __init__(self, task_id, owner, epoch, enqueued_at):
        self.task_id = task_id
        self.owner = tuple(owner)
        self.owner_key = (self.owner[0], self.owner[1])
        self.epoch = epoch
        self.enqueued_at = enqueued_at
        self.state = TASK_PENDING
        self.wait_ms = None
        self.service_ms = None
        self.terminal_reason = None


class _OwnerBucket:
    """The cumulative counters of one ``(owner, epoch)``.

    Buckets are never reset by an owner change: a new owner simply gets a
    new bucket, and the late completion of an old task lands in the old
    bucket where it belongs.
    """

    __slots__ = ('owner', 'epoch', 'counters', 'wait_total_ms', 'wait_max_ms',
                 'wait_samples', 'service_total_ms', 'service_max_ms',
                 'service_samples', 'created_mono')

    def __init__(self, owner, epoch):
        self.owner = tuple(owner)
        self.epoch = epoch
        self.counters = dict.fromkeys(QUEUE_COUNTERS, 0)
        self.wait_total_ms = 0.0
        self.wait_max_ms = 0.0
        self.wait_samples = 0
        self.service_total_ms = 0.0
        self.service_max_ms = 0.0
        self.service_samples = 0
        self.created_mono = time.monotonic()


class QueueAccounting:
    """Accounting for one work queue, without changing what the queue does.

    The object only counts. It never blocks the GUI, never keeps a reference
    to a Qt object and never decides anything: the caller says what happened
    and the accounting says how much of it there is.

    Every transition is explicit about *where* the work was, because that is
    what the diagnosis needs. A request cancelled while it was still waiting
    never ran; a request cancelled while it was running could not be
    interrupted, so the work continues and only its result is dropped. The
    second case is counted as ``cancel_after_start`` and flagged
    ``atomic_in_progress`` -- it is never reported as an instant cancel.
    """

    __slots__ = ('category', 'limit', 'min_interval_s', '_lock', '_counters',
                 '_pending', '_running', '_ready', '_peak_pending', '_peak_running',
                 '_busy_since', '_tasks', '_owner', '_reset_reason',
                 '_last_written_counters',
                 '_consumers', '_shared_tasks', '_atomic_in_progress',
                 '_cancelled_remaining', '_last_rollup', '_last_reason',
                 '_owner_buckets', '_current_epoch', '_epoch_counter',
                 '_id_pending', '_id_running', '_id_ready',
                 '_duplicate_task_ids', '_late_terminal_ignored',
                 '_gauge_clamped', '_running_unresolved', '_incomplete_reason',
                 '_running_failed_pending')

    def __init__(self, category, *, limit=None, min_interval_s=1.0):
        self.category = str(category)
        self.limit = None if limit is None else int(limit)
        self.min_interval_s = float(min_interval_s)
        self._lock = threading.RLock()
        #: Live depth of the work that carries **no task identity**. It is
        #: reported as ``mixed``: without an identity it cannot be attributed
        #: to an owner and must not be presented as the new owner's depth.
        self._pending = 0
        self._running = 0
        self._ready = 0
        #: Live depth of the work that **does** carry a task identity.
        self._id_pending = 0
        self._id_running = 0
        self._id_ready = 0
        self._peak_pending = 0
        self._peak_running = 0
        #: When the queue became non-empty. This is the start of the current
        #: **busy period**, not the age of the oldest task.
        self._busy_since = None
        #: ``task_id -> _TaskRecord`` for the tasks that are still tracked.
        self._tasks = {}
        self._owner = (None, None, None)
        self._reset_reason = 'initial'
        self._last_written_counters = None
        self._consumers = {}
        self._shared_tasks = 0
        self._atomic_in_progress = 0
        self._cancelled_remaining = 0
        self._last_rollup = 0.0
        self._last_reason = None
        #: ``(owner_key, epoch) -> _OwnerBucket``. One bucket per owner
        #: **epoch**; an owner that comes back later gets a new epoch and
        #: therefore a new bucket.
        self._owner_buckets = {}
        self._current_epoch = 0
        self._epoch_counter = 0
        #: The cumulative counters of the **current** owner. It is the
        #: current owner's bucket; the bucket itself keeps the history.
        bucket = _OwnerBucket(self._owner, self._current_epoch)
        self._owner_buckets[((self._owner[0], self._owner[1]),
                             self._current_epoch)] = bucket
        self._counters = bucket.counters
        #: Accounting anomalies, reported instead of being hidden.
        self._duplicate_task_ids = 0
        self._late_terminal_ignored = 0
        self._gauge_clamped = 0
        self._running_unresolved = 0
        #: Failures that happened while the work was running and have not
        #: been resolved by a ``service_done`` yet. The real business path
        #: calls ``failed(state='running')`` and then ``service_done``; the
        #: task is therefore resolved, not unresolved.
        self._running_failed_pending = 0
        #: Set when an update failed half way. The epoch is then marked
        #: incomplete and every later snapshot carries the reason; the
        #: counters are **not** silently reset to fake conservation.
        self._incomplete_reason = None

    # -- transitions ------------------------------------------------------

    def _counters_changed_since_last_rollup(self, data):
        """Has anything been counted since the last rollup line?"""
        last = self._last_written_counters
        if last is None:
            return any(data.get(name) for name in QUEUE_COUNTERS)
        return any(data.get(name) != last.get(name) for name in QUEUE_COUNTERS)

    def _bucket_locked(self, owner=None, epoch=None):
        """The cumulative bucket of one ``(owner, epoch)``."""
        owner = tuple(owner) if owner is not None else self._owner
        if epoch is None:
            epoch = self._current_epoch
        key = ((owner[0], owner[1]), epoch)
        bucket = self._owner_buckets.get(key)
        if bucket is None:
            bucket = _OwnerBucket(owner, epoch)
            self._owner_buckets[key] = bucket
        return bucket

    def _depth_locked(self):
        """The live depth: identified tasks plus the unattributed ones."""
        return (self._id_pending + self._pending,
                self._id_running + self._running,
                self._id_ready + self._ready)

    def _task_locked(self, task_id):
        """The tracked task, or ``None`` when it is unknown."""
        if task_id is None:
            return None
        return self._tasks.get(task_id)

    def _sync_owner(self):
        """Detect an owner change at the moment work is offered.

        The boundary is detected **here**, not when the rollup is written:
        a submission that belongs to the new owner must never be added to
        the previous owner's counters.

        The previous owner's bucket is **kept** -- its late completions
        still land there -- and the new owner starts a new epoch with a new
        bucket. Nothing is reset, so nothing can be lost or misattributed.
        """
        owner = _recording_owner()
        with self._lock:
            if owner == self._owner:
                return None
            previous_owner = self._owner
            self._owner = owner
            self._epoch_counter += 1
            self._current_epoch = self._epoch_counter
            self._reset_reason = 'owner_changed'
            self._counters = self._bucket_locked().counters
        return previous_owner

    def submitted(self, n=1, *, task_id=None, enqueued_at=None, owner=None,
                  epoch=None):
        """Work was offered to the queue.

        ``task_id`` identifies one offered task. With it, the task's owner
        and epoch are **frozen at registration**: every later transition of
        that task is counted for the owner it was registered under, even
        after the queue has moved on to another project.

        Without it the call keeps the legacy behaviour, and the work is
        reported as ``mixed`` (unattributed) -- never as the new owner's.
        """
        self._sync_owner()
        with self._lock:
            # Explicit registration identity must select the submission bucket
            # too; later task transitions use this same frozen owner/epoch.
            bucket = self._bucket_locked(owner, epoch)
            bucket.counters['submitted'] += n
            was_empty = self._depth_locked()[0] == 0
            if task_id is not None:
                if task_id in self._tasks:
                    self._duplicate_task_ids += 1
                else:
                    self._tasks[task_id] = _TaskRecord(
                        task_id,
                        bucket.owner,
                        bucket.epoch,
                        float(enqueued_at) if enqueued_at is not None
                        else time.monotonic())
                    self._id_pending += 1
            else:
                self._pending += n
            if was_empty:
                self._busy_since = time.monotonic()
            depth = self._depth_locked()[0]
            if depth > self._peak_pending:
                self._peak_pending = depth

    def started(self, *, wait_ms=None, task_id=None):
        """A worker began the work. ``wait_ms`` is queue wait, not service."""
        with self._lock:
            task = self._task_locked(task_id)
            bucket = (self._bucket_locked(task.owner, task.epoch)
                      if task is not None else self._bucket_locked())
            bucket.counters['started'] += 1
            if task is not None:
                if task.state != TASK_PENDING:
                    # The task already left the queue: this is a late
                    # transition and must not move any gauge.
                    self._late_terminal_ignored += 1
                    return
                task.state = TASK_RUNNING
                self._id_pending -= 1
                self._id_running += 1
            else:
                if self._pending > 0:
                    self._pending -= 1
                else:
                    self._gauge_clamped += 1
                self._running += 1
            if wait_ms is not None:
                wait = float(wait_ms)
                bucket.wait_total_ms += wait
                bucket.wait_samples += 1
                if wait > bucket.wait_max_ms:
                    bucket.wait_max_ms = wait
                if task is not None:
                    task.wait_ms = wait
            if self._depth_locked()[0] == 0:
                self._busy_since = None
            depth = self._depth_locked()[1]
            if depth > self._peak_running:
                self._peak_running = depth

    def service_done(self, *, service_ms=None, usable=True, task_id=None):
        """The worker finished.

        With a task identity this is the **terminal** transition of that
        task: the task leaves the queue exactly once, so it can never be
        counted as failed *and* installed. A second terminal transition is
        ignored and counted as ``late_terminal_ignored``.

        Without an identity the call keeps the legacy behaviour: it is the
        only place that decrements the running gauge.
        """
        with self._lock:
            task = self._task_locked(task_id)
            bucket = (self._bucket_locked(task.owner, task.epoch)
                      if task is not None else self._bucket_locked())
            if task is not None:
                if task.state in TASK_TERMINAL_STATES:
                    self._late_terminal_ignored += 1
                    return
                bucket.counters['completed'] += 1
                if task.state == TASK_RUNNING:
                    self._id_running -= 1
                if usable:
                    task.state = TASK_READY
                    self._id_ready += 1
                else:
                    task.state = TASK_FAILED
                    bucket.counters['failed'] += 1
            else:
                bucket.counters['completed'] += 1
                if self._running > 0:
                    self._running -= 1
                else:
                    self._gauge_clamped += 1
                if self._running_failed_pending > 0:
                    # The failure that happened while this work was running
                    # is resolved by this departure.
                    self._running_failed_pending -= 1
                if usable:
                    self._ready += 1
            if service_ms is not None:
                service = float(service_ms)
                bucket.service_total_ms += service
                bucket.service_samples += 1
                if service > bucket.service_max_ms:
                    bucket.service_max_ms = service
                if task is not None:
                    task.service_ms = service

    def installed(self, n=1, *, task_id=None):
        """The result reached its destination."""
        with self._lock:
            task = self._task_locked(task_id)
            bucket = (self._bucket_locked(task.owner, task.epoch)
                      if task is not None else self._bucket_locked())
            if task is not None:
                if task.state in TASK_TERMINAL_STATES:
                    self._late_terminal_ignored += 1
                    return
                if task.state == TASK_READY:
                    self._id_ready -= 1
                task.state = TASK_INSTALLED
            bucket.counters['installed'] += n
            if task is None:
                if self._ready > 0:
                    self._ready -= n
                else:
                    self._gauge_clamped += 1

    def cancelled(self, *, state='pending', atomic_in_progress=False,
                  remaining=None, reason=None, task_id=None):
        """A request was cancelled.

        ``state`` is where the request was when the cancel arrived:
        ``pending`` (never ran), ``running`` (cannot be interrupted) or
        ``ready`` (finished, but its result was dropped).

        With a task identity this is the task's terminal transition, so a
        cancel and a later completion of the same task cannot both count.
        """
        with self._lock:
            task = self._task_locked(task_id)
            bucket = (self._bucket_locked(task.owner, task.epoch)
                      if task is not None else self._bucket_locked())
            if task is not None:
                if task.state in TASK_TERMINAL_STATES:
                    self._late_terminal_ignored += 1
                    return
                if task.state == TASK_PENDING:
                    task.state = TASK_CANCELLED
                    self._id_pending -= 1
                elif task.state == TASK_RUNNING:
                    task.state = TASK_CANCELLED_RUNNING
                    self._id_running -= 1
                    atomic_in_progress = True
                elif task.state == TASK_READY:
                    task.state = TASK_CANCELLED
                    self._id_ready -= 1
            elif state == 'running':
                atomic_in_progress = True
                if self._running > 0:
                    self._running -= 1
                else:
                    self._gauge_clamped += 1
            elif state == 'ready':
                if self._ready > 0:
                    self._ready -= 1
                else:
                    self._gauge_clamped += 1
            else:
                if self._pending > 0:
                    self._pending -= 1
                else:
                    self._gauge_clamped += 1
            bucket.counters['cancelled'] += 1
            if atomic_in_progress:
                bucket.counters['cancel_after_start'] += 1
                self._atomic_in_progress += 1
            if remaining:
                self._cancelled_remaining += int(remaining)
            if reason:
                self._last_reason = reason
            if self._depth_locked()[0] == 0:
                # The queue is empty again: the busy period is over.
                self._busy_since = None

    def stale_discarded(self, *, state='pending', n=1, reason=None, task_id=None):
        """The work finished, but its result was no longer wanted."""
        with self._lock:
            task = self._task_locked(task_id)
            bucket = (self._bucket_locked(task.owner, task.epoch)
                      if task is not None else self._bucket_locked())
            if task is not None:
                if task.state in TASK_TERMINAL_STATES:
                    self._late_terminal_ignored += 1
                    return
                if task.state == TASK_PENDING:
                    self._id_pending -= 1
                elif task.state == TASK_RUNNING:
                    self._id_running -= 1
                elif task.state == TASK_READY:
                    self._id_ready -= 1
                task.state = TASK_STALE
            elif state == 'ready':
                if self._ready > 0:
                    self._ready -= n
                else:
                    self._gauge_clamped += 1
            elif state == 'running':
                if self._running > 0:
                    self._running -= n
                else:
                    self._gauge_clamped += 1
            else:
                if self._pending > 0:
                    self._pending -= n
                else:
                    self._gauge_clamped += 1
            bucket.counters['stale_discarded'] += n
            if reason:
                self._last_reason = reason
            if self._depth_locked()[0] == 0:
                self._busy_since = None

    def rejected(self, n=1, *, reason=None):
        """The queue refused the work (full, not needed any more, ...)."""
        with self._lock:
            self._counters['rejected'] += n
            if reason:
                self._last_reason = reason

    def retried(self, n=1, *, reason=None):
        """The work will be tried again."""
        with self._lock:
            self._counters['retried'] += n
            if reason:
                self._last_reason = reason

    def coalesced(self, n=1, *, reason=None):
        """The request was merged into one that was already running."""
        with self._lock:
            self._counters['coalesced'] += n
            if reason:
                self._last_reason = reason

    def failed(self, n=1, *, reason=None, state=None, task_id=None):
        """The work ran and failed.

        Two roles are kept strictly apart:

        * **with a task identity** this call is the task's **terminal**
          transition. The task leaves the queue exactly once, so it can
          never be counted as failed *and* installed; the failure count is
          the consequence of that one transition.
        * **without an identity** the call is a **result count only**. It
          does not move the running gauge -- that belongs to
          ``service_done`` alone, so the two can never each decrement it
          for the same failure. A failure that never reaches
          ``service_done`` is published as ``running_unresolved`` instead
          of being silently absorbed.

        ``state`` names the gauge the failed work occupied: ``'pending'``
        releases queued work that will never run; ``'running'``
        when it died inside the worker, ``'ready'`` when it finished but
        its result is unusable. ``None`` leaves the gauges to the caller.
        """
        with self._lock:
            task = self._task_locked(task_id)
            bucket = (self._bucket_locked(task.owner, task.epoch)
                      if task is not None else self._bucket_locked())
            if task is not None:
                if task.state in TASK_TERMINAL_STATES:
                    self._late_terminal_ignored += 1
                    return
                bucket.counters['failed'] += n
                if task.state == TASK_RUNNING:
                    self._id_running -= 1
                elif task.state == TASK_READY:
                    self._id_ready -= 1
                elif task.state == TASK_PENDING:
                    self._id_pending -= 1
                task.state = TASK_FAILED
                task.terminal_reason = reason
            else:
                bucket.counters['failed'] += n
                if state == 'pending':
                    if self._pending >= n:
                        self._pending -= n
                    else:
                        self._pending = 0
                        self._gauge_clamped += 1
                    if self._depth_locked()[0] == 0:
                        self._busy_since = None
                elif state == 'running':
                    # The worker died inside itself. The running gauge is
                    # released by service_done alone; until then the task
                    # is unresolved, and that is published rather than
                    # guessed away.
                    self._running_failed_pending += n
                    self._running_unresolved = self._running_failed_pending
                elif state == 'ready':
                    if self._ready > 0:
                        self._ready -= n
                    else:
                        self._gauge_clamped += 1
            if reason:
                self._last_reason = reason

    # -- shared work ------------------------------------------------------

    def consumer_joined(self, key):
        """A consumer attached to a shared task."""
        with self._lock:
            count = self._consumers.get(key, 0) + 1
            self._consumers[key] = count
            if count == 2:
                self._shared_tasks += 1

    def consumer_left(self, key):
        """A consumer detached from a shared task."""
        with self._lock:
            count = self._consumers.get(key, 0) - 1
            if count <= 0:
                self._consumers.pop(key, None)
                if count == 1:
                    self._shared_tasks = max(0, self._shared_tasks-1)
            else:
                self._consumers[key] = count

    def consumer_count(self, key):
        with self._lock:
            return self._consumers.get(key, 0)

    # -- reporting --------------------------------------------------------

    def snapshot(self):
        """The current numbers. Cheap enough to call on every transition.

        The reported things are kept apart on purpose:

        * ``pending``/``running``/``ready`` -- the **global** live gauges,
          across every owner. ``*_all`` are aliases of the same numbers.
        * ``owner_buckets`` -- the **per owner, per epoch** cumulative
          counters. The current owner's bucket is also published as
          ``counters`` for the existing readers.
        * ``oldest_age_ms`` -- the age of the genuinely oldest **pending**
          task. It needs the task identity and its enqueue time; without
          them it is ``None`` and says so. It is never the busy period and
          never the age of an in-flight task.
        * ``busy_period_ms`` -- how long the queue has been non-empty.
        * ``peak_pending``/``peak_running`` -- the historical maximum of the
          global gauges, since the session started.
        * ``mixed`` -- true when some work carries no task identity and
          therefore cannot be attributed to an owner.
        """
        with self._lock:
            now = time.monotonic()
            oldest_age = None
            oldest_task = None
            oldest_at = None
            for task in self._tasks.values():
                if task.state != TASK_PENDING:
                    continue
                if oldest_at is None or task.enqueued_at < oldest_at:
                    oldest_at = task.enqueued_at
                    oldest_task = task.task_id
            if oldest_at is not None:
                oldest_age = round(max(0.0, now-oldest_at)*1000, 1)
            pending, running, ready = self._depth_locked()
            busy_ms = None
            if self._busy_since is not None and pending:
                busy_ms = round(max(0.0, now-self._busy_since)*1000, 1)
            mixed = bool(self._pending or self._running or self._ready)
            unattributed = self._pending + self._running + self._ready
            bucket = self._bucket_locked()
            data = dict(bucket.counters)
            data['counters'] = dict(bucket.counters)
            buckets = []
            for (owner_key, epoch), entry in self._owner_buckets.items():
                if not (any(entry.counters.values()) or entry.wait_samples
                        or entry.service_samples):
                    # An empty bucket carries no information; it would only
                    # make the report look busier than it is.
                    continue
                buckets.append({
                    'owner_project_token': owner_key[0],
                    'owner_open_epoch': owner_key[1],
                    'owner_generation': entry.owner[2] if len(entry.owner) > 2 else None,
                    'epoch': epoch,
                    'counters': dict(entry.counters),
                    'wait_ms_total': round(entry.wait_total_ms, 1),
                    'wait_ms_max': round(entry.wait_max_ms, 1),
                    'wait_samples': entry.wait_samples,
                    'service_ms_total': round(entry.service_total_ms, 1),
                    'service_ms_max': round(entry.service_max_ms, 1),
                    'service_samples': entry.service_samples,
                })
            data.update(
                pending=pending, running=running, ready=ready,
                pending_all=pending, running_all=running, ready_all=ready,
                limit=self.limit, peak_pending=self._peak_pending,
                peak_running=self._peak_running,
                peak_scope='since_session_start',
                oldest_age_ms=oldest_age,
                oldest_task_id=oldest_task,
                oldest_age_scope='oldest_pending_task',
                oldest_age_status=('known_from_task_identity'
                                   if oldest_age is not None
                                   else 'not_accounted_without_task_ids'),
                busy_period_ms=busy_ms,
                busy_period_status=('known' if busy_ms is not None else 'idle'),
                busy_since_mono=(round(self._busy_since, 3)
                                 if self._busy_since is not None else None),
                pending_busy_ms=busy_ms,
                pending_busy_ms_semantics=('current_busy_period_'
                                           'since_queue_became_nonempty'),
                wait_ms_total=round(bucket.wait_total_ms, 1),
                wait_ms_max=round(bucket.wait_max_ms, 1),
                wait_samples=bucket.wait_samples,
                service_ms_total=round(bucket.service_total_ms, 1),
                service_ms_max=round(bucket.service_max_ms, 1),
                service_samples=bucket.service_samples,
                shared_tasks=self._shared_tasks,
                atomic_in_progress=self._atomic_in_progress,
                cancelled_remaining_work=self._cancelled_remaining,
                mixed=mixed,
                unattributed_count=unattributed,
                identified_count=len(self._tasks),
                owner_epoch=self._current_epoch,
                owner_buckets=buckets,
                duplicate_task_ids=self._duplicate_task_ids,
                late_terminal_ignored=self._late_terminal_ignored,
                gauge_clamped=self._gauge_clamped,
                running_unresolved=self._running_failed_pending,
                accounting_incomplete=self._incomplete_reason is not None,
                accounting_incomplete_reason=self._incomplete_reason)
            return data

    def _mark_incomplete(self, reason):
        """Mark this accounting as incomplete.

        A metering failure that happens half way through an update must not
        be swallowed and then reported as a precise-looking count. The
        epoch is marked, every later snapshot carries the reason, and the
        task and depth numbers are **not** reset to fake conservation.
        """
        with self._lock:
            if self._incomplete_reason is None:
                self._incomplete_reason = str(reason)
            else:
                self._incomplete_reason = ('%s; %s' % (self._incomplete_reason, reason))

    def _emit(self, data, *, owner, epoch, reset_reason, flush_reason=None,
              **fields):
        """Write one ``queue_rollup`` line."""
        counters = {name: data[name] for name in QUEUE_COUNTERS}
        payload = dict(
            category=self.category, counters=counters,
            pending=data['pending'], running=data['running'], ready=data['ready'],
            pending_all=data['pending'], running_all=data['running'],
            ready_all=data['ready'],
            limit=data['limit'],
            peak_pending=data['peak_pending'], peak_running=data['peak_running'],
            peak_scope=data['peak_scope'],
            oldest_age_ms=data['oldest_age_ms'],
            oldest_age_status=data['oldest_age_status'],
            oldest_age_scope=data['oldest_age_scope'],
            oldest_task_id=data['oldest_task_id'],
            busy_period_ms=data['busy_period_ms'],
            busy_period_status=data['busy_period_status'],
            wait_ms_total=data['wait_ms_total'], wait_ms_max=data['wait_ms_max'],
            wait_samples=data['wait_samples'],
            service_ms_total=data['service_ms_total'],
            service_ms_max=data['service_ms_max'],
            service_samples=data['service_samples'],
            shared_tasks=data['shared_tasks'],
            atomic_in_progress=data['atomic_in_progress'],
            cancelled_remaining_work=data['cancelled_remaining_work'],
            mixed=data['mixed'],
            unattributed_count=data['unattributed_count'],
            identified_count=data['identified_count'],
            owner_epoch=epoch,
            owner_buckets=data['owner_buckets'],
            duplicate_task_ids=data['duplicate_task_ids'],
            late_terminal_ignored=data['late_terminal_ignored'],
            gauge_clamped=data['gauge_clamped'],
            running_unresolved=data['running_unresolved'],
            accounting_incomplete=data['accounting_incomplete'],
            accounting_incomplete_reason=data['accounting_incomplete_reason'],
            counters_cumulative=True,
            cumulative_scope='per_owner_per_epoch',
            reset_reason=reset_reason,
            owner_project_token=owner[0], owner_open_epoch=owner[1],
            owner_generation=owner[2],
            sampling_strategy='interval_rollup',
            recording_contract_version=RECORDING_CONTRACT_VERSION)
        if flush_reason:
            payload['flush_reason'] = flush_reason
        payload.update(fields)
        record('queue_rollup', priority=PRIORITY_STATE, **payload)
        with self._lock:
            self._last_written_counters = dict(counters)

    def rollup(self, *, force=False, **fields):
        """Write one rollup line: at most one per ``min_interval_s``.

        The counters are nested in one field so the event stays flat, and the
        line is throttled so a busy queue cannot flood the log. The line
        names the owner and the epoch the counters belong to; an owner
        change opens a **new** epoch with a new bucket, and the old owner's
        late completions still land in the old bucket.
        """
        now = time.monotonic()
        self._sync_owner()
        owner = _recording_owner()
        with self._lock:
            epoch = self._current_epoch
            if not force and now-self._last_rollup < self.min_interval_s:
                return None
            self._last_rollup = now
            data = self.snapshot()
        self._emit(data, owner=owner, epoch=epoch,
                   reset_reason=self._reset_reason, **fields)
        return data


# ── resource accounting (owner / budget / current / peak / eviction) ─────

_RESOURCE_CLASSES = {}
_RESOURCE_LOCK = threading.Lock()


class ResourceAccounting:
    """Accounting for one class of rebuildable resource.

    Each class reports owner, budget, current, peak, evicted bytes and the
    visible over-budget. The budget is the budget that applies to *this*
    class: a shared cache budget does not bound the whole process, and the
    class says so instead of pretending that it does.
    """

    __slots__ = ('name', 'owner', 'budget', 'unit', 'source', '_lock',
                 '_current', '_peak', '_evicted', '_pin_over_budget', '_samples')

    def __init__(self, name, *, owner=None, budget=None, unit='bytes',
                 source='measured'):
        self.name = str(name)
        self.owner = owner
        self.budget = budget
        self.unit = unit
        self.source = source
        self._lock = threading.RLock()
        self._current = 0
        self._peak = 0
        self._evicted = 0
        self._pin_over_budget = 0
        self._samples = 0

    def observe(self, current, *, evicted=None, pin_over_budget=None,
                budget=None, source=None):
        """Record one observation of the class."""
        with self._lock:
            value = max(0, int(current))
            self._current = value
            if value > self._peak:
                self._peak = value
            self._samples += 1
            if evicted is not None:
                self._evicted = max(self._evicted, int(evicted))
            if pin_over_budget is not None:
                self._pin_over_budget = max(self._pin_over_budget, int(pin_over_budget))
            if budget is not None:
                self.budget = budget
            if source is not None:
                self.source = source
            return self.snapshot()

    def snapshot(self):
        with self._lock:
            return {'name': self.name, 'owner': self.owner, 'budget': self.budget,
                    'unit': self.unit, 'current': self._current, 'peak': self._peak,
                    'evicted': self._evicted,
                    'pin_over_budget': self._pin_over_budget,
                    'source': self.source, 'samples': self._samples}


def resource_accounting(name, *, owner=None, budget=None, unit='bytes',
                        source='measured'):
    """Get (or create) the accounting for one resource class."""
    with _RESOURCE_LOCK:
        entry = _RESOURCE_CLASSES.get(name)
        if entry is None:
            entry = ResourceAccounting(name, owner=owner, budget=budget, unit=unit,
                                       source=source)
            _RESOURCE_CLASSES[name] = entry
        else:
            if owner is not None:
                entry.owner = owner
            if budget is not None:
                entry.budget = budget
            if unit is not None:
                entry.unit = unit
        return entry


def not_accounted(name, reason):
    """A class nobody measures. Never a zero: a zero reads like "nothing used"."""
    return {'name': name, 'owner': None, 'budget': None, 'unit': 'bytes',
            'current': None, 'peak': None, 'evicted': None,
            'pin_over_budget': None, 'source': 'not_accounted', 'reason': reason}


def unknown_resource(name, reason):
    """A class that cannot be measured on this machine (GPU, mostly)."""
    return {'name': name, 'owner': None, 'budget': None, 'unit': 'bytes',
            'current': None, 'peak': None, 'evicted': None,
            'pin_over_budget': None, 'source': 'unknown', 'reason': reason}


def record_resource_rollup(reason='manual', classes=None, **fields):
    """Write the resource rollup.

    ``classes`` is a sequence of ``(name, payload)`` pairs. A class that is
    not measured is reported with ``source='not_accounted'`` (or
    ``'unknown'``), never with a zero that would read like "nothing is used".
    """
    payload = {}
    for name, entry in classes or ():
        payload[name] = entry
    record('resource_rollup', priority=PRIORITY_STATE, reason=reason,
           classes=payload, **fields)


class PerformanceTrace(QtCore.QObject):
    writer_failed = QtCore.pyqtSignal(str)
    close_requested = QtCore.pyqtSignal()
    INTERVAL = .05
    MAX_EVENT_BYTES = 32*1024

    def __init__(self, app, window, directory):
        super().__init__(app)
        self.window = window
        self.session = uuid.uuid4().hex[:12]
        self._started_at = time.time()
        self.path = Path(directory)/f'Prism-operations-{os.getpid()}.jsonl'
        # Creating a diagnostic directory can fail too. Do it in the writer's
        # guarded path; logging must not abort application construction.
        self.events = queue.Queue(maxsize=384)
        # Reserved capacity for events that explain a stall. Ordinary input
        # samples are what gets dropped when the queue saturates -- never the
        # diagnostic that names the file and the line. The old implementation
        # claimed to evict "an ordinary event from the middle" and in fact
        # called get_nowait(), which removed the *head*: an important `start`
        # could be lost and orphan its own `end`.
        self.important = queue.Queue(maxsize=128)
        self.dropped = 0
        self.dropped_by_kind = Counter()
        self.queue_peak = 0
        self.seq_gaps = 0
        self.trace_incomplete = False
        self._overflow_pending = False
        self._last_overflow_note = 0.0
        self._loss_lock = threading.Lock()
        # Admission and stopping share one short lock. A producer that began
        # formatting before close cannot enqueue after the writer has drained.
        self._record_gate = threading.Lock()
        self.self_ms_total = 0.0
        self.self_ms_max = 0.0
        self.writer_errors = 0
        self.writer_reopens = 0
        #: What the writer thread actually wrote, and how long that took.
        #: Bytes alone do not say what the diagnostic cost.
        self.events_written = 0
        self.bytes_written = 0
        self.writer_ms_total = 0.0
        self.writer_ms_max = 0.0
        self._written_since_heartbeat = 0
        self._bytes_since_heartbeat = 0
        self.version_snapshot = None
        self._last_written_sequence = 0
        self.sequence = itertools.count(1)
        self.last_action = None
        self.recent = deque(maxlen=128)
        self.last_heartbeat = time.perf_counter()
        self._last_budget_sample = float('-inf')
        self.last_dispatch = None
        self.dispatches_since_heartbeat = 0
        #: The project identity as the GUI thread last saw it. The watchdog
        #: runs in another thread, where the ContextVar is empty, so the
        #: heartbeat is the only place this can be read.
        self._context_snapshot = {}
        #: Traced stages that are currently open, per thread. It is the only
        #: positive evidence that a **background** stage was running while
        #: the GUI was stuck -- without it that classification is unreachable.
        self._open_spans = {}
        self._span_since = {}
        self._span_lock = threading.RLock()
        self.gui_thread = threading.get_ident()
        self.stopped = threading.Event()
        self._closed = False
        self.writer_failed.connect(self._on_writer_failed, QtCore.Qt.ConnectionType.QueuedConnection)
        self.actions = set()
        self.presses = {}
        self.counts = Counter()
        self.max_dispatch = 0
        # Event dispatches currently in progress, outermost to innermost.  Each
        # layer has its own identity; parent/root describe nesting only.
        self.current_dispatch = []
        # Recently closed immutable intervals let a stall close against the
        # dispatches that actually overlapped its frozen window, not against a
        # short recovery dispatch that happens to be current afterwards.
        self._dispatch_history = deque(maxlen=512)
        self._dispatch_history_lock = threading.Lock()
        self._dispatch_counter = itertools.count(1)
        # Per-dispatch stack samples, keyed by dispatch_id.
        self._dispatch_samples = {}
        # Total milliseconds spent inside each (target class, event type)
        # since the last `performance` summary.
        self.dispatch_time = Counter()
        self._dispatch_stat_keys = {}
        # (time.perf_counter(), action) of the most recent user input;
        # consumed by the next completed Paint dispatch to measure the
        # end-to-end "click -> screen updated" latency.
        self.input_marker = None
        self.input_to_paint = []
        self._gc_started = {}
        self._gc_intervals = deque(maxlen=32)
        # The GC callback runs on whichever thread triggered the collection
        # and the watchdog reads the intervals from another one. The lock
        # keeps either from seeing a half-updated state.
        self._gc_lock = threading.Lock()
        #: A broken diagnostic must never break collection itself -- but it
        #: must not disappear either. The failures are counted and reported
        #: with the session summary.
        self._gc_collection_errors = 0
        self._gc_error_types = Counter()
        gc.callbacks.append(self._gc_callback)
        self._startup_archive_errors = 0
        self.writer = threading.Thread(target=self._write, daemon=True, name='PrismTraceWriter')
        self.writer.start()
        self.watchdog = threading.Thread(target=self._watch, daemon=True, name='PrismGuiWatchdog')
        self.watchdog.start()
        # The child-process sample is taken in the background: the
        # enumeration costs tens of milliseconds and must never run on the
        # GUI thread.  Starting it here means the first rollup already has
        # a sample instead of an honest "not yet".
        _child_sampler.start()
        self.timer = QtCore.QTimer(self)
        self.timer.setInterval(50)
        self.timer.timeout.connect(self._heartbeat)
        self.timer.start()
        self.summary_time = time.perf_counter()
        self._last_periodic_sample = self.summary_time
        self.cpu_time = time.process_time()
        self.hook_actions(window)
        app.installEventFilter(self)
        app.aboutToQuit.connect(self.close)
        self.close_requested.connect(self.close, QtCore.Qt.ConnectionType.QueuedConnection)
        self.record('session_start', log=str(self.path), pid=os.getpid(),
                     schema_version=SCHEMA_VERSION, mode=mode(),
                     recording_contract_version=RECORDING_CONTRACT_VERSION,
                     thread=threading.current_thread().name)
        self.record('runtime', python=sys.version, platform=sys.platform, qt=QtCore.QT_VERSION_STR,
                    webengine_flags=os.environ.get('QTWEBENGINE_CHROMIUM_FLAGS', ''),
                    qt_platform=app.platformName(), schema_version=SCHEMA_VERSION,
                    mode=mode(), dpi=app.primaryScreen().devicePixelRatio()
                    if app.primaryScreen() is not None else 'unknown',
                    frozen=bool(getattr(sys, 'frozen', False)),
                    gc_min_duration_ms=GC_MIN_DURATION_MS, stack_capture_scope='python_only',
                    native_stack_capture_available=False,
                    process_io_scope='current_process_lifetime_transfer_counters')
        # The manifest states what produced this log and how long the
        # evidence is kept. It is written once here and once at the end, so
        # a report can say what the contract was even when the middle of
        # the log is gone.
        self.record('trace_manifest', priority=PRIORITY_SESSION,
                    **self._manifest('start'))
        # Hashing the key files is real I/O; it never runs on the GUI thread
        # and never per event.
        versioner = threading.Thread(target=self._emit_version,
                                      daemon=True, name='PrismTraceVersion')
        versioner.start()

    def _emit_version(self):
        try:
            snapshot = build_version_snapshot()
        except Exception as error:  # never let diagnostics break startup
            snapshot = {'error': '%s: %s' % (type(error).__name__, error)}
        self.version_snapshot = snapshot
        try:
            self.record('trace_version', priority=PRIORITY_SESSION, **snapshot)
        except Exception:
            pass

    def _manifest(self, phase, **extra):
        """The session manifest: what produced this log and how it is kept.

        It names the recording contract, the sampling strategies, the
        rotation settings and how long the evidence survives. A report that
        finds the middle of the log gone can still say what the contract
        was.
        """
        fields = dict(
            phase=phase, session=self.session, pid=os.getpid(),
            schema_version=SCHEMA_VERSION,
            recording_contract_version=RECORDING_CONTRACT_VERSION,
            startup_identity_archive=f'Prism-startup-{os.getpid()}.jsonl',
            startup_archive_errors=getattr(self, '_startup_archive_errors', 0),
            startup_archive_scope='identity_and_session_boundaries_not_complete_operation_history',
            writer_lifecycle_version=1,
            rotation_evidence='successful_rollovers_with_exact_closed_segment_sequences',
            writer_checkpoint_interval_s=5,
            mode=mode(), log=str(self.path),
            rotation_max_bytes=ROTATION_MAX_BYTES,
            rotation_backup_count=ROTATION_BACKUP_COUNT,
            retained_log_files=RETAINED_LOG_FILES,
            cleanup_interval_seconds=LOG_CLEANUP_INTERVAL_S,
            retention_max_age_seconds=LOG_RETENTION_AGE_S,
            retention_max_bytes=LOG_RETENTION_MAX_BYTES,
            diagnostic_archive_max_bytes=DIAGNOSTIC_ARCHIVE_MAX_BYTES,
            retention_policy=(
                'the newest %d process logs are kept side by side, each with '
                '%d rotated parts of %d bytes; older runs are removed when a '
                'new run starts and periodically; age/byte budgets also apply, live processes protected'
                % (RETAINED_LOG_FILES, ROTATION_BACKUP_COUNT, ROTATION_MAX_BYTES)),
            evidence_retention=(
                'bounded by run count, age and bytes; significant events also archived'),
            hotpath_rollup_interval_s=HOTPATH_ROLLUP_INTERVAL_S,
            normal_process_sample_interval_s=5,
            diagnostic_process_sample_interval_s=1,
            normal_budget_sample_interval_s=15,
            diagnostic_budget_sample_interval_s=5,
            periodic_sampling_note='periodic snapshots are samples, not guaranteed transient peaks; operation/error events unchanged',
            gc_min_duration_ms=GC_MIN_DURATION_MS,
            queue_counters=list(QUEUE_COUNTERS),
            hotpath_paths=_hotpath_registry_snapshot(),
            diagnostic_failures=diagnostic_failures())
        fields.update(writer_errors=self.writer_errors,writer_reopens=self.writer_reopens,
                      trace_incomplete=self.trace_incomplete)
        fields.update(extra)
        return fields

    def record(self, kind, /, *, priority=None, **fields):
        """Append one event.

        A diagnostic failure is **counted**, never raised: the log must not
        be able to break the business it observes. Only ``Exception`` is
        isolated; ``KeyboardInterrupt``/``SystemExit`` and a user
        cancellation still propagate untouched.
        """
        try:
            return self._record_body(kind, priority=priority, **fields)
        except Exception as error:
            _note_diagnostic_failure('record', error)
            return False

    def _record_body(self, kind, /, *, priority=None, **fields):
        finishing_open_span = (kind in ('operation', 'open_phase')
                               and fields.get('role') == 'end'
                               and bool(fields.get('span_id')))
        if mode() == MODE_OFF and not finishing_open_span and kind not in (
                'session_start', 'session_end', 'runtime', 'trace_mode',
                'trace_version', 'trace_version_pending', 'trace_shutdown_pending'):
            return False
        entered = time.perf_counter()
        if self.stopped.is_set():
            return False
        cheap = mode() == MODE_OFF
        # Internal keys are owned by the recorder; a caller field that
        # shadows one (e.g. ``kind`` on a page-open operation) must not
        # break the event -- it belongs in the payload instead.
        reserved = ('event', 'sequence', 'session', 'time', 'monotonic',
                    'schema_version', 'pid', 'thread_id', 'thread_name',
                    'priority')
        fields = {key: value for key, value in fields.items()
                  if key not in reserved}
        if len(fields) > MAX_FIELDS:
            kept = {key: fields[key] for key in _LIFECYCLE_FIELDS if key in fields}
            for key, item in fields.items():
                if key not in kept and len(kept) < MAX_FIELDS:
                    kept[key] = item
            kept['_dropped_fields'] = len(fields) - len(kept)
            fields = kept
        context = _context.get() or TraceContext()
        thread_id, thread_name = thread_identity()
        value = {
            'schema_version': SCHEMA_VERSION,
            'event': kind, 'sequence': next(self.sequence),
            'session': self.session, 'time': time.time(),
            'monotonic': time.perf_counter(),
            'pid': os.getpid(), 'thread_id': thread_id, 'thread_name': thread_name,
            'priority': _priority_for(kind, priority),
        }
        if context.trace_id:
            value['trace_id'] = context.trace_id
        if context.span_id:
            value['span_id'] = context.span_id
            # A diagnostic observed inside a span is not its closing half.
            # Real lifecycle producers explicitly pass role=start/end.
            value['role'] = 'attachment'
        if context.parent_span_id:
            value['parent_span_id'] = context.parent_span_id
        if context.origin_action_id:
            value['origin_action_id'] = context.origin_action_id
        if context.project_token:
            value['project_token'] = context.project_token
        if context.open_epoch is not None:
            value['open_epoch'] = context.open_epoch
        if context.generation is not None:
            value['generation'] = context.generation
        if context.source_revision:
            value['source_revision'] = context.source_revision
        if cheap:
            # ``off`` must not pay for deep field walks or format work.
            for key, item in fields.items():
                value[key] = item if isinstance(item, (str, int, float, bool,
                                                        type(None))) else str(item)[:MAX_STRING]
        else:
            for key, item in fields.items():
                value[key] = _cap(item)
        if kind in ('input', 'action'):
            # The old attribution guessed from "whatever was clicked last".
            # A later click cannot rewrite where an already-running task came
            # from, so the id is minted here and carried in the context.
            self.last_action = {k: v for k, v in value.items()
                                if k not in ('session', 'monotonic')}
            self.input_marker = (value['monotonic'], value.get('target'),
                                 value.get('operation', kind))
            _context.set(replace(context, origin_action_id=value['sequence']))
        if mode() == MODE_DIAGNOSTIC and kind in CALLER_EVENTS and 'caller' not in value:
            value['caller'] = _caller_stack()
        # The writer thread owns encoding. json.dumps on a GUI thread is the
        # single easiest way for a diagnostic to become the stall it is
        # measuring.
        self.recent.append(value)
        accepted = self._enqueue(value, _priority_for(kind, priority))
        self.self_ms_total += (time.perf_counter()-entered)*1000
        peak = (time.perf_counter()-entered)*1000
        if peak > self.self_ms_max:
            self.self_ms_max = peak
        return accepted

    def _note_drop(self, kind, where):
        with self._loss_lock:
            self.dropped += 1
            self.dropped_by_kind[kind or '?'] += 1
            # This is the count of *known dropped sequences*. Priority lanes
            # can reorder delivery, so a high-water jump is not evidence of loss.
            self.seq_gaps += 1
            self.trace_incomplete = True
            self._overflow_pending = True

    def _enqueue(self, value, priority):
        """Return actual admission, serialized against stopping the writer."""
        kind = value.get('event')
        with _lock_of(self, '_record_gate'):
            stopped = getattr(self, 'stopped', None)
            if stopped is not None and stopped.is_set():
                return False
            try:
                self.important.put_nowait(value) if priority in (
                    PRIORITY_SESSION, PRIORITY_ERROR, PRIORITY_TERMINAL) \
                    else self.events.put_nowait(value)
                self.queue_peak = max(self.queue_peak,
                                      self.important.qsize() + self.events.qsize())
                return True
            except queue.Full:
                self._note_drop(kind, 'important_lane_full' if priority in (
                    PRIORITY_SESSION, PRIORITY_ERROR, PRIORITY_TERMINAL)
                    else 'queue_full')
                return False

    def crash_report(self, exc_type, exc, tb):
        path = self.path.parent/f'Prism-crash-{os.getpid()}.json'
        # Keep the failure in the ordinary parser input as well as its sidecar.
        # A drained session_end describes the writer, not a successful app exit.
        self.record('crash', error=str(exc) or exc_type.__name__,
                    exception_type=exc_type.__name__)
        report = dict(session=self.session, time=time.time(), error=str(exc),
                      traceback=traceback.format_exception(exc_type, exc, tb),
                      last_action=self.last_action, recent=list(self.recent),
                      operation_log=str(self.path))
        try:
            path.write_text(json.dumps(report, ensure_ascii=False, default=str, indent=2), encoding='utf-8')
        except OSError:
            pass
        return path

    def _writer_evidence(self, kind, **fields):
        """Writer-owned evidence; never enqueue a final event after stopping."""
        ident, name = thread_identity()
        return dict(event=kind, schema_version=SCHEMA_VERSION,
                    sequence=next(self.sequence), session=self.session,
                    pid=os.getpid(), thread_id=ident, thread_name=name,
                    time=time.time(), monotonic=time.perf_counter(),
                    priority=PRIORITY_SESSION, writer_lifecycle_version=1,
                    **fields)

    def _writer_progress(self, rotation_count):
        return self._writer_evidence('trace_writer_checkpoint',
            events_written=self.events_written, bytes_written=self.bytes_written,
            rotation_count=rotation_count,
            pending_unwritten=self.events.qsize()+self.important.qsize(),
            dropped_events=self.dropped, seq_gap_count=self.seq_gaps,
            writer_errors=self.writer_errors, trace_incomplete=self.trace_incomplete)

    def _archive_write(self, stream, value):
        if stream is None:
            return None
        try:
            stream.write(json.dumps(value, ensure_ascii=False, default=str)+'\n')
            stream.flush()
            return stream
        except OSError:
            self._startup_archive_errors += 1
            self.trace_incomplete = True
            try:
                stream.close()
            except OSError:
                pass
            return None

    @staticmethod
    def _sequence_ranges(sequences):
        """Compact a single bounded log segment; no lifetime-sized ID set."""
        ranges = []
        for sequence in sorted(sequences):
            if ranges and sequence == ranges[-1][1] + 1:
                ranges[-1][1] = sequence
            else:
                ranges.append([sequence, sequence])
        return ranges

    def _write(self):
        handler = None
        startup_stream = None
        segment_sequences = set()
        rotation_count = 0
        diagnostic_archive_bytes = 0
        diagnostic_archive_skipped = Counter()
        last_checkpoint = time.monotonic()
        last_cleanup = time.monotonic()
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            try:
                cleanup = _prune_trace_logs(self.path.parent)
            except OSError as error:
                cleanup = dict(error_type=type(error).__name__, failed=True)
            handler = RotatingFileHandler(self.path, maxBytes=ROTATION_MAX_BYTES,
                                          backupCount=ROTATION_BACKUP_COUNT,
                                          encoding='utf-8')
            try:
                startup_stream = (self.path.parent/f'Prism-startup-{os.getpid()}.jsonl').open(
                    'w', encoding='utf-8')
            except OSError:
                self._startup_archive_errors += 1
                self.trace_incomplete = True
            startup_stream = self._archive_write(startup_stream,
                self._writer_evidence('trace_log_cleanup', **cleanup))
            while not self.stopped.is_set() or not self.events.empty() \
                    or not self.important.empty() or self._overflow_pending:
                value = None
                now = time.monotonic()
                if now-last_cleanup >= LOG_CLEANUP_INTERVAL_S:
                    try:
                        cleanup = _prune_trace_logs(self.path.parent)
                    except OSError as error:
                        cleanup = dict(error_type=type(error).__name__, failed=True)
                    startup_stream = self._archive_write(startup_stream,
                        self._writer_evidence('trace_log_cleanup', **cleanup))
                    last_cleanup = now
                if self._overflow_pending and (self.stopped.is_set() or now-self._last_overflow_note >= 1):
                    with self._loss_lock:
                        value = dict(event='trace_overflow', schema_version=SCHEMA_VERSION,
                                     sequence=next(self.sequence), session=self.session,
                                     time=time.time(), monotonic=time.perf_counter(),
                                     dropped_total=self.dropped, dropped_by_kind=dict(self.dropped_by_kind),
                                     trace_incomplete=True, seq_gap_count=self.seq_gaps,
                                     seq_gap_basis='known_dropped_events', sequence_order_guaranteed=False)
                        self._overflow_pending = False
                        self._last_overflow_note = now
                # Diagnostics first: a saturated ordinary lane must not be
                # able to starve the event that explains the saturation.
                for lane in (() if value is not None else (self.important, self.events)):
                    try:
                        value = lane.get_nowait()
                        break
                    except queue.Empty:
                        continue
                if value is None:
                    try:
                        value = self.important.get(timeout=.1)
                    except queue.Empty:
                        # A healthy idle writer must remain distinguishable
                        # from a stopped diagnostic channel.
                        if time.monotonic()-last_checkpoint >= 5:
                            startup_stream = self._archive_write(startup_stream,
                                self._writer_progress(rotation_count))
                            last_checkpoint = time.monotonic()
                        continue
                # Encoding happens here, on the writer thread, not on the
                # GUI thread that produced the event.
                write_started = time.perf_counter()
                sequence = value.get('sequence')
                if isinstance(sequence, int):
                    self._last_written_sequence = max(self._last_written_sequence or 0,
                                                      sequence)
                payload = json.dumps(value, ensure_ascii=False,
                                     default=str).encode('utf-8')
                if len(payload) > self.MAX_EVENT_BYTES:
                    limited = {key: value[key] for key in _LIFECYCLE_FIELDS
                               if key in value}
                    limited.update(truncated=True, original_bytes=len(payload),
                                   details=payload[:8000].decode('utf-8',
                                                                errors='replace'))
                    payload = json.dumps(limited, ensure_ascii=False).encode('utf-8')
                if startup_stream is not None and value.get('event') in (
                        'session_start', 'runtime', 'trace_version',
                        'trace_manifest', 'session_end', 'trace_shutdown_pending', 'crash'):
                    startup_stream = self._archive_write(startup_stream,
                                                        json.loads(payload))
                elif startup_stream is not None and _diagnostic_archive_candidate(value):
                    if diagnostic_archive_bytes + len(payload) + 1 <= DIAGNOSTIC_ARCHIVE_MAX_BYTES:
                        startup_stream = self._archive_write(startup_stream, json.loads(payload))
                        diagnostic_archive_bytes += len(payload) + 1
                    else:
                        diagnostic_archive_skipped[value.get('event', 'unknown')] += 1
                # logging.config/shutdown may close registered handlers while
                # this independently owned writer is alive. Use the handler's
                # lock for the entire direct write, and reopen our own stream
                # if global configuration closed it between records.
                handler.acquire()
                try:
                    if handler.stream is None:
                        handler.stream=handler._open()
                        self.writer_reopens+=1
                        self.record('trace_stream_reopened',priority=PRIORITY_STATE,
                                    reopens=self.writer_reopens)
                    handler.stream.write(payload.decode('utf-8')+'\n')
                    handler.flush()
                    if isinstance(sequence, int):
                        segment_sequences.add(sequence)
                    if handler.stream.tell() >= handler.maxBytes:
                        handler.doRollover()
                        rotation_count += 1
                        startup_stream = self._archive_write(startup_stream,
                            self._writer_evidence('trace_rotation',
                                rotation_count=rotation_count,
                                closed_segment_index=rotation_count-1,
                                rotation_max_bytes=ROTATION_MAX_BYTES,
                                rotation_backup_count=ROTATION_BACKUP_COUNT,
                                closed_segment_sequence_ranges=self._sequence_ranges(segment_sequences),
                                dropped_events=self.dropped,
                                seq_gap_count=self.seq_gaps,
                                trace_incomplete=self.trace_incomplete))
                        segment_sequences.clear()
                finally:handler.release()
                writer_ms = (time.perf_counter()-write_started)*1000
                self.writer_ms_total += writer_ms
                if writer_ms > self.writer_ms_max:
                    self.writer_ms_max = writer_ms
                self.events_written += 1
                self.bytes_written += len(payload)
                self._written_since_heartbeat += 1
                self._bytes_since_heartbeat += len(payload)
                if time.monotonic()-last_checkpoint >= 5:
                    startup_stream = self._archive_write(startup_stream,
                        self._writer_progress(rotation_count))
                    last_checkpoint = time.monotonic()
            # End-recorded and writer-drained are different facts. This marker
            # is emitted only after the accepted queues have actually drained.
            startup_stream = self._archive_write(startup_stream,
                self._writer_evidence('trace_diagnostic_archive_summary',
                    budget_bytes=DIAGNOSTIC_ARCHIVE_MAX_BYTES,
                    retained_bytes=diagnostic_archive_bytes,
                    skipped_by_kind=dict(diagnostic_archive_skipped),
                    complete=not bool(diagnostic_archive_skipped),
                    coverage='significant_events_only; full trace may have rotated'))
            drained = self._writer_evidence('trace_writer_drained',
                pending_unwritten=0, events_written=self.events_written,
                bytes_written=self.bytes_written, dropped_events=self.dropped,
                seq_gap_count=self.seq_gaps, writer_errors=self.writer_errors,
                startup_archive_errors=self._startup_archive_errors,
                trace_incomplete=self.trace_incomplete)
            handler.acquire()
            try:
                if handler.stream is None:
                    handler.stream = handler._open()
                handler.stream.write(json.dumps(drained, ensure_ascii=False)+'\n')
                handler.flush()
            finally:
                handler.release()
            startup_stream = self._archive_write(startup_stream, drained)
        except Exception as error:
            # A diagnostic failure must not prevent editing or saving.
            self.writer_errors += 1
            self.trace_incomplete = True
            with _lock_of(self, '_record_gate'):
                self.stopped.set()
            # The separate startup stream is not a registered logging handler.
            # Preserve an explicit failure marker when that channel still works.
            if startup_stream is not None:
                try:
                    startup_stream = self._archive_write(startup_stream,
                        self._writer_evidence('trace_writer_failed',
                            error_type=type(error).__name__,
                            writer_errors=self.writer_errors, trace_incomplete=True,
                            queued_unwritten=self.events.qsize()+self.important.qsize()))
                except Exception:
                    pass
            try:
                self.writer_failed.emit('Diagnostic log could not be written')
            except RuntimeError:
                pass
        finally:
            if startup_stream is not None:
                startup_stream.close()
            if handler is not None:
                handler.close()

    @QtCore.pyqtSlot(str)
    def _on_writer_failed(self, _message):
        self.close()

    #: A stall shorter than this is normal event-loop jitter. The previous
    #: value was .5 s, which missed every stall between 200 and 500 ms --
    #: including the ones that happen in bursts while a project opens.
    STALL_THRESHOLD = .2
    #: The 100-200 ms band is invisible at the full threshold but is still
    #: a felt hitch (a dropped frame is 16 ms). Logged as `severity=minor`
    #: and sampled with a smaller cap to keep the report proportional.
    MINOR_STALL_THRESHOLD = .1
    MINOR_STALL_SAMPLE_LIMIT = 400
    #: How often the watchdog re-examines the GUI thread. Fine enough to
    #: catch a 32 ms handler several times, cheap enough to run forever.
    WATCH_INTERVAL = .005
    #: Only dispatches that have already run this long get stack-sampled.
    DISPATCH_SAMPLE_AGE = .016
    #: Sample caps per bucket so a minute-long stall cannot balloon the
    #: report; past the cap the bucket is marked truncated.
    STALL_SAMPLE_LIMIT = 4000
    DISPATCH_SAMPLE_LIMIT = 800

    def _gc_callback(self, phase, info):
        """Log pauses caused by the garbage collector running in-process.

        A GC pause happens on whichever thread triggers collection and is
        invisible to the stall watchdog's Python stack sampler: the frames
        live in the ``gc`` module but the wall time is spent in C. Logging
        the pauses separately closes that gap in the stall picture.
        """
        if mode() == MODE_OFF:
            return
        try:
            generation = info.get('generation')
            guard = _lock_of(self, '_gc_lock')
            if phase == 'start':
                with guard:
                    self._gc_started[generation] = (time.perf_counter(),
                                                    capture_context(),
                                                    threading.get_ident())
                return
            with guard:
                started = self._gc_started.pop(generation, None)
            if started is None:
                return
            started, context, trigger_thread = started
            ended = time.perf_counter()
            duration = (ended-started)*1000
            if duration >= GC_MIN_DURATION_MS:
                store = _gc_interval_store(self)
                with guard:
                    store.append((started, ended, trigger_thread == self.gui_thread))
                with restore_context(context):
                    self.record('gc', gc_generation=generation, duration_ms=round(duration, 3),
                                gc_start_monotonic=started, gc_end_monotonic=ended,
                                gc_trigger_thread_id=trigger_thread,
                                gc_on_gui_thread=trigger_thread == self.gui_thread,
                                gc_counts_after=gc.get_count(), gc_thresholds=gc.get_threshold(),
                                gc_trigger_span_id=context.span_id,
                                gc_min_duration_ms=GC_MIN_DURATION_MS, gc_collection_observed=True,
                                attribution='trigger_context_not_allocation_root_cause',
                                collected=info.get('collected'),
                                uncollectable=info.get('uncollectable'))
        except Exception as error:
            # A broken diagnostic must never break collection itself. It
            # must not vanish either: the failure is counted and published
            # with the session summary, so "no GC was seen" can never be
            # confused with "the GC watcher was broken".
            with guard:
                try:
                    self._gc_collection_errors = (
                        getattr(self, '_gc_collection_errors', 0) + 1)
                    kinds = getattr(self, '_gc_error_types', None)
                    if kinds is None:
                        kinds = Counter()
                        self._gc_error_types = kinds
                    kinds[type(error).__name__] += 1
                    try:
                        self._gc_last_error = ('%s: %s'
                                               % (type(error).__name__, error))
                    except Exception:
                        pass
                except Exception:
                    pass

    def _sample(self, bucket, limit):
        if bucket['count'] >= limit:
            bucket['truncated'] = True
            return
        frame = sys._current_frames().get(self.gui_thread)
        if frame is not None:
            bucket['samples'][_frame_signature(frame)] += 1
            bucket['count'] += 1

    def _sample_other_threads(self, bucket, limit=200):
        """Snapshot every thread, not just the GUI thread.

        When the GUI thread is waiting on the GIL, its own stack says
        "waiting" while the interesting frames belong to whichever thread
        holds it. Sampled at a fraction of the stall sampler's rate so the
        cost stays trivial even in minute-long stalls.
        """
        if bucket['others'] >= limit:
            bucket['others_truncated'] = True
            return
        frames = sys._current_frames()
        names = {info.ident: info.name for info in threading.enumerate()}
        bucket['others'] += 1
        for ident, frame in frames.items():
            if ident == self.gui_thread or frame is None:
                continue
            signature = _frame_signature(frame, max_frames=6)
            name = names.get(ident, str(ident))
            bucket['other_samples'][f'{name}: {signature}'] += 1

    @staticmethod
    def _histogram_fields(bucket, top=12):
        """The [count, signature] pairs every bucket reports, most hit first."""
        return dict(samples=bucket['count'], truncated=bucket['truncated'],
                    stack_histogram=[[count, signature] for signature, count in
                                     bucket['samples'].most_common(top)])

    @staticmethod
    def _other_thread_fields(bucket, top=4):
        if 'other_samples' not in bucket:
            return {}
        return dict(other_thread_samples=bucket['others'],
                    other_threads_truncated=bucket.get('others_truncated', False),
                    other_threads=[[count, signature] for signature, count in
                                   bucket['other_samples'].most_common(top)])

    def _current_dispatch_fields(self, now=None):
        """Describe the innermost active dispatch without changing its identity."""
        active = tuple(self.current_dispatch)
        top = active[-1] if active else None
        if top is None:
            return {}
        now = time.perf_counter() if now is None else now
        return dict(dispatch_id=top['dispatch_id'],
                    parent_dispatch_id=top['parent_dispatch_id'],
                    root_dispatch_id=top['root_dispatch_id'], depth=top['depth'],
                    dispatch_target=top['target'], dispatch_event=top['event_type'],
                    dispatch_start_monotonic=top['start_monotonic'],
                    dispatch_age_ms=round((now-top['start_monotonic'])*1000, 1))

    @staticmethod
    def _impact_level(milliseconds):
        if milliseconds >= 5000:
            return 'extreme'
        if milliseconds >= 1000:
            return 'severe'
        if milliseconds >= 200:
            return 'moderate'
        return 'minor'

    def _stall_dispatch_intervals(self, stall, end_monotonic):
        """Freeze dispatch intervals that overlap this stall's original window."""
        start = stall['window_start_monotonic']
        snapshots = dict(stall.get('dispatch_snapshots') or {})
        with self._dispatch_history_lock:
            history = tuple(self._dispatch_history)
        for item in history:
            if (item['end_monotonic'] >= start
                    and item['start_monotonic'] <= end_monotonic):
                snapshots[item['dispatch_id']] = dict(item)
        for item in tuple(self.current_dispatch):
            if item['start_monotonic'] <= end_monotonic:
                current = dict(item)
                current['end_monotonic'] = end_monotonic
                current['interval_open_at_stall_end'] = True
                snapshots[item['dispatch_id']] = current
        return sorted(snapshots.values(), key=lambda item: (
            item['start_monotonic'], item['depth'], item['dispatch_id']))

    def _watch(self):
        reported = False
        last_report = 0.0
        stall = None
        while not self.stopped.wait(self.WATCH_INTERVAL):
            if mode() == MODE_OFF:
                stall = None
                reported = False
                continue
            now = time.perf_counter()
            stalled = now-self.last_heartbeat
            if stalled < self.MINOR_STALL_THRESHOLD:
                if stall is not None:
                    peak = stall.get('peak', 0.0)
                    peak_ms = peak*1000.0
                    window_end = stall['window_start_monotonic'] + peak
                    stall['window_end_monotonic'] = window_end
                    stall['dispatch_intervals'] = self._stall_dispatch_intervals(
                        stall, window_end)
                    stall['dispatches_completed'] = sum(
                        item.get('end_monotonic', window_end+1) <= window_end
                        for item in stall['dispatch_intervals'])
                    context = self._record_stall_context(stall, peak_ms, window_end)
                    end_fields = dict(
                        stall_id=stall['stall_id'], phase='end', role='end',
                        severity=stall.get('severity', 'normal'),
                        impact_level=stall.get('impact_level', self._impact_level(peak_ms)),
                        impact_policy_version=1, duration_ms=round(peak_ms, 1),
                        window_start_monotonic=stall['window_start_monotonic'],
                        window_end_monotonic=window_end,
                        heartbeat_delay_ms=round(stall.get('heartbeat_max', 0.0), 3),
                        completed_dispatches=stall['dispatches_completed'],
                        background_observed=bool(stall.get('background_observed')),
                        dispatch_intervals=context.get('dispatch_intervals', []),
                        dispatch_batch_version=context.get('dispatch_batch_version'),
                        dispatch_batch_count=context.get('dispatch_batch_count'),
                        dispatch_interval_count=context.get('dispatch_interval_count'),
                        classification=context.get('classification', 'unclassified'),
                        classification_reason=context.get(
                            'classification_reason', 'classification_failed'),
                        last_action=self.last_action,
                        blocked_thread=self._blocked_thread_name(
                            sys._current_frames().get(self.gui_thread)))
                    self.record('gui_stall', priority=PRIORITY_TERMINAL,
                                **end_fields, **self._histogram_fields(stall),
                                **self._other_thread_fields(stall))
                    stall = None
                reported = False
            else:
                if stall is None:
                    active = {item['dispatch_id']: dict(item)
                              for item in self.current_dispatch}
                    stall = dict(
                        stall_id=uuid.uuid4().hex[:16], samples=Counter(), count=0,
                        truncated=False, detected_monotonic=now,
                        window_start_monotonic=self.last_heartbeat,
                        peak=0.0, others=0, others_truncated=False,
                        other_samples=Counter(), heartbeat_max=0.0,
                        dispatches_completed=self.dispatches_since_heartbeat,
                        dispatch_snapshots=active, background_observed=False,
                        context_snapshot=dict(self._context_snapshot))
                stall['dispatches_completed'] = max(
                    stall['dispatches_completed'], self.dispatches_since_heartbeat)
                for item in self.current_dispatch:
                    stall['dispatch_snapshots'].setdefault(
                        item['dispatch_id'], dict(item))
                stall['background_observed'] = (
                    stall['background_observed'] or self.background_activity())
                stall['peak'] = max(stall['peak'], stalled)
                stall['heartbeat_max'] = max(stall['heartbeat_max'], stalled*1000)
                stall['severity'] = ('normal'
                                     if stall['peak'] >= self.STALL_THRESHOLD
                                     else 'minor')
                impact = self._impact_level(stall['peak']*1000)
                # Thresholds are ordered, so retaining the maximum peak also
                # guarantees the impact level can only rise within a window.
                stall['impact_level'] = impact
                limit = (self.STALL_SAMPLE_LIMIT if stall['severity'] == 'normal'
                         else self.MINOR_STALL_SAMPLE_LIMIT)
                self._sample(stall, limit)
                if stall['count'] % 10 == 0:
                    self._sample_other_threads(stall)
                if stall['severity'] != 'minor' and (
                        not reported or now-last_report >= 2):
                    frame = sys._current_frames().get(self.gui_thread)
                    completed = self.last_dispatch
                    age = ((now-completed['end_monotonic'])*1000
                           if completed else None)
                    self.record(
                        'gui_stall', phase='ongoing', role='attachment',
                        stall_id=stall['stall_id'], severity='normal',
                        impact_level=stall['impact_level'], impact_policy_version=1,
                        duration_ms=round(stalled*1000, 1),
                        window_start_monotonic=stall['window_start_monotonic'],
                        heartbeat_delay_ms=round(stall['heartbeat_max'], 3),
                        background_observed=stall['background_observed'],
                        last_action=self.last_action,
                        completed_dispatches=stall['dispatches_completed'],
                        last_dispatch_id=(completed or {}).get('dispatch_id'),
                        last_dispatch_age_ms=round(age, 1) if age is not None else None,
                        last_dispatch_target=(completed or {}).get('target'),
                        last_dispatch_event=(completed or {}).get('event_type'),
                        timer_starved=True,
                        blocked_thread=self._blocked_thread_name(frame),
                        stack=traceback.format_stack(frame, limit=-40) if frame else [],
                        **self._histogram_fields(stall),
                        **self._other_thread_fields(stall),
                        **self._current_dispatch_fields(now))
                    reported, last_report = True, now
            # Sample any event dispatch that outlives the age threshold.
            self._sample_active_dispatches(now)
            cutoff = now-10
            active_ids = {item['dispatch_id'] for item in tuple(self.current_dispatch)}
            for key, bucket in list(self._dispatch_samples.items()):
                if key not in active_ids and bucket.get('started', now) < cutoff:
                    self._dispatch_samples.pop(key, None)

    def _sample_active_dispatches(self, now):
        """One Python stack sample shared across bounded active ancestors.

        Parent samples include descendant execution, not an exclusive parent
        native stack. Keep buckets across child transitions; do not reset them
        whenever the innermost dispatch changes.
        """
        active = tuple(self.current_dispatch)
        if not active:
            return
        eligible = [item for item in active[:64]
                    if now-item['start_monotonic'] >= self.DISPATCH_SAMPLE_AGE]
        if not eligible:
            return
        frame = sys._current_frames().get(self.gui_thread)
        if frame is None:
            return
        signature = None
        for item in eligible:
            bucket = self._dispatch_samples.setdefault(item['dispatch_id'], dict(
                samples=Counter(), count=0, truncated=False,
                started=item['start_monotonic'], descendant_samples=0))
            if bucket['count'] >= self.DISPATCH_SAMPLE_LIMIT:
                bucket['truncated'] = True
                continue
            if signature is None:
                signature = _frame_signature(frame)
            bucket['samples'][signature] += 1
            bucket['count'] += 1
            if item['dispatch_id'] != active[-1]['dispatch_id']:
                bucket['descendant_samples'] += 1

    def note_span_open(self):
        """A traced stage started on this thread."""
        ident = threading.get_ident()
        with self._span_lock:
            self._open_spans[ident] = self._open_spans.get(ident, 0)+1
            self._span_since.setdefault(ident, time.monotonic())

    def note_span_close(self):
        """A traced stage ended on this thread."""
        ident = threading.get_ident()
        with self._span_lock:
            count = self._open_spans.get(ident, 0)-1
            if count > 0:
                self._open_spans[ident] = count
            else:
                self._open_spans.pop(ident, None)
                self._span_since.pop(ident, None)

    def background_activity(self, *, max_age_s=120):
        """True when a background thread is inside a traced stage right now.

        A stage that was never closed (a crashed worker) must not make the
        GUI look busy forever, so an entry older than ``max_age_s`` is
        ignored.
        """
        now = time.monotonic()
        with self._span_lock:
            for ident, count in self._open_spans.items():
                if ident == self.gui_thread or count <= 0:
                    continue
                since = self._span_since.get(ident)
                if since is None or now-since <= max_age_s:
                    return True
        return False

    def _record_stall_context(self, stall, peak_ms, window_end):
        """Build and publish the terminal stall context from frozen evidence."""
        fallback = {
            'stall_id': stall.get('stall_id'), 'classification': 'unclassified',
            'classification_reason': 'classification_failed',
        }
        classifier_state = 'available'
        try:
            from prism.project_catalog_index import build_stall_context, record_stall
        except Exception:  # pragma: no cover - pkg3 is optional
            # The classifier is optional. The GC measurement below is not:
            # it is computed from the frozen intervals either way, and this
            # module writes the evidence itself when the classifier is not
            # there. "Computed" and "written" are separate facts.
            build_stall_context = record_stall = None
            classifier_state = 'unavailable'
        heartbeat_delay = stall.get('heartbeat_max')
        snapshot = stall.get('context_snapshot') or {}
        intervals = stall.get('dispatch_intervals') or []
        threshold_ms = (float(getattr(self, 'STALL_THRESHOLD',
                                      PerformanceTrace.STALL_THRESHOLD))
                        * 1000)
        if build_stall_context is None:
            fields = dict(fallback, classification_reason='classifier_unavailable')
        else:
            try:
                fields = build_stall_context(
                    stall_id=stall['stall_id'], duration_ms=peak_ms, stage='gui_stall',
                    window_start_monotonic=stall['window_start_monotonic'],
                    window_end_monotonic=window_end,
                    dispatch_intervals=intervals,
                    dispatches_completed=stall.get('dispatches_completed'),
                    heartbeat_delay_ms=heartbeat_delay,
                    timer_starved=(None if heartbeat_delay is None
                                   else heartbeat_delay >= threshold_ms),
                    sample_count=stall.get('count'), modal=snapshot.get('modal'),
                    nested_loop=None,
                    background_busy=bool(stall.get('background_observed')),
                    project=snapshot.get('project_token'), mode=None,
                    effective_mode=None, scope_fingerprint=None,
                    stack_thread_id=self.gui_thread)
            except Exception as error:  # never break the watchdog
                classifier_state = 'raised'
                fields = dict(fallback, classification_error=type(error).__name__)
        with _lock_of(self, '_gc_lock'):
            gc_intervals = tuple(self._gc_intervals)
        gc_overlaps = [(max(start, stall['window_start_monotonic']), min(end, window_end))
                       for start, end, on_gui in gc_intervals
                       if on_gui and end > stall['window_start_monotonic'] and start < window_end]
        # Overlapping collections cover the wall clock **once**. Adding the
        # intervals up would invent time that never elapsed, and the
        # fraction would then exceed what the window can hold.
        gc_union = union_intervals(gc_overlaps)
        gc_ms = sum((end-start)*1000 for start, end in gc_union)
        gc_naive_sum_ms = sum((end-start)*1000 for start, end in gc_overlaps)
        window_ms = max(0, (window_end-stall['window_start_monotonic'])*1000)
        gc_fraction = (gc_ms/window_ms) if window_ms else None
        fields['observed_gui_gc_ms'] = round(gc_ms, 3)
        fields['observed_gui_gc_union_ms'] = round(gc_ms, 3)
        fields['observed_gui_gc_naive_sum_ms'] = round(gc_naive_sum_ms, 3)
        fields['observed_gui_gc_interval_count'] = len(gc_overlaps)
        fields['observed_gui_gc_merged_interval_count'] = len(gc_union)
        fields['observed_gui_gc_fraction'] = gc_fraction
        fields['gc_attribution'] = 'measured_collection_overlap_not_allocation_cause'
        fields['gc_classification_applied'] = False
        heartbeat_starved = (heartbeat_delay is not None
                             and heartbeat_delay >= threshold_ms)
        modal_context = bool(snapshot.get('modal'))
        fields['modal_context'] = modal_context
        fields['heartbeat_starved'] = heartbeat_starved
        fields['classifier_state'] = classifier_state
        # A modal dialog and "the event loop is running, waiting for the
        # user" are **not** the same fact. Inside an export/save/material
        # dialog the GUI can still be blocked by the collector or by
        # synchronous I/O. So the modal context is published as evidence,
        # and only the *absence* of any blocking evidence marks the window
        # as a possible user wait.
        fields['user_waiting_possible'] = bool(modal_context
                                               and not heartbeat_starved)
        if gc_fraction is not None and gc_fraction >= .9:
            # GC dominance is real blocking evidence and a modal does not
            # cancel it: the GUI thread was in the collector, not waiting
            # for the user.
            fields['classification'] = 'gc_pause'
            fields['classification_reason'] = (
                'main_thread_gc_covers_at_least_90pct_stall_window')
            fields['gc_classification_applied'] = True
        elif modal_context and heartbeat_starved:
            # The modal is up **and** the event loop was starved: that is a
            # block, not a user wait.
            fields['classification'] = 'heartbeat_starved'
            fields['classification_reason'] = (
                'modal_dialog_with_starved_heartbeat_is_a_real_block')
            fields['gc_classification_applied'] = False
        fields.update(
            role='attachment', trace_mode=mode(),
            project_generation=snapshot.get('generation'),
            project_open_epoch=snapshot.get('open_epoch'),
            background_busy=bool(stall.get('background_observed')),
            background_evidence='observed_open_traced_stages',
            dispatch_intervals=intervals)
        covering = fields.get('covering_dispatches') or []
        if len(covering) == 1:
            fields['gui_dispatch_id'] = covering[0].get('dispatch_id')
            fields['gui_dispatch_target'] = covering[0].get('target')
            fields['gui_dispatch_event'] = covering[0].get('event_type')
        prefix, batches, batch_metadata = _stall_dispatch_payloads(intervals)
        fields.update(dispatch_intervals=prefix, **batch_metadata)
        for index, batch in enumerate(batches):
            self.record('ui.stall_dispatch_batch', priority=PRIORITY_TERMINAL,
                        stall_id=stall['stall_id'], dispatch_batch_version=1,
                        batch_index=index, batch_count=len(batches),
                        dispatch_interval_count=batch_metadata['dispatch_interval_count'],
                        window_start_monotonic=stall['window_start_monotonic'],
                        window_end_monotonic=window_end, dispatch_intervals=batch)
        # One stall context is written once; bounded interval batches carry
        # only the already-frozen evidence, not additional samples or queries.
        written = False
        if record_stall is not None and classifier_state == 'available':
            try:
                record_stall(**fields)
                written = True
            except Exception as error:
                _note_diagnostic_failure('stall_context_write', error)
        if not written:
            try:
                self.record('ui.stall_context', priority=PRIORITY_TERMINAL,
                            **fields)
            except Exception as error:
                _note_diagnostic_failure('stall_context_write', error)
        return fields

    @staticmethod
    def _blocked_thread_name(frame):
        """Which of our own threads is holding the GIL, when it is us.

        A stall that is really the writer thread or the watchdog doing work
        looks identical to a GUI stall from the outside; this names it.
        """
        if frame is None:
            return None
        for ident, candidate in sys._current_frames().items():
            if candidate is frame:
                for info in threading.enumerate():
                    if info.ident == ident:
                        return info.name
        return threading.current_thread().name

    def _heartbeat(self):
        now = time.perf_counter()
        budget_interval = 5.0 if mode() == MODE_DIAGNOSTIC else 15.0
        if mode() != MODE_OFF and now - self._last_budget_sample >= budget_interval:
            self._last_budget_sample = now
            try:
                # GUI timer: do not inspect QObject-backed caches from the
                # watchdog thread. Sampling does not enforce/evict or GC.
                from prism.resource_budget import shared_cache_budget
                shared_cache_budget()._record_snapshot('periodic', force=True)
            except Exception as error:
                self.record('resource_budget_sample_failed', error=type(error).__name__)
        gap = now-self.last_heartbeat
        self.last_heartbeat = now
        if mode() == MODE_OFF:
            return
        self.dispatches_since_heartbeat = 0
        # The heartbeat runs on the GUI thread, so this is the only place
        # where the *current* project identity and the modal state can be
        # read: the watchdog thread has its own (empty) context, and Qt
        # widget state must not be touched from another thread.
        context = _context.get()
        snapshot = dict(self._context_snapshot)
        if context is not None and context.project_token:
            snapshot.update(project_token=context.project_token,
                            open_epoch=context.open_epoch,
                            generation=context.generation,
                            trace_id=context.trace_id)
        try:
            snapshot['modal'] = (QtWidgets.QApplication.activeModalWidget()
                                 is not None)
        except Exception:  # pragma: no cover - defensive only
            snapshot['modal'] = None
        self._context_snapshot = snapshot
        if gap > .25:
            self.record('gui_recovered', blocked_ms=round((gap-self.INTERVAL)*1000, 1), last_action=self.last_action)
        if self.window is None:
            # install() runs before the main window exists; keep the
            # heartbeat alive without the window-dependent summary.
            self.summary_time = now
            return
        if now-self.summary_time >= 1:
            view = self.window.view
            scene = view.scene
            decoder = getattr(QtWidgets.QApplication.instance(), '_prism_display_decoder', None)
            manager = scene._viewport_items
            tiles = view._overview_renderer.tiles
            browser = getattr(self.window, 'asset_browser', None)
            canvas = getattr(browser, 'list', None)
            asset_tiles = getattr(canvas, '_tiles', None)
            from prism import lod
            library = getattr(self.window, 'material_library', None)
            periodic_interval = 1.0 if mode() == MODE_DIAGNOSTIC else 5.0
            if now-self._last_periodic_sample >= periodic_interval:
                cpu = time.process_time()
                child = child_process_sample()
                self.record('process', rss_bytes=process_memory_bytes(),
                        io=process_io_bytes(),
                        webengine_child_bytes=(child.get('bytes') if child else None),
                        webengine_child_bytes_status=('measured' if child and
                                                     child.get('bytes') is not None
                                                     else 'unknown'),
                        webengine_child_count=(child.get('count') if child else None),
                        webengine_child_sample_age_s=(round(max(0.0, time.perf_counter() -
                                                              child['monotonic']), 3)
                                                      if child else None),
                        webengine_child_source=(child.get('source') if child else None),
                        webengine_child_error=(child.get('error') if child else None),
                        sample_interval_ms=(now-self._last_periodic_sample)*1000,
                        cpu_one_core_percent=round((cpu-self.cpu_time)/(now-self._last_periodic_sample)*100, 2))
                self.cpu_time = cpu
                self._last_periodic_sample = now
                if library is not None and getattr(library, 'initialized', True):
                    attachments = scene.attachments
                    self.record('material_performance', visible=library.isVisible(),
                            records=library.materials.count(), selected=library.selected_id,
                            jobs=len(library._jobs), worker_active=library._worker_pool.activeThreadCount(),
                            worker_limit=library._worker_pool.maxThreadCount(),
                            icon_pending=len(library._icon_pending), icon_cache=len(library._icon_cache),
                            render_cache=len(library._render_cache),
                            temporary_directories=len(library._render_directories),
                            attachment_blobs_loaded=(attachments.loaded_count() if hasattr(attachments, 'loaded_count') else None),
                            attachment_bytes_loaded=(attachments.total_loaded_bytes() if hasattr(attachments, 'total_loaded_bytes') else None))
                    self.record('material_cache', thumbnail_bytes=library._thumbnail_cache.cache.bytes,
                            thumbnail_hits=library._thumbnail_cache.cache.hits,
                            thumbnail_misses=library._thumbnail_cache.cache.misses,
                            slot_icon_bytes=library._icon_cache.bytes)
            written_last = self._written_since_heartbeat
            bytes_last = self._bytes_since_heartbeat
            self._written_since_heartbeat = 0
            self._bytes_since_heartbeat = 0
            self.record('performance', dispatch_counts=dict(self.counts), max_dispatch_ms=round(self.max_dispatch, 3),
                        assets=scene._item_count, edit_objects=len(manager.materialized),
                        scale=round(view.transform().m11(), 6), tile_bytes=tiles.bytes,
                        tile_coverage_missing=tiles.coverage_missing,
                        tile_pending=len(tiles.pending), decode_pending=len(decoder.requests) if decoder else 0,
                        dropped_events=self.dropped, record_layers=len(manager.layers), image_cache_bytes=lod.cache().bytes,
                        trace_incomplete=self.trace_incomplete, queue_peak=self.queue_peak,
                        dropped_by_kind=dict(self.dropped_by_kind) if self.dropped else None,
                        record_self_ms_total=round(self.self_ms_total, 3),
                        record_self_ms_max=round(self.self_ms_max, 3),
                        events_written_total=self.events_written,
                        bytes_written_total=self.bytes_written,
                        events_written_last_second=written_last,
                        bytes_written_last_second=bytes_last,
                        writer_ms_total=round(self.writer_ms_total, 3),
                        writer_ms_max=round(self.writer_ms_max, 3),
                        writer_ms_per_event=(round(self.writer_ms_total/self.events_written, 4)
                                             if self.events_written else None),
                        gc_collection_errors=self._gc_collection_errors,
                        gc_error_types=(dict(self._gc_error_types)
                                        if self._gc_error_types else None),
                        trace_mode=mode(), schema_version=SCHEMA_VERSION,
                        recording_contract_version=RECORDING_CONTRACT_VERSION,
                        active_surface=('materials' if library is not None and library.isVisible() else
                                        'assets' if canvas is not None and canvas.isVisible() else 'canvas'),
                        asset_tile_bytes=asset_tiles.bytes if asset_tiles else 0,
                        asset_tile_coverage_missing=asset_tiles.coverage_missing if asset_tiles else 0,
                        asset_tile_pending=len(asset_tiles.pending) if asset_tiles else 0,
                        asset_cards_painted=getattr(canvas, '_last_painted_count', 0),
                        slow_classes=[[round(ms, 1), '%s:%s' % (cls, evt)]
                                      for (cls, evt), ms in self.dispatch_time.most_common()
                                      if ms >= 50][:30],
                        input_to_paint=self._input_to_paint_summary())
            self.counts.clear()
            self.dispatch_time.clear()
            self.input_to_paint = []
            self.max_dispatch = 0
            self.summary_time = now

    def _input_to_paint_summary(self):
        """Per-second stats of click -> screen-update latency, when present."""
        values = self.input_to_paint
        if not values:
            return None
        return dict(count=len(values),
                    min_ms=round(min(values), 1),
                    avg_ms=round(sum(values)/len(values), 1),
                    max_ms=round(max(values), 1))

    def dispatch_started(self, receiver, event_type, started):
        """Mark one uniquely identified event-dispatch layer."""
        if mode() == MODE_OFF or threading.get_ident() != self.gui_thread:
            return None
        parent = self.current_dispatch[-1] if self.current_dispatch else None
        dispatch_id = '%s-%s' % (self.session, next(self._dispatch_counter))
        entry = {
            'dispatch_id': dispatch_id,
            'parent_dispatch_id': parent['dispatch_id'] if parent else None,
            'root_dispatch_id': parent['root_dispatch_id'] if parent else dispatch_id,
            'depth': len(self.current_dispatch),
            'start_monotonic': float(started),
            'target': type(receiver).__name__, 'event_type': event_type,
        }
        self.current_dispatch.append(entry)
        return dispatch_id

    def dispatch_finished(self, receiver, event_type, milliseconds, *, nested_loop=False,
                          started=None):
        if mode() == MODE_OFF or threading.get_ident() != self.gui_thread:
            return None
        ended = time.perf_counter()
        matched = None
        if (self.current_dispatch and started is not None
                and self.current_dispatch[-1]['start_monotonic'] == started):
            matched = self.current_dispatch.pop()
        elif started is not None:
            for index in range(len(self.current_dispatch)-1, -1, -1):
                if self.current_dispatch[index]['start_monotonic'] == started:
                    matched = self.current_dispatch.pop(index)
                    break
        if matched is None:
            # Compatibility for direct callers that only supplied a duration.
            dispatch_id = '%s-%s' % (self.session, next(self._dispatch_counter))
            matched = {
                'dispatch_id': dispatch_id, 'parent_dispatch_id': None,
                'root_dispatch_id': dispatch_id, 'depth': 0,
                'start_monotonic': ended-float(milliseconds)/1000.0,
                'target': type(receiver).__name__, 'event_type': event_type,
                'synthetic_start': True,
            }
        interval = dict(matched, end_monotonic=ended,
                        duration_ms=round(float(milliseconds), 3),
                        nested_loop=bool(nested_loop))
        with self._dispatch_history_lock:
            self._dispatch_history.append(interval)
        self.last_dispatch = interval
        self.dispatches_since_heartbeat += 1
        receiver_type = type(receiver)
        key = (receiver_type, event_type)
        labels = self._dispatch_stat_keys.get(key)
        if labels is None:
            labels = (str(event_type), (receiver_type.__name__, str(event_type)))
            if len(self._dispatch_stat_keys) >= 2048:
                self._dispatch_stat_keys.clear()
            self._dispatch_stat_keys[key] = labels
        self.counts[labels[0]] += 1
        self.dispatch_time[labels[1]] += milliseconds
        if not nested_loop:
            self.max_dispatch = max(self.max_dispatch, milliseconds)
        if milliseconds >= 32:
            try:
                object_name = QtCore.QObject.objectName(receiver)
                parent = QtCore.QObject.parent(receiver)
                owner = type(parent).__name__ if parent is not None else None
            except (TypeError, RuntimeError):
                object_name, owner = '', None
            histogram = {}
            bucket = self._dispatch_samples.pop(matched['dispatch_id'], None)
            if bucket is not None:
                histogram = self._histogram_fields(bucket, top=10)
            if not histogram.get('samples'):
                histogram['unattributed_ms'] = round(milliseconds, 1)
            self.record(
                'nested_dispatch' if nested_loop else 'slow_dispatch',
                role='attachment', target=type(receiver).__name__,
                event_type=event_type, object_name=object_name, owner=owner,
                dispatch_id=matched['dispatch_id'],
                parent_dispatch_id=matched['parent_dispatch_id'],
                root_dispatch_id=matched['root_dispatch_id'], depth=matched['depth'],
                dispatch_start_monotonic=matched['start_monotonic'],
                dispatch_end_monotonic=ended,
                duration_ms=round(milliseconds, 3), last_action=self.last_action,
                stack_scope='python_gui_inclusive_active_dispatch',
                descendant_samples=(bucket or {}).get('descendant_samples', 0),
                native_stack_collected=False,
                **histogram)
        if event_type == 12:
            self._consume_input_marker(receiver)
        return matched['dispatch_id']

    def _consume_input_marker(self, receiver):
        """Legacy metric: latest input to first ANY widget paint, not content."""
        marker = self.input_marker
        if marker is None:
            return
        self.input_marker = None
        now = time.perf_counter()
        if now - marker[0] > 5:
            return
        latency = (now - marker[0]) * 1000
        self.input_to_paint.append(latency)
        if latency >= 300:
            try:
                receiver_name = receiver.objectName()
            except (AttributeError, RuntimeError):
                receiver_name = ''
            self.record('slow_input_to_paint', latency_ms=round(latency, 1),
                        target=type(receiver).__name__, action_target=marker[1],
                        action=marker[2], last_action=self.last_action,
                        measurement_basis='latest_input_to_first_any_widget_paint',
                        marker_policy='latest_input_overwrites',
                        receiver_object_name=receiver_name,
                        target_content_confirmed=False, display_confirmed=False)

    def attach_window(self, window):
        """Hook the main window once it is constructed.

        ``install()`` runs before the window exists so startup time is
        covered; the window-dependent bits come here instead.
        """
        self.window = window
        self.hook_actions(window)

    def hook_action(self, action):
        """Hook one QAction: cheap, called once per newly added action."""
        if action is None or not isinstance(action, QtGui.QAction):
            return
        if action in self.actions:
            return
        self.actions.add(action)
        # Many QActions have neither objectName nor text (menu separators,
        # dynamic tag entries). Name them from their owning menu so an
        # `action` event never arrives anonymous -- the recent-save
        # complaint left empty names in the trace and no way to tell which
        # menu entry had been clicked.
        name = action.objectName() or action.text()
        if not name:
            try:
                owner = QtCore.QObject.parent(action)
                if isinstance(owner, QtWidgets.QMenu) and owner.title():
                    name = '%s/%s' % (owner.title(), action.text() or '?')
                else:
                    name = type(owner).__name__ if owner is not None else '?'
            except (TypeError, RuntimeError):
                name = '?'
        action.triggered.connect(lambda checked=False, name=name: self.record('action', name=name, checked=checked))

    def hook_actions(self, widget):
        if widget is None:
            return
        for action in widget.findChildren(QtGui.QAction):
            self.hook_action(action)

    def eventFilter(self, watched, event):
        """App-wide input tracing.

        A tracing filter is the last component allowed to break the app: any
        unexpected event class must be counted and skipped, never raised.
        """
        try:
            return self._filter_event(watched, event)
        except Exception:  # noqa: BLE001 - counted; a filter must not crash
            self.filter_errors = getattr(self, 'filter_errors', 0) + 1
            return False

    def _filter_event(self, watched, event):
        if mode() == MODE_OFF:
            return False
        kind = event.type()
        types = QtCore.QEvent.Type
        fields = None
        if kind in (types.MouseButtonPress, types.MouseButtonDblClick, types.MouseButtonRelease):
            point = event.position()
            fields = dict(operation=kind.name, x=round(point.x(), 1), y=round(point.y(), 1), button=event.button().value,
                          modifiers=event.modifiers().value)
            key = event.button().value
            if kind == types.MouseButtonPress:
                self.presses[key] = (time.perf_counter(), point.x(), point.y())
            elif kind == types.MouseButtonRelease and key in self.presses:
                started, x, y = self.presses.pop(key)
                fields.update(gesture_ms=round((time.perf_counter()-started)*1000, 2), dx=round(point.x()-x, 1), dy=round(point.y()-y, 1))
        elif kind == types.Wheel:
            fields = dict(operation='wheel', dx=event.angleDelta().x(), dy=event.angleDelta().y(),
                          pixel_dy=event.pixelDelta().y(), modifiers=event.modifiers().value)
        elif kind == types.KeyPress:
            fields = dict(operation='key', key=event.key(), modifiers=event.modifiers().value, repeat=event.isAutoRepeat())
        elif kind == types.Drop:
            fields = dict(operation='drop')
        elif kind == types.ActionAdded:
            # QActionEvent carries the newly added action: hook just that
            # one. A full-window `findChildren` scan here ran once per menu
            # action added -- O(actions²) while a tag menu with thousands
            # of entries was built, freezing the context menu for ~25 s on
            # a 100k-asset project.
            #
            # The binding may deliver this type as a plain QEvent wrapper
            # without ``action()``; calling it unconditionally crashed the
            # whole app on the next click. Check the real class first.
            action = event.action() if isinstance(event, QtGui.QActionEvent) else None
            if action is not None:
                self.hook_action(action)
            else:  # pragma: no cover - defensive
                self.hook_actions(self.window)
        if fields is not None:
            self.record('input', target=type(watched).__name__, object=watched.objectName(), **fields)
        return False

    @QtCore.pyqtSlot()
    def close(self):
        if QtCore.QThread.currentThread() != self.thread():
            self.close_requested.emit()
            return
        if self._closed:
            return
        self._closed = True
        app = QtWidgets.QApplication.instance()
        if getattr(app, '_performance_trace', None) is self:
            app._performance_trace = None
        global _active
        if _active is self:
            _active = None
        try:
            gc.callbacks.remove(self._gc_callback)
        except ValueError:
            pass
        self.timer.stop()
        QtWidgets.QApplication.instance().removeEventFilter(self)
        if self.version_snapshot is None:
            # Shutdown must not hash source files or invoke git on the GUI.
            # An unfinished version snapshot is unknown, not a reason to block.
            self.record('trace_version_pending', priority=PRIORITY_SESSION,
                        version_complete=False)
        # The tail of the hot-path counters is written before the session
        # ends. It belongs to this session and must not be carried into the
        # next one.
        flushed = flush_hotpath_rollups(reason='session_end', trace=self)
        self.record('trace_manifest', priority=PRIORITY_SESSION,
                    **self._manifest('end',
                                     started_at=self._started_at,
                                     ended_at=time.time(),
                                     duration_s=round(time.time()-self._started_at, 3),
                                     events_written=self.events_written,
                                     bytes_written=self.bytes_written,
                                     writer_ms_total=round(self.writer_ms_total, 3),
                                     writer_ms_max=round(self.writer_ms_max, 3),
                                     record_self_ms_total=round(self.self_ms_total, 3),
                                     record_self_ms_max=round(self.self_ms_max, 3),
                                     gc_collection_errors=self._gc_collection_errors,
                                     hotpath_flushes=len(flushed),
                                     version_snapshot=self.version_snapshot))
        # The honest loss report goes in before the flag is set: a bounded
        # queue can never promise "never dropped", so the numbers that
        # explain a gap have to be in the log itself.
        self.record('session_end', dropped_events=self.dropped,
                    priority=PRIORITY_SESSION, mode=mode(),
                    dropped_by_kind=dict(self.dropped_by_kind),
                    trace_incomplete=self.trace_incomplete,
                    queue_peak=self.queue_peak,
                    queue_capacity=(self.events.maxsize+self.important.maxsize),
                    record_self_ms_total=round(self.self_ms_total, 3),
                    record_self_ms_max=round(self.self_ms_max, 3),
                    events_written=self.events_written,
                    bytes_written=self.bytes_written,
                    writer_ms_total=round(self.writer_ms_total, 3),
                    writer_ms_max=round(self.writer_ms_max, 3),
                    gc_collection_errors=self._gc_collection_errors,
                    diagnostic_failures=diagnostic_failures(),
                    recording_contract_version=RECORDING_CONTRACT_VERSION,
                    writer_lifecycle_version=1,
                    end_scope='recorder_close_requested_not_process_exit',
                    writer_errors_before_close=self.writer_errors)
        pending = self.events.qsize() + self.important.qsize()
        if pending:
            # Bounded flush: never join forever on the GUI thread.
            self.record('trace_shutdown_pending', priority=PRIORITY_SESSION,
                        pending_at_close=pending)
        with _lock_of(self, '_record_gate'):
            self.stopped.set()
        if self.writer is not threading.current_thread():
            self.writer.join(timeout=1)
        self.window = None  # A closed diagnostic must not keep a top-level QWidget alive.


def install(app, window, directory):
    global _active
    if _active is None:
        _active = PerformanceTrace(app, window, directory)
        app._performance_trace = _active
    return _active
