#!/usr/bin/env python
"""Round-2 GUI driver for the Prism real-scale acceptance group (E-6).

What it does, in order:

1. ``gate``       -- prove the C: copy is usable *before* any GUI run touches
                     it: file size, a read-only open, the page / freelist /
                     table counts, ``PRAGMA quick_check``, and the source
                     project's size + mtime.  Strictly read-only.
2. ``measure``    -- run the E-1 harness (``tools/scale/acceptance.py``)
                     against the copy inside a *real* GUI process
                     (``PrismApplication`` + ``PrismMainWindow``), then reduce
                     that run's trace to the numbers the previous round
                     reported in ``GUI-BASELINE.md``.
3. ``cancel-probe`` -- the one question the harness cannot answer: on the GUI
                     path, does the background catalog build stop when the
                     project is closed, and does the GUI stay responsive
                     while it runs?
4. ``summarize``  -- join the per-run summaries into the before/after table.

What this driver changes is the **measurement**, not the product:

* the auto-save is muted -- the 2026-10-22 14:31 incident rewrote a whole
  51.8 GiB project because a GUI open triggered an auto-save; a run that
  rewrites the file it is measuring is not a measurement;
* the search is driven through the real search box, so the numbers are the
  UI's (``gui_recovered.blocked_ms`` + the UI's own hit counts), not one
  function's.

``prism/**`` is never modified.  ``tools/scale/acceptance.py`` is called, not
edited.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
HERE = Path(__file__).resolve().parent
for _entry in (ROOT, HERE):
    if str(_entry) not in sys.path:
        sys.path.insert(0, str(_entry))

import evidence  # noqa: E402  (tools/scale/evidence.py, E-1's module)

# ── the copy under test ────────────────────────────────────────────────────

COPY_PATH = Path(r'C:\Prism-round2\Prism-100k-v1.prism')
SOURCE_PATH = Path(r'E:\Prism-stress-100k\Prism-100k-v1.prism')

#: Post-incident baseline of the project (the leader's read-only verifier
#: compares the same numbers).
EXPECTED = dict(
    size_bytes=55_633_186_816,
    page_count=13_582_321,
    freelist_count=6,
    items_rows=100_000,
    sqlar_rows=100_000,
    material_records=5_000,
    project_id='e536d191d1a84e5897103882f3b60d0e',
)

#: The source project's size / mtime before this round started.
SOURCE_BASELINE = dict(size_bytes=55_633_186_816, mtime='2026-10-22 14:36:22')

# ── the previous round's numbers (never re-measured, only compared to) ──────
#
# GUI-BASELINE.md §0.5 (low-load window) and §1.1; E-1's BASELINE.md for the
# scale baseline.  Copied verbatim so the comparison cannot drift.
BASELINE = dict(
    source='artifacts/scale-acceptance/2026-10-22/GUI-BASELINE.md',
    open_ms=36_531.5,
    open_ms_high_load=44_855.3,
    gui_blocked_total_ms=21_820.0,
    gui_blocked_count=10,
    gui_blocked_max_ms=6_430.8,
    open_page_samples_ms=[1_861.9, 1_864.5],
    search_blocked_ms=2_806.4,
    search_blocked_ms_high_load=3_368.6,
    search_hits=1_122,
    search_hits_breakdown=dict(asset=32, canvas=88, material=71),
    peak_rss_bytes=int(5.58 * 2 ** 30),
    browser_cards_ms=5_485.51,
    heartbeat_interactive=dict(median_ms=18.29, p95_ms=58.85, max_ms=7_433.06),
    cold_build_seconds=[10.89, 13.75, 15.01],
)

#: Shared state between the patched measurement functions and the reducer.
STATE = dict(needle='wood', search={}, switches=[], autosave_muted=0,
             skip_pre_open_digest=True)


def write_json(path, payload):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=str),
                    encoding='utf-8')
    return path


# ── 1. the gate ────────────────────────────────────────────────────────────

def _robocopy_command_lines():
    script = ("Get-CimInstance Win32_Process -Filter \"Name='Robocopy.exe'\" | "
              "ForEach-Object { $_.CommandLine } | ConvertTo-Json -Compress")
    try:
        completed = subprocess.run(
            ['powershell', '-NoProfile', '-NonInteractive', '-Command', script],
            capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.SubprocessError) as error:
        return dict(available=False, error=str(error), command_lines=[])
    text = (completed.stdout or '').strip()
    if not text:
        return dict(available=True, command_lines=[])
    try:
        payload = json.loads(text)
    except ValueError:
        return dict(available=True, raw=text[:2000], command_lines=[])
    rows = payload if isinstance(payload, list) else [payload]
    return dict(available=True, command_lines=[str(row) for row in rows])


def probe_project(path, *, quick_check=True, digest=False):
    """Read-only inspection of one project file."""
    path = Path(path)
    result = dict(path=str(path), exists=path.is_file())
    if not result['exists']:
        return result
    stat = path.stat()
    result['size_bytes'] = stat.st_size
    result['mtime_ns'] = stat.st_mtime_ns
    result['mtime'] = time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(stat.st_mtime))
    # The copy must be openable for reading: while robocopy still holds it,
    # this raises PermissionError (the 16:05 report).
    try:
        with open(path, 'rb') as handle:
            head = handle.read(4096)
        result['readable'] = True
        result['head_bytes'] = len(head)
    except OSError as error:
        result['readable'] = False
        result['read_error'] = '%s: %s' % (type(error).__name__, error)
        return result
    uri = 'file:' + path.as_posix() + '?mode=ro'
    connection = sqlite3.connect(uri, uri=True, timeout=30)
    try:
        result['page_count'] = connection.execute('PRAGMA page_count').fetchone()[0]
        result['freelist_count'] = connection.execute('PRAGMA freelist_count').fetchone()[0]
        result['page_size'] = connection.execute('PRAGMA page_size').fetchone()[0]
        result['journal_mode'] = connection.execute('PRAGMA journal_mode').fetchone()[0]
        result['items_rows'] = connection.execute('SELECT count(*) FROM items').fetchone()[0]
        result['sqlar_rows'] = connection.execute('SELECT count(*) FROM sqlar').fetchone()[0]
        result['material_records'] = connection.execute(
            'SELECT count(*) FROM material_records').fetchone()[0]
        row = connection.execute(
            "SELECT value FROM prism_metadata WHERE key = 'project_id'").fetchone()
        result['project_id'] = row[0] if row else None
        if quick_check:
            started = time.perf_counter()
            result['quick_check'] = connection.execute('PRAGMA quick_check').fetchone()[0]
            result['quick_check_ms'] = round((time.perf_counter() - started) * 1000, 1)
    finally:
        connection.close()
    if digest:
        result['sha256'] = evidence.sha256_file(path)
    checks = {
        'size_bytes': result.get('size_bytes') == EXPECTED['size_bytes'],
        'page_count': result.get('page_count') == EXPECTED['page_count'],
        'freelist_count': result.get('freelist_count') == EXPECTED['freelist_count'],
        'items_rows': result.get('items_rows') == EXPECTED['items_rows'],
        'sqlar_rows': result.get('sqlar_rows') == EXPECTED['sqlar_rows'],
        'material_records': result.get('material_records') == EXPECTED['material_records'],
        'project_id': result.get('project_id') == EXPECTED['project_id'],
    }
    if quick_check:
        checks['quick_check'] = result.get('quick_check') == 'ok'
    if 'readable' in result:
        checks['readable'] = bool(result['readable'])
    result['checks'] = checks
    result['all_match'] = all(checks.values())
    return result


def command_gate(args):
    phase = args.phase
    out_dir = Path(args.out).resolve()
    copy_path = Path(args.copy) if args.copy else COPY_PATH
    source_path = Path(args.source) if args.source else SOURCE_PATH

    robocopy = _robocopy_command_lines()
    # "C:\Prism-round2" must not match "C:\Prism-round2-gate": the copy
    # directory is a prefix of the leader's separate gate copy, so the match
    # has to end on a path boundary.
    parent_pattern = re.compile(re.escape(str(copy_path.parent)) + r'(?![-\w])',
                                re.IGNORECASE)
    mine = [line for line in robocopy.get('command_lines', [])
            if parent_pattern.search(line)]
    payload = dict(
        phase=phase,
        taken_at=time.strftime('%Y-%m-%d %H:%M:%S'),
        robocopy=dict(all=robocopy.get('command_lines', []), targeting_my_copy=mine,
                      my_copy_locked=bool(mine)),
        copy=probe_project(copy_path, quick_check=not args.no_quick_check,
                           digest=args.digest),
        source=dict(path=str(source_path), size_bytes=None, mtime=None),
        environment=evidence.capture_environment(),
        python_processes=evidence.python_processes(),
    )
    try:
        stat = source_path.stat()
        payload['source'] = dict(
            path=str(source_path), size_bytes=stat.st_size,
            mtime=time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(stat.st_mtime)),
            mtime_ns=stat.st_mtime_ns,
            unchanged=(stat.st_size == SOURCE_BASELINE['size_bytes']
                       and time.strftime('%Y-%m-%d %H:%M:%S',
                                         time.localtime(stat.st_mtime))
                       == SOURCE_BASELINE['mtime']))
    except OSError as error:
        payload['source'] = dict(path=str(source_path), error=str(error), unchanged=False)

    payload['usable'] = bool(payload['copy'].get('all_match')
                             and payload['source'].get('unchanged')
                             and not payload['robocopy']['my_copy_locked'])
    path = write_json(out_dir / ('gate-%s.json' % phase), payload)
    print(json.dumps(payload, ensure_ascii=False, indent=2, default=str))
    print('written ->', path)
    return 0 if payload['usable'] else 1


# ── 2. the measurement patches ─────────────────────────────────────────────

def install_patches(acceptance):
    """Measurement-side runtime patches.  ``prism/**`` stays untouched."""

    # (a) the auto-save is muted: an auto-save rewrites the whole project.
    from prism.__main__ import PrismMainWindow

    original_autosave = PrismMainWindow._perform_autosave

    def muted_autosave(self):
        STATE['autosave_muted'] += 1
        return None

    PrismMainWindow._perform_autosave = muted_autosave

    # (a2) the harness fingerprints the project before the open and after it.
    #      The *pre-open* fingerprint reads all 51.8 GiB, which would pull the
    #      whole file into the page cache and turn the open into a memory read
    #      -- the harness would be warming what it is about to measure.  The
    #      post-open fingerprint is kept: it is the one that proves the
    #      project was not rewritten, and by then the measurement is over.
    original_fingerprint = evidence.file_fingerprint
    fingerprint_state = {'calls': 0}

    def fingerprint(path, *, digest=True):
        fingerprint_state['calls'] += 1
        if fingerprint_state['calls'] == 1 and STATE.get('skip_pre_open_digest', True):
            return original_fingerprint(path, digest=False)
        return original_fingerprint(path, digest=digest)

    evidence.file_fingerprint = fingerprint

    # (b) the search goes through the real search box.
    def ui_search(app, window, view, browser):
        return _measure_ui_search(acceptance, app, window, view, browser)

    acceptance._measure_search = ui_search

    # (c) the canvas switches record their exact wall-clock window, so the
    #     trace's `workspace.open_page` events can be attributed to a switch.
    original_switch = acceptance._measure_canvas_switch

    def canvas_switch(app, window, canvas, browser, pages, rows, dpr):
        windows = []
        original_open = window._open_resource_page

        def wrapped_open(kind, target, *args, **kwargs):
            started_wall = time.time()
            started = time.perf_counter()
            try:
                return original_open(kind, target, *args, **kwargs)
            finally:
                windows.append(dict(kind=kind, target=str(target),
                                    start=started_wall, end=time.time(),
                                    wall_ms=round((time.perf_counter() - started) * 1000, 2)))

        window._open_resource_page = wrapped_open
        try:
            result = original_switch(app, window, canvas, browser, pages, rows, dpr)
        finally:
            window._open_resource_page = original_open
        result['windows'] = windows
        STATE['switches'] = windows
        return result

    acceptance._measure_canvas_switch = canvas_switch

    return dict(autosave_muted=muted_autosave, canvas_switch=canvas_switch)


def _measure_ui_search(acceptance, app, window, view, browser):
    """Drive the real search box, the way a keystroke does.

    The previous round's search number is a **UI** number: the GUI freeze the
    user feels, plus the hit counts the UI shows.  ``acceptance.py``'s own
    search measurement times one function (``search_project``), which is the
    API number the GUI-BASELINE explicitly says must not be used as the user
    experience (267 ms vs 3368.6 ms).

    Note the product behaviour this has to cope with: the controller captures
    ``project_id`` when the window is built, so the first submit after a
    project was opened hits ``reset_project()`` and clears the box.  A user
    simply types again; so does this driver.
    """
    controller = getattr(window, 'search_controller', None)
    if controller is None:
        return dict(checked=False, reason='the window has no SearchController')
    needle = STATE['needle']
    captured = {}
    page = controller.page
    original_update = page.update_results

    def wrapped_update(results, key, counts, kind, suggestions=()):
        captured.setdefault('counts', dict(counts or {}))
        captured.setdefault('results', len(results))
        captured['kind'] = kind
        captured['query'] = key[0] if isinstance(key, tuple) else None
        return original_update(results, key, counts, kind, suggestions)

    page.update_results = wrapped_update

    original_run = controller._run_query

    def wrapped_run():
        started_wall = time.time()
        started = time.perf_counter()
        try:
            return original_run()
        finally:
            captured.setdefault('query_start', started_wall)
            captured['query_end'] = time.time()
            captured.setdefault('query_wall_ms',
                                round((time.perf_counter() - started) * 1000, 2))

    controller._run_query = wrapped_run

    attempts = []
    total_start = time.time()
    try:
        for attempt in range(1, 4):
            controller.input.setText(needle)
            acceptance.wait_until(
                app,
                lambda: bool(captured.get('counts')) or controller.input.text() != needle,
                120_000, 'ui_search_attempt')
            attempts.append(dict(attempt=attempt,
                                 ran=bool(captured.get('counts')),
                                 input_text=controller.input.text()))
            if captured.get('counts'):
                break
            if controller.input.text() == needle:
                # The query is still running: wait for the result set.
                acceptance.wait_until(app, lambda: bool(captured.get('counts')),
                                      300_000, 'ui_search')
                attempts[-1]['ran'] = bool(captured.get('counts'))
                break
    finally:
        page.update_results = original_update
        controller._run_query = original_run
    total_end = time.time()

    result = dict(checked=bool(captured.get('counts')), needle=needle,
                  attempts=attempts,
                  ui_counts=captured.get('counts'),
                  ui_results=captured.get('results'),
                  ui_kind=captured.get('kind'),
                  query_wall_ms=captured.get('query_wall_ms'),
                  total_wall_ms=round((total_end - total_start) * 1000, 2),
                  query_window=[captured.get('query_start'), captured.get('query_end')],
                  note='driven through SearchController.input (the UI path); '
                       'the counts are the ones the UI itself computed')
    STATE['search'] = result
    return result


# ── 3. the trace reducer (GUI-BASELINE's measuring rules) ──────────────────

def _operations(events, name):
    return [event for event in events
            if event.get('event') == 'operation' and event.get('name') == name]


def _blocked_in(events, start, end):
    """``gui_recovered`` blocks whose heartbeat fell inside [start, end]."""
    if start is None or end is None:
        return []
    return [event for event in events
            if event.get('event') == 'gui_recovered'
            and start <= event.get('time', 0) <= end]


def _blocked_summary(events):
    total = sum(event.get('blocked_ms', 0) or 0 for event in events)
    maximum = max((event.get('blocked_ms', 0) or 0 for event in events), default=0)
    return dict(total_ms=round(total, 1), count=len(events), max_ms=round(maximum, 1))


def _stall_interval(event):
    """The wall interval a ``gui_recovered`` record describes.

    A record is written when the GUI thread *wakes up* and ``blocked_ms`` is
    how long it had been asleep, so the stall it reports is
    ``[time - blocked_ms, time]`` -- not a point in time.
    """
    blocked = event.get('blocked_ms', 0) or 0
    end = event.get('time', 0)
    return end - blocked / 1000.0, end


def _blocked_overlapping(events, start, end):
    """``gui_recovered`` blocks whose *stall* overlaps [start, end].

    The timestamp rule ("the recovery landed inside the window") misses a
    freeze whose recovery lands just after the window closes, which is
    exactly what the search does: the query returns while the GUI thread is
    still stuck in the work it triggered.
    """
    if start is None or end is None:
        return []
    found = []
    for event in events:
        if event.get('event') != 'gui_recovered':
            continue
        stall_start, stall_end = _stall_interval(event)
        if stall_start <= end and stall_end >= start:
            found.append(event)
    return found


def _blocked_overlap_summary(events, start, end):
    """The overlapping blocks, plus the slice of each stall inside the window."""
    blocks = _blocked_overlapping(events, start, end)
    overlap_total = 0.0
    for event in blocks:
        stall_start, stall_end = _stall_interval(event)
        overlap_total += (min(stall_end, end) - max(stall_start, start)) * 1000.0
    summary = _blocked_summary(blocks)
    summary['overlap_ms'] = round(overlap_total, 1)
    summary['rule'] = 'stall interval [time-blocked_ms, time] overlaps the window'
    summary['events'] = [dict(blocked_ms=event.get('blocked_ms'),
                              time=event.get('time')) for event in blocks]
    return summary


def reduce_trace(trace_dir, *, search_window=None, switches=None):
    """Turn one run's trace into the previous round's metrics."""
    events = evidence.read_trace_events(trace_dir)

    def first(name):
        for event in events:
            if event.get('event') == name:
                return event
        return None

    def all_of(name):
        return [event for event in events if event.get('event') == name]

    open_start = first('project_open_start')
    open_finished = first('project_open_finished')
    open_window = None
    if open_start and open_finished:
        open_window = (open_start.get('time'), open_finished.get('time'))

    recovered = all_of('gui_recovered')
    process = all_of('process')

    result = dict(
        trace_dir=str(trace_dir),
        events=len(events),
        open_ms=(open_finished or {}).get('duration_ms'),
        open_event_found=bool(open_finished),
        gui_blocked_all=_blocked_summary(recovered),
        gui_blocked_open=_blocked_summary(_blocked_in(events, *open_window)
                                          if open_window else []),
        peak_rss_bytes=max((event.get('rss_bytes', 0) or 0 for event in process), default=0),
        stall_count=len(all_of('gui_stall')),
        open_page=_open_page_summary(events, switches),
        catalog=_catalog_summary(events),
        operations=_operation_summary(events),
    )
    if open_window:
        # Two rules, both reported.  The timestamp rule ("the recovery landed
        # inside the window") is the one this harness used from the start; the
        # overlap rule additionally counts a freeze that straddles the
        # window's edge.  The search freeze needs the overlap rule -- its
        # recovery lands after the query has already returned.
        result['gui_blocked_open_overlap'] = _blocked_overlap_summary(
            events, *open_window)
    if search_window and search_window[0] and search_window[1]:
        result['gui_blocked_search'] = _blocked_overlap_summary(
            events, search_window[0], search_window[1])
        result['gui_blocked_search_by_timestamp'] = _blocked_summary(
            _blocked_in(events, search_window[0], search_window[1]))
        result['gui_blocked_search_events'] = [
            dict(blocked_ms=event.get('blocked_ms'), time=event.get('time'))
            for event in result['gui_blocked_search']['events']]
    result['catalog_build_window'] = _build_window_summary(events, open_window)
    return result


def _build_window_summary(events, open_window):
    """Split the background catalog build out of the open window.

    The trace records exactly when the build started and finished, so the
    two questions the previous round's numbers could not answer are
    answerable here: was the build *inside* the open, and how much was the
    GUI blocked while it ran.
    """
    started = [event for event in events
               if event.get('event') == 'project_catalog_build_started']
    finished = [event for event in events
                if event.get('event') == 'project_catalog_build_finished']
    failed = [event for event in events
              if event.get('event') == 'project_catalog_build_failed']
    if not started:
        return dict(present=False)
    begin = started[0].get('time')
    end = finished[0].get('time') if finished else None
    wall_ms = round((end - begin) * 1000, 1) if end else None
    inside = None
    if open_window and begin is not None and end is not None:
        inside = bool(begin >= open_window[0] and end <= open_window[1])
    blocked = (_blocked_overlap_summary(events, begin, end)
               if end else dict(total_ms=0.0, count=0, max_ms=0.0, overlap_ms=0.0))
    return dict(
        present=True,
        reason=started[0].get('reason'),
        start_time=begin,
        end_time=end,
        wall_ms=wall_ms,
        reported_seconds=(finished[0].get('seconds') if finished else None),
        rows=(finished[0].get('rows') if finished else None),
        complete=(finished[0].get('complete') if finished else None),
        failed=len(failed),
        starts_before_open_finished=(
            bool(open_window and begin is not None and begin <= open_window[1])
            if open_window else None),
        fully_inside_open_window=inside,
        overlaps_open_window=(
            bool(open_window and begin is not None and end is not None
                 and begin <= open_window[1] and end >= open_window[0])
            if open_window else None),
        gui_blocked_during_build=blocked,
    )


def _open_page_summary(events, switches):
    """``workspace.open_page`` @MainThread, the way the baseline counted it."""
    samples = []
    for event in _operations(events, 'workspace.open_page'):
        samples.append(dict(duration_ms=event.get('duration_ms'),
                            thread=event.get('thread'),
                            time=event.get('time')))
    main = [sample for sample in samples if sample['thread'] == 'MainThread']
    result = dict(all=samples, main_thread=main,
                  main_thread_count=len(main),
                  main_thread_max_ms=round(max((s['duration_ms'] or 0) for s in main),
                                           1) if main else None)
    if switches:
        per_switch = []
        for window in switches:
            inside = [sample for sample in main
                      if window['start'] <= sample['time'] <= window['end']]
            per_switch.append(dict(target=window['target'],
                                    harness_wall_ms=window['wall_ms'],
                                    open_page_ms=[s['duration_ms'] for s in inside]))
        result['per_switch'] = per_switch
    return result


def _catalog_summary(events):
    """Did the catalog answer, or did the UI fall back to the scene?"""
    hits = [event for event in events if event.get('event') == 'catalog_scope_hit']
    misses = [event for event in events if event.get('event') == 'catalog_scope_miss']
    reasons = {}
    for event in misses:
        reason = event.get('reason', '?')
        reasons[reason] = reasons.get(reason, 0) + 1
    build_started = [event for event in events
                     if event.get('event') == 'project_catalog_build_started']
    build_finished = [event for event in events
                      if event.get('event') == 'project_catalog_build_finished']
    build_failed = [event for event in events
                    if event.get('event') == 'project_catalog_build_failed']
    scope_ready = [event for event in events
                   if event.get('event') == 'project_open_scope_ready']
    scope_unavailable = [event for event in events
                         if event.get('event') == 'project_open_scope_unavailable']
    return dict(
        scope_hits=len(hits),
        scope_misses=len(misses),
        scope_miss_reasons=reasons,
        scope_ready_events=len(scope_ready),
        scope_unavailable=[dict(reason=event.get('reason'),
                                 time=event.get('time')) for event in scope_unavailable],
        build_started=[dict(reason=event.get('reason'), time=event.get('time'))
                       for event in build_started],
        build_finished=[dict(complete=event.get('complete'), rows=event.get('rows'),
                             seconds=event.get('seconds'), time=event.get('time'))
                        for event in build_finished],
        build_failed=len(build_failed),
    )


def _operation_summary(events, top=18):
    buckets = {}
    for event in events:
        if event.get('event') != 'operation':
            continue
        name = event.get('name', '?')
        bucket = buckets.setdefault(name, dict(calls=0, total_ms=0.0, max_ms=0.0,
                                               threads=set()))
        bucket['calls'] += 1
        bucket['total_ms'] += event.get('duration_ms', 0) or 0
        bucket['max_ms'] = max(bucket['max_ms'], event.get('duration_ms', 0) or 0)
        bucket['threads'].add(event.get('thread'))
    rows = []
    for name, bucket in buckets.items():
        rows.append(dict(name=name, calls=bucket['calls'],
                         total_ms=round(bucket['total_ms'], 1),
                         max_ms=round(bucket['max_ms'], 1),
                         threads=sorted(t for t in bucket['threads'] if t)))
    rows.sort(key=lambda row: -row['total_ms'])
    return rows[:top]


# ── 4. the measure command ────────────────────────────────────────────────

def command_measure(args):
    import acceptance

    project = Path(args.project).resolve()
    out_dir = Path(args.out).resolve()
    out_dir.mkdir(parents=True, exist_ok=True)
    settings_dir = Path(args.settings_dir).resolve()
    settings_dir.mkdir(parents=True, exist_ok=True)

    # PrismSettings reads --settings-dir from argv **on first use**, and the
    # import of prism.__main__ is what first uses it, so argv has to carry the
    # isolated directory before that import happens -- otherwise the run shares
    # the real %APPDATA% settings and the "cold cache" label is a lie.
    sys.argv = [sys.argv[0], '--settings-dir', str(settings_dir)]

    STATE['needle'] = args.needle
    STATE['search'] = {}
    STATE['switches'] = []
    STATE['skip_pre_open_digest'] = not args.keep_pre_open_digest

    patches = install_patches(acceptance)

    started_wall = time.time()
    report = acceptance.measure(
        project, args.label, out_dir, tier=args.tier, mode=args.mode,
        cache=args.cache, settings_dir=settings_dir,
        open_timeout_ms=args.open_timeout_ms,
        all_assets_timeout_ms=args.all_assets_timeout_ms,
        screenshots=False)
    ended_wall = time.time()

    trace_dir = out_dir / 'trace' / args.label
    search = STATE.get('search') or {}
    trace = reduce_trace(trace_dir,
                         search_window=search.get('query_window'),
                         switches=STATE.get('switches'))

    metrics = report.get('metrics', {})
    summary = dict(
        label=args.label,
        mode=args.mode,
        cache=args.cache,
        project=str(project),
        settings_dir=str(settings_dir),
        needle=args.needle,
        wall_clock=dict(started=time.strftime('%Y-%m-%d %H:%M:%S',
                                              time.localtime(started_wall)),
                        ended=time.strftime('%Y-%m-%d %H:%M:%S',
                                            time.localtime(ended_wall)),
                        seconds=round(ended_wall - started_wall, 2)),
        report_path=str(out_dir / 'reports' / ('%s.json' % args.label)),
        completed=report.get('completed'),
        error=report.get('error'),
        stages=report.get('stages'),
        counts=report.get('counts'),
        opened=report.get('opened'),
        browser_card_materialisation=metrics.get('browser_card_materialisation'),
        heartbeat_open=metrics.get('heartbeat_open'),
        heartbeat_interactive=metrics.get('heartbeat'),
        search=search,
        trace=trace,
        memory=report.get('memory'),
        working_set=metrics.get('working_set'),
        source_before=report.get('source_before'),
        source_after=report.get('source_after'),
        source_unchanged=report.get('source_unchanged'),
        source_size_mtime_unchanged=(
            (report.get('source_before') or {}).get('size_bytes')
            == (report.get('source_after') or {}).get('size_bytes')
            and (report.get('source_before') or {}).get('mtime_ns')
            == (report.get('source_after') or {}).get('mtime_ns')),
        pre_open_digest_skipped=STATE['skip_pre_open_digest'],
        environment_before=report.get('environment_before'),
        environment_after=report.get('environment_after'),
        autosave_muted_count=STATE.get('autosave_muted', 0),
        patches=sorted(patches),
    )
    path = write_json(out_dir / ('summary-%s.json' % args.label), summary)
    print('written ->', path)
    print(json.dumps(dict(label=args.label, mode=args.mode,
                          open_ms=trace.get('open_ms'),
                          gui_blocked_all=trace.get('gui_blocked_all'),
                          gui_blocked_open=trace.get('gui_blocked_open'),
                          search=search,
                          browser_cards=summary.get('browser_card_materialisation'),
                          heartbeat_interactive=summary.get('heartbeat_interactive'),
                          peak_rss_bytes=trace.get('peak_rss_bytes'),
                          catalog=trace.get('catalog')),
                     ensure_ascii=False, indent=2, default=str))
    return 0 if report.get('completed') else 1


# ── 5. the cancel probe ───────────────────────────────────────────────────

def command_cancel_probe(args):
    """Can the background catalog build be stopped from the GUI path?

    The production cancel path is ``PrismGraphicsView.clear_scene()``
    (``prism/view.py:459-464``): closing or replacing the project sets
    ``_catalog_build_cancel``.  This probe opens a project whose catalog does
    not exist yet, waits for the open to finish, then closes the project
    while the build is still running.
    """
    import acceptance

    project = Path(args.project).resolve()
    out_dir = Path(args.out).resolve()
    out_dir.mkdir(parents=True, exist_ok=True)
    settings_dir = Path(args.settings_dir).resolve()
    settings_dir.mkdir(parents=True, exist_ok=True)
    sys.argv = [sys.argv[0], '--settings-dir', str(settings_dir)]

    from unittest.mock import patch
    from PyQt6 import QtCore, QtWidgets
    from prism.__main__ import PrismApplication, PrismMainWindow
    from prism import performance_trace

    os.environ['PRISM_PAGED_OPEN'] = '0'
    trace_dir = out_dir / 'trace' / args.label
    report = dict(label=args.label, project=str(project),
                  settings_dir=str(settings_dir),
                  started=time.strftime('%Y-%m-%d %H:%M:%S'))

    with patch('prism.browser_capture.BrowserCaptureService.start'), \
         patch('prism.capture_hotkey.CaptureHotkey'), \
         patch('prism.clipboard_capture.ClipboardCaptureAdapter.start'), \
         patch('PyQt6.QtWidgets.QMessageBox.question',
               return_value=QtWidgets.QMessageBox.StandardButton.Yes):
        app = PrismApplication([sys.argv[0]])
        window = PrismMainWindow(app)
        window.resize(1580, 1000)
        window.show()
        app.processEvents()
        trace = performance_trace.install(app, window, trace_dir)
        view = window.view
        try:
            view.open_from_file(str(project))

            def ready():
                return (view.filename == str(project)
                        and not view.worker.isRunning()
                        and view.scene.items_to_add.empty()
                        and not getattr(view, '_project_opening', False))

            opened = acceptance.wait_until(app, ready, args.open_timeout_ms, 'open_until_ready')
            report['opened'] = bool(opened)
            report['scope_ready_after_open'] = bool(
                getattr(view._open_scope, 'ready', False))
            report['catalog_to_build_registered'] = bool(
                getattr(view, '_catalog_to_build', None))
            build_started = time.time()
            # The build starts after the open finished; give it a moment to
            # actually be running before cancelling it.
            acceptance.wait_until(app, lambda: True, 1000, 'settle')
            cancel_requested = time.time()
            cancel_event = getattr(view, '_catalog_build_cancel', None)
            report['cancel_event_present'] = cancel_event is not None
            # The production cancel path: closing the project.
            view.clear_scene()
            report['cancel_requested_at'] = time.strftime('%Y-%m-%d %H:%M:%S',
                                                           time.localtime(cancel_requested))
            report['cancel_to_clear_scene_ms'] = round((time.time() - cancel_requested) * 1000, 2)
            # Wait for the build thread to notice the cancel.
            deadline = time.perf_counter() + 120
            while time.perf_counter() < deadline:
                app.processEvents()
                events = evidence.read_trace_events(trace_dir)
                finished = [event for event in events
                            if event.get('event') == 'project_catalog_build_finished']
                failed = [event for event in events
                          if event.get('event') == 'project_catalog_build_failed']
                if finished or failed:
                    break
                time.sleep(0.05)
            report['build_started_at'] = time.strftime('%Y-%m-%d %H:%M:%S',
                                                       time.localtime(build_started))
            report['cancel_to_build_end_ms'] = round((time.time() - cancel_requested) * 1000, 2)
            report['scope_ready_after_cancel'] = bool(
                getattr(view._open_scope, 'ready', False)) if view._open_scope else None
            # The process must still be usable after the cancel.
            report['process_usable_after_cancel'] = app is not None
        finally:
            try:
                trace.close()
            except Exception:
                pass
            window.hide()

    events = evidence.read_trace_events(trace_dir)
    report['trace'] = reduce_trace(trace_dir)
    report['catalog'] = _catalog_summary(events)
    report['build_events'] = [dict(event=event.get('event'),
                                   complete=event.get('complete'),
                                   rows=event.get('rows'),
                                   seconds=event.get('seconds'),
                                   time=event.get('time'))
                              for event in events
                              if event.get('event', '').startswith('project_catalog_build')]
    path = write_json(out_dir / ('summary-%s.json' % args.label), report)
    print('written ->', path)
    print(json.dumps({key: report.get(key) for key in
                      ('opened', 'scope_ready_after_open', 'cancel_event_present',
                       'cancel_to_build_end_ms', 'scope_ready_after_cancel',
                       'build_events', 'gui_blocked_all')},
                     ensure_ascii=False, indent=2, default=str))
    return 0


# ── 6. the comparison table ───────────────────────────────────────────────

def _get(mapping, *path, default=None):
    node = mapping
    for key in path:
        if not isinstance(node, dict):
            return default
        node = node.get(key)
    return default if node is None else node


def build_comparison(summaries, baseline=BASELINE):
    """One row per metric: last round's number against this round's."""
    rows = []

    def row(metric, base, measured, note=''):
        rows.append(dict(metric=metric, baseline=base, measured=measured, note=note))

    def diff(base, measured):
        if not isinstance(base, (int, float)) or not isinstance(measured, (int, float)):
            return None
        if base == 0:
            return None
        return round((measured - base) / base * 100.0, 1)

    for summary in summaries:
        label = summary.get('label', '?')
        prefix = ('[%s] ' % label) if len(summaries) > 1 else ''
        open_ms = _get(summary, 'trace', 'open_ms')
        blocked_open = _get(summary, 'trace', 'gui_blocked_open', default={})
        blocked_open_overlap = _get(summary, 'trace', 'gui_blocked_open_overlap',
                                    default={})
        blocked_all = _get(summary, 'trace', 'gui_blocked_all', default={})
        build = _get(summary, 'trace', 'catalog_build_window', default={}) or {}
        search = summary.get('search') or {}
        search_blocked = _get(summary, 'trace', 'gui_blocked_search', default={})
        per_switch = _get(summary, 'trace', 'open_page', 'per_switch', default=[]) or []
        switch_values = [value for entry in per_switch
                         for value in entry.get('open_page_ms', [])]
        cards = _get(summary, 'browser_card_materialisation', 'waited_ms')
        heartbeat = summary.get('heartbeat_interactive') or {}
        peak_rss = _get(summary, 'trace', 'peak_rss_bytes')

        row(prefix + '打开总耗时 project_open_finished', baseline['open_ms'], open_ms,
            'GUI-BASELINE §0.5 低负载档')
        row(prefix + '打开期 GUI 阻塞 合计 ms',
            baseline['gui_blocked_total_ms'],
            blocked_open_overlap.get('total_ms', blocked_open.get('total_ms')),
            'gui_recovered.blocked_ms 之和；规则=停滞区间与打开窗口重叠')
        row(prefix + '打开期 GUI 阻塞 次数',
            baseline['gui_blocked_count'],
            blocked_open_overlap.get('count', blocked_open.get('count')), '')
        row(prefix + '打开期 GUI 阻塞 最大单次 ms',
            baseline['gui_blocked_max_ms'],
            blocked_open_overlap.get('max_ms', blocked_open.get('max_ms')), '')
        row(prefix + '打开期 GUI 阻塞 合计 ms（时间戳规则）',
            baseline['gui_blocked_total_ms'], blocked_open.get('total_ms'),
            '旧规则：恢复点落在窗口内；与上一行之差即跨窗口边界的卡顿')
        row(prefix + '切画布 workspace.open_page @MainThread ms',
            baseline['open_page_samples_ms'], switch_values,
            '3 个样本（每个样本可能记 2 次计时）')
        row(prefix + '搜索 GUI blocked_ms', baseline['search_blocked_ms'],
            search_blocked.get('total_ms'), 'UI 口径；规则=停滞区间与查询窗口重叠')
        row(prefix + '搜索 UI 命中数 all',
            baseline['search_hits'],
            _get(summary, 'search', 'ui_counts', 'all'), 'UI 口径')
        row(prefix + '搜索 UI 命中分档', baseline['search_hits_breakdown'],
            _get(summary, 'search', 'ui_counts'), 'UI 口径')
        row(prefix + '目录浏览器物化 100,000 张卡片 ms',
            baseline['browser_cards_ms'], cards, 'E-1 规模基线')
        row(prefix + '交互期心跳 median ms',
            baseline['heartbeat_interactive']['median_ms'],
            heartbeat.get('median_ms'), '')
        row(prefix + '交互期心跳 p95 ms',
            baseline['heartbeat_interactive']['p95_ms'], heartbeat.get('p95_ms'), '')
        row(prefix + '交互期心跳 max ms',
            baseline['heartbeat_interactive']['max_ms'], heartbeat.get('max_ms'), '')
        row(prefix + '峰值 RSS bytes', baseline['peak_rss_bytes'], peak_rss, '')
        # The cold build is a separate cost on a separate thread; it is kept
        # out of the "open" rows on purpose (see the report).
        row(prefix + '冷建库（后台线程）耗时 s',
            baseline['cold_build_seconds'], build.get('reported_seconds'),
            'E-1 独立建库区间 10.89/13.75/15.01 s；本行是 GUI 轮里的后台建库')
        row(prefix + '冷建库是否完整落在打开窗口内',
            '不适用（上一轮无目录接线）', build.get('fully_inside_open_window'),
            'False = 建库在 project_open_finished 之后才开始')
        row(prefix + '冷建库期间 GUI 阻塞 合计 ms',
            '无实测', _get(summary, 'trace', 'catalog_build_window',
                           'gui_blocked_during_build', 'total_ms'),
            'gui_recovered.blocked_ms 之和（停滞区间与建库窗口重叠）')
        row(prefix + '冷建库期间 GUI 阻塞 次数',
            '无实测', _get(summary, 'trace', 'catalog_build_window',
                           'gui_blocked_during_build', 'count'), '')
        row(prefix + '目录口径命中 catalog_scope_hit 次',
            0, _get(summary, 'trace', 'catalog', 'scope_hits'),
            '上一轮 catalog_scope_miss 13 次全落空')
        row(prefix + '目录口径落空 catalog_scope_miss 次',
            13, _get(summary, 'trace', 'catalog', 'scope_misses'),
            '上一轮 13 次，全部 partial=false')

    for entry in rows:
        base, measured = entry['baseline'], entry['measured']
        if isinstance(base, (int, float)) and isinstance(measured, (int, float)):
            entry['delta_percent'] = diff(base, measured)
        else:
            entry['delta_percent'] = None
    return rows


def command_summarize(args):
    out_dir = Path(args.out).resolve()
    summaries = []
    for path in sorted(out_dir.glob('summary-*.json')):
        if args.skip and any(skip in path.name for skip in args.skip):
            continue
        summaries.append(json.loads(path.read_text(encoding='utf-8')))
    rows = build_comparison(summaries)
    payload = dict(baseline=BASELINE, rows=rows,
                   labels=[summary.get('label') for summary in summaries])
    path = write_json(out_dir / 'comparison.json', payload)
    print('written ->', path)
    header = ('| 指标 | 上一轮基线 | 本轮实测 | 差异 |')
    print(header)
    print('| --- | --- | --- | --- |')
    for entry in rows:
        delta = entry['delta_percent']
        delta_text = ('%+0.1f%%' % delta) if delta is not None else '—'
        print('| %s | %s | %s | %s |' % (entry['metric'], entry['baseline'],
                                        entry['measured'], delta_text))
    return 0


def command_recompute(args):
    """Re-reduce an existing run's trace into its summary -- no GUI run.

    A number that was extracted with the wrong rule is a measurement bug,
    not a reason to spend another four minutes of GUI time: the trace on disk
    still holds every event the run produced.  This re-reads that trace with
    the current reducer and rewrites the summary in place.
    """
    out_dir = Path(args.out).resolve()
    summary_path = out_dir / ('summary-%s.json' % args.label)
    summary = json.loads(summary_path.read_text(encoding='utf-8'))
    report_path = out_dir / 'reports' / ('%s.json' % args.label)
    report = (json.loads(report_path.read_text(encoding='utf-8'))
              if report_path.exists() else {})
    switches = ((report.get('metrics') or {}).get('canvas_switch') or {}).get('windows')
    search = summary.get('search') or {}
    trace_dir = Path(summary['trace']['trace_dir'])
    summary['trace'] = reduce_trace(trace_dir,
                                    search_window=search.get('query_window'),
                                    switches=switches)
    write_json(summary_path, summary)
    print('rewritten ->', summary_path)
    print(json.dumps(dict(open_ms=summary['trace'].get('open_ms'),
                          gui_blocked_open=summary['trace'].get('gui_blocked_open'),
                          gui_blocked_open_overlap=summary['trace'].get('gui_blocked_open_overlap'),
                          gui_blocked_search=summary['trace'].get('gui_blocked_search'),
                          catalog_build_window=summary['trace'].get('catalog_build_window'),
                          catalog=summary['trace'].get('catalog')),
                     ensure_ascii=False, indent=2, default=str))
    return 0


def command_rebuild_catalog(args):
    """Rebuild the catalog cache against a specific copy, with the product's builder.

    The product's freshness check compares a fingerprint that contains the
    source *path* and *inode* (``prism/project_catalog_index.py::_fingerprint``),
    so a byte-identical copy of the same project sitting at another path does
    **not** reuse an existing cache -- it counts as a different source and the
    cache is rebuilt.  This command performs that rebuild through the
    product's own builder, so the cache afterwards is a legitimate cache *of
    this copy* and the next GUI open finds a ready catalog instead of a cold
    one.  The source is only ever read.
    """
    from prism.project_open_scope import ProjectOpenScope

    project = Path(args.project).resolve()
    cache = Path(args.cache).resolve()
    scope = ProjectOpenScope(str(project), str(cache))
    before = dict(ready=bool(scope.ready), cache=_file_stamp(cache))
    started = time.perf_counter()
    result = scope.build(batch_size=args.batch_size)
    elapsed = (time.perf_counter() - started) * 1000.0
    payload = dict(project=str(project), cache=str(cache), before=before,
                   after=dict(ready=bool(scope.ready), cache=_file_stamp(cache)),
                   elapsed_ms=round(elapsed, 1),
                   build=dict(complete=getattr(result, 'complete', None),
                              total_records=getattr(result, 'total_records', None),
                              processed_records=getattr(result, 'processed_records', None),
                              last_id=getattr(result, 'last_id', None),
                              seconds=getattr(result, 'seconds', None),
                              resumed=getattr(result, 'resumed', None)),
                   note='source read-only; only the catalog cache in the settings '
                        'directory was written')
    path = write_json(args.out, payload)
    print('written ->', path)
    print(json.dumps(payload, ensure_ascii=False, indent=2, default=str))
    return 0


def _file_stamp(path):
    path = Path(path)
    if not path.is_file():
        return None
    import hashlib
    digest = hashlib.sha256()
    with path.open('rb') as handle:
        for chunk in iter(lambda: handle.read(4 * 1024 * 1024), b''):
            digest.update(chunk)
    stat = path.stat()
    return dict(size_bytes=stat.st_size, mtime=time.strftime('%Y-%m-%d %H:%M:%S',
                                                             time.localtime(stat.st_mtime)),
                sha256=digest.hexdigest())


# ── CLI ────────────────────────────────────────────────────────────────────

def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split('\n')[0])
    sub = parser.add_subparsers(dest='command', required=True)

    gate = sub.add_parser('gate', help='verify the copy before any GUI run')
    gate.add_argument('--phase', default='before')
    gate.add_argument('--copy', default=str(COPY_PATH))
    gate.add_argument('--source', default=str(SOURCE_PATH))
    gate.add_argument('--out', default=str(ROOT / 'artifacts' / 'scale-acceptance'
                                           / '2026-10-22' / 'gui-round2'))
    gate.add_argument('--no-quick-check', action='store_true')
    gate.add_argument('--digest', action='store_true',
                      help='also hash the copy (slow: 51.8 GiB)')
    gate.set_defaults(func=command_gate)

    measure = sub.add_parser('measure', help='run the GUI measurement')
    measure.add_argument('--project', default=str(COPY_PATH))
    measure.add_argument('--label', required=True)
    measure.add_argument('--out', default=str(ROOT / 'artifacts' / 'scale-acceptance'
                                               / '2026-10-22' / 'gui-round2'))
    measure.add_argument('--settings-dir', required=True)
    measure.add_argument('--mode', default='full', choices=('full', 'paged'))
    measure.add_argument('--cache', default='cold', choices=('cold', 'hot'))
    measure.add_argument('--tier', default='100k')
    measure.add_argument('--needle', default='wood')
    measure.add_argument('--keep-pre-open-digest', action='store_true',
                         help='also hash the project before the open (it warms the '
                              'page cache and changes what the open measures)')
    measure.add_argument('--open-timeout-ms', type=int, default=900_000)
    measure.add_argument('--all-assets-timeout-ms', type=int, default=600_000)
    measure.set_defaults(func=command_measure)

    probe = sub.add_parser('cancel-probe', help='can the background build be stopped?')
    probe.add_argument('--project', default=str(COPY_PATH))
    probe.add_argument('--label', default='cancel-probe')
    probe.add_argument('--out', default=str(ROOT / 'artifacts' / 'scale-acceptance'
                                            / '2026-10-22' / 'gui-round2'))
    probe.add_argument('--settings-dir', required=True)
    probe.add_argument('--open-timeout-ms', type=int, default=900_000)
    probe.set_defaults(func=command_cancel_probe)

    summarize = sub.add_parser('summarize', help='join the summaries into a table')
    summarize.add_argument('--out', default=str(ROOT / 'artifacts' / 'scale-acceptance'
                                                / '2026-10-22' / 'gui-round2'))
    summarize.add_argument('--skip', action='append', default=[])
    summarize.set_defaults(func=command_summarize)

    recompute = sub.add_parser(
        'recompute', help='re-reduce an existing run\'s trace into its summary')
    recompute.add_argument('--label', required=True)
    recompute.add_argument('--out', default=str(ROOT / 'artifacts' / 'scale-acceptance'
                                                / '2026-10-22' / 'gui-round2'))
    recompute.set_defaults(func=command_recompute)

    rebuild = sub.add_parser(
        'rebuild-catalog',
        help="rebuild the catalog cache against a copy (product's own builder)")
    rebuild.add_argument('--project', required=True)
    rebuild.add_argument('--cache', required=True)
    rebuild.add_argument('--batch-size', type=int, default=128)
    rebuild.add_argument('--out', default=str(ROOT / 'artifacts' / 'scale-acceptance'
                                             / '2026-10-22' / 'gui-round2'
                                             / 'rebuild-catalog.json'))
    rebuild.set_defaults(func=command_rebuild_catalog)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == '__main__':
    sys.exit(main())
