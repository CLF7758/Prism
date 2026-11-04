"""Model-library events on the existing trace pipeline.

One entry point, one vocabulary, one privacy guard. This module never opens a
file, never touches Qt, never blocks on I/O and never raises into the business
it observes.

* ``record_model_event(event, **fields)`` keeps its historic signature, so the
  calls that already exist (``shell_state``, ``feature_unavailable``,
  ``diagnostic_folder_opened``) keep working without any change.
* New events and fields are added **here**, never in a second log system.
* Model bytes, texture Base64, note bodies and credentials never reach the
  log. Paths are masked with the same token the diagnostic bundle uses, so the
  operations log and the bundle agree on what a path is.
* Durations come from a monotonic clock inside one process. Two processes
  never subtract each other's timestamps; the end-to-end duration is measured
  by the main process that owns both ends.
"""
from __future__ import annotations

import contextlib
import contextvars
import hashlib
import ntpath
import re
import threading
import time
from collections import OrderedDict

#: Every model event reaches the trace pipeline under this prefix.
MODEL_EVENT_PREFIX = 'model_'

#: Bumped when the field contract changes in a way a reader must notice.
EVENT_SCHEMA = 'model_event/1'

# ── vocabulary ────────────────────────────────────────────────────────────
# The lifecycle of one model job. ``rendered`` is *not* "shown to the user",
# and ``installed`` is *not* "imported": each name says exactly one thing.
EVENT_QUEUED = 'queued'
EVENT_STARTED = 'started'
EVENT_SOURCE_PREPARED = 'source_prepared'
EVENT_NATIVE_LOADED = 'native_loaded'
EVENT_RENDERED = 'rendered'
EVENT_OUTPUT_WRITTEN = 'output_written'
EVENT_INSTALLED = 'installed'
EVENT_COMPLETED = 'completed'
EVENT_FAILED = 'failed'
EVENT_CANCELLED = 'cancelled'
EVENT_STALE = 'stale'

#: One native (F3D) log line, and the merged summary of a repeated one.
EVENT_NATIVE_LOG = 'native_log'
EVENT_NATIVE_LOG_MERGED = 'native_log_merged'

# Business sub-steps. The names are dotted so a reader can group them.
EVENT_IMPORT_DEPENDENCIES_DETECTED = 'import.dependencies_detected'
EVENT_IMPORT_COPIED = 'import.copied'
EVENT_IMPORT_VALIDATED = 'import.validated'
EVENT_IMPORT_COMMITTED = 'import.committed'
EVENT_APPEARANCE_SNAPSHOT = 'appearance.snapshot'
EVENT_APPEARANCE_DERIVED_MAPS = 'appearance.derived_maps'
EVENT_EXPORT_MANIFEST_FIXED = 'export.manifest_fixed'
EVENT_EXPORT_WRITTEN = 'export.written'
EVENT_EXPORT_ITEM_FAILED = 'export.item_failed'
EVENT_EXPORT_VALIDATED = 'export.validated'
EVENT_EXPORT_PUBLISHED = 'export.published'
EVENT_PROJECT_SAVE_SNAPSHOT = 'project.save_snapshot'
#: Committing the record and syncing the scene are two facts.  They used to
#: share one event, which is why "did my edit get saved?" had no answer.
EVENT_COMMIT_DURABLE = 'commit_durable'
EVENT_COMMIT_APPLY_FAILED = 'commit_apply_failed'
EVENT_PROJECT_MODEL_APPLIED = 'project.model_applied'
#: Thumbnail lifecycle: bytes on disk, then the card actually showing them.
EVENT_THUMBNAIL_REQUESTED = 'thumbnail.requested'
EVENT_THUMBNAIL_REQUEST_SKIPPED = 'thumbnail.request_skipped'
EVENT_THUMBNAIL_CALCULATED = 'thumbnail.planned'
EVENT_THUMBNAIL_CACHE_WRITTEN = 'thumbnail.cache_written'
EVENT_UI_IMAGE_ACCEPTED = 'ui.image_accepted'
EVENT_PROJECT_SAVE_CONFIRMED = 'project.save_confirmed'

