"""Read-only evidence collection for the Prism scale-acceptance baseline.

Everything in this module is a *reader*. It never writes to a project file,
never mutates Prism's own sources and never touches another group's
artefacts. The only writes happen in the acceptance runner, and only under
the acceptance output directory.

Three responsibilities live here:

* **forensics** -- streaming digests, size/mtime stamps and directory
  listings, so a run can prove it did not rewrite the project it opened;
* **environment** -- what the machine looked like while the numbers were
  produced (CPU count, RAM, and the other Python processes that were running
  at the same time and therefore competing for the same cores);
* **trace evidence** -- the operation log that ``prism.performance_trace``
  writes, reduced to the SQL read counters that prove whether an open was
  on-demand or a full load.
"""
from __future__ import annotations

import ctypes
import hashlib
import importlib.util
import json
import os
import platform
import statistics
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]

#: SQL read labels emitted by ``prism/fileio/sql.py::_read_rows``.
SQL_LABELS = ('image_metadata', 'native_item_metadata', 'embedded_glb',
              'original_sizes', 'workspace_headers')


# ── the prior harness ──────────────────────────────────────────────────────

def load_prior_harness():
    """Load ``tools/scale_acceptance.py`` by path (tools/ is not a package).

    The prior harness owns the canonical ``stats()`` shape and the canonical
    fixture builder. Reusing them by import keeps one definition of both,
    instead of a second copy that can drift.
    """
    path = ROOT / 'tools' / 'scale_acceptance.py'
    spec = importlib.util.spec_from_file_location('prism_prior_scale_acceptance', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def stats(values):
    """The shape the existing scale-audit reports use.

    Identical to ``tools/scale_acceptance.stats``; asserted equal by
    ``tests/test_scale_acceptance_harness.py`` so the two can never drift.
    """
    values = sorted(values)
    if not values:
        return {}
    return dict(count=len(values),
                median_ms=round(statistics.median(values), 2),
                p95_ms=round(values[min(len(values) - 1, int(len(values) * .95))], 2),
                max_ms=round(max(values), 2))


# ── forensics ──────────────────────────────────────────────────────────────

def sha256_file(path, chunk=4 * 1024 * 1024):
    """Streaming digest; a 51.8 GiB file must not be read into memory."""
    digest = hashlib.sha256()
    with open(path, 'rb') as handle:
        for block in iter(lambda: handle.read(chunk), b''):
            digest.update(block)
    return digest.hexdigest()


def file_fingerprint(path, *, digest=True):
    """Size + mtime (+ digest) of one file, read without modifying it."""
    path = Path(path)
    stat = path.stat()
    record = dict(path=str(path),
                  size_bytes=stat.st_size,
                  mtime_ns=stat.st_mtime_ns,
                  mtime=time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(stat.st_mtime)))
    if digest:
        record['sha256'] = sha256_file(path)
    return record


def directory_listing(path):
    """One directory's entries, sorted; used to spot files created by a run."""
    path = Path(path)
    entries = []
    if not path.is_dir():
        return entries
    for child in sorted(path.iterdir(), key=lambda item: item.name):
        try:
            stat = child.stat()
        except OSError:
            continue
        entries.append(dict(name=child.name,
                            is_dir=child.is_dir(),
                            size_bytes=stat.st_size if not child.is_dir() else None,
                            mtime_ns=stat.st_mtime_ns))
    return entries


def diff_listings(before, after):
    """Names added / removed between two :func:`directory_listing` results."""
    before_names = {entry['name'] for entry in before}
    after_names = {entry['name'] for entry in after}
    return dict(added=sorted(after_names - before_names),
                removed=sorted(before_names - after_names),
                added_detail=[entry for entry in after if entry['name'] in (after_names - before_names)],
                removed_detail=[entry for entry in before if entry['name'] in (before_names - after_names)])


# ── environment ────────────────────────────────────────────────────────────

class _MemoryStatusEx(ctypes.Structure):
    _fields_ = [('dwLength', ctypes.c_ulong),
                ('dwMemoryLoad', ctypes.c_ulong),
                ('ullTotalPhys', ctypes.c_ulonglong),
                ('ullAvailPhys', ctypes.c_ulonglong),
                ('ullTotalPageFile', ctypes.c_ulonglong),
                ('ullAvailPageFile', ctypes.c_ulonglong),
                ('ullTotalVirtual', ctypes.c_ulonglong),
                ('ullAvailVirtual', ctypes.c_ulonglong),
                ('ullAvailExtendedVirtual', ctypes.c_ulonglong)]


def system_memory():
    """Total/available RAM, read from the kernel (psutil is not installed)."""
    status = _MemoryStatusEx()
    status.dwLength = ctypes.sizeof(status)
    ok = ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status))
    if not ok:
        return dict(total_bytes=None, available_bytes=None, load_percent=None)
    return dict(total_bytes=int(status.ullTotalPhys),
                available_bytes=int(status.ullAvailPhys),
                load_percent=int(status.dwMemoryLoad))


