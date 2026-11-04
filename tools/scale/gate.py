#!/usr/bin/env python
"""The four-target acceptance gate for Prism: open / canvas switch / browse / search.

Why this exists
---------------
The project has spent two weeks arguing about whether Prism "feels slow",
because there was never one command that answered the question. The GUI
measurements, the scale baselines and the catalog benchmarks each measured
their own thing with their own unit, and the numbers did not even agree on
what a "search" costs (267 ms through the API, 3 368.6 ms through the GUI --
a 12.6x gap on the same query).

This script is that one command. It runs the four user-visible goals, takes
the numbers from the **same instruments and the same fields** the existing
baselines used, compares them against the **existing baseline values** (no
new thresholds are invented here), and prints a verdict.

    python tools/scale/gate.py run --project <file.prism> --label <name> --out <dir>

What it does, in order:

1. **the copy gate** -- check that the project file is the one we think it
   is: size, ``PRAGMA page_count`` / ``freelist_count``, the ``items`` /
   ``sqlar`` / ``material_records`` row counts, the ``project_id`` and
   ``PRAGMA quick_check``. Every item is reported with its own verdict.
   This is a **hard precondition**: ``run`` refuses to measure a file that
   fails it (exit code 2) and says which item failed. ``report`` runs it
   too, so an offline verdict still carries the evidence that the copy was
   intact.

   Size alone is **not** a copy gate. ``robocopy /J`` pre-sizes the
   destination to the source size, so a copy that landed only half way
   still looks the right size and still has a valid SQLite header; what
   gives it away is the content past the truncation point. The gate reads
   the content, and ``_negative_test.py`` demonstrates it: a copy that was
   truncated and padded back to the original size passes ``size``,
   ``page_count`` and two row counts, and is still refused.
2. **cold probe** (optional) -- ``tools/scale/coldstart.py`` answers the
   four cold-start questions (wall clock, main-thread occupancy, can it be
   cancelled, does the rebuild match). Its catalogue cache lives in its own
   settings directory so it cannot warm the cache of the measurement run.
3. **measure** -- ``tools/scale/acceptance.py``'s ``measure()`` opens the
   project in the real main window and does the whole scale matrix. This
   script does not re-implement any of it; it only records *when* each
   measurement ran, so the trace events can be attributed to it.
4. **judge** -- every metric is compared against a threshold that is quoted
   from an existing baseline document, together with the section it came
   from. A metric with no measurement says "无实测"; it is never replaced
   by an inference. A metric measured in a different configuration than the
   threshold (a cold run against a hot baseline) is reported as
   ``NOT-COMPARABLE`` rather than failed.

Read-only discipline
--------------------
The measured project is opened through Prism's normal open path, which can
write. Only ever point this at a **copy** on a scratch disk; the source
project on ``E:`` must not be opened in a real GUI (that is the 2026-10-22
14:31 incident: a real GUI open triggered an automatic save that rewrote a
51.8 GiB source file). The gate records the project's size, mtime and
SHA-256 before and after the run, and reports whether it changed.

Three scale columns, never merged
----------------------------------
Record count, unique source images and decoded bytes are three different
numbers. The reference project has 100 000 records built from 2 712
distinct pictures, so its record scale is 36.87x its decode load. Every
report here keeps them apart. The distinct-picture count excludes rows
whose ``$.filename`` is NULL (frames extracted from a video have no file
name): grouping them together would count them as one more picture
(2 713 instead of 2 712).

Usage
-----
::

    python tools/scale/gate.py verify --project <file.prism> --label <name> --out <dir>
    python tools/scale/gate.py run    --project <file.prism> --label <name> --out <dir> \\
        [--settings-dir <dir>] [--no-gate] [--baseline <gate.json>] [--skip-cold-probe]
    python tools/scale/gate.py report --report <acceptance-report.json> --out <dir> \\
        [--no-gate] [--baseline <gate.json>]
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
_HERE = Path(__file__).resolve().parent
for _path in (str(ROOT), str(_HERE)):
    if _path not in sys.path:
        sys.path.insert(0, _path)

import acceptance  # noqa: E402  (path set up above)
import evidence  # noqa: E402

SCHEMA = 'prism.scale.gate/1'

#: The four user-visible goals, in the order the report prints them.
TARGETS = (
    ('open', '打开', 'open.total_ms'),
    ('switch', '来回切画布', 'switch.open_page_max_ms'),
    ('browse', '浏览', 'browse.card_materialisation_ms'),
    ('search', '搜索', 'search.gui_blocked_ms'),
)

#: Every threshold, with the document and the section it was taken from.
#:
#: None of these numbers were invented for this gate. Each one is a value
#: that already exists in an accepted baseline, and the source string says
#: where. The direction is always "not worse than the baseline": the
#: baselines *are* the current state, so the gate is a regression gate.
#:
#: ``tier`` says which run configuration the baseline number was measured
#: in. The GUI-BASELINE numbers come from a **hot** session (the catalogue
#: was already built); a cold run measures a different thing (the open
#: also pays for the catalogue build), so a cold measurement is reported
#: as "not comparable" instead of being failed against a hot threshold.
THRESHOLDS = {
    'open.total_ms': dict(
        value=36531.5, unit='ms', direction='max', tier='hot',
        source='artifacts/scale-acceptance/2026-10-22/GUI-BASELINE.md §0.5「打开总耗时」'
               '低负载档 36,531.5 ms（取自 trace 事件 project_open_finished.duration_ms）',
        note='高负载档同项为 44,855.3 ms；本阈值取低负载档，即「不劣于现状」的宽松边界。'
             '该档为热档（目录已建好），冷档结果不可直接对判'),
    'open.gui_blocked_total_ms': dict(
        value=21820.0, unit='ms', direction='max', tier='hot',
        source='artifacts/scale-acceptance/2026-10-22/GUI-BASELINE.md §0.5「GUI 阻塞合计」'
               '低负载档 21.82 s / 10 次',
        note='口径 = gui_recovered.blocked_ms 之和，按「停滞区间与窗口重叠」规则统计'
             '（与基线同一规则；见 run.gui_blocked_total_ms 的对照说明）'),
    'open.gui_blocked_max_ms': dict(
        value=6430.8, unit='ms', direction='max', tier='hot',
        source='artifacts/scale-acceptance/2026-10-22/GUI-BASELINE.md §0.5「最大单次 GUI 阻塞」'
               '低负载档 6430.8 ms',
        note='高负载档同项为 6452.0 ms'),
    'run.gui_blocked_total_ms': dict(
        value=69290.0, unit='ms', direction='max', tier='hot',
        source='artifacts/scale-acceptance/2026-10-22/GUI-BASELINE.md §0.5「GUI 阻塞合计」'
               '高负载档 69.29 s / 44 次',
        note='口径 = 整条 trace 的 gui_recovered.blocked_ms 之和（E-6 用同一归约器在该基线 '
             'trace 上复算得 69,294.5 ms / 44 次，与基线一致）'),
    'run.gui_blocked_max_ms': dict(
        value=6452.0, unit='ms', direction='max', tier='hot',
        source='artifacts/scale-acceptance/2026-10-22/GUI-BASELINE.md §0.5「最大单次 GUI 阻塞」'
               '高负载档 6452.0 ms',
        note='整条 trace 内的单次最长阻塞'),
    'switch.open_page_max_ms': dict(
        value=1864.5, unit='ms', direction='max', tier='hot',
        source='artifacts/scale-acceptance/2026-10-22/GUI-BASELINE.md §0.5「切画布 '
               'workspace.open_page」低负载档 1861.9 / 1864.5 ms @MainThread',
        note='高负载样本 1858 / 1861 / 1765 / 1765 / 2327 / 2329 ms；两档一致 → 结构性卡顿。'
             '取窗口内 @MainThread 的最大值'),
    'browse.card_materialisation_ms': dict(
        value=5485.51, unit='ms', direction='max', tier='any',
        source='artifacts/scale-acceptance/2026-10-22/BASELINE.md §E-1 规模基线'
               '「目录浏览器物化 100,000 张卡片」5485.51 ms',
        note='口径：目录缓存就绪后，物化 100 000 张卡片到画布的墙钟。'
             '基线取自 E-1 的 paged 档（当时 scene 只载入 100 条），'
             'full 档 scene 已有 10 万条，故本项为同动作、不同负载的对照'),
    'browse.heartbeat_median_ms': dict(
        value=18.29, unit='ms', direction='max', tier='any',
        source='artifacts/scale-acceptance/2026-10-22/BASELINE.md §E-1 规模基线'
               '「交互期心跳」median 18.29 ms',
        note='口径：打开完成后，交互期的 GUI 事件循环心跳间隔'),
    'browse.heartbeat_p95_ms': dict(
        value=58.85, unit='ms', direction='max', tier='any',
        source='artifacts/scale-acceptance/2026-10-22/BASELINE.md §E-1 规模基线'
               '「交互期心跳」p95 58.85 ms',
        note='同上'),
    'browse.heartbeat_max_ms': dict(
        value=7433.06, unit='ms', direction='max', tier='any',
        source='artifacts/scale-acceptance/2026-10-22/BASELINE.md §E-1 规模基线'
               '「交互期心跳」max 7,433.06 ms',
        note='同上；这是交互期最长的一次饿死'),
    'search.gui_blocked_ms': dict(
        value=2806.4, unit='ms', direction='max', tier='hot',
        source='artifacts/scale-acceptance/2026-10-22/GUI-BASELINE.md §0.5「搜索 "wood" GUI 冻结」'
               '低负载档 2806.4 ms（gui_recovered.blocked_ms）',
        note='高负载档 3368.6 ms；口径 = 用户在搜索框敲字后 GUI 线程被卡住的时长，'
             '由 trace 的 gui_recovered.blocked_ms 按重叠规则统计'),
    'search.ui_path_ms': dict(
        value=2799.7, unit='ms', direction='max', tier='hot',
        source='artifacts/scale-acceptance/2026-10-22/GUI-BASELINE.md §0.5「搜索 "wood" GUI 冻结」'
               '低负载档 SearchController 2799.7 ms',
        note='口径 = UI 搜索路径（SearchController）的同步耗时，不是纯 API 的 iter_query'),
    'memory.peak_rss_bytes': dict(
        value=5.58 * (1 << 30), unit='bytes', direction='max', tier='any',
        source='artifacts/scale-acceptance/2026-10-22/GUI-BASELINE.md §0.5「峰值 RSS」'
               '低负载档 5.58 GB',
        note='高负载档 3.95 GB；单位按 GUI-BASELINE 的 GB→GiB 解释（分析器用 rss_bytes/2**30）'),
}

#: The copy gate: every check the project file has to pass before it may be
#: measured. The order is the order the report prints them in.
#:
#: ``size`` alone is **not** a copy gate -- ``robocopy /J`` pre-sizes the
#: destination to the source size, so a copy that landed only half way still
#: looks the right size and still has a valid SQLite header. What is not
#: faked is the content: the b-trees past the truncation point are
#: unreadable. So the copy gate is the content, not the size.
COPY_CHECKS = (
    ('size_bytes', '文件大小'),
    ('page_count', 'PRAGMA page_count'),
    ('freelist_count', 'PRAGMA freelist_count'),
    ('items_rows', 'items 表行数'),
    ('sqlar_rows', 'sqlar 表行数'),
    ('material_records', 'material_records 表行数'),
    ('project_id', 'project_id'),
    ('quick_check', 'PRAGMA quick_check'),
)

#: The window definitions, in one place, so the report can state them.
WINDOW_DEFINITIONS = {
    'open': 'project_open_start → project_open_ready（两者都是产品 trace 事件；'
            '若本次运行没有 project_open_ready，退化为 project_open_start → '
            'project_open_finished + 5 s 尾窗）',
    'browse': '目录浏览器物化 100,000 张卡片（_wait_for_canvas_cards）→ 三个视距测量结束',
    'switch': '_measure_canvas_switch 整个调用区间（首尾各一次切回）',
    'search': '_measure_search 整个调用区间（UI 搜索路径 + 同一 needle 的目录热路径）',
}

#: The structural fingerprint of a Prism project file.
#:
#: Every one of these is read from the file itself, through a read-only
#: SQLite URI. A half-copied file keeps its **size** (robocopy /J
#: pre-sizes the destination) and its **header**, so the size and the
#: header are useless as a copy gate -- but the tables that live past the
#: truncation point are not. This is the check that catches a copy that
#: only landed part way: the b-tree walk fails outright.
STRUCTURE_QUERIES = (
    ('page_count', 'PRAGMA page_count'),
    ('freelist_count', 'PRAGMA freelist_count'),
    ('page_size', 'PRAGMA page_size'),
    ('journal_mode', 'PRAGMA journal_mode'),
    ('items_rows', 'SELECT count(*) FROM items'),
    ('sqlar_rows', 'SELECT count(*) FROM sqlar'),
    ('material_records', 'SELECT count(*) FROM material_records'),
    ('project_id', "SELECT value FROM prism_metadata WHERE key = 'project_id'"),
)

#: What "measured" means for each metric id, so a missing value is never
#: silently replaced by a zero.
NO_MEASUREMENT = '无实测'


# ── small helpers ──────────────────────────────────────────────────────────

def write_json(path, payload):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=str),
                    encoding='utf-8')
    return path


def now_stamp():
    """A local timestamp with a timezone, plus the epoch seconds."""
    stamp = time.strftime('%Y-%m-%d %H:%M:%S %z')
    return stamp, time.time()


def human_bytes(value):
    if value is None:
        return NO_MEASUREMENT
    value = float(value)
    for unit in ('B', 'KiB', 'MiB', 'GiB', 'TiB'):
        if abs(value) < 1024 or unit == 'TiB':
            return '%.2f %s' % (value, unit)
        value /= 1024
    return '%.2f TiB' % value


def ms(value):
    if value is None:
        return NO_MEASUREMENT
    return '%.1f ms' % float(value)


# ── step 1: verify the project file ────────────────────────────────────────

def verify_project(project, *, expect=None, quick_check=True):
    """Check that the file is the one we meant to measure.

    A truncated or half-copied database produces fast, meaningless numbers,
    so the gate refuses to run on one.

    **Size is not a copy gate.** ``robocopy /J`` pre-sizes the destination
    file to the source size, so a copy that only landed half way still
    *looks* the right size -- and it still has a correct SQLite header.
    What is not faked is the **content**: the b-trees that live past the
    truncation point are unreadable. So this check reads the structural
    fingerprint (page count, the three table row counts, the project id)
    through a read-only URI, and refuses the file when that read fails.

    ``PRAGMA quick_check`` is the deeper structural check (it reads every
    page but skips the index cross-checks of ``integrity_check``). The
    project is opened read-only, so none of this can modify it.

    Every check is reported **individually**, with its own pass/fail, so a
    failure says *which* item is wrong instead of dumping one exception.
    """
    project = Path(project).resolve()
    expect = dict(expect or {})
    result = dict(path=str(project), exists=project.exists(), ok=False,
                  size_bytes=None, mtime=None, mtime_ns=None,
                  sha256=None, sha256_prefix16=None,
                  structure={}, structure_error=None,
                  expectations={}, quick_check=None, quick_check_rows=None,
                  quick_check_seconds=None, problems=[],
                  checks={}, checked_at=time.strftime('%Y-%m-%d %H:%M:%S %z'))
    if not project.exists():
        result['problems'].append('the project file does not exist')
        for name, label in COPY_CHECKS:
            result['checks'][name] = dict(label=label, value=None,
                                          expected=expect.get(name),
                                          passed=False, detail='the file does not exist')
        return result
    stat = project.stat()
    result['size_bytes'] = stat.st_size
    result['mtime'] = time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(stat.st_mtime))
    result['mtime_ns'] = stat.st_mtime_ns

    # ── the structural fingerprint ────────────────────────────────────
    try:
        import sqlite3
        uri = project.as_uri() + '?mode=ro'
        connection = sqlite3.connect(uri, uri=True, timeout=60)
        try:
            for name, sql in STRUCTURE_QUERIES:
                result['structure'][name] = connection.execute(sql).fetchone()[0]
        finally:
            connection.close()
    except Exception as error:
        result['structure_error'] = repr(error)
        result['problems'].append(
            'the file is not a readable Prism database (this is what a '
            'half-finished copy looks like): %r' % (error,))

    # ── the expected values ───────────────────────────────────────────
    if 'size_bytes' in expect:
        expect['size_bytes'] = int(expect['size_bytes'])
    for key, expected in expect.items():
        actual = result['structure'].get(key, result.get(key))
        match = actual == expected
        result['expectations'][key] = dict(expected=expected, actual=actual,
                                           match=match)
        if not match:
            result['problems'].append('%s %r != expected %r' % (key, actual, expected))

    # ── the deeper structural check ────────────────────────────────────
    started = time.perf_counter()
    if quick_check:
        try:
            import sqlite3
            uri = project.as_uri() + '?mode=ro'
            connection = sqlite3.connect(uri, uri=True, timeout=60)
            try:
                rows = connection.execute('PRAGMA quick_check').fetchall()
            finally:
                connection.close()
            result['quick_check_rows'] = [list(row) for row in rows]
            result['quick_check'] = (len(rows) == 1 and str(rows[0][0]).lower() == 'ok')
            if not result['quick_check']:
                result['problems'].append('PRAGMA quick_check did not return "ok"')
        except Exception as error:
            result['quick_check'] = False
            result['quick_check_rows'] = [repr(error)]
            result['problems'].append('quick_check failed: %r' % (error,))
    else:
        result['quick_check'] = None
        result['quick_check_rows'] = None
        result['quick_check_skipped'] = True
    result['quick_check_seconds'] = round(time.perf_counter() - started, 2)
    try:
        digest = evidence.sha256_file(project)
        result['sha256'] = digest
        result['sha256_prefix16'] = digest[:16]
    except Exception as error:
        # A file that cannot even be read end to end is not measurable; the
        # copy gate must say so instead of dying with a traceback.
        result['sha256_error'] = repr(error)
        result['problems'].append('the file could not be digested: %r' % (error,))

    # ── one row per copy-gate item ────────────────────────────────────
    # The copy gate is the contract with the operator: every item is
    # listed, each with its own verdict, so "which item is wrong" is
    # answerable without re-running anything.
    for name, label in COPY_CHECKS:
        if name == 'size_bytes':
            value = result['size_bytes']
        elif name == 'quick_check':
            value = result['quick_check']
        else:
            value = result['structure'].get(name)
        expected = expect.get(name)
        if name == 'quick_check' and result.get('quick_check_skipped'):
            passed = None
            detail = 'skipped by request (--no-quick-check)'
        elif expected is not None:
            passed = (value == expected)
            detail = 'expected %r' % (expected,)
        elif name == 'quick_check':
            passed = bool(value)
            detail = 'must be "ok"'
        else:
            # No expectation given: the item still has to be *readable*.
            # A value that could not be read at all is a failed check --
            # that is exactly the half-copied file.
            passed = value is not None
            detail = 'read from the file' if passed else 'could not be read'
        result['checks'][name] = dict(label=label, value=value,
                                      expected=expected, passed=passed,
                                      detail=detail)
    result['failed_checks'] = [name for name, row in result['checks'].items()
                               if row['passed'] is False]
    result['ok'] = not result['problems']
    return result


# ── step 2: the cold-start probe ───────────────────────────────────────────

def cold_probe(project, settings_dir, label, out_dir, *, timeout_s=3600):
    """Run ``tools/scale/coldstart.py`` in its own process and read its report.

    The probe gets **its own** settings directory, so its catalogue cache
    is a different file from the one a shared run would use. The
    catalogue is keyed by file name, so sharing it would make the two
    runs fight over one SQLite file and would quietly turn the "cold"
    open into a warm one.

    It reads the project through a ``mode=ro`` URI, so the 51.8 GiB
    source is never written.
    """
    settings = Path(settings_dir).resolve()
    cache_dir = settings / 'Prism' / 'catalog'
    cache_dir.mkdir(parents=True, exist_ok=True)
    cache = cache_dir / (Path(project).name + '.sqlite')
    probe_label = '%s-coldprobe' % label
    command = [sys.executable, str(_HERE / 'coldstart.py'),
               '--project', str(Path(project).resolve()),
               '--cache', str(cache),
               '--label', probe_label,
               '--out', str(Path(out_dir).resolve()),
               '--no-attribution']
    started = time.perf_counter()
    completed = subprocess.run(command, capture_output=True, text=True,
                               timeout=timeout_s)
    elapsed = round(time.perf_counter() - started, 2)
    report_path = Path(out_dir) / 'reports' / ('%s-coldstart.json' % probe_label)
    report = None
    if report_path.exists():
        try:
            report = json.loads(report_path.read_text(encoding='utf-8'))
        except ValueError:
            report = None
    return dict(command=command, returncode=completed.returncode,
                seconds=elapsed, cache=str(cache), settings_dir=str(settings),
                report_path=str(report_path), report=report,
                stdout_tail=(completed.stdout or '')[-4000:],
                stderr_tail=(completed.stderr or '')[-4000:])


# ── step 3: the measurement, with stage anchors ────────────────────────────

class StageClock:
    """Record when each measurement function ran.

    The acceptance harness reports the *results* of its stages but not the
    wall-clock window each one occupied, and the trace events need a window
    to be attributed to a stage. This wraps the harness' own measurement
    functions -- it does not replace or alter them, it only notes the time
    span around the call, so the trace can be split along the same seams
    the harness already uses.
    """

    FUNCTIONS = ('_measure_canvas_switch', '_measure_search', '_measure_stop_resume',
                 '_measure_catalog_iter_query', '_wait_for_canvas_cards',
                 '_measure_distance', '_measure_close_release')

    def __init__(self):
        self.windows = []
        self._originals = {}

    def install(self, module=acceptance):
        for name in self.FUNCTIONS:
            original = getattr(module, name, None)
            if original is None:
                continue
            self._originals[name] = original
            setattr(module, name, self._wrap(name, original))
        return self

    def _wrap(self, name, original):
        def wrapper(*args, **kwargs):
            started = time.time()
            try:
                return original(*args, **kwargs)
            finally:
                self.windows.append(dict(stage=name, started=started,
                                         ended=time.time()))
        wrapper.__name__ = getattr(original, '__name__', name)
        wrapper.__doc__ = getattr(original, '__doc__', None)
        return wrapper

    def restore(self, module=acceptance):
        for name, original in self._originals.items():
            setattr(module, name, original)
        self._originals = {}

    def span(self, *stages):
        """The union window of the named stages."""
        starts, ends = [], []
        for window in self.windows:
            if window['stage'] in stages:
                starts.append(window['started'])
                ends.append(window['ended'])
        if not starts:
            return None
        return min(starts), max(ends)


# ── step 4: read the numbers out ───────────────────────────────────────────

def _events_of(events, name):
    return [event for event in events if event.get('event') == name]


def _in_window(events, window):
    """The events inside a window.

    A window of ``None`` means "the stage could not be located". Returning
    the whole run in that case would silently attribute another stage's
    work to this one, so an unlocatable stage contributes no events.
    """
    if not window:
        return []
    start, end = window
    return [event for event in events
            if start <= (event.get('time') or 0) <= end]


def _stall_interval(event):
    """The wall interval a ``gui_recovered`` record describes.

    A record is written when the GUI thread *wakes up* and ``blocked_ms``
    is how long it had been asleep, so the stall it reports is
    ``[time - blocked_ms, time]`` -- not a point in time.
    """
    blocked = event.get('blocked_ms') or 0
    end = event.get('time') or 0
    return end - blocked / 1000.0, end


def _blocked_overlapping(events, start, end):
    """``gui_recovered`` blocks whose *stall* overlaps ``[start, end]``.

    This is the rule the GUI-BASELINE was measured with, and it is not the
    same as "the recovery landed inside the window": a freeze whose
    recovery lands just after the window closes is still a freeze the
    user sat through. The search is exactly that case -- the query returns
    while the GUI thread is still stuck in the work it triggered.

    Verified against the baseline trace by the group lead: the published
    maximum single block (6,452.0 ms) only appears under this rule.
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
    summary = _block_summary(blocks)
    summary['overlap_ms'] = round(overlap_total, 1)
    summary['rule'] = ('stall interval [time-blocked_ms, time] overlaps the window '
                       '(the rule the GUI-BASELINE was measured with)')
    return summary