#: The task lifecycle. Every one of these names the job it belongs to.
LIFECYCLE_EVENTS = frozenset({
    EVENT_QUEUED, EVENT_STARTED, EVENT_SOURCE_PREPARED, EVENT_NATIVE_LOADED,
    EVENT_RENDERED, EVENT_OUTPUT_WRITTEN, EVENT_INSTALLED, EVENT_COMPLETED,
    EVENT_FAILED, EVENT_CANCELLED, EVENT_STALE,
})

#: The explicit final states. A job that never reaches one of them is a bug
#: in the caller, and the log has to make that visible.
TERMINAL_EVENTS = frozenset({
    EVENT_INSTALLED, EVENT_COMPLETED, EVENT_FAILED, EVENT_CANCELLED, EVENT_STALE,
})

#: The business sub-steps, grouped by the owner that has to add the calls.
IMPORT_EVENTS = frozenset({
    EVENT_IMPORT_DEPENDENCIES_DETECTED, EVENT_IMPORT_COPIED,
    EVENT_IMPORT_VALIDATED, EVENT_IMPORT_COMMITTED,
})
APPEARANCE_EVENTS = frozenset({
    EVENT_APPEARANCE_SNAPSHOT, EVENT_APPEARANCE_DERIVED_MAPS,
})
EXPORT_EVENTS = frozenset({
    EVENT_EXPORT_MANIFEST_FIXED, EVENT_EXPORT_WRITTEN,
    EVENT_EXPORT_VALIDATED, EVENT_EXPORT_PUBLISHED,
})
PROJECT_EVENTS = frozenset({
    EVENT_PROJECT_SAVE_SNAPSHOT, EVENT_PROJECT_SAVE_CONFIRMED,
})

#: The identity every task-scoped event carries. Two events belong to the
#: same task only when all of these agree.
LEGACY_IDENTITY_FIELDS = (
    'project_session', 'open_epoch', 'model_id', 'request_id', 'job_id',
    'source_revision', 'appearance_revision', 'camera_revision', 'backend_version',
)

# Optional on existing model events. Material events declare their required
# scope separately: a whole-model restore has no single binding/material ID.
MATERIAL_IDENTITY_FIELDS = (
    'material_id', 'binding_id', 'catalog_revision', 'state_revision',
    'draft_generation', 'gesture_id',
)
IDENTITY_FIELDS = LEGACY_IDENTITY_FIELDS + MATERIAL_IDENTITY_FIELDS
MATERIAL_EVENT_SCHEMA = 'model_material_event/1'
MATERIAL_EVENTS = frozenset(
    'material.' + phase + '.' + state
    for phase, states in (
        ('parse', ('queued', 'running', 'completed', 'failed')),
        ('draft', ('created', 'updated')),
        ('commit', ('queued', 'completed', 'failed', 'cancelled', 'stale')),
        ('install', ('completed', 'failed', 'stale')),
    )
    for state in states
)

