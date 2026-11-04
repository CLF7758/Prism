"""Read-only diagnostic summary for the model library.

The snapshot answers "where did this task stop?" **without** reading a model
byte, a texture pixel or a note body. Everything in it is metadata that
already exists in memory:

* the version of the app, the trace pipeline and the native backend
* the capabilities the composition root actually granted
* the queue's own accounting (wait / service / installed / cancelled ...)
* the identity of the job that is in flight
* the resource budget of the rebuildable caches
* the error counters, including the ones that describe log loss

It is deliberately bounded: every list is capped, every string is short, and
a value that is not known is reported as unknown rather than as zero.
"""
from __future__ import annotations

import dataclasses
import hashlib
import ntpath
import os
import platform
import re
import sys
import time
from datetime import datetime, timezone

SNAPSHOT_SCHEMA = 'model_diagnostic/1'

#: Cap on the number of entries in every list in the snapshot.
MAX_LIST_ITEMS = 24
#: Cap on the length of a single free-text value.
MAX_TEXT = 400

#: The identity fields a snapshot may echo back.
IDENTITY_FIELDS = (
    'project_session', 'open_epoch', 'model_id', 'request_id', 'job_id',
    'source_revision', 'appearance_revision', 'camera_revision', 'backend_version',
)

#: Mirrors ``prism.diagnostics.PATHS``: a path in the snapshot is masked the
#: same way it is masked in the bundle, so both sides agree on what a path is.
_PATH_PATTERN = re.compile(
    r'''(?:[A-Za-z]:[\\/]|\\\\)[^\r\n\t"'<>|]*|/(?:home|Users)/[^\s"'<>]+''')


def _mask_path(raw):
    suffix = ntpath.splitext(raw)[1]
    if not re.fullmatch(r'\.[A-Za-z0-9]{1,12}', suffix):
        suffix = ''
    return '<path:' + hashlib.sha256(raw.encode('utf-8', 'replace')).hexdigest()[:12] + suffix + '>'


def _mask_paths(text):
    return _PATH_PATTERN.sub(lambda match: _mask_path(match.group(0)), text)


def _utc_now():
    return datetime.now(timezone.utc).isoformat()


def _text(value, limit=MAX_TEXT):
    if value is None:
        return None
    text = _mask_paths(str(value))
    return text if len(text) <= limit else text[:limit] + '…[cut]'


def _trace_module():
    """The trace module *if it is already loaded*.

    The snapshot never imports the trace pipeline: importing it would pull in
    Qt into a process that may only want to read metadata.
    """
    return sys.modules.get('prism.performance_trace')


def _capabilities(service=None, backend=None):
    """What the composition root granted, not what the code could do."""
    result = {}
    for label, holder in (('service', service), ('backend', backend)):
        if holder is None:
            result[label] = {'state': 'not_supplied'}
            continue
        capabilities = getattr(holder, 'capabilities', None)
        if capabilities is None:
            result[label] = {'state': 'unavailable', 'reason': 'no_capabilities_attribute'}
            continue
        if dataclasses.is_dataclass(capabilities):
            result[label] = dataclasses.asdict(capabilities)
        elif isinstance(capabilities, dict):
            result[label] = dict(capabilities)
        else:
            result[label] = {'state': 'unknown', 'type': type(capabilities).__name__}
    return result


def _queue(queue=None):
    """The queue's own accounting. Never a re-implementation of it."""
    if queue is None:
        return {'state': 'not_supplied',
                'reason': 'no queue accounting was handed to the snapshot'}
    if isinstance(queue, dict):
        # A caller may hand over already-measured numbers (the export path
        # reads them from the service). Accepting that is not the same as
        # inventing them: the payload carries its own `basis`.
        data = dict(queue)
        data.setdefault('state', 'measured')
        return data
    snapshot = getattr(queue, 'snapshot', None)
    if not callable(snapshot):
        return {'state': 'unavailable', 'reason': 'object_has_no_snapshot'}
    try:
        data = snapshot()
    except Exception as error:
        return {'state': 'unknown', 'reason': type(error).__name__}
    if not isinstance(data, dict):
        return {'state': 'unknown', 'reason': 'snapshot_not_a_mapping'}
    return data