def python_processes():
    """The other Python processes running right now.

    The acceptance numbers are produced on a machine that other acceptance
    groups are using at the same time. Recording the competitors is the
    only way to keep the numbers honest: a run that shared 32 cores with
    three other workers is not a quiet-machine measurement.
    """
    script = ("Get-Process python -ErrorAction SilentlyContinue | "
              "ForEach-Object { [PSCustomObject]@{ Id=$_.Id; CPU=$_.CPU; "
              "WorkingSet64=$_.WorkingSet64; StartTime=$_.ToString('o') } } | "
              "ConvertTo-Json -Compress")
    try:
        completed = subprocess.run(
            ['powershell', '-NoProfile', '-NonInteractive', '-Command', script],
            capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.SubprocessError) as error:
        return dict(available=False, error=str(error), processes=[])
    text = (completed.stdout or '').strip()
    if not text:
        return dict(available=True, count=0, processes=[], raw='')
    try:
        payload = json.loads(text)
    except ValueError:
        return dict(available=True, count=None, processes=[], raw=text[:2000])
    rows = payload if isinstance(payload, list) else [payload]
    return dict(available=True, count=len(rows), processes=rows, raw=text[:2000])


def capture_environment():
    """Everything about the machine that a reader of the numbers needs."""
    try:
        from PyQt6 import QtCore
        qt_version = QtCore.QT_VERSION_STR
        pyqt_version = QtCore.PYQT_VERSION_STR
    except Exception:  # pragma: no cover - the GUI is optional for forensics
        qt_version = None
        pyqt_version = None
    memory = system_memory()
    return dict(
        python=sys.version,
        python_version=platform.python_version(),
        implementation=platform.python_implementation(),
        qt_version=qt_version,
        pyqt_version=pyqt_version,
        platform=platform.platform(),
        machine=platform.machine(),
        logical_cpus=os.cpu_count(),
        memory=memory,
        settings_dir=None,
        note='CPU contention from concurrent workers is recorded separately '
             'in environment.python_processes_at_start/end.',
    )


# ── trace evidence ─────────────────────────────────────────────────────────

def read_trace_events(trace_dir):
    """Every event the operation log recorded for one run."""
    trace_dir = Path(trace_dir)
    events = []
    if not trace_dir.is_dir():
        return events
    for path in sorted(trace_dir.glob('Prism-operations-*.jsonl*')):
        try:
            with open(path, 'r', encoding='utf-8', errors='replace') as handle:
                for line in handle:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        events.append(json.loads(line))
                    except ValueError:
                        continue
        except OSError:
            continue
    events.sort(key=lambda event: event.get('sequence', 0))
    return events


def sql_read_rows(events):
    """Rows fetched per SQL label, from ``project.sql.fetch`` operations.

    This is the hard evidence for "did the open read everything?". A scoped
    (on-demand) open fetches one page of image metadata; a full load fetches
    the whole table.
    """
    buckets = {}
    for event in events:
        if event.get('event') != 'operation':
            continue
        if event.get('name') != 'project.sql.fetch':
            continue
        label = event.get('query')
        rows = event.get('rows')
        if label is None or rows is None:
            continue
        bucket = buckets.setdefault(label, dict(calls=0, total_rows=0, max_rows=0, samples=[]))
        bucket['calls'] += 1
        bucket['total_rows'] += int(rows)
        bucket['max_rows'] = max(bucket['max_rows'], int(rows))
        bucket['samples'].append(int(rows))
    for bucket in buckets.values():
        bucket['samples'] = bucket['samples'][:200]
    return buckets


def sql_execute_rows(events):
    """``project.sql.execute`` operations, without the row transfer."""
    buckets = {}
    for event in events:
        if event.get('event') != 'operation' or event.get('name') != 'project.sql.execute':
            continue
        label = event.get('query')
        if label is None:
            continue
        buckets[label] = buckets.get(label, 0) + 1
    return buckets


def trace_stages(events, name):
    """All ``operation`` events with a given name, in order."""
    return [event for event in events
            if event.get('event') == 'operation' and event.get('name') == name]


def trace_events_named(events, event_name):
    return [event for event in events if event.get('event') == event_name]


# ── count-point probes ─────────────────────────────────────────────────────

def source_contains(path, needles):
    """Which of ``needles`` appear in a source file (read-only)."""
    try:
        text = Path(path).read_text(encoding='utf-8', errors='replace')
    except OSError:
        return dict(readable=False, found=[])
    return dict(readable=True, found=[needle for needle in needles if needle in text])


__all__ = ['ROOT', 'SQL_LABELS', 'capture_environment', 'diff_listings',
           'directory_listing', 'file_fingerprint', 'load_prior_harness',
           'python_processes', 'read_trace_events', 'sha256_file',
           'source_contains', 'sql_execute_rows', 'sql_read_rows',
           'stats', 'system_memory', 'trace_events_named', 'trace_stages']