#: What each lifecycle event must carry. A missing field is *reported*, never
#: raised: an old call site must keep working.
REQUIRED_FIELDS = {
    EVENT_QUEUED: ('job_id',),
    EVENT_STARTED: ('job_id',),
    EVENT_SOURCE_PREPARED: ('job_id', 'source_revision'),
    EVENT_NATIVE_LOADED: ('job_id', 'backend_version'),
    EVENT_RENDERED: ('job_id',),
    EVENT_OUTPUT_WRITTEN: ('job_id',),
    EVENT_INSTALLED: ('job_id',),
    EVENT_COMPLETED: ('job_id',),
    EVENT_FAILED: ('job_id', 'error_code'),
    EVENT_CANCELLED: ('job_id',),
    EVENT_STALE: ('job_id',),
    # One model or a batch: ``model_count`` is the field both paths can supply,
    # so a batch edit is never reported as if it touched a single model.
    EVENT_COMMIT_DURABLE: ('model_count', 'state'),
    EVENT_COMMIT_APPLY_FAILED: ('model_count', 'durable', 'error_code'),
    EVENT_PROJECT_MODEL_APPLIED: ('model_count', 'durable', 'state'),
    EVENT_THUMBNAIL_CACHE_WRITTEN: ('model_id', 'subject', 'cache_key'),
    EVENT_UI_IMAGE_ACCEPTED: ('job_id', 'subject', 'state'),
    'material.preview_presented': ('frontmost', 'headless'),
    'material.lod_selected': ('level', 'bindings'),
    'model_tx.committed': ('path', 'state'),
    'model_tx.rollback': ('path', 'error_code'),
    'appearance.snapshot': ('model_id', 'revision'),
    'material.first_frame': ('bindings', 'shown'),
    'material.native.failed': ('job_id', 'action', 'error_type'),
    'material.native.timing': ('job_id','action','timing','elapsed_ms'),
    'material.native.resources': ('actors','decoded_texture_bytes','gpu_bytes'),
    'material.native.environment': ('state',),
    'material_thumbnail_stored': ('material', 'real'),
    'model_project_bound': ('project_session', 'state'),
    'material_settings_edited': ('material', 'changed'),
    'material_picker.applied': ('selected', 'entries'),
    'model_cover_render_started': ('job_id', 'mode'),
    'material_preview.event': ('event_name', 'state'),
    'material_preview.picked': ('binding_id', 'state'),
    'material.edit.highlight': ('model_id', 'binding_count'),
    'model_tx.begin': ('model_id', 'expected_revision'),
    'model_tx.conflict': ('model_id', 'expected_revision', 'stored_revision'),
    'model_items_trashed': ('count', 'trash_stamp'),
    'model_items_restored': ('count',),
    'model_leases_released': ('count',),
    'app_closing': ('state',),
    'material.edit.patch': ('request_id', 'operation', 'material_count'),
    'material.edit.replace_surface': ('request_id', 'snapshot'),
    'material.edit.replace_full': ('request_id', 'snapshot'),
    'material.edit.paste': ('request_id', 'snapshot'),
    'material.edit.make_unique': ('request_id', 'clone_count'),
    'material.edit.restore_material': ('request_id', 'material_count'),
    'material.edit.restore_model': ('request_id', 'model_id'),
    EVENT_THUMBNAIL_REQUESTED: ('model_id', 'subject', 'via'),
    EVENT_THUMBNAIL_REQUEST_SKIPPED: ('subject', 'reason'),
    EVENT_THUMBNAIL_CALCULATED: ('subject', 'model_count', 'requested'),
    EVENT_IMPORT_DEPENDENCIES_DETECTED: ('job_id', 'dependency_count'),
    EVENT_IMPORT_COPIED: ('job_id', 'file_count'),
    EVENT_IMPORT_VALIDATED: ('job_id', 'file_count', 'digest_verified'),
    EVENT_IMPORT_COMMITTED: ('job_id', 'model_id', 'file_count'),
    EVENT_EXPORT_MANIFEST_FIXED: ('job_id', 'model_count'),
    EVENT_EXPORT_ITEM_FAILED: ('job_id', 'model_id', 'error_code'),
}

_MATERIAL_SCOPE = ('project_session', 'open_epoch', 'model_id',
                   'request_id', 'source_revision')
for _event in MATERIAL_EVENTS:
    _required = _MATERIAL_SCOPE
    if not _event.startswith('material.parse.'):
        _required += ('catalog_revision', 'state_revision')
    if _event.startswith(('material.draft.', 'material.install.')):
        _required += ('draft_generation',)
    if _event.startswith('material.commit.'):
        _required += ('gesture_id',)
    if _event.endswith('.failed'):
        _required += ('error_code',)
    REQUIRED_FIELDS[_event] = _required
del _event, _required