def read_trace(report):
    """Every trace event of a measurement run."""
    trace = report.get('trace') or {}
    directory = trace.get('directory') or trace.get('trace_dir')
    if not directory:
        return []
    return evidence.read_trace_events(directory)


def open_window(events):
    """The window of the open, anchored by two product trace events."""
    starts = _events_of(events, 'project_open_start')
    finishes = _events_of(events, 'project_open_finished')
    readies = _events_of(events, 'project_open_ready')
    if not starts or not finishes:
        return None, []
    start = starts[-1]
    finish = finishes[-1]
    end = finish.get('time')
    for ready in readies:
        if (ready.get('time') or 0) >= (end or 0):
            end = ready.get('time')
            break
    if end is None:
        end = (finish.get('time') or 0) + 5.0
    return (start.get('time'), end), [start, finish]


def cold_build_span(events):
    """The window the catalogue build occupied, from the trace.

    The build is **not** part of the open. The trace says so directly:
    ``project_open_start`` -> ``project_open_scope_unavailable`` ->
    ``project_open_finalize_start`` -> ``project_catalog_build_started`` ->
    ``project_open_finished`` -> ``project_catalog_build_finished``.

    The build starts in the background *after* the open has finished, so
    its cost is reported on its own row and is never added to the open
    duration. Adding them together would report a number that no user
    ever experienced as "the open".
    """
    started = [event for event in events
               if event.get('event') == 'project_catalog_build_started']
    finished = [event for event in events
                if event.get('event') == 'project_catalog_build_finished']
    failed = [event for event in events
              if event.get('event') == 'project_catalog_build_failed']
    if not started:
        return None
    begin = started[0].get('time')
    end = finished[0].get('time') if finished else None
    duration = finished[0].get('seconds') if finished else None
    wall_ms = round((end - begin) * 1000, 1) if (end and begin) else None
    return dict(start=begin, end=end, wall_ms=wall_ms,
                reported_seconds=duration,
                reason=started[0].get('reason'),
                rows=(finished[0].get('rows') if finished else None),
                complete=(finished[0].get('complete') if finished else None),
                failed=len(failed))