def _resources(resources=None):
    """The rebuildable-cache budget, or an explicit "nobody measures this"."""
    if resources is not None:
        snapshot = getattr(resources, 'snapshot', None)
        if callable(snapshot):
            try:
                data = snapshot()
                return data if isinstance(data, dict) else {'state': 'unknown'}
            except Exception as error:
                return {'state': 'unknown', 'reason': type(error).__name__}
        if isinstance(resources, dict):
            return dict(resources)
        return {'state': 'unknown', 'type': type(resources).__name__}
    trace = _trace_module()
    if trace is None:
        return {'state': 'not_measured',
                'reason': 'trace pipeline not loaded in this process'}
    try:
        accounting = trace.resource_accounting('model_library_cache')
        data = accounting.snapshot()
        data['source'] = data.get('source') or 'measured'
        return data
    except Exception as error:
        return {'state': 'unknown', 'reason': type(error).__name__}


def _job(job=None):
    """The identity of the job in flight. Only the identity, nothing else."""
    if job is None:
        return {'state': 'idle'}
    if dataclasses.is_dataclass(job) and not isinstance(job, type):
        data = dataclasses.asdict(job)
    elif isinstance(job, dict):
        data = dict(job)
    else:
        data = {name: getattr(job, name) for name in IDENTITY_FIELDS
                if hasattr(job, name)}
    identity = {name: data.get(name) for name in IDENTITY_FIELDS if data.get(name) is not None}
    identity['state'] = _text(data.get('state'), 32) if data.get('state') else 'unknown'
    if data.get('error_code'):
        identity['error_code'] = _text(data.get('error_code'), 64)
    if data.get('error_message'):
        identity['error_message'] = _text(data.get('error_message'), MAX_TEXT)
    return identity


def _errors(errors=None):
    """The recent failures, capped. No note bodies, no raw payloads."""
    if errors is None:
        return {'state': 'not_supplied', 'count': 0}
    if isinstance(errors, dict):
        items = [{'code': key, 'count': value} for key, value in list(errors.items())[:MAX_LIST_ITEMS]]
        return {'state': 'available', 'count': len(errors), 'by_code': items}
    try:
        sequence = list(errors)
    except TypeError:
        return {'state': 'unknown', 'reason': 'not_iterable'}
    items = []
    for entry in sequence[-MAX_LIST_ITEMS:]:
        if isinstance(entry, dict):
            items.append({key: _text(value, MAX_TEXT)
                          for key, value in list(entry.items())[:12]})
        else:
            items.append({'message': _text(entry, MAX_TEXT)})
    return {'state': 'available', 'count': len(sequence), 'recent': items}


def model_diagnostic_snapshot(*, service=None, backend=None, queue=None,
                              resources=None, job=None, errors=None):
    """The bounded, read-only model-library diagnostic summary.

    Nothing in here reads a file. A missing value is reported as missing, so
    a reader can tell "not measured" from "measured as zero".
    """
    trace = _trace_module()
    version = 'unknown'
    try:
        from prism import constants
        version = getattr(constants, 'VERSION', 'unknown')
    except Exception:
        pass
    pipeline = {
        'trace_module_loaded': trace is not None,
        'schema_version': getattr(trace, 'SCHEMA_VERSION', None) if trace else None,
        'recording_contract_version': (getattr(trace, 'RECORDING_CONTRACT_VERSION', None)
                                       if trace else None),
        'mode': None,
        'trace_stats': None,
        'diagnostic_failures': None,
    }
    if trace is not None:
        try:
            pipeline['mode'] = trace.mode()
            pipeline['trace_stats'] = trace.trace_stats()
            pipeline['diagnostic_failures'] = trace.diagnostic_failures()
        except Exception as error:
            pipeline['error'] = type(error).__name__
    try:
        from prism.logging import rotation_failures
        pipeline['log_rotation'] = rotation_failures()
    except Exception as error:                       # noqa: BLE001 - recorded
        pipeline['log_rotation'] = {'failures': None,
                                    'reason': type(error).__name__}
    model_events = None
    try:
        from prism.model_library.events import model_event_state, model_log_completeness
        model_events = model_event_state()
        pipeline['model_log_completeness'] = model_log_completeness()
    except Exception as error:
        pipeline['model_log_error'] = type(error).__name__
    return {
        'schema': SNAPSHOT_SCHEMA,
        'collected_at': _utc_now(),
        'collected_monotonic': time.monotonic(),
        'pid': os.getpid(),
        'version': version,
        'python': platform.python_version(),
        'platform': platform.platform(),
        'basis': ('metadata_only; no model bytes, no texture pixels, no note bodies '
                  'and no file reads are performed by this snapshot'),
        'reads_model_bytes': False,
        'capabilities': _capabilities(service, backend),
        'queue': _queue(queue),
        'current_job': _job(job),
        'resources': _resources(resources),
        'errors': _errors(errors),
        'pipeline': pipeline,
        'model_events': model_events,
    }


__all__ = ['SNAPSHOT_SCHEMA', 'MAX_LIST_ITEMS', 'model_diagnostic_snapshot']