# ── privacy ───────────────────────────────────────────────────────────────
#: Field names whose value must never be written: model bytes, texture
#: Base64, note bodies, credentials. Matches the bundle redactor's list.
_FORBIDDEN_KEYS = frozenset({
    'notes', 'note', 'text', 'body', 'content', 'html', 'document', 'prompt',
    'token', 'data', 'payload', 'raw', 'bytes', 'base64', 'image', 'thumbnail',
    'thumbnail_data_url', 'data_url', 'blob', 'buffer', 'pixel_data',
    'password', 'secret', 'authorization', 'cookie', 'clipboard', 'api_key',
    'access_token', 'credential', 'credentials', 'user_note', 'comment',
})

#: Mirrors ``prism.diagnostics.PATHS`` so both sides mask the same strings.
_PATH_PATTERN = re.compile(
    r'''(?:[A-Za-z]:[\\/]|\\\\)[^\r\n\t"'<>|]*|/(?:home|Users)/[^\s"'<>]+''')

#: A long base64-looking run is a payload, not a message.
_PAYLOAD_PATTERN = re.compile(r'^[A-Za-z0-9+/=]{512,}$')

MAX_STRING = 2000
MAX_SEQUENCE = 24
MAX_DICT = 24

# ── native log merging ────────────────────────────────────────────────────
#: Repeats of the same native line inside this window are merged.
NATIVE_LOG_WINDOW_S = 1.0
#: A group reports itself once this many further occurrences piled up, so an
#: error storm cannot hide the evidence until the window ends. The report is
#: cumulative: the count never restarts.
NATIVE_LOG_FLUSH_COUNT = 512
#: How many distinct native lines may wait for their window.
NATIVE_LOG_MAX_GROUPS = 128

_STATE_LOCK = threading.RLock()
_STATE = {
    'recorded': 0, 'schema_notes': 0, 'native_groups': 0,
    'native_merged': 0, 'native_suppressed': 0, 'failures': 0,
}
_PENDING_NATIVE = OrderedDict()

_IDENTITY = contextvars.ContextVar('prism_model_identity', default=None)


# ── helpers ───────────────────────────────────────────────────────────────
def _mask_path(raw):
    """The same token the diagnostic bundle uses for one path."""
    suffix = ntpath.splitext(raw)[1]
    if not re.fullmatch(r'\.[A-Za-z0-9]{1,12}', suffix):
        suffix = ''
    return '<path:' + hashlib.sha256(raw.encode('utf-8', 'replace')).hexdigest()[:12] + suffix + '>'


def _mask_paths(text):
    return _PATH_PATTERN.sub(lambda match: _mask_path(match.group(0)), text)


def _sanitize(name, value):
    """Reduce one field to something safe to write.

    The rule is blunt on purpose: a field whose *name* suggests private
    content is replaced by a marker, never by a shortened copy of the value.
    """
    key = str(name).lower()
    if key == 'bytes' and type(value) is int and value >= 0:
        return value  # A byte count is telemetry; original bytes remain forbidden.
    if key in _FORBIDDEN_KEYS:
        return '<omitted:%s>' % key
    if isinstance(value, (bytes, bytearray, memoryview)):
        return '<%d bytes>' % len(value)
    if isinstance(value, str):
        if value.startswith('data:'):
            return '<omitted:data_url>'
        if _PAYLOAD_PATTERN.match(value):
            return '<omitted:payload>'
        masked = _mask_paths(value)
        return masked if len(masked) <= MAX_STRING else masked[:MAX_STRING] + '…[cut]'
    if isinstance(value, dict):
        return {str(key): _sanitize(key, item)
                for key, item in list(value.items())[:MAX_DICT]}
    if isinstance(value, (list, tuple, set, frozenset)):
        return [_sanitize(name, item) for item in list(value)[:MAX_SEQUENCE]]
    if isinstance(value, (int, float, bool, type(None))):
        return value
    return _sanitize(name, str(value))


