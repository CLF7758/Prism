"""Cold-start catalog build measurement: the four questions, answered.

The catalog sidecar is a **cache**, not part of the project. It lives in
``%APPDATA%\\Prism\\catalog\\<project>.sqlite`` -- next to the thumbnail cache,
**not** next to the project file. When the source fingerprint changes, the
cache stops being trustworthy and has to be rebuilt, and that rebuild reads
the whole source project.

This module measures that cold path and answers four questions with
evidence instead of with a transferred number:

1. **How long does a from-zero build take, and where does the time go?**
   The per-batch wall clock is sampled through the production
   ``on_batch`` callback, and the three phases of the build (source read,
   JSON projection, cache write) are timed separately in an isolated replica
   of the same three steps.

2. **Does the build occupy the main thread?** The build runs in a worker
   thread while the main thread samples its own heartbeat, so the report can
   say how long the main thread was actually starved. The production path
   already runs the build off the GUI thread
   (``prism/paged_open_preparation.py`` -> ``QThreadPool``); this measures
   what that costs the GUI.

3. **Does cancelling actually stop it, and does the process stay usable?**
   A build is cancelled after N committed batches. The report records whether
   the build returned promptly, whether the cache is left in a resumable
   state, whether the scope correctly refuses to report readiness, and
   whether a later build resumes from the checkpoint.

4. **Does the rebuilt cache match the hot one?** Row counts, index list,
   the ready check and its cost, the hot-path queries, and a content digest
   of the whole ``assets``/``counts`` tables compared against the cache that
   was in place before the rebuild.

Read-only discipline
--------------------
The project is opened through a ``mode=ro`` SQLite URI and is never written.
The existing cache is **renamed**, not deleted, so the previous state can be
compared against the rebuilt one and restored if anything goes wrong. The
renamed backup is left on disk and reported; nothing is removed silently.

Usage
-----
::

    python tools/scale/coldstart.py \\
        --project E:\\Prism-stress-100k\\Prism-100k-v1.prism \\
        --cache "%APPDATA%\\Prism\\catalog\\Prism-100k-v1.prism.sqlite" \\
        --label T100K-cold \\
        --out artifacts/scale-acceptance/2026-10-22/scale-acceptance \\
        --sha256
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import sqlite3
import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

import evidence  # noqa: E402  (path set up above)


#: The batch size the production builder uses
#: (``prism/paged_open_preparation.py:104``).
PRODUCTION_BATCH_SIZE = 128

#: How often the main thread samples its own heartbeat while the build runs.
#: This is a sampling interval, not a wait: the point is to measure how long
#: the main thread is starved, so the interval has to be short enough to see
#: a stall and long enough not to cost the build measurable time.
HEARTBEAT_INTERVAL_S = 0.010


def write_json(path, payload):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=str),
                    encoding='utf-8')
    return path


# ── the build lock ─────────────────────────────────────────────────────────

def build_lock_state(path):
    """Is the ``.build-lock`` file currently held by a builder?

    The production builder takes an ``EXCLUSIVE`` transaction on this file
    for the duration of the build (``prism/project_catalog_index.py:155``).
    The lock is released when that connection closes, but **the file stays on
    disk** -- so a leftover zero-byte ``.build-lock`` is a normal artefact of
    a finished build, not evidence of a stuck one.

    The probe mirrors the production call exactly: connect with
    ``timeout=0`` and try ``BEGIN EXCLUSIVE``. If the builder holds the
    lock, SQLite refuses immediately instead of waiting.
    """
    path = Path(path)
    if not path.exists():
        return dict(exists=False, path=str(path))
    stat = path.stat()
    record = dict(exists=True, path=str(path), size_bytes=stat.st_size,
                  mtime_ns=stat.st_mtime_ns,
                  mtime=time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(stat.st_mtime)))
    acquired, error = None, None
    try:
        connection = sqlite3.connect(path, timeout=0)
        try:
            connection.execute('BEGIN EXCLUSIVE')
            acquired = True
            connection.rollback()
        finally:
            connection.close()
    except sqlite3.Error as caught:
        acquired = False
        error = str(caught)
    record['exclusive_acquired'] = acquired
    record['held_by_someone'] = (acquired is False)
    record['probe_error'] = error
    return record


# ── the cold build ─────────────────────────────────────────────────────────

def cold_build(project, cache, *, label, batch_size=PRODUCTION_BATCH_SIZE,
               out_dir=None, cancel_after_batches=None):
    """Build the catalog from zero and time it batch by batch.

    The build runs in a worker thread. That is not a cosmetic choice: the
    production path already builds the catalog off the GUI thread, and the
    point of this measurement is to see what the GUI thread loses while the
    build runs. The main thread samples its own heartbeat for the whole
    duration, so the report can state the longest stall it suffered.
    """
    from prism.project_open_scope import ProjectOpenScope

    project = Path(project).resolve()
    cache = Path(cache).resolve()
    cache.parent.mkdir(parents=True, exist_ok=True)

    scope = ProjectOpenScope(str(project), str(cache))
    report = dict(label=label, project=str(project), cache=str(cache),
                  batch_size=batch_size, ready_before=bool(scope.ready),
                  completed=False)

    batches = []
    lock_samples = []
    heartbeat = []
    stop_heartbeat = threading.Event()
    outcome = {}

    def on_batch(indexed):
        batches.append((time.perf_counter(), int(indexed)))

    def sampler():
        """Main-thread heartbeat: how long was this thread starved?"""
        previous = time.perf_counter()
        while not stop_heartbeat.wait(HEARTBEAT_INTERVAL_S):
            now = time.perf_counter()
            heartbeat.append((now - previous) * 1000)
            previous = now

    def lock_watcher():
        """Probe the build lock while the build owns it.

        The probe never creates the file: it reports "absent" until the
        builder has created it, so the watcher cannot be mistaken for the
        builder itself.
        """
        lock_path = Path(str(cache) + '.build-lock')
        while not stop_heartbeat.is_set():
            lock_samples.append(build_lock_state(lock_path))
            stop_heartbeat.wait(0.1)

    cancel_state = dict(batches=0)

    def cancel():
        if cancel_after_batches is None:
            return False
        return cancel_state['batches'] >= cancel_after_batches

    def worker():
        try:
            started = time.perf_counter()
            result = scope.build(batch_size=batch_size, cancel=cancel,
                                 on_batch=on_batch)
            outcome['result'] = result
            outcome['seconds'] = time.perf_counter() - started
            outcome['error'] = None
        except Exception as caught:  # pragma: no cover - reported, not raised
            outcome['error'] = repr(caught)
            outcome['seconds'] = time.perf_counter() - outcome.get('_started', time.perf_counter())
        finally:
            # The heartbeat counter must follow the *committed* batches, not
            # the callback count, so it is read from the batch log.
            cancel_state['batches'] = len(batches)

    started = time.perf_counter()
    outcome['_started'] = started
    thread = threading.Thread(target=worker, name='catalog-build', daemon=True)
    sampler_thread = threading.Thread(target=sampler, name='heartbeat', daemon=True)
    lock_thread = threading.Thread(target=lock_watcher, name='lock-watcher', daemon=True)
    sampler_thread.start()
    lock_thread.start()
    thread.start()
    thread.join()
    stop_heartbeat.set()
    sampler_thread.join(timeout=2)
    lock_thread.join(timeout=2)

    wall_ms = (time.perf_counter() - started) * 1000
    result = outcome.get('result')

    # Per-batch wall clock, straight from the production on_batch callback.
    stamps = [entry[0] for entry in batches]
    deltas = [(stamps[index] - stamps[index - 1]) * 1000
              for index in range(1, len(stamps))]
    first_batch_ms = (stamps[0] - started) * 1000 if stamps else None
    last_batch_ms = (stamps[-1] - stamps[-2]) * 1000 if len(stamps) > 1 else None

    report['wall_ms'] = round(wall_ms, 2)
    report['builder_seconds'] = round(outcome.get('seconds') or 0.0, 3)
    report['error'] = outcome.get('error')
    report['batches'] = len(batches)
    report['batch_ms'] = evidence.stats(deltas)
    report['first_batch_ms'] = round(first_batch_ms, 2) if first_batch_ms is not None else None
    report['last_batch_ms'] = round(last_batch_ms, 2) if last_batch_ms is not None else None
    report['batch_cumulative_head'] = [entry[1] for entry in batches[:5]]
    report['batch_cumulative_tail'] = [entry[1] for entry in batches[-5:]]
    report['main_thread_heartbeat'] = evidence.stats(heartbeat)
    report['main_thread_samples'] = len(heartbeat)
    held = [sample for sample in lock_samples if sample.get('held_by_someone')]
    present = [sample for sample in lock_samples if sample.get('exists')]
    report['build_lock_during_build'] = dict(
        samples=len(lock_samples),
        present_samples=len(present),
        held_samples=len(held),
        held_at_least_once=bool(held),
        first_held_sample=held[0] if held else None,
        first_present_sample=present[0] if present else None)
    if result is not None:
        report['result'] = dict(complete=bool(result.complete),
                                total_records=int(result.total_records),
                                processed=int(result.processed_records),
                                last_id=result.last_id,
                                resumed=bool(result.resumed),
                                json_characters=int(result.json_characters))
    report['completed'] = True
    if out_dir:
        write_json(Path(out_dir) / 'reports' / ('%s-cold-build.json' % label), report)
    return report


# ── phase attribution ──────────────────────────────────────────────────────

def phase_attribution(project, *, batch_size=PRODUCTION_BATCH_SIZE, workdir=None):
    """Time the three steps of the catalog build separately, on the real data.

    This is an **isolated** measurement. It repeats the same SQL, the same
    JSON projection and the same cache inserts that
    ``prism/project_catalog_index.py::_build`` performs, and times each step
    in a single pass. It does not patch production code, and it writes only
    to a throwaway database under ``workdir``.

    The production build's own wall clock is reported next to it, so the
    reader can see how much of the total each step explains.
    """
    from collections import Counter

    project = Path(project).resolve()
    workdir = Path(workdir).resolve() if workdir else None
    buckets = dict(source_read=0.0, projection=0.0, cache_write=0.0,
                   source_guard=0.0, cache_setup=0.0)
    counts = Counter()
    rows_seen = 0
    characters = 0

    uri = project.as_uri() + '?mode=ro'
    source = sqlite3.connect(uri, uri=True, timeout=5)
    # The cache has to be a real file: the production builder commits every
    # batch to disk, and an in-memory database would hide that cost and make
    # the phase split incomparable with the real build.
    scratch = None
    try:
        setup_started = time.perf_counter()
        if workdir is not None:
            workdir.mkdir(parents=True, exist_ok=True)
            scratch = Path(workdir) / 'attribution-catalog.sqlite'
            if scratch.exists():
                scratch.unlink()
            cache = sqlite3.connect(scratch, timeout=5)
        else:
            cache = sqlite3.connect(':memory:')
        cache.executescript('''
            CREATE TABLE catalog_state(key TEXT PRIMARY KEY,value TEXT NOT NULL);
            CREATE TABLE assets(
                id INTEGER PRIMARY KEY,kind TEXT NOT NULL,canvas_id TEXT,
                title TEXT,group_id TEXT,width INTEGER,height INTEGER,
                has_preview INTEGER NOT NULL,has_original INTEGER NOT NULL,
                tags TEXT NOT NULL DEFAULT '[]',rating INTEGER NOT NULL DEFAULT 0,
                notes TEXT NOT NULL DEFAULT '',filename TEXT NOT NULL DEFAULT '',
                deleted_at TEXT);
            CREATE INDEX assets_page ON assets(canvas_id,id);
            CREATE INDEX assets_kind_page ON assets(canvas_id,kind,id);
            CREATE TABLE counts(scope TEXT PRIMARY KEY,total INTEGER NOT NULL);
        ''')
        buckets['cache_setup'] += time.perf_counter() - setup_started

        last_id = None
        while True:
            guard_started = time.perf_counter()
            where = '' if last_id is None else 'WHERE i.id>?'
            parameters = () if last_id is None else (last_id,)
            # The production builder re-stats the source and re-reads
            # ``PRAGMA data_version`` before every batch (``check_source``).
            # That guard is part of the cost, so it is timed on its own.
            project.stat()
            source.execute('PRAGMA data_version').fetchone()
            buckets['source_guard'] += time.perf_counter() - guard_started

            read_started = time.perf_counter()
            cursor = source.execute(
                f'SELECT i.id,i.type,i.data,EXISTS('
                f'SELECT 1 FROM sqlar s WHERE s.item_id=i.id) '
                f'FROM items i {where} ORDER BY i.id LIMIT ?',
                (*parameters, batch_size))
            rows = cursor.fetchmany(batch_size)
            cursor.close()
            buckets['source_read'] += time.perf_counter() - read_started

            project_started = time.perf_counter()
            projected, batch_counts = [], Counter()
            for key, kind, payload, has_original in rows:
                extra = json.loads(payload) if payload else {}
                canvas = extra.get('canvasId', 'default-canvas')
                tags = extra.get('tags') or []
                rating = max(0, min(5, int(extra.get('rating', 0) or 0)))
                projected.append((key, kind, canvas, extra.get('title', ''),
                                  extra.get('groupId'), extra.get('imageWidth'),
                                  extra.get('imageHeight'),
                                  bool(extra.get('displayThumbnail') or extra.get('displayOverview')),
                                  has_original,
                                  json.dumps(tags, ensure_ascii=False, separators=(',', ':')),
                                  rating, extra.get('notes', '') or '',
                                  extra.get('filename', '') or '', extra.get('deletedAt')))
                batch_counts[(canvas, kind)] += 1
                characters += len(payload or '')
            buckets['projection'] += time.perf_counter() - project_started

            write_started = time.perf_counter()
            with cache:
                cache.executemany(
                    'INSERT INTO assets(id,kind,canvas_id,title,group_id,width,height,'
                    'has_preview,has_original,tags,rating,notes,filename,deleted_at) '
                    'VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)', projected)
                cache.executemany(
                    'INSERT INTO counts VALUES(?,?) '
                    'ON CONFLICT(scope) DO UPDATE SET total=total+excluded.total',
                    [(json.dumps([canvas, kind]), count)
                     for (canvas, kind), count in batch_counts.items()])
            buckets['cache_write'] += time.perf_counter() - write_started

            rows_seen += len(rows)
            for key in batch_counts:
                counts[key] += batch_counts[key]
            if not rows:
                break
            last_id = rows[-1][0]
            if len(rows) < batch_size:
                break
        cache.close()
    finally:
        source.close()
        if scratch is not None and scratch.exists():
            try:
                scratch.unlink()
            except OSError:
                pass

    total = sum(buckets.values())
    return dict(
        note='isolated single-pass replica of the three build steps; '
             'not a patch of production code',
        cache_target=str(scratch) if scratch is not None else ':memory:',
        rows=rows_seen,
        json_characters=characters,
        total_ms=round(total * 1000, 2),
        phases_ms={key: round(value * 1000, 2) for key, value in buckets.items()},
        phase_share={key: (round(value / total * 100, 2) if total else None)
                     for key, value in buckets.items()},
        counts_rows=len(counts),
    )


# ── verification ───────────────────────────────────────────────────────────

def catalog_digest(path):
    """A content digest of the whole catalog, so two caches can be compared."""
    path = Path(path)
    if not path.exists():
        return dict(exists=False, path=str(path))
    uri = path.as_uri() + '?mode=ro'
    connection = sqlite3.connect(uri, uri=True, timeout=5)
    try:
        tables = {row[0] for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        indexes = sorted(row[0] for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type='index' AND name NOT LIKE 'sqlite_%'"))
        digest = hashlib.sha256()
        assets_rows = 0
        if 'assets' in tables:
            for row in connection.execute(
                    'SELECT id,kind,canvas_id,title,group_id,width,height,'
                    'has_preview,has_original,tags,rating,notes,filename,deleted_at '
                    'FROM assets ORDER BY id'):
                digest.update(repr(row).encode('utf-8'))
                assets_rows += 1
        counts_rows = 0
        if 'counts' in tables:
            for row in connection.execute('SELECT scope,total FROM counts ORDER BY scope'):
                digest.update(repr(row).encode('utf-8'))
                counts_rows += 1
        state = None
        if 'catalog_state' in tables:
            row = connection.execute(
                "SELECT value FROM catalog_state WHERE key='build'").fetchone()
            if row:
                try:
                    state = json.loads(row[0])
                except ValueError:
                    state = {'unparsable': row[0][:200]}
        return dict(exists=True, path=str(path), size_bytes=path.stat().st_size,
                    tables=sorted(tables), indexes=indexes,
                    assets_rows=assets_rows, counts_rows=counts_rows,
                    content_sha256=digest.hexdigest(), build_state=state)
    finally:
        connection.close()


def verify_rebuilt(project, cache, *, expected_assets=None, expected_counts=None,
                   expected_indexes=('assets_kind_page', 'assets_page'),
                   ready_repeats=20):
    """Is the rebuilt catalog usable, and does it say what it should?

    Checks the ready flag and its cost, the row counts, the index list, and
    the hot-path queries the UI actually uses.
    """
    from prism.project_open_scope import ProjectOpenScope

    project = Path(project).resolve()
    cache = Path(cache).resolve()
    scope = ProjectOpenScope(str(project), str(cache))
    report = dict(project=str(project), cache=str(cache))

    ready_costs = []
    ready = None
    for _ in range(ready_repeats):
        started = time.perf_counter()
        ready = scope.ready
        ready_costs.append((time.perf_counter() - started) * 1000)
    report['ready'] = bool(ready)
    report['ready_check_ms'] = evidence.stats(ready_costs)

    started = time.perf_counter()
    counts = scope.counts()
    report['counts_ms'] = round((time.perf_counter() - started) * 1000, 2)
    report['counts_rows'] = len(counts) if counts is not None else None
    report['total'] = scope.total()

    digest = catalog_digest(cache)
    report['catalog'] = digest
    report['expected_indexes_present'] = bool(
        digest.get('indexes') and set(expected_indexes) <= set(digest['indexes']))
    report['assets_rows_ok'] = (expected_assets is None
                                or digest.get('assets_rows') == expected_assets)
    report['counts_rows_ok'] = (expected_counts is None
                                or digest.get('counts_rows') == expected_counts)

    # The hot path the sidebar and the search box use.
    canvas_id = None
    if counts:
        canvas_id = max(counts.items(), key=lambda item: item[1])[0][0]
    hot = {}
    if canvas_id is not None:
        started = time.perf_counter()
        rows = list(scope.iter_query(canvas_ids=(canvas_id,), limit=100))
        hot['iter_query_canvas_limit_100_ms'] = round((time.perf_counter() - started) * 1000, 2)
        hot['iter_query_canvas_rows'] = len(rows)
        started = time.perf_counter()
        hot_rows = list(scope.iter_query(limit=100))
        hot['iter_query_unfiltered_ms'] = round((time.perf_counter() - started) * 1000, 2)
        hot['iter_query_unfiltered_rows'] = len(hot_rows)
        started = time.perf_counter()
        matched = list(scope.iter_query(text='a', limit=100))
        hot['iter_query_text_a_ms'] = round((time.perf_counter() - started) * 1000, 2)
        hot['iter_query_text_a_rows'] = len(matched)
        started = time.perf_counter()
        details = scope.details([row['id'] for row in rows[:100]])
        hot['details_100_ms'] = round((time.perf_counter() - started) * 1000, 2)
        hot['details_100_rows'] = len(details)
    report['hot_path'] = hot
    report['canvas_sampled'] = canvas_id
    return report


# ── cancellation ───────────────────────────────────────────────────────────

def cancel_test(project, cache, *, label, batch_size=PRODUCTION_BATCH_SIZE,
                cancel_after_batches=20, out_dir=None):
    """Start a build, cancel it, and check what state it leaves behind.

    The build must stop promptly, leave a **resumable** checkpoint (not a
    half-written row), keep the scope from claiming readiness, and leave the
    process able to work. A later build must pick up from the checkpoint.
    """
    from prism.project_open_scope import ProjectOpenScope

    project = Path(project).resolve()
    cache = Path(cache).resolve()
    cache.parent.mkdir(parents=True, exist_ok=True)
    for suffix in ('', '-wal', '-shm'):
        candidate = Path(str(cache) + suffix)
        if candidate.exists():
            candidate.unlink()
    lock = Path(str(cache) + '.build-lock')
    if lock.exists():
        lock.unlink()

    report = dict(label=label, project=str(project), cache=str(cache),
                  cancel_after_batches=cancel_after_batches, batch_size=batch_size)

    scope = ProjectOpenScope(str(project), str(cache))
    batches = []
    state = dict(cancelled_at=None)

    def on_batch(indexed):
        batches.append((time.perf_counter(), int(indexed)))

    def cancel():
        if len(batches) >= cancel_after_batches:
            state['cancelled_at'] = time.perf_counter()
            return True
        return False

    started = time.perf_counter()
    result = scope.build(batch_size=batch_size, cancel=cancel, on_batch=on_batch)
    elapsed_ms = (time.perf_counter() - started) * 1000
    report['build_ms'] = round(elapsed_ms, 2)
    report['batches_committed'] = len(batches)
    report['result'] = dict(complete=bool(result.complete),
                             total_records=int(result.total_records),
                             processed=int(result.processed_records),
                             last_id=result.last_id,
                             resumed=bool(result.resumed))
    report['stopped_early'] = bool(not result.complete
                                   and len(batches) < cancel_after_batches + 2)
    report['cancel_latency_ms'] = (round((state['cancelled_at'] - started) * 1000, 2)
                                   if state['cancelled_at'] else None)

    # The scope must not claim readiness on an incomplete cache.
    started = time.perf_counter()
    report['ready_after_cancel'] = bool(scope.ready)
    report['ready_after_cancel_ms'] = round((time.perf_counter() - started) * 1000, 2)

    checkpoint = catalog_digest(cache)
    report['checkpoint'] = dict(assets_rows=checkpoint.get('assets_rows'),
                                counts_rows=checkpoint.get('counts_rows'),
                                build_state=checkpoint.get('build_state'))
    report['checkpoint_is_resumable'] = bool(
        checkpoint.get('build_state')
        and not checkpoint['build_state'].get('complete')
        and checkpoint['build_state'].get('last_id') is not None)

    # The process must still be usable: read the source again and time it.
    started = time.perf_counter()
    uri = project.as_uri() + '?mode=ro'
    connection = sqlite3.connect(uri, uri=True, timeout=5)
    try:
        probe = connection.execute('SELECT count(*) FROM items').fetchone()[0]
    finally:
        connection.close()
    report['process_usable_after_cancel'] = True
    report['process_probe_rows'] = int(probe)
    report['process_probe_ms'] = round((time.perf_counter() - started) * 1000, 2)

    # The next build must resume from the checkpoint, not start over.
    resumed_started = time.perf_counter()
    resumed = scope.build(batch_size=batch_size, on_batch=on_batch)
    report['resume'] = dict(
        complete=bool(resumed.complete),
        resumed=bool(resumed.resumed),
        processed=int(resumed.processed_records),
        total_records=int(resumed.total_records),
        ms=round((time.perf_counter() - resumed_started) * 1000, 2))
    report['ready_after_resume'] = bool(scope.ready)
    if out_dir:
        write_json(Path(out_dir) / 'reports' / ('%s-cancel.json' % label), report)
    return report


def cached_sha256(project, out_dir):
    """The project digest, computed once and reused while the file is unchanged.

    Digesting 51.8 GiB takes minutes and the point of the digest is to
    prove the file was not rewritten -- which the size and mtime already
    decide for the *reuse* decision. So the digest is kept next to the
    reports and only recomputed when the size or mtime moved.
    """
    project = Path(project)
    stat = project.stat()
    cache_path = Path(out_dir) / 'reports' / ('%s.project.sha256.json' % project.name)
    try:
        cached = json.loads(cache_path.read_text(encoding='utf-8'))
        if (cached.get('size_bytes') == stat.st_size
                and cached.get('mtime_ns') == stat.st_mtime_ns
                and cached.get('sha256')):
            cached['reused'] = True
            return cached['sha256']
    except (OSError, ValueError):
        pass
    digest = evidence.sha256_file(project)
    write_json(cache_path, dict(path=str(project), size_bytes=stat.st_size,
                                 mtime_ns=stat.st_mtime_ns, sha256=digest,
                                 computed=time.strftime('%Y-%m-%d %H:%M:%S')))
    return digest


# ── the run ────────────────────────────────────────────────────────────────

def run(project, cache, *, label, out_dir, sha256=False, run_cancel=True,
        cancel_after_batches=20, attribution=True):
    """The whole cold-start sequence, with a report for each part."""
    from prism.project_open_scope import ProjectOpenScope

    project = Path(project).resolve()
    cache = Path(cache).resolve()
    out_dir = Path(out_dir).resolve()
    out_dir.mkdir(parents=True, exist_ok=True)
    report = dict(label=label, project=str(project), cache=str(cache),
                  started=time.time(), completed=False)

    # Every step is written to disk as it finishes. A run that is interrupted
    # half way keeps the measurements it already made, and the report says
    # which steps never ran.
    report_path = out_dir / 'reports' / ('%s-coldstart.json' % label)

    def persist():
        report['steps_done'] = report.get('steps_done', []) + [report.get('current_step')]
        write_json(report_path, report)

    report['current_step'] = 'preflight'
    report['environment_before'] = evidence.capture_environment()
    report['environment_before']['python_processes'] = evidence.python_processes()

    # ── preflight ──────────────────────────────────────────────────────
    preflight = dict()
    preflight['project_stat'] = dict(path=str(project), size_bytes=project.stat().st_size,
                                     mtime_ns=project.stat().st_mtime_ns)
    if sha256:
        preflight['project_sha256'] = cached_sha256(project, out_dir)
    preflight['cache_dir_before'] = evidence.directory_listing(cache.parent)
    preflight['build_lock_before'] = build_lock_state(Path(str(cache) + '.build-lock'))
    scope = ProjectOpenScope(str(project), str(cache))
    preflight['ready_before'] = bool(scope.ready)
    preflight['catalog_before'] = catalog_digest(cache) if cache.exists() else dict(exists=False)
    report['preflight'] = preflight
    persist()

    # ── backup the existing cache (rename, never delete) ──────────────
    stamp = time.strftime('%Y%m%d-%H%M%S')
    backup = {}
    renamed = []
    for suffix in ('', '-wal', '-shm', '.build-lock'):
        candidate = Path(str(cache) + suffix)
        if not candidate.exists():
            continue
        target = Path(str(candidate) + '.bak-' + stamp)
        try:
            os.replace(candidate, target)
            renamed.append(dict(original=str(candidate), backup=str(target),
                                size_bytes=target.stat().st_size))
        except OSError as error:
            renamed.append(dict(original=str(candidate), error=str(error)))
    backup['renamed'] = renamed
    backup['cache_exists_after_rename'] = cache.exists()
    report['backup'] = backup

    # A cold build is only a cold build if the cache really is gone. If the
    # rename failed (another process had the file open on Windows), the build
    # below would measure a no-op, so the run stops here instead of reporting
    # a meaningless number.
    if cache.exists():
        report['error'] = ('the cache could not be moved aside; it still exists '
                            'at %s. Refusing to report a cold-build number for a '
                            'warm cache.' % cache)
        report['completed'] = False
        write_json(out_dir / 'reports' / ('%s-coldstart.json' % label), report)
        return report

    # ── the cold build ─────────────────────────────────────────────────
    report['current_step'] = 'cold_build'
    report['cold_build'] = cold_build(project, cache, label=label,
                                      out_dir=out_dir)
    report['build_lock_after'] = build_lock_state(Path(str(cache) + '.build-lock'))
    persist()

    # ── verification ───────────────────────────────────────────────────
    report['current_step'] = 'verify'
    expected_assets = expected_counts = None
    before = preflight.get('catalog_before') or {}
    if before.get('assets_rows') is not None:
        expected_assets = before['assets_rows']
    if before.get('counts_rows') is not None:
        expected_counts = before['counts_rows']
    report['verify'] = verify_rebuilt(project, cache,
                                       expected_assets=expected_assets,
                                       expected_counts=expected_counts)
    persist()

    # ── compare with the cache that was there before ──────────────────
    report['current_step'] = 'comparison'
    comparison = dict()
    after = catalog_digest(cache)
    for entry in renamed:
        # Only the cache itself is comparable. The renamed ``.build-lock`` is
        # a lock file, not a catalog, and must not be mistaken for one.
        if entry.get('original') != str(cache) or entry.get('error'):
            continue
        source = entry.get('backup')
        old = catalog_digest(source)
        comparison[str(cache)] = dict(
            backup_path=source,
            before=old, after=after,
            content_identical=bool(old.get('content_sha256')
                                   and old.get('content_sha256') == after.get('content_sha256')),
            assets_rows_before=old.get('assets_rows'),
            assets_rows_after=after.get('assets_rows'),
            counts_rows_before=old.get('counts_rows'),
            counts_rows_after=after.get('counts_rows'),
            size_bytes_before=old.get('size_bytes'),
            size_bytes_after=after.get('size_bytes'))
    report['comparison_with_previous_cache'] = comparison
    persist()

    # ── phase attribution ─────────────────────────────────────────────
    if attribution:
        report['current_step'] = 'phase_attribution'
        report['phase_attribution'] = phase_attribution(
            project, workdir=out_dir / '_work' / ('attribution-' + label))
        persist()

    # ── cancellation ───────────────────────────────────────────────────
    if run_cancel:
        report['current_step'] = 'cancel_test'
        scratch = out_dir / '_work' / ('cancel-' + label) / 'catalog.sqlite'
        report['cancel_test'] = cancel_test(
            project, scratch, label=label,
            cancel_after_batches=cancel_after_batches, out_dir=out_dir)
        persist()

    # ── forensics ──────────────────────────────────────────────────────
    report['current_step'] = 'forensics'
    report['project_stat_after'] = dict(path=str(project), size_bytes=project.stat().st_size,
                                        mtime_ns=project.stat().st_mtime_ns)
    report['project_unchanged'] = bool(
        preflight['project_stat']['size_bytes'] == report['project_stat_after']['size_bytes']
        and preflight['project_stat']['mtime_ns'] == report['project_stat_after']['mtime_ns'])
    if sha256:
        report['project_sha256_after'] = evidence.sha256_file(project)
        report['project_sha256_unchanged'] = bool(
            preflight.get('project_sha256') == report['project_sha256_after'])
    report['cache_dir_after'] = evidence.directory_listing(cache.parent)
    report['cache_dir_diff'] = evidence.diff_listings(preflight['cache_dir_before'],
                                                       report['cache_dir_after'])
    report['environment_after'] = evidence.capture_environment()
    report['environment_after']['python_processes'] = evidence.python_processes()
    report['current_step'] = 'done'
    report['completed'] = True
    write_json(report_path, report)
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--project', type=Path, required=True)
    parser.add_argument('--cache', type=Path, required=True,
                         help='the catalog sidecar path (usually under %%APPDATA%%)')
    parser.add_argument('--label', required=True)
    parser.add_argument('--out', type=Path, required=True)
    parser.add_argument('--sha256', action='store_true',
                         help='digest the project before and after (slow on 51.8 GiB)')
    parser.add_argument('--no-cancel-test', action='store_true')
    parser.add_argument('--cancel-after-batches', type=int, default=20)
    parser.add_argument('--no-attribution', action='store_true')
    args = parser.parse_args(argv)

    report = run(args.project, args.cache, label=args.label, out_dir=args.out,
                 sha256=args.sha256, run_cancel=not args.no_cancel_test,
                 cancel_after_batches=args.cancel_after_batches,
                 attribution=not args.no_attribution)
    print(json.dumps(report, ensure_ascii=False, indent=2, default=str))
    return 0 if report.get('completed') else 1


if __name__ == '__main__':
    _code = main()
    sys.stdout.flush()
    sys.stderr.flush()
    os._exit(_code)