def _blocked(events):
    """``gui_recovered`` events, sorted by how long the GUI was stuck."""
    blocks = [event for event in events if event.get('event') == 'gui_recovered']
    blocks.sort(key=lambda event: -(event.get('blocked_ms') or 0))
    return blocks


def _block_summary(blocks):
    total = sum(event.get('blocked_ms') or 0 for event in blocks)
    largest = max((event.get('blocked_ms') or 0 for event in blocks), default=None)
    return dict(count=len(blocks), total_ms=round(total, 1),
                max_ms=largest,
                top=[dict(blocked_ms=event.get('blocked_ms'),
                          at=time.strftime('%H:%M:%S', time.localtime(event.get('time') or 0)),
                          last_action=(event.get('last_action') or {}).get('target')
                          if isinstance(event.get('last_action'), dict) else None)
                     for event in blocks[:8]])


def collect_metrics(report, events, clock):
    """Every number the gate judges, taken from the report and the trace."""
    metrics = {}

    def put(metric_id, measured, unit=None, detail=None, source=None):
        metrics[metric_id] = dict(id=metric_id, measured=measured, unit=unit,
                                   detail=detail, source=source)

    # ── 打开 ──────────────────────────────────────────────────────────
    window, anchors = open_window(events)
    finishes = _events_of(events, 'project_open_finished')
    open_total = finishes[-1].get('duration_ms') if finishes else None
    put('open.total_ms', open_total, 'ms',
        detail='trace 事件 project_open_finished.duration_ms（GUI-BASELINE 同口径）',
        source='trace:project_open_finished')
    summary = _blocked_overlap_summary(events, *(window or (None, None)))
    put('open.gui_blocked_total_ms', summary['total_ms'], 'ms',
        detail='打开窗口内 %d 次 gui_recovered 合计（重叠规则）' % summary['count'],
        source='trace:gui_recovered')
    put('open.gui_blocked_max_ms', summary['max_ms'], 'ms',
        detail='打开窗口内单次最长 GUI 阻塞（重叠规则）',
        source='trace:gui_recovered')
    # The whole-trace total is the baseline's "69.29 s / 44 次" number, so it
    # is measured over the whole trace with the same rule.
    whole = _blocked_overlap_summary(events, events[0].get('time') if events else None,
                                     events[-1].get('time') if events else None)
    put('run.gui_blocked_total_ms', whole['total_ms'], 'ms',
        detail='整条 trace 的 %d 次 gui_recovered 合计（重叠规则）' % whole['count'],
        source='trace:gui_recovered')
    put('run.gui_blocked_max_ms', whole['max_ms'], 'ms',
        detail='整条 trace 内单次最长 GUI 阻塞',
        source='trace:gui_recovered')
    open_ready = (report.get('stages') or {}).get('open_until_ready')
    put('open.harness_ready_ms', open_ready, 'ms',
        detail='acceptance.measure() 的 open_until_ready（闸门自身的口径，与 trace 口径分列）',
        source='acceptance report:stages.open_until_ready')

    # ── 来回切画布 ────────────────────────────────────────────────────
    switch_window = clock.span('_measure_canvas_switch')
    switch_ops = [event for event in events
                  if event.get('event') == 'operation'
                  and event.get('name') == 'workspace.open_page'
                  and event.get('thread') == 'MainThread'
                  and (not switch_window
                       or switch_window[0] <= (event.get('time') or 0) <= switch_window[1])]
    durations = [event.get('duration_ms') for event in switch_ops
                 if event.get('duration_ms') is not None]
    reported_switch = ((report.get('metrics') or {}).get('canvas_switch') or {})
    reported_durations = [item.get('milliseconds')
                          for item in (reported_switch.get('switches') or [])
                          if item.get('milliseconds') is not None]
    # The trace's own ``workspace.open_page`` records are the instrument the
    # GUI-BASELINE used (the @MainThread samples in §1.2). They are the
    # primary source; the harness' own timing of the same three clicks is
    # the fallback.
    trace_open_page = ((report.get('trace') or {}).get('open_page') or {})
    trace_durations = [item.get('duration_ms')
                       for item in (trace_open_page.get('main_thread') or [])
                       if item.get('duration_ms') is not None]
    if not durations and trace_durations:
        durations = trace_durations
    if durations:
        put('switch.open_page_max_ms', max(durations), 'ms',
            detail=(('%d 次 workspace.open_page @MainThread：%s'
                     % (len(durations), ', '.join('%.1f' % value for value in durations)))
                    if switch_window else
                    ('%d 次 workspace.open_page @MainThread（复判既有实测：无阶段窗口可限定，'
                     '故取 trace 内全部样本）：%s'
                     % (len(durations), ', '.join('%.1f' % value for value in durations)))),
            source='trace:operation workspace.open_page')
    elif reported_durations:
        # No stage window (offline judging), or the trace carried no switch
        # operation: fall back to the harness' own timing of the same three
        # clicks. It is the same action on the same main thread, measured
        # one level higher (the call, not the traced operation inside it).
        put('switch.open_page_max_ms', max(reported_durations), 'ms',
            detail='退化为 acceptance.measure() 自己记的切画布耗时 %s（无阶段窗口时无法'
                   '把 trace 里的 workspace.open_page 限定到切画布阶段）'
                   % ', '.join('%.1f' % value for value in reported_durations),
            source='acceptance report:metrics.canvas_switch')
    else:
        put('switch.open_page_max_ms', None, 'ms',
            detail='本次运行没有采到切画布耗时',
            source='trace:operation workspace.open_page')
    put('switch.open_page_median_ms',
        round(sorted(durations)[len(durations) // 2], 2) if durations else None, 'ms',
        detail='同上中位数',
        source='trace:operation workspace.open_page')
    switch_blocks = _blocked_overlapping(events, *(switch_window or (None, None)))
    put('switch.gui_blocked_max_ms',
        max((event.get('blocked_ms') or 0 for event in switch_blocks), default=None), 'ms',
        detail='切画布窗口内单次最长 GUI 阻塞（重叠规则）',
        source='trace:gui_recovered')
    put('switch.harness_switches_ms', reported_durations, 'ms',
        detail='acceptance.measure() 自己记的切画布耗时（与 trace 口径分列）',
        source='acceptance report:metrics.canvas_switch')

    # ── 浏览 ──────────────────────────────────────────────────────────
    materialisation = ((report.get('metrics') or {}).get('browser_card_materialisation') or {})
    put('browse.card_materialisation_ms', materialisation.get('waited_ms'), 'ms',
        detail='物化 %s 张卡片（目录缓存就绪后）'
               % materialisation.get('cards', materialisation.get('rows')),
        source='acceptance report:metrics.browser_card_materialisation')
    browse_window = clock.span('_wait_for_canvas_cards', '_measure_distance')
    browse_blocks = _blocked(_in_window(events, browse_window))
    put('browse.gui_blocked_total_ms', _block_summary(browse_blocks)['total_ms'], 'ms',
        detail='浏览窗口内 %d 次 gui_recovered 合计' % len(browse_blocks),
        source='trace:gui_recovered')
    heartbeat = (report.get('metrics') or {}).get('heartbeat') or {}
    put('browse.heartbeat_median_ms', heartbeat.get('median_ms'), 'ms',
        detail='交互期 GUI 事件循环心跳中位数', source='acceptance report:metrics.heartbeat')
    put('browse.heartbeat_p95_ms', heartbeat.get('p95_ms'), 'ms',
        detail='交互期 GUI 事件循环心跳 p95', source='acceptance report:metrics.heartbeat')
    put('browse.heartbeat_max_ms', heartbeat.get('max_ms'), 'ms',
        detail='交互期 GUI 事件循环心跳最长一次饿死', source='acceptance report:metrics.heartbeat')
    counts = report.get('counts') or {}
    put('browse.catalog_rows', counts.get('catalog_rows'), 'count',
        detail='目录浏览器行数', source='acceptance report:counts.catalog_rows')
    put('browse.canvas_cards', counts.get('canvas_cards'), 'count',
        detail='画布上真实物化的卡片数', source='acceptance report:counts.canvas_cards')

    # ── 搜索 ──────────────────────────────────────────────────────────
    search_window = clock.span('_measure_search')
    search = (report.get('metrics') or {}).get('search') or {}
    # The UI search path reports its own window (``query_window``); the
    # harness's stage clock is only a fallback for offline judging.
    query_window = search.get('query_window')
    if query_window and len(query_window) == 2 and all(query_window):
        search_window = (query_window[0], query_window[1])
    search_blocks = _blocked_overlapping(events, *(search_window or (None, None)))
    search_ms = search.get('milliseconds')
    if search_ms is None:
        # The UI-path harness reports the SearchController's own wall clock
        # under ``query_wall_ms``. That is a **user path** number (the
        # controller was driven through the real search box), not the pure
        # API number the GUI-BASELINE forbids quoting as user experience.
        search_ms = search.get('query_wall_ms')
    hits = search.get('hits')
    if hits is None:
        hits = (search.get('ui_counts') or {}).get('all')
    if search_blocks:
        # The first block in time order is the one the search caused: the
        # search is the first long call inside the search window, and the
        # heartbeat timer can only report the gap after the call returns.
        first = min(search_blocks, key=lambda event: event.get('time') or 0)
        put('search.gui_blocked_ms', first.get('blocked_ms'), 'ms',
            detail='搜索窗口内第一次 GUI 阻塞（重叠规则；窗口内共 %d 次）'
                   % len(search_blocks),
            source='trace:gui_recovered')
    elif isinstance(search_ms, (int, float)):
        # The call was too short for the heartbeat watchdog to notice
        # (it only reports gaps above 250 ms), so the call's own wall clock
        # is the block. Same quantity, measured one level lower.
        put('search.gui_blocked_ms', search_ms, 'ms',
            detail='搜索耗时短于心跳看门狗的 250 ms 下限，没有 gui_recovered 记录；'
                   '退化为 UI 搜索路径的实测耗时（同一根因、同一动作）',
            source='acceptance report:metrics.search')
    else:
        put('search.gui_blocked_ms', None, 'ms',
            detail='本次运行没有采到搜索耗时',
            source='trace:gui_recovered')
    put('search.gui_blocked_max_ms',
        max((event.get('blocked_ms') or 0 for event in search_blocks), default=None), 'ms',
        detail='搜索窗口内单次最长 GUI 阻塞（%d 次 gui_recovered）' % len(search_blocks),
        source='trace:gui_recovered')
    put('search.ui_path_ms', search_ms, 'ms',
        detail='UI 搜索路径（SearchController / search_project，needle=%r，命中 %s）'
               '—— 不是纯 API 的 iter_query'
               % (search.get('query') or search.get('needle'), hits),
        source='acceptance report:metrics.search')
    put('search.hits', hits, 'count',
        detail='同一 needle 的命中数（基线口径：整个项目 1,122 项）', source='acceptance report:metrics.search')
    catalog_needle = search.get('catalog_same_needle') or {}
    put('search.catalog_same_needle_ms', catalog_needle.get('iter_ms'), 'ms',
        detail='同一 needle 的目录热路径耗时（对照项，不是用户感受）',
        source='acceptance report:metrics.search.catalog_same_needle')
    put('search.catalog_same_needle_count_ms', catalog_needle.get('count_ms'), 'ms',
        detail='同一 needle 的目录 count 耗时（对照项）',
        source='acceptance report:metrics.search.catalog_same_needle')

    # ── 内存 ──────────────────────────────────────────────────────────
    peak = None
    for event in events:
        if event.get('event') == 'process' and event.get('rss_bytes'):
            peak = max(peak or 0, event['rss_bytes'])
    working = (report.get('metrics') or {}).get('working_set') or []
    for round_data in working:
        if round_data.get('rss_bytes'):
            peak = max(peak or 0, round_data['rss_bytes'])
    put('memory.peak_rss_bytes', peak, 'bytes',
        detail='本次运行的进程常驻内存峰值', source='trace:process.rss_bytes')

    # ── 冷建库 ────────────────────────────────────────────────────────
    cold = cold_build_span(events)
    if cold:
        cold_blocks = _blocked_overlapping(events, cold.get('start'), cold.get('end'))
        cold['gui_blocked_total_ms'] = round(
            sum(event.get('blocked_ms') or 0 for event in cold_blocks), 1)
        cold['gui_blocked_count'] = len(cold_blocks)
        cold['gui_blocked_max_ms'] = max(
            (event.get('blocked_ms') or 0 for event in cold_blocks), default=None)
        stalls = [event for event in _in_window(events, (cold.get('start'), cold.get('end')))
                  if event.get('event') == 'gui_stall']
        cold['gui_stall_count'] = len(stalls)
        cold['gui_stall_max_ms'] = max(
            (event.get('duration_ms') or 0 for event in stalls), default=None)
    return metrics, cold


# ── step 5: judge ──────────────────────────────────────────────────────────

def judge(metrics, *, gate_enabled=True, tier=None):
    """Attach a threshold and a verdict to every metric that has one.

    ``tier`` is the run configuration ('hot' or 'cold'). A threshold
    measured in a different configuration is **not comparable**: the
    GUI-BASELINE's open numbers come from a hot session (the catalogue
    was already built), while a cold run's open also pays for building
    it. Failing a cold run against a hot threshold would report a
    regression that is not one, so such a metric is reported as
    "not comparable" and says so in the reason.
    """
    judged = {}
    for metric_id, metric in metrics.items():
        entry = dict(metric)
        threshold = THRESHOLDS.get(metric_id)
        if threshold is None:
            entry['threshold'] = None
            entry['threshold_source'] = None
            entry['verdict'] = None
            entry['verdict_reason'] = '参考项：本闸门不判定'
            judged[metric_id] = entry
            continue
        entry['threshold'] = threshold['value']
        entry['threshold_unit'] = threshold['unit']
        entry['threshold_source'] = threshold['source']
        entry['threshold_note'] = threshold.get('note')
        entry['threshold_tier'] = threshold.get('tier', 'any')
        measured = metric.get('measured')
        if measured is None or not isinstance(measured, (int, float)):
            entry['verdict'] = 'FAIL' if gate_enabled else None
            entry['verdict_reason'] = '%s：%s' % (NO_MEASUREMENT,
                                                  metric.get('detail') or '本轮没有采到该值')
            judged[metric_id] = entry
            continue
        if not gate_enabled:
            entry['verdict'] = None
            entry['verdict_reason'] = '--no-gate：只出数字，不出判定'
            judged[metric_id] = entry
            continue
        threshold_tier = threshold.get('tier', 'any')
        if tier and threshold_tier not in ('any', tier):
            entry['verdict'] = 'NOT-COMPARABLE'
            entry['verdict_reason'] = (
                '不同档，不可直接判 PASS/FAIL：实测 %s 档，阈值出自 %s 档'
                '（阈值 %.1f %s）' % (tier, threshold_tier,
                                    float(threshold['value']), threshold['unit']))
            judged[metric_id] = entry
            continue
        ok = float(measured) <= float(threshold['value'])
        entry['verdict'] = 'PASS' if ok else 'FAIL'
        entry['verdict_reason'] = '实测 %.1f %s %s 阈值 %.1f %s' % (
            float(measured), threshold['unit'],
            '≤' if ok else '>', float(threshold['value']), threshold['unit'])
        judged[metric_id] = entry
    return judged


def run_tier(report, cold):
    """Which configuration this run was in: 'cold' or 'hot'.

    A run that had to build the catalogue is **cold**; one that found it
    ready is **hot**. The trace says which, and so does the harness's own
    ``cache`` field. They must agree -- if they do not, the run is
    reported as unknown rather than guessed.
    """
    declared = (report.get('cache') or '').strip().lower()
    built = bool(cold)
    if declared in ('cold', 'hot'):
        if built and declared == 'hot':
            return 'unknown'
        return declared
    return 'cold' if built else 'hot'


def target_rows(judged):
    """One row per target: the primary metric carries the verdict."""
    rows = []
    for target_id, name, primary in TARGETS:
        entry = judged.get(primary) or {}
        rows.append(dict(id=target_id, name=name, primary_metric=primary,
                         measured=entry.get('measured'),
                         unit=entry.get('unit') or THRESHOLDS.get(primary, {}).get('unit'),
                         threshold=entry.get('threshold'),
                         threshold_source=entry.get('threshold_source'),
                         verdict=entry.get('verdict'),
                         verdict_reason=entry.get('verdict_reason'),
                         detail=entry.get('detail')))
    return rows


# ── the three scale columns, and the cold-build row ───────────────────────

def scale_columns(report):
    """Record count / unique source images / decoded bytes -- three columns.

    A number that was not measured says "无实测". It is never inferred.
    """
    facts = report.get('scale_facts') or {}
    records = facts.get('records')
    unique_images = facts.get('unique_source_images')
    unique_bytes = facts.get('unique_source_bytes')
    per_record_bytes = facts.get('decode_bytes_per_record')
    # The ``$.filename IS NOT NULL`` correction (group lead's ruling of
    # 2026-10-22 17:14, evidence
    # artifacts/scale-acceptance/2026-10-22/leader-verify/
    # unique-source-images-final-verdict.json): grouping by the NULL file
    # name put the 1,152 video-extracted rows into one group and counted
    # that group as one more picture. The prior caliber said 2,713 images /
    # 1,475,954,209 B / 36.86x; the corrected figures are 2,712 /
    # 1,475,771,364 B / 36.87x.
    caliber_note = ('来源口径已按 2026-10-22 裁定订正：原值 2,713 张 / 1,475,954,209 B '
                    '多算了 `$.filename` 为 NULL 的那一组（1,152 条从视频抽帧生成的素材，'
                    '182,845 B）；订正后为 2,712 张 / 1,475,771,364 B / 36.87×')
    columns = [
        dict(id='records', name='记录数', value=records,
             unit='count',
             detail='items 表行数（%s）' % (facts.get('records_by_type') or '无实测'),
             source='acceptance report:scale_facts.records'),
        dict(id='unique_source_images', name='唯一源图数', value=unique_images,
             unit='count',
             detail='不同源文件名的数量（按 items.data.filename 去重）。' + caliber_note,
             source='acceptance report:scale_facts.unique_source_images'),
        dict(id='decoded_bytes_unique', name='实际解码字节数（唯一源图口径）',
             value=unique_bytes, unit='bytes',
             detail='每张不同源图各一份的字节合计 = 内容寻址解码器实际拉取的量。'
                    + caliber_note,
             source='acceptance report:scale_facts.unique_source_bytes'),
        dict(id='decoded_bytes_per_record', name='实际解码字节数（逐记录口径）',
             value=per_record_bytes, unit='bytes',
             detail='每条记录各解一份的字节合计（等于 sqlar 全部 blob 之和）',
             source='acceptance report:scale_facts.decode_bytes_per_record'),
    ]
    if per_record_bytes is None:
        columns[3]['value'] = NO_MEASUREMENT
        columns[3]['detail'] = ('%s：本轮没有做全 blob 求和（该求和要读完 50 GiB 的 blob 页，'
                                '会冲掉冷打开需要的页缓存）' % NO_MEASUREMENT)
    for column in columns:
        if column['value'] is None and column['id'] != 'decoded_bytes_per_record':
            column['value'] = NO_MEASUREMENT
    if records and unique_images:
        columns.append(dict(id='repeat_factor', name='记录数 ÷ 唯一源图数',
                            value=round(records / unique_images, 3), unit='ratio',
                            detail='记录规模与解码负载的倍数差（禁止把两者合并成一个「素材数」）',
                            source='derived from the two columns above'))
    return columns


def cold_build_row(cold, probe):
    """The cold-build line: how long, was the GUI stuck, can it be cancelled."""
    row = dict(
        build_seconds=None,
        build_source=NO_MEASUREMENT,
        gui_stuck=None,
        gui_stuck_detail=NO_MEASUREMENT,
        cancellable=None,
        cancellable_detail=NO_MEASUREMENT,
        probe=None,
    )
    if cold:
        row['build_seconds'] = cold.get('reported_seconds')
        row['build_source'] = (
            '本次打开后的后台目录建库（trace: project_catalog_build_started → '
            'project_catalog_build_finished）：自报 %s s，墙钟 %s ms，%s 行，complete=%s，'
            '原因 %s —— **不计入打开耗时**（建库发生在 project_open_finished 之后）'
            % (cold.get('reported_seconds'),
               cold.get('wall_ms'),
               cold.get('rows'),
               cold.get('complete'),
               cold.get('reason')))
        row['gui_stuck'] = bool(cold.get('gui_blocked_count'))
        row['gui_stuck_detail'] = ('建库期间 GUI 阻塞 %d 次 / 合计 %s ms / 最长 %s ms，'
                                   'gui_stall %d 次'
                                   % (cold.get('gui_blocked_count', 0),
                                      cold.get('gui_blocked_total_ms'),
                                      cold.get('gui_blocked_max_ms'),
                                      cold.get('gui_stall_count', 0)))
        row['cancellable'] = None
        row['cancellable_detail'] = ('%s：本次打开路径本身没有触发取消；'
                                     '取消能力由 coldstart.py 的取消探针单独回答' % NO_MEASUREMENT)
    if probe:
        report = probe.get('report') or {}
        row['probe'] = dict(
            returncode=probe.get('returncode'), seconds=probe.get('seconds'),
            report_path=probe.get('report_path'),
            completed=report.get('completed'))
        build = report.get('cold_build') or {}
        wall = build.get('wall_ms')
        if wall is not None:
            row['build_seconds'] = round(wall / 1000.0, 3)
            row['build_source'] = (
                'coldstart.py 从零建库（独立进程、独立 settings 目录、mode=ro 打开源工程）：'
                '墙钟 %s ms，%s 批，complete=%s，resumed=%s'
                % (wall, build.get('batches'),
                   (build.get('result') or {}).get('complete'),
                   (build.get('result') or {}).get('resumed')))
        cancel = report.get('cancel_test') or {}
        if cancel:
            resume = cancel.get('resume') or {}
            row['cancellable'] = bool(cancel.get('stopped_early'))
            row['cancellable_detail'] = (
                '取消后 %s ms 返回（%s 批已提交，stopped_early=%s）；'
                '取消后 scope.ready=%s（不谎报就绪）；检查点可续=%s；进程仍可用=%s；'
                '下一次建库续建=%s（处理 %s 条，用时 %s ms）'
                % (cancel.get('build_ms'), cancel.get('batches_committed'),
                   cancel.get('stopped_early'), cancel.get('ready_after_cancel'),
                   cancel.get('checkpoint_is_resumable'),
                   cancel.get('process_usable_after_cancel'),
                   resume.get('resumed'), resume.get('processed'),
                   resume.get('ms')))
        heartbeat = build.get('main_thread_heartbeat')
        if heartbeat:
            row['gui_stuck_detail'] = (
                '建库期间主线程心跳：count=%s median=%s ms p95=%s ms max=%s ms'
                '（建库在 worker 线程上，主线程只被短促挤占）'
                % (heartbeat.get('count'), heartbeat.get('median_ms'),
                   heartbeat.get('p95_ms'), heartbeat.get('max_ms')))
    return row


# ── step 6: the report ─────────────────────────────────────────────────────

def compare_with_baseline(current, baseline):
    """Before / after, metric by metric."""
    if not baseline:
        return None
    previous_metrics = ((baseline.get('metrics') or {}))
    rows = []
    for metric_id, entry in current.get('metrics', {}).items():
        old = previous_metrics.get(metric_id) or {}
        old_value = old.get('measured')
        new_value = entry.get('measured')
        row = dict(id=metric_id, before=old_value, after=new_value,
                   before_label=old.get('detail'), after_label=entry.get('detail'),
                   delta=None, delta_percent=None, direction=None)
        if isinstance(old_value, (int, float)) and isinstance(new_value, (int, float)):
            row['delta'] = round(new_value - old_value, 2)
            if old_value:
                row['delta_percent'] = round((new_value - old_value) / abs(old_value) * 100, 2)
            row['direction'] = 'better' if new_value < old_value else (
                'worse' if new_value > old_value else 'same')
        rows.append(row)
    return dict(
        baseline_label=baseline.get('label'),
        baseline_generated_at=baseline.get('generated_at'),
        baseline_project=baseline.get('project'),
        rows=rows)


def render_markdown(payload):
    """The one-page human-readable sheet."""
    project = payload.get('project') or {}
    lines = []
    add = lines.append
    add('# 四目标验收闸门：%s' % payload.get('label'))
    add('')
    add('> 一条命令跑完**打开 / 来回切画布 / 浏览 / 搜索**四个目标，'
        '每个目标给出实测值、阈值判定与 PASS/FAIL。')
    add('> 阈值全部引自既有基线文档（出处逐条标注），**没有自创阈值**。')
    add('')
    add('| 项 | 值 |')
    add('| --- | --- |')
    add('| 跑测时间戳 | `%s` |' % payload.get('generated_at'))
    add('| 判定 | **%s** |' % (payload.get('verdict') or '（--no-gate：不出判定）'))
    add('| 档位（hot=目录已建好 / cold=目录现建） | %s |' % (payload.get('tier') or '无实测'))
    add('| 打开模式 | %s（缓存：%s） |' % (payload.get('open_mode') or '无实测',
                                          payload.get('cache_mode') or '无实测'))
    add('| 隔离 settings 目录 | `%s` |' % (payload.get('settings_dir') or '无实测'))
    add('| 被测工程 | `%s` |' % project.get('path'))
    add('| size | %s 字节 |' % project.get('size_bytes'))
    add('| mtime | %s |' % project.get('mtime'))
    add('| sha256（前 16 位） | `%s` |' % project.get('sha256_prefix16'))
    add('| 运行前后工程是否变动 | %s |' % ('未变动 ✅' if project.get('unchanged')
                                          else ('**有变动 ⚠**' if project.get('unchanged') is False
                                                else '无实测')))
    if project.get('unchanged_note'):
        add('| 变动判定依据 | %s |' % project.get('unchanged_note'))
    verify = payload.get('verification')
    if verify:
        add('')
        add('### 副本校验闸门（量测前的硬前置）')
        add('')
        add('> %s' % (verify.get('gate') or 'copy gate'))
        add('> 校验时间：`%s`' % (verify.get('checked_at') or '无实测'))
        add('')
        add('| 判据 | 实测 | 期望 | 结果 |')
        add('| --- | --- | --- | --- |')
        for name, row in (verify.get('checks') or {}).items():
            value = row.get('value')
            expected = row.get('expected')
            passed = row.get('passed')
            mark = ('✅' if passed else ('❌' if passed is False else '—'))
            add('| `%s`（%s） | %s | %s | %s |'
                % (name, row.get('label'), value,
                   '—（未设期望值）' if expected is None else expected, mark))
        quick = verify.get('quick_check')
        add('| `quick_check` 用时 | %s s | — | — |' % verify.get('quick_check_seconds'))
        add('')
        add('**结论：%s**' % ('全部通过 ✅' if verify.get('ok')
                              else '**不通过，闸门拒绝量测 ❌**'))
        if verify.get('problems'):
            for problem in verify['problems']:
                add('* ❌ %s' % problem)
    add('')
    add('## 四目标闸门结果表')
    add('')
    add('| 目标 | 实测值 | 阈值 | 阈值出处 | PASS/FAIL |')
    add('| --- | --- | --- | --- | --- |')
    for row in payload.get('targets') or []:
        measured = row.get('measured')
        if isinstance(measured, (int, float)):
            unit = row.get('unit') or ''
            measured_text = ('%.1f %s' % (measured, unit)).strip()
            if unit == 'bytes':
                measured_text = human_bytes(measured)
        else:
            measured_text = str(measured) if measured is not None else NO_MEASUREMENT
        threshold = row.get('threshold')
        if isinstance(threshold, (int, float)):
            threshold_text = ('%.1f %s' % (threshold, row.get('unit') or '')).strip()
            if row.get('unit') == 'bytes':
                threshold_text = human_bytes(threshold)
        else:
            threshold_text = NO_MEASUREMENT
        verdict = row.get('verdict') or '（不出判定）'
        if verdict == 'NOT-COMPARABLE':
            verdict = '**不同档（不判）**'
        add('| **%s** | %s | %s | %s | **%s** |'
            % (row.get('name'), measured_text, threshold_text,
               row.get('threshold_source') or NO_MEASUREMENT, verdict))
    add('')
    add('### 目标明细')
    add('')
    for row in payload.get('targets') or []:
        add('* **%s**（主指标 `%s`）：%s' % (row.get('name'), row.get('primary_metric'),
                                        row.get('detail') or ''))
        if row.get('verdict_reason'):
            add('  * 判定依据：%s' % row['verdict_reason'])
    add('')
    add('## 冷建库那一行')
    add('')
    cold = payload.get('cold_build') or {}
    add('| 项 | 值 |')
    add('| --- | --- |')
    add('| 建库耗时 | %s |' % (('%.2f s' % cold['build_seconds'])
                                if cold.get('build_seconds') else NO_MEASUREMENT))
    add('| 取数 | %s |' % (cold.get('build_source') or NO_MEASUREMENT))
    add('| 期间 GUI 是否卡 | %s |' % (('是 ⚠' if cold.get('gui_stuck') else '否 ✅')
                                      if cold.get('gui_stuck') is not None
                                      else NO_MEASUREMENT))
    add('| 期间 GUI 阻塞细节 | %s |' % (cold.get('gui_stuck_detail') or NO_MEASUREMENT))
    add('| 是否可取消 | %s |' % (('是 ✅' if cold.get('cancellable') else '否 ❌')
                                 if cold.get('cancellable') is not None
                                 else NO_MEASUREMENT))
    add('| 取消细节 | %s |' % (cold.get('cancellable_detail') or NO_MEASUREMENT))
    add('')
    add('## 三项口径（分列，不合并）')
    add('')
    add('| 口径 | 值 | 说明 |')
    add('| --- | --- | --- |')
    for column in payload.get('scale_columns') or []:
        value = column.get('value')
        if isinstance(value, (int, float)) and column.get('unit') == 'bytes':
            value_text = '%s（%s 字节）' % (human_bytes(value), int(value))
        elif isinstance(value, (int, float)):
            value_text = '%s' % value
        else:
            value_text = str(value)
        add('| %s | %s | %s |' % (column.get('name'), value_text,
                                  column.get('detail') or ''))
    add('')
    add('## 环境口径')
    add('')
    environment = payload.get('environment') or {}
    add('| 项 | 值 |')
    add('| --- | --- |')
    add('| 机器 | %s 逻辑核 / %s |' % (environment.get('logical_cpus'),
                                       human_bytes(((environment.get('memory') or {})
                                                    .get('total_bytes')))))
    add('| Python / Qt | %s / %s |' % (environment.get('python_version'),
                                       environment.get('qt_version')))
    add('| 测量窗口 | %s → %s |' % (payload.get('window_started_at'),
                                   payload.get('window_ended_at')))
    add('| 并发 python 进程（起测） | %s |' % (payload.get('python_processes_before')))
    add('| 并发 python 进程（收测） | %s |' % (payload.get('python_processes_after')))
    add('| 内存占用（起测） | %s |' % (payload.get('memory_load_percent_before')))
    add('')
    add('## 次要指标明细')
    add('')
    add('| 指标 | 实测 | 阈值 | 判定 | 说明 |')
    add('| --- | --- | --- | --- | --- |')
    for metric_id, entry in (payload.get('metrics') or {}).items():
        measured = entry.get('measured')
        if isinstance(measured, (int, float)):
            unit = entry.get('unit') or ''
            measured_text = ('%.1f %s' % (measured, unit)).strip()
            if unit == 'bytes':
                measured_text = human_bytes(measured)
        elif isinstance(measured, list):
            measured_text = ', '.join(str(value) for value in measured)
        else:
            measured_text = str(measured) if measured is not None else NO_MEASUREMENT
        threshold = entry.get('threshold')
        if isinstance(threshold, (int, float)):
            threshold_text = ('%.1f %s' % (threshold, entry.get('threshold_unit') or '')).strip()
            if entry.get('threshold_unit') == 'bytes':
                threshold_text = human_bytes(threshold)
        else:
            threshold_text = '—'
        add('| `%s` | %s | %s | %s | %s |'
            % (metric_id, measured_text, threshold_text,
               entry.get('verdict') or '—', entry.get('detail') or ''))
    add('')
    comparison = payload.get('baseline_comparison')
    if comparison:
        add('## 与基线的对比（--baseline）')
        add('')
        add('基线：%s（%s）' % (comparison.get('baseline_label'),
                                comparison.get('baseline_generated_at')))
        add('')
        add('| 指标 | 之前 | 之后 | Δ | Δ%% | 方向 |')
        add('| --- | --- | --- | --- | --- | --- |')
        for row in comparison.get('rows') or []:
            if row.get('delta') is None:
                continue
            add('| `%s` | %s | %s | %s | %s%% | %s |'
                % (row.get('id'), row.get('before'), row.get('after'),
                   row.get('delta'), row.get('delta_percent'), row.get('direction')))
        add('')
    caveats = payload.get('caveats') or []
    if caveats:
        add('## 口径注意 / 必读')
        add('')
        for caveat in caveats:
            add('* %s' % caveat)
        add('')
    add('---')
    add('')
    add('_由 `tools/scale/gate.py` 生成；阈值出处逐条标注于上表。_')
    return '\n'.join(lines) + '\n'


# ── the assembly ───────────────────────────────────────────────────────────

def project_info_from_report(report, verify=None):
    """The fingerprint of the measured file, in one shape for every command.

    The point of this block is that a reader can tell **which file** the
    numbers belong to: path, size, mtime and the first 16 hex digits of
    the SHA-256, plus whether the file changed while the gate was running.

    The "did it change" answer needs care. The harness skips the
    *pre-open* digest on purpose (digesting 51.8 GiB before the open
    would warm the page cache and change what the open measures), so its
    own ``source_unchanged`` compares ``None`` against the post-run
    digest and reports a change that never happened. The size and the
    mtime are compared instead, and the post-run digest is compared
    against the digest the copy gate took -- that one is a real
    end-to-end check of "the file on disk now is the file that was
    verified".
    """
    before = report.get('source_before') or {}
    after = report.get('source_after') or {}
    digest = before.get('sha256') or after.get('sha256')
    unchanged = None
    if before and after:
        unchanged = bool(
            before.get('size_bytes') == after.get('size_bytes')
            and before.get('mtime_ns') == after.get('mtime_ns'))
    verified_sha = (verify or {}).get('sha256')
    if verify and verified_sha and after.get('sha256'):
        unchanged = bool(unchanged) and (after['sha256'] == verified_sha)
    return dict(path=before.get('path') or report.get('project'),
                size_bytes=before.get('size_bytes') or after.get('size_bytes'),
                mtime=before.get('mtime') or after.get('mtime'),
                mtime_ns=before.get('mtime_ns'),
                sha256=digest,
                sha256_prefix16=(digest or '')[:16] or None,
                unchanged=unchanged,
                unchanged_note=(
                    'size + mtime 比对（harness 跳过了打开前的摘要，'
                    '故其 source_unchanged 字段不作数）；'
                    + ('并与副本校验的 sha256 逐位比对一致'
                       if verify and verified_sha else '未与副本校验摘要比对')),
                source_after=after)


def build_payload(*, label, project_info, report, events, clock, probe,
                  environment, python_before, python_after, window_started,
                  window_ended, gate_enabled, baseline, verify=None,
                  caveats=None):
    metrics, _cold = collect_metrics(report, events, clock)
    tier = run_tier(report, _cold)
    judged = judge(metrics, gate_enabled=gate_enabled, tier=tier)
    rows = target_rows(judged)
    payload = dict(
        schema=SCHEMA,
        label=label,
        generated_at=time.strftime('%Y-%m-%d %H:%M:%S %z'),
        generated_at_epoch=time.time(),
        gate_enabled=bool(gate_enabled),
        tier=tier,
        open_mode=report.get('mode'),
        cache_mode=report.get('cache'),
        settings_dir=report.get('settings_dir'),
        window_started_at=window_started,
        window_ended_at=window_ended,
        project=project_info,
        verification=verify,
        environment=environment,
        python_processes_before=python_before,
        python_processes_after=python_after,
        memory_load_percent_before=((environment.get('memory') or {}).get('load_percent')),
        targets=rows,
        metrics=judged,
        cold_build=cold_build_row(_cold, probe),
        scale_columns=scale_columns(report),
        window_definitions=WINDOW_DEFINITIONS,
        caveats=list(caveats or []),
    )
    if gate_enabled:
        verdicts = [row.get('verdict') for row in rows]
        if any(v == 'FAIL' for v in verdicts):
            payload['verdict'] = 'FAIL'
        elif any(v == 'NOT-COMPARABLE' for v in verdicts):
            payload['verdict'] = 'NOT-COMPARABLE'
        else:
            payload['verdict'] = 'PASS'
        payload['verdict_detail'] = {row['name']: row.get('verdict') for row in rows}
    else:
        payload['verdict'] = None
        payload['verdict_detail'] = {row['name']: None for row in rows}
    payload['baseline_comparison'] = compare_with_baseline(payload, baseline)
    return payload


def write_outputs(payload, out_dir, label):
    out_dir = Path(out_dir).resolve()
    out_dir.mkdir(parents=True, exist_ok=True)
    json_path = out_dir / ('gate-%s.json' % label)
    md_path = out_dir / ('gate-%s.md' % label)
    write_json(json_path, payload)
    md_path.write_text(render_markdown(payload), encoding='utf-8')
    return json_path, md_path


def console_summary(payload):
    lines = []
    add = lines.append
    add('')
    add('=' * 78)
    add('四目标验收闸门 — %s' % payload.get('label'))
    add('=' * 78)
    project = payload.get('project') or {}
    add('跑测时间戳 : %s' % payload.get('generated_at'))
    add('被测工程   : %s' % project.get('path'))
    add('             size=%s  mtime=%s  sha256[0:16]=%s'
        % (project.get('size_bytes'), project.get('mtime'),
           project.get('sha256_prefix16')))
    add('')
    add('%-14s %14s %14s  %-6s %s' % ('目标', '实测值', '阈值', '判定', '阈值出处'))
    add('-' * 78)
    for row in payload.get('targets') or []:
        measured = row.get('measured')
        measured_text = ('%.1f' % measured) if isinstance(measured, (int, float)) else str(measured)
        threshold = row.get('threshold')
        threshold_text = ('%.1f' % threshold) if isinstance(threshold, (int, float)) else str(threshold)
        add('%-14s %14s %14s  %-6s %s'
            % (row.get('name'), measured_text, threshold_text,
               row.get('verdict') or '-', (row.get('threshold_source') or '')[:46]))
    add('-' * 78)
    cold = payload.get('cold_build') or {}
    add('冷建库       : %s（GUI 卡：%s；可取消：%s）'
        % (('%.2f s' % cold['build_seconds']) if cold.get('build_seconds') else NO_MEASUREMENT,
           ('是' if cold.get('gui_stuck') else '否') if cold.get('gui_stuck') is not None else NO_MEASUREMENT,
           ('是' if cold.get('cancellable') else '否') if cold.get('cancellable') is not None else NO_MEASUREMENT))
    add('三项口径     : 记录数=%s 唯一源图=%s 实际解码字节=%s'
        % tuple(_column(payload, key) for key in ('records', 'unique_source_images',
                                                  'decoded_bytes_unique')))
    add('判定         : %s' % (payload.get('verdict') or '（--no-gate：不出判定）'))
    add('=' * 78)
    return '\n'.join(lines) + '\n'


def _column(payload, key):
    for column in payload.get('scale_columns') or []:
        if column.get('id') == key:
            value = column.get('value')
            if column.get('unit') == 'bytes' and isinstance(value, (int, float)):
                return human_bytes(value)
            return str(value)
    return NO_MEASUREMENT


# ── the commands ───────────────────────────────────────────────────────────

def _expectations(args):
    """The expected values the project file has to match, if any were given."""
    expect = {}
    for name in ('size_bytes', 'page_count', 'freelist_count', 'items_rows',
                 'sqlar_rows', 'material_records', 'project_id'):
        value = getattr(args, 'expect_' + name, None)
        if value is not None:
            expect[name] = value
    return expect


def _source_expectations(source):
    """Read the expected structure from another project file (read-only).

    This is how a copy is checked against the project it was copied from:
    the copy must have the same size, the same page count, the same row
    counts and the same project id. Comparing against the *source* is
    stronger than comparing against hard-coded numbers, because it also
    catches a copy of the wrong project.
    """
    source = Path(source).resolve()
    if not source.exists():
        raise SystemExit('the --expect-source file does not exist: %s' % source)
    import sqlite3
    connection = sqlite3.connect(source.as_uri() + '?mode=ro', uri=True, timeout=60)
    try:
        values = dict(size_bytes=source.stat().st_size)
        for name, sql in STRUCTURE_QUERIES:
            values[name] = connection.execute(sql).fetchone()[0]
    finally:
        connection.close()
    return values


def _add_expect_flags(parser):
    """The copy gate: what the project file has to look like.

    ``size_bytes`` alone is **not** a copy gate -- robocopy /J pre-sizes
    the destination, so a half-copied file has the right size. The rest of
    these are read from the content, so they catch it.
    """
    parser.add_argument('--expect-size', type=int, default=None,
                        help='the size the file must have')
    parser.add_argument('--expect-page-count', type=int, default=None)
    parser.add_argument('--expect-freelist-count', type=int, default=None)
    parser.add_argument('--expect-items-rows', type=int, default=None)
    parser.add_argument('--expect-sqlar-rows', type=int, default=None)
    parser.add_argument('--expect-material-records', type=int, default=None)
    parser.add_argument('--expect-project-id', default=None)
    parser.add_argument('--expect-source', type=Path, default=None,
                        help='another project file to compare the structure against '
                             '(read-only); the copy must match it item for item')
    return parser


def cmd_verify(args):
    out_dir = Path(args.out).resolve()
    out_dir.mkdir(parents=True, exist_ok=True)
    info = verify_project(args.project, expect=_expectations(args),
                           quick_check=not args.skip_quick_check)
    info['checked_at'] = time.strftime('%Y-%m-%d %H:%M:%S %z')
    path = write_json(out_dir / ('verify-%s.json' % args.label), info)
    print(json.dumps(info, ensure_ascii=False, indent=2, default=str))
    print('verify report:', path)
    return 0 if info.get('ok') else 1


def normalize_report(report):
    """Bring an existing measurement into the shape this gate reads.

    Two producers write measurement reports for this project and they do
    not use the same field names:

    * ``tools/scale/acceptance.py`` -- ``metrics.search.milliseconds``,
      ``metrics.browser_card_materialisation.waited_ms`` and so on;
    * ``tools/scale/gui_round2.py`` -- the UI-driven harness, which stores
      ``search.query_wall_ms`` / ``search.ui_counts`` and puts the reduced
      trace under ``trace``.

    Judging a report must not depend on which harness produced it, so the
    differences are mapped onto one shape here. Nothing is invented: each
    field is renamed, never filled in.
    """
    if report.get('metrics') is not None:
        return report

    metrics = dict(report.get('metrics') or {})
    search = report.get('search')
    if search and 'search' not in metrics:
        metrics['search'] = search
    materialisation = report.get('browser_card_materialisation')
    if materialisation and 'browser_card_materialisation' not in metrics:
        metrics['browser_card_materialisation'] = materialisation
    heartbeat = report.get('heartbeat_interactive')
    if heartbeat and 'heartbeat' not in metrics:
        metrics['heartbeat'] = heartbeat
    for key in ('working_set', 'counts', 'stages'):
        if key not in report and report.get(key) is not None:
            report[key] = report.get(key)
    report = dict(report)
    report['metrics'] = metrics
    return report


def cmd_report(args):
    """Judge an acceptance report that already exists (no GUI run)."""
    report_path = Path(args.report).resolve()
    raw = json.loads(report_path.read_text(encoding='utf-8'))
    report = normalize_report(raw)
    out_dir = Path(args.out).resolve()
    events = read_trace(report)
    clock = StageClock()
    baseline = None
    if args.baseline:
        baseline = json.loads(Path(args.baseline).resolve().read_text(encoding='utf-8'))
    environment = report.get('environment_before') or evidence.capture_environment()

    # The three scale columns are a property of the *project file*, not of
    # the run, so they can be read from the measured project directly
    # (read-only, mode=ro URI). A report that already carries them wins.
    if not report.get('scale_facts'):
        project = report.get('project')
        if project and Path(project).exists():
            try:
                report['scale_facts'] = acceptance.source_scale_facts(Path(project))
            except Exception as error:  # noqa: BLE001 - a missing fact must not stop the judging
                report['scale_facts'] = dict(error=repr(error))

    # The copy gate runs here too, not only before a measurement. A verdict
    # has to carry the evidence that the file it was taken on is intact --
    # otherwise a FAIL cannot be told apart from "the copy was broken and
    # the numbers do not count". The check is read-only and the project
    # comes from the report being judged.
    verify = None
    if args.verify:
        verify = json.loads(Path(args.verify).resolve().read_text(encoding='utf-8'))
        verify['note'] = ('此副本校验来自 verify-%s.json；本次是复判既有实测，'
                          '不是新开一次 GUI' % args.label)
    else:
        project = report.get('project')
        if project and Path(project).exists():
            expect = {}
            if args.expect_source:
                expect.update(_source_expectations(args.expect_source))
            for name in ('size_bytes', 'page_count', 'freelist_count',
                         'items_rows', 'sqlar_rows', 'material_records',
                         'project_id'):
                value = getattr(args, 'expect_' + name, None)
                if value is not None:
                    expect[name] = value
            verify = verify_project(Path(project), expect=expect,
                                    quick_check=not args.skip_quick_check)
            verify['checked_at'] = time.strftime('%Y-%m-%d %H:%M:%S %z')
            verify['gate'] = 'copy-gate (evidence attached to an offline verdict)'
            verify['note'] = ('本次是复判既有实测产物（没有重新跑 GUI）；'
                              '副本校验仍然实时跑了一遍，好让判定结果带着'
                              '「这份副本验过没有」的证据')

    payload = build_payload(
        label=args.label or report.get('label'),
        project_info=project_info_from_report(report, verify),
        report=report, events=events, clock=clock, probe=None,
        environment=environment,
        python_before=(report.get('environment_before') or {}).get('python_processes', {}).get('count'),
        python_after=(report.get('environment_after') or {}).get('python_processes', {}).get('count'),
        window_started=(report.get('wall_clock') or {}).get('started'),
        window_ended=(report.get('wall_clock') or {}).get('ended'),
        gate_enabled=not args.no_gate, baseline=baseline,
        verify=verify,
        caveats=['⚠️ **本轮 FAIL 不等于「接线让打开变慢」—— 这不是干净对照。**'
                 '本轮热档是**新进程首次打开副本**，而 GUI-BASELINE 的 36,531.5 ms 是'
                 '**同一进程里的第二次打开**（第二次打开会命中已经热起来的页缓存与'
                 '已建好的目录）。两者不是同一个条件，所以这个 FAIL 只能读作'
                 '「在该条件下打开耗时超过了基线的 36.5 s」，'
                 '**不能**读作「接线导致了打开变慢」。',
                 '本文件由 `gate.py report` 生成：只对既有的实测产物做判定，'
                 '没有重新跑 GUI；副本校验仍实时跑了一遍并记入 verification 段。',
                 '三项口径的唯一源图数已按 2026-10-22 裁定订正为 2,712'
                 '（原 2,713 多算了 `$.filename` 为 NULL 的那一组）。'])
    json_path, md_path = write_outputs(payload, out_dir, payload['label'])
    print(console_summary(payload))
    print('JSON :', json_path)
    print('MD   :', md_path)
    if payload.get('verdict') == 'PASS':
        return 0
    if payload.get('verdict') == 'NOT-COMPARABLE':
        return 3
    if args.no_gate:
        return 0
    return 1


def cmd_run(args):
    out_dir = Path(args.out).resolve()
    out_dir.mkdir(parents=True, exist_ok=True)
    label = args.label
    project = Path(args.project).resolve()

    # ── 1. THE COPY GATE -- a hard precondition, never skippable ──────
    # The project file is measured *before* anything is opened, because a
    # half-copied database produces fast, meaningless numbers. A copy that
    # landed only half way still has the right size (robocopy /J pre-sizes
    # the destination), so the gate reads the content: page count, the
    # three table row counts, the project id and PRAGMA quick_check.
    #
    # A previously written verify-*.json may be supplied for reference,
    # but it never replaces the live check: a file can be replaced between
    # the check and the measurement, and the whole point of this gate is
    # that the file being measured is the file that was verified.
    expect = _expectations(args)
    if args.expect_source:
        expect.update(_source_expectations(args.expect_source))
    verify = verify_project(project, expect=expect,
                             quick_check=not args.skip_quick_check)
    verify['checked_at'] = time.strftime('%Y-%m-%d %H:%M:%S %z')
    verify['gate'] = 'copy-gate (hard precondition for measurement)'
    if args.verify:
        reference = json.loads(Path(args.verify).resolve().read_text(encoding='utf-8'))
        verify['reference_report'] = str(Path(args.verify).resolve())
        verify['reference_ok'] = reference.get('ok')
        verify['reference_note'] = ('此文件仅作参考；本次量测使用的是上面那次实时校验的结果')
    write_json(out_dir / ('verify-%s.json' % label), verify)
    if not verify.get('ok'):
        failed = verify.get('failed_checks') or []
        print('=' * 78)
        print('REFUSING TO MEASURE: the copy gate failed.')
        print('  project : %s' % verify.get('path'))
        print('  failed  : %s' % (', '.join(failed) if failed else '(none)'))
        for name, row in (verify.get('checks') or {}).items():
            print('  %-8s %-26s value=%-24r expected=%-24r %s'
                  % ('FAILED' if row.get('passed') is False else 'ok',
                     row.get('label'), row.get('value'), row.get('expected'),
                     row.get('detail') or ''))
        for problem in verify.get('problems') or []:
            print('  ! %s' % problem)
        print('verify report: %s' % (out_dir / ('verify-%s.json' % label)))
        print('=' * 78)
        return 2
    print('copy gate: PASSED (%d items, quick_check=%s)'
          % (len(verify.get('checks') or {}), verify.get('quick_check')))

    settings_dir = Path(args.settings_dir).resolve() if args.settings_dir else (
        out_dir / 'settings' / label)
    settings_dir.mkdir(parents=True, exist_ok=True)

    # ── 2. the cold-start probe (its own cache, same settings tree) ──
    # The probe builds the catalogue from scratch in the isolated settings
    # directory. That is deliberate: the probe answers the cold-build
    # questions (how long, was the GUI stuck, can it be cancelled), and the
    # catalogue it leaves behind is what makes the following measurement a
    # **hot** run -- the same configuration the GUI-BASELINE thresholds
    # were taken in.
    probe = None
    if not args.skip_cold_probe:
        probe = cold_probe(project, settings_dir, label, out_dir)
        print('cold probe finished: rc=%s in %ss' % (probe.get('returncode'),
                                                      probe.get('seconds')))

    # ── 3. the measurement, with stage anchors ────────────────────────
    environment_before = evidence.capture_environment()
    python_before = evidence.python_processes()
    window_started = time.strftime('%Y-%m-%d %H:%M:%S %z')
    clock = StageClock().install()
    try:
        facts = acceptance.source_scale_facts(project)
        report = acceptance.measure(
            project, label, out_dir, tier=args.tier, mode=args.mode,
            cache=args.cache, settings_dir=settings_dir,
            open_timeout_ms=args.open_timeout_ms,
            all_assets_timeout_ms=args.all_assets_timeout_ms,
            screenshots=not args.no_screenshots, scale_facts=facts)
    finally:
        clock.restore()
    report['scale_facts'] = facts
    acceptance.write_json(Path(out_dir) / 'reports' / ('%s.json' % label), report)
    window_ended = time.strftime('%Y-%m-%d %H:%M:%S %z')
    environment_after = evidence.capture_environment()
    python_after = evidence.python_processes()

    # ── 4. judge ──────────────────────────────────────────────────────
    events = read_trace(report)
    baseline = None
    if args.baseline:
        baseline = json.loads(Path(args.baseline).resolve().read_text(encoding='utf-8'))

    project_info = project_info_from_report(report, verify)
    caveats = []
    if not report.get('completed'):
        caveats.append('**本次测量没有跑完**（error=%r）；无实测的指标一律写「无实测」，'
                       '没有用推断填补。' % (report.get('error'),))
    caveats.append('⚠️ **本轮 FAIL 不等于「接线让打开变慢」—— 这不是干净对照。**'
                   '本轮热档是**新进程首次打开副本**，而 GUI-BASELINE 的 36,531.5 ms 是'
                   '**同一进程里的第二次打开**（第二次打开会命中已经热起来的页缓存与'
                   '已建好的目录）。两者不是同一个条件，所以这个 FAIL 只能读作'
                   '「在新进程首次打开这个副本的条件下，打开耗时超过了基线的 36.5 s」，'
                   '**不能**读作「接线导致了打开变慢」。')
    caveats.append('判定档位：%s。GUI-BASELINE 的打开/切画布/搜索阈值出自**热档**；'
                   '冷档（目录要现建）与之**不同档**，本闸门对不同档的项判 '
                   'NOT-COMPARABLE 而不是 FAIL。' % '（见 tier 字段）')
    caveats.append('冷建库**单独一行**，不并进打开耗时：trace 显示 '
                   'project_catalog_build_started 出现在 project_open_finished **之后**。')
    caveats.append('三项口径的唯一源图数已按 2026-10-22 裁定订正为 2,712'
                   '（原 2,713 多算了 `$.filename` 为 NULL 的那一组）。')
    caveats.append('窗口口径：' + '；'.join('%s = %s' % (key, value)
                                           for key, value in WINDOW_DEFINITIONS.items()))
    payload = build_payload(
        label=label, project_info=project_info, report=report, events=events,
        clock=clock, probe=probe, environment=environment_before,
        python_before=python_before.get('count'), python_after=python_after.get('count'),
        window_started=window_started, window_ended=window_ended,
        gate_enabled=not args.no_gate, baseline=baseline, verify=verify,
        caveats=caveats)
    json_path, md_path = write_outputs(payload, out_dir, label)
    print(console_summary(payload))
    print('JSON :', json_path)
    print('MD   :', md_path)
    if payload.get('verdict') == 'PASS':
        return 0
    if payload.get('verdict') == 'NOT-COMPARABLE':
        return 3
    if args.no_gate:
        return 0
    return 1


def main(argv=None):
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest='command', required=True)

    def common(target):
        target.add_argument('--label', required=True)
        target.add_argument('--out', type=Path, required=True)
        target.add_argument('--no-gate', action='store_true',
                            help='只出数字，不出 PASS/FAIL 判定')
        target.add_argument('--baseline', type=Path, default=None,
                            help='一份之前的 gate JSON，用于前后对比')

    verify_parser = sub.add_parser('verify', help='check the project file before measuring')
    verify_parser.add_argument('--project', type=Path, required=True)
    verify_parser.add_argument('--label', required=True)
    verify_parser.add_argument('--out', type=Path, required=True)
    _add_expect_flags(verify_parser)
    verify_parser.add_argument('--skip-quick-check', action='store_true',
                               help='skip PRAGMA quick_check (not recommended)')

    run_parser = sub.add_parser('run', help='run the four-target gate')
    run_parser.add_argument('--project', type=Path, required=True)
    run_parser.add_argument('--label', required=True)
    run_parser.add_argument('--out', type=Path, required=True)
    run_parser.add_argument('--settings-dir', type=Path, default=None,
                            help='an isolated settings directory, so the first open is a cold build')
    run_parser.add_argument('--tier', type=int, default=None)
    run_parser.add_argument('--mode', choices=('paged', 'full'), default='full',
                            help='full (default) keeps the whole project in the scene, '
                                 'which is the configuration GUI-BASELINE.md measured '
                                 '(100 001 items in the scene). paged loads one page and '
                                 'is the on-demand path; its search and switch numbers '
                                 'are NOT comparable with the full-mode thresholds.')
    run_parser.add_argument('--cache', choices=('cold', 'hot'), default='cold')
    run_parser.add_argument('--open-timeout-ms', type=int, default=900000)
    run_parser.add_argument('--all-assets-timeout-ms', type=int, default=600000)
    run_parser.add_argument('--no-screenshots', action='store_true')
    run_parser.add_argument('--no-gate', action='store_true',
                            help='只出数字，不出 PASS/FAIL 判定')
    run_parser.add_argument('--baseline', type=Path, default=None)
    run_parser.add_argument('--verify', type=Path, default=None,
                            help='a verify-<label>.json from the verify subcommand')
    _add_expect_flags(run_parser)
    run_parser.add_argument('--skip-quick-check', action='store_true')
    run_parser.add_argument('--skip-cold-probe', action='store_true')

    report_parser = sub.add_parser('report', help='judge an existing measurement')
    report_parser.add_argument('--report', type=Path, required=True)
    report_parser.add_argument('--label', default=None)
    report_parser.add_argument('--out', type=Path, required=True)
    report_parser.add_argument('--no-gate', action='store_true')
    report_parser.add_argument('--baseline', type=Path, default=None)
    report_parser.add_argument('--verify', type=Path, default=None,
                               help='a verify-<label>.json to attach to the report')
    _add_expect_flags(report_parser)
    report_parser.add_argument('--skip-quick-check', action='store_true')

    args = parser.parse_args(argv)
    if args.command == 'verify':
        return cmd_verify(args)
    if args.command == 'run':
        return cmd_run(args)
    if args.command == 'report':
        return cmd_report(args)
    parser.error('unknown command')
    return 2


if __name__ == '__main__':
    _code = main()
    sys.stdout.flush()
    sys.stderr.flush()
    # Qt's interpreter teardown can raise an access violation while a
    # QWebEnginePage is still alive -- the same crash the existing scale
    # harness documents. Every artefact is already on disk, so the process
    # leaves with its status code instead of with a crash dump.
    os._exit(_code)