def _priority_for_event(name):
    """Terminal evidence must survive a full queue."""
    from prism.performance_trace import (
        PRIORITY_ERROR, PRIORITY_SAMPLE, PRIORITY_STATE, PRIORITY_TERMINAL,
    )
    if name == EVENT_FAILED or (name in MATERIAL_EVENTS and name.endswith('.failed')):
        return PRIORITY_ERROR
    if name in TERMINAL_EVENTS or (name in MATERIAL_EVENTS and
            name.endswith(('.completed', '.cancelled', '.stale'))):
        return PRIORITY_TERMINAL
    if name in (EVENT_QUEUED, EVENT_NATIVE_LOG_MERGED):
        return PRIORITY_STATE
    return PRIORITY_SAMPLE


def _note_failure(where, error=None):
    """Count a log failure without asking the log to report its own failure."""
    try:
        from prism.performance_trace import _note_diagnostic_failure
        _note_diagnostic_failure(where, error)
    except Exception:
        pass


def _prepare(name, fields):
    """Build the payload: sanitized, identity-filled, schema-checked."""
    payload = {}
    for key, value in fields.items():
        payload[str(key)] = _sanitize(key, value)
    context = _IDENTITY.get() or {}
    for key in IDENTITY_FIELDS:
        if payload.get(key) is None and context.get(key) is not None:
            payload[key] = _sanitize(key, context[key])
    required = REQUIRED_FIELDS.get(name, ())
    missing_required = [key for key in required if payload.get(key) is None]
    if name in LIFECYCLE_EVENTS or name in MATERIAL_EVENTS:
        applicable_identity = required if name in MATERIAL_EVENTS else LEGACY_IDENTITY_FIELDS
        missing_identity = [key for key in applicable_identity if payload.get(key) is None]
        if missing_identity:
            payload['schema_missing_identity'] = ','.join(missing_identity)
    if missing_required:
        payload['schema_missing_required'] = ','.join(missing_required)
        payload['schema_ok'] = False
    payload['event_schema'] = EVENT_SCHEMA
    if name in MATERIAL_EVENTS:
        payload['material_event_schema'] = MATERIAL_EVENT_SCHEMA
    return payload


# ── public API ────────────────────────────────────────────────────────────
def record_model_event(event: str, **fields):
    """Append one model event to the existing pipeline.

    The signature is the historic one, so every existing call site keeps
    working. A diagnostic failure is counted, never raised.
    """
    try:
        name = str(event)
        payload = _prepare(name, fields)
        # Worker scheduling priority is data, not the trace pipeline's priority.
        # Native started events carry it; forwarding it twice used to drop the
        # entire event with "multiple values for keyword argument 'priority'".
        if 'priority' in payload:
            payload['job_priority'] = payload.pop('priority')
        from prism.performance_trace import record
        record(MODEL_EVENT_PREFIX + name, priority=_priority_for_event(name), **payload)
        with _STATE_LOCK:
            _STATE['recorded'] += 1
            if payload.get('schema_missing_required'):
                _STATE['schema_notes'] += 1
    except Exception as error:
        with _STATE_LOCK:
            _STATE['failures'] += 1
        _note_failure('model_event', error)


@contextlib.contextmanager
def model_identity(**identity):
    """Fill the identity fields for every event recorded inside the block.

    The block only *defaults* fields: a field the caller passes explicitly
    always wins, so an event can never claim another job's identity.
    """
    current = dict(_IDENTITY.get() or {})
    current.update({key: value for key, value in identity.items() if value is not None})
    token = _IDENTITY.set(current)
    try:
        yield current
    finally:
        _IDENTITY.reset(token)


def _native_key(level, message, fields):
    parts = [level, message]
    parts.extend('%s=%s' % (key, fields[key]) for key in sorted(fields))
    return hashlib.sha256('\u0000'.join(parts).encode('utf-8', 'replace')).hexdigest()[:16]


def _emit_native_merged(group):
    """Report the occurrences of one group that were not reported yet.

    The report is cumulative: ``occurrences_total`` is everything the group
    has seen, ``occurrences_merged`` is everything since the last report.
    Adding up the merged events and the first individual event gives the
    real number of occurrences.
    """
    pending = group['total'] - group['reported']
    if pending <= 0:
        return False
    record_model_event(
        EVENT_NATIVE_LOG_MERGED,
        level=group['level'], message=group['message'],
        occurrences_total=group['total'], occurrences_merged=pending,
        first_mono=round(group['first_mono'], 6), last_mono=round(group['last_mono'], 6),
        window_s=NATIVE_LOG_WINDOW_S, native_key=group['key'], **group['fields'])
    group['reported'] = group['total']
    return True


def forward_native_log(level, message, **fields):
    """Forward one native (F3D) log line. Safe to call from a native callback.

    * never touches Qt, never writes to disk, never blocks on I/O
    * identical lines inside the merge window become one merged event that
      keeps the number of occurrences and the first/last monotonic time

    The first occurrence is written immediately, so a lone error is visible
    without waiting for the window. The merged event repeats the whole
    window: ``occurrences_total`` counts every occurrence including that
    first one, ``occurrences_merged`` counts the ones that were folded in.
    """
    now = time.monotonic()
    merged = []
    try:
        level = str(level or 'info').lower()
        text = str(message)[:MAX_STRING]
        key = _native_key(level, text, fields)
        with _STATE_LOCK:
            group = _PENDING_NATIVE.get(key)
            if group is not None and (now - group['last_mono']) <= NATIVE_LOG_WINDOW_S:
                group['total'] += 1
                group['last_mono'] = now
                _STATE['native_suppressed'] += 1
                if (group['total'] - group['reported']) >= NATIVE_LOG_FLUSH_COUNT:
                    merged.append(group)
                first = False
            else:
                if group is not None:
                    merged.append(group)
                    _PENDING_NATIVE.pop(key, None)
                _PENDING_NATIVE[key] = dict(
                    key=key, level=level, message=text, fields=dict(fields),
                    total=1, reported=1, first_mono=now, last_mono=now)
                _STATE['native_groups'] += 1
                while len(_PENDING_NATIVE) > NATIVE_LOG_MAX_GROUPS:
                    _, stale = _PENDING_NATIVE.popitem(last=False)
                    merged.append(stale)
                first = True
        for group in merged:
            if _emit_native_merged(group):
                with _STATE_LOCK:
                    _STATE['native_merged'] += 1
        if first:
            record_model_event(EVENT_NATIVE_LOG, level=level, message=text,
                               native_key=key, **fields)
    except Exception as error:
        with _STATE_LOCK:
            _STATE['failures'] += 1
        _note_failure('forward_native_log', error)


def flush_model_events():
    """Emit the pending merged native-log groups.

    Call it when the session goes idle or before it closes, so the last
    window is not lost. It is safe to call at any time.
    """
    with _STATE_LOCK:
        groups = list(_PENDING_NATIVE.values())
        _PENDING_NATIVE.clear()
    emitted = 0
    for group in groups:
        if _emit_native_merged(group):
            with _STATE_LOCK:
                _STATE['native_merged'] += 1
            emitted += 1
    return {'flushed_groups': len(groups), 'merged_events': emitted}


def model_event_state():
    """What this module has done. Bounded, and it says what was not kept."""
    with _STATE_LOCK:
        state = dict(_STATE)
        state['pending_native_groups'] = len(_PENDING_NATIVE)
    state['schema'] = EVENT_SCHEMA
    state['note'] = ('模型事件计数；被合并的重复原生日志保留次数与首末单调时间，'
                     '未合并的部分不出现在这里。')
    return state


def model_log_completeness():
    """What the log actually holds, including what it lost.

    A bounded queue can drop. When it does, the loss is counted and reported
    here; the log never claims to be complete.
    """
    from prism.performance_trace import diagnostic_failures, trace_stats
    stats = trace_stats()
    failures = diagnostic_failures()
    complete = bool(stats.get('active')) and not stats.get('trace_incomplete') \
        and not failures.get('total')
    return {
        'trace': stats,
        'diagnostic_failures': failures,
        'model_events': model_event_state(),
        'complete': complete,
        'basis': 'bounded queues can drop events; drops and writer errors are counted, never hidden',
    }


def model_event_contract():
    """The machine-readable "which events, which fields" table.

    The storage, native and UI owners add the calls; this is the contract
    those calls have to satisfy.
    """
    return {
        'schema': EVENT_SCHEMA,
        'prefix': MODEL_EVENT_PREFIX,
        'entry_point': 'prism.model_library.events.record_model_event(event, **fields)',
        'identity_fields': list(IDENTITY_FIELDS),
        'legacy_identity_fields': list(LEGACY_IDENTITY_FIELDS),
        'material_identity_fields': list(MATERIAL_IDENTITY_FIELDS),
        'material_event_schema': MATERIAL_EVENT_SCHEMA,
        'material_events': sorted(MATERIAL_EVENTS),
        'required_fields': {name: list(fields) for name, fields in REQUIRED_FIELDS.items()},
        'lifecycle_events': sorted(LIFECYCLE_EVENTS),
        'terminal_events': sorted(TERMINAL_EVENTS),
        'import_events': sorted(IMPORT_EVENTS),
        'appearance_events': sorted(APPEARANCE_EVENTS),
        'export_events': sorted(EXPORT_EVENTS),
        'project_events': sorted(PROJECT_EVENTS),
        'native_log': {
            'entry_point': 'prism.model_library.events.forward_native_log(level, message, **fields)',
            'window_s': NATIVE_LOG_WINDOW_S,
            'flush_count': NATIVE_LOG_FLUSH_COUNT,
            'merged_event': EVENT_NATIVE_LOG_MERGED,
            'rule': ('相同的原生日志行在窗口内合并为一条事件，保留次数与首末单调时间；'
                     '调用方不得在回调里改 UI 或直接写盘。'),
        },
        'forbidden_fields': sorted(_FORBIDDEN_KEYS),
        'timing_rule': ('耗时只用同一进程内的单调时钟；端到端耗时由主进程测量，'
                        '不得用两个进程未对齐的时间戳相减。'),
    }


__all__ = [
    'LEGACY_IDENTITY_FIELDS', 'MATERIAL_IDENTITY_FIELDS',
    'MATERIAL_EVENT_SCHEMA', 'MATERIAL_EVENTS',
    'MODEL_EVENT_PREFIX', 'EVENT_SCHEMA',
    'EVENT_QUEUED', 'EVENT_STARTED', 'EVENT_SOURCE_PREPARED',
    'EVENT_NATIVE_LOADED', 'EVENT_RENDERED', 'EVENT_OUTPUT_WRITTEN',
    'EVENT_INSTALLED', 'EVENT_COMPLETED', 'EVENT_FAILED', 'EVENT_CANCELLED',
    'EVENT_STALE', 'EVENT_NATIVE_LOG', 'EVENT_NATIVE_LOG_MERGED',
    'EVENT_IMPORT_DEPENDENCIES_DETECTED', 'EVENT_IMPORT_COPIED',
    'EVENT_IMPORT_VALIDATED', 'EVENT_IMPORT_COMMITTED',
    'EVENT_APPEARANCE_SNAPSHOT', 'EVENT_APPEARANCE_DERIVED_MAPS',
    'EVENT_EXPORT_MANIFEST_FIXED', 'EVENT_EXPORT_WRITTEN',
    'EVENT_EXPORT_VALIDATED', 'EVENT_EXPORT_PUBLISHED',
    'EVENT_PROJECT_SAVE_SNAPSHOT', 'EVENT_PROJECT_SAVE_CONFIRMED',
    'LIFECYCLE_EVENTS', 'TERMINAL_EVENTS', 'IDENTITY_FIELDS', 'REQUIRED_FIELDS',
    'record_model_event', 'model_identity', 'forward_native_log',
    'flush_model_events', 'model_event_state', 'model_log_completeness',
    'model_event_contract',
]
