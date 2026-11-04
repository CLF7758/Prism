"""Summarise Prism JSONL interaction logs without loading them into memory.

Reports, for each log:
* event counts, sessions, dropped/malformed lines;
* the slowest individual entries (operations, dispatches, stalls);
* ``operation`` aggregates by name -- the phase breakdown of page switches
  (``page.material.open_folder``, ``page.store_to_scene``, ``page.open.*``);
* ``gui_stall`` aggregates by sampled code path (``stack_histogram``): where
  the GUI thread was actually stuck, in order of total time;
* ``slow_dispatch`` aggregates by (target class, event type): which widgets
  spend the most time handling which events, with their top code path;
* GC pause totals;
* a ``verdict`` (``PASS``/``FAIL``) plus the list of threshold breaches, so
  "卡不卡" stops being a judgement call.  ``--baseline`` compares against a
  previous report and reports only the *new* breaches.

Streaming and bounded: only the top-N entries and a few counters live in
memory, so the multi-hundred-MB logs from scale runs stay cheap.
"""
import argparse
from collections import Counter, defaultdict
import heapq
import json
from pathlib import Path
import sys


#: Thresholds for the automatic verdict.  Overridable with
#: ``--threshold name=value`` (repeatable) or ``--threshold-file``.
#: Ratios are fractions; everything else is milliseconds or a count.
DEFAULT_THRESHOLDS = {
    # 卡顿总时长 / 会话时长
    'stall_total_ratio': 0.05,
    # 单次 gui_stall
    'stall_single_ms': 2000.0,
    # slow_input_to_paint 次数
    'input_to_paint_count': 10,
    # 单次 slow_input_to_paint
    'input_to_paint_single_ms': 1000.0,
    # 单条 sql.execute / sql.fetch
    'sql_single_ms': 500.0,
    # 同一查询累计
    'sql_query_total_ms': 5000.0,
    # GC 累计
    'gc_total_ms': 5000.0,
    # 单次 GC
    'gc_single_ms': 500.0,
    # 单条 page.* operation
    'page_operation_single_ms': 1500.0,
}


def top_signature(entry):
    histogram = entry.get('stack_histogram')
    if not histogram:
        return None
    return histogram[0][1] if isinstance(histogram[0], list) else None


def analyse(paths, limit=20, thresholds=None):
    counts = Counter()
    slow = []
    sessions = set()
    dropped = 0
    malformed = 0
    stalls = []
    ops = defaultdict(lambda: [0, 0.0, 0.0])          # name -> [count, total, max]
    stall_hotspots = defaultdict(lambda: [0, 0, 0.0])  # sig -> [stalls, ?, total]
    dispatch_hotspots = defaultdict(lambda: [0, 0.0, None])  # (t, e) -> [count, total, sig]
    sql_totals = defaultdict(lambda: [0, 0.0, 0.0])    # (mode, query) -> [count, total, max]
    gc_pauses = [0, 0.0]
    itp_pauses = [0, 0.0]                              # slow_input_to_paint -> [count, total]
    saw_stall_end = False
    saw_legacy = False
    # --- numbers the automatic verdict is made of ------------------------
    first_time = None
    last_time = None
    stall_total_ms = 0.0
    stall_max_ms = 0.0
    itp_max_ms = 0.0
    sql_max_ms = 0.0
    gc_max_ms = 0.0
    page_operation_max_ms = 0.0
    page_operation_max_name = ''
    for path in paths:
        with Path(path).open(encoding='utf-8') as stream:
            for number, line in enumerate(stream, 1):
                try:
                    entry = json.loads(line)
                except (ValueError, TypeError):
                    malformed += 1
                    continue
                kind = entry.get('event', '')
                counts[kind] += 1
                sessions.add(entry.get('session'))
                dropped = max(dropped, entry.get('dropped_events', 0))
                stamp = entry.get('time')
                if isinstance(stamp, (int, float)) and not isinstance(stamp, bool):
                    first_time = stamp if first_time is None else min(first_time, stamp)
                    last_time = stamp if last_time is None else max(last_time, stamp)
                milliseconds = entry.get('duration_ms', entry.get('blocked_ms',
                                                                 entry.get('latency_ms', 0)))
                if kind in ('operation', 'slow_dispatch', 'gui_stall', 'gui_recovered',
                            'gc', 'sql.execute', 'sql.fetch', 'slow_input_to_paint'):
                    entry.update(file=str(path), line=number)
                    # Bounded top entries; sequence avoids comparing dictionaries.
                    heapq.heappush(slow, (float(milliseconds), counts.total(), entry))
                    if len(slow) > limit:
                        heapq.heappop(slow)
                if kind == 'operation':
                    name = entry.get('name', '?')
                    cell = ops[name]
                    cell[0] += 1
                    cell[1] += milliseconds
                    cell[2] = max(cell[2], milliseconds)
                    if name.startswith('page.') and milliseconds > page_operation_max_ms:
                        page_operation_max_ms = milliseconds
                        page_operation_max_name = name
                elif kind in ('sql.execute', 'sql.fetch'):
                    key = (entry.get('mode', '?'), entry.get('query', '?')[:80])
                    cell = sql_totals[key]
                    cell[0] += 1
                    cell[1] += milliseconds
                    cell[2] = max(cell[2], milliseconds)
                    sql_max_ms = max(sql_max_ms, milliseconds)
                elif kind == 'gui_stall':
                    stalls.append(entry)
                    stalls = stalls[-limit:]
                    if 'phase' not in entry:
                        # Legacy single-shot event: count it directly.
                        saw_legacy = True
                    elif entry.get('phase') == 'end':
                        saw_stall_end = True
                    elif entry.get('phase') == 'ongoing':
                        continue
                    # Totals prefer `end` entries (they carry the stall's true
                    # span); ongoing entries count only when the run crashed
                    # mid-stall and no end was ever written.
                    signature = top_signature(entry) or '(no samples)'
                    cell = stall_hotspots[signature]
                    cell[0] += 1
                    cell[2] += milliseconds
                    stall_total_ms += milliseconds
                    stall_max_ms = max(stall_max_ms, milliseconds)
                elif kind == 'slow_dispatch':
                    key = (entry.get('target', '?'), entry.get('event_type', '?'))
                    cell = dispatch_hotspots[key]
                    cell[0] += 1
                    cell[1] += milliseconds
                    cell[2] = top_signature(entry) or cell[2]
                elif kind == 'gc':
                    gc_pauses[0] += 1
                    gc_pauses[1] += milliseconds
                    gc_max_ms = max(gc_max_ms, milliseconds)
                elif kind == 'slow_input_to_paint':
                    latency = entry.get('latency_ms', 0)
                    itp_pauses[0] += 1
                    itp_pauses[1] += latency
                    itp_max_ms = max(itp_max_ms, latency)
    if not saw_stall_end and not saw_legacy:
        # Only ongoing events exist (the run died mid-stall): count them instead.
        stall_hotspots.clear()
        stall_total_ms = 0.0
        stall_max_ms = 0.0
        for entry in stalls:
            if entry.get('phase') != 'ongoing':
                continue
            signature = top_signature(entry) or '(no samples)'
            cell = stall_hotspots[signature]
            cell[0] += 1
            cell[2] += entry.get('duration_ms', 0)
            stall_total_ms += entry.get('duration_ms', 0)
            stall_max_ms = max(stall_max_ms, entry.get('duration_ms', 0))
    metrics = dict(
        session_seconds=round((last_time - first_time), 3)
        if first_time is not None and last_time is not None else 0.0,
        stall_total_ms=round(stall_total_ms, 1),
        stall_max_ms=round(stall_max_ms, 1),
        input_to_paint_count=itp_pauses[0],
        input_to_paint_total_ms=round(itp_pauses[1], 1),
        input_to_paint_max_ms=round(itp_max_ms, 1),
        sql_max_ms=round(sql_max_ms, 1),
        gc_total_ms=round(gc_pauses[1], 1),
        gc_max_ms=round(gc_max_ms, 1),
        page_operation_max_ms=round(page_operation_max_ms, 1),
        page_operation_max_name=page_operation_max_name,
    )
    # A log without a single measured event cannot be judged: saying PASS
    # there would be a green light with nothing behind it.
    measured = (counts['operation'] + counts['gui_stall'] + counts['gc']
                + counts['sql.execute'] + counts['sql.fetch']
                + counts['slow_input_to_paint'] + counts['slow_dispatch'])
    warnings = []
    if measured == 0:
        warnings.append(
            '这个日志里没有任何可测事件（operation/gui_stall/gc/sql/slow_input_to_paint/'
            'slow_dispatch），判定没有证据支撑 —— PASS 不代表"不卡"。')
    if metrics['session_seconds'] <= 0:
        warnings.append('日志里没有时间跨度，无法判断卡顿占会话的比例。')
    result = dict(
        events=dict(counts), sessions=len(sessions), dropped_events=dropped,
        malformed_lines=malformed, slowest=[v[2] for v in sorted(slow, reverse=True)],
        recent_stalls=stalls,
        operation_totals=[
            [name, cell[0], round(cell[1], 1), round(cell[2], 1)]
            for name, cell in sorted(ops.items(), key=lambda pair: -pair[1][1])[:limit]
        ],
        stall_hotspots=[
            [round(total, 1), count, signature]
            for signature, cell in sorted(stall_hotspots.items(),
                                          key=lambda pair: -pair[1][2])[:limit]
            for count, total in [(cell[0], cell[2])]],
        dispatch_hotspots=[
            [round(cell[1], 1), cell[0], '%s:%s' % key, cell[2]]
            for key, cell in sorted(dispatch_hotspots.items(),
                                    key=lambda pair: -pair[1][1])[:limit]],
        sql_totals=[
            [mode, round(cell[1], 1), cell[0], round(cell[2], 1), query]
            for (mode, query), cell in sorted(sql_totals.items(),
                                              key=lambda pair: -pair[1][1])[:limit]
        ],
        slow_input_to_paint=dict(
            count=itp_pauses[0], total_ms=round(itp_pauses[1], 1)),
        gc=dict(pauses=gc_pauses[0], total_ms=round(gc_pauses[1], 1)),
        metrics=metrics,
        evidence=dict(measured_events=measured),
        warnings=warnings,
        note=('stall_hotspots rank where the GUI thread was actually stuck '
              '(sampled stack histogram); correlate with operation_totals '
              'before claiming a root cause.'),
    )
    effective = dict(DEFAULT_THRESHOLDS)
    if thresholds:
        effective.update(thresholds)
    result['thresholds'] = effective
    result['violations'] = evaluate(metrics, effective, sql_totals)
    result['verdict'] = 'FAIL' if result['violations'] else 'PASS'
    return result


def evaluate(metrics, thresholds, sql_totals=()):
    """Return the list of threshold breaches.

    Every breach carries a stable ``id`` so a later run can tell a *new*
    breach from one that was already there in the baseline report.
    """
    violations = []

    def fail(check, subject, value, limit, detail):
        violations.append(dict(
            check=check,
            id='%s:%s' % (check, subject) if subject else check,
            subject=subject,
            value=value,
            threshold=limit,
            detail=detail,
        ))

    session = metrics.get('session_seconds') or 0.0
    if session > 0:
        ratio = metrics.get('stall_total_ms', 0.0) / (session * 1000.0)
        if ratio > thresholds['stall_total_ratio']:
            fail('stall_total_ratio', '', round(ratio, 4),
                 thresholds['stall_total_ratio'],
                 '卡顿总时长 %.0f ms，占会话 %.0f s 的 %.1f%%（阈值 %.1f%%）' % (
                     metrics.get('stall_total_ms', 0.0), session, ratio * 100,
                     thresholds['stall_total_ratio'] * 100))
    if metrics.get('stall_max_ms', 0.0) > thresholds['stall_single_ms']:
        fail('stall_single_ms', '', metrics['stall_max_ms'],
             thresholds['stall_single_ms'],
             '单次卡顿 %.0f ms（阈值 %.0f ms）' % (
                 metrics['stall_max_ms'], thresholds['stall_single_ms']))
    if metrics.get('input_to_paint_count', 0) > thresholds['input_to_paint_count']:
        fail('input_to_paint_count', '', metrics['input_to_paint_count'],
             thresholds['input_to_paint_count'],
             '点击到画面更新 ≥300ms 发生 %d 次（阈值 %d）' % (
                 metrics['input_to_paint_count'],
                 thresholds['input_to_paint_count']))
    if metrics.get('input_to_paint_max_ms', 0.0) > thresholds['input_to_paint_single_ms']:
        fail('input_to_paint_single_ms', '', metrics['input_to_paint_max_ms'],
             thresholds['input_to_paint_single_ms'],
             '单次点击到画面更新 %.0f ms（阈值 %.0f ms）' % (
                 metrics['input_to_paint_max_ms'],
                 thresholds['input_to_paint_single_ms']))
    if metrics.get('sql_max_ms', 0.0) > thresholds['sql_single_ms']:
        fail('sql_single_ms', '', metrics['sql_max_ms'],
             thresholds['sql_single_ms'],
             '单条 SQL %.0f ms（阈值 %.0f ms）' % (
                 metrics['sql_max_ms'], thresholds['sql_single_ms']))
    for (mode, query), cell in sorted(sql_totals.items(), key=lambda pair: -pair[1][1]):
        if cell[1] > thresholds['sql_query_total_ms']:
            fail('sql_query_total_ms', '%s:%s' % (mode, query), round(cell[1], 1),
                 thresholds['sql_query_total_ms'],
                 '同一查询累计 %.0f ms / %d 次（阈值 %.0f ms）：%s' % (
                     cell[1], cell[0], thresholds['sql_query_total_ms'], query))
    if metrics.get('gc_total_ms', 0.0) > thresholds['gc_total_ms']:
        fail('gc_total_ms', '', metrics['gc_total_ms'], thresholds['gc_total_ms'],
             'GC 暂停累计 %.0f ms（阈值 %.0f ms）' % (
                 metrics['gc_total_ms'], thresholds['gc_total_ms']))
    if metrics.get('gc_max_ms', 0.0) > thresholds['gc_single_ms']:
        fail('gc_single_ms', '', metrics['gc_max_ms'], thresholds['gc_single_ms'],
             '单次 GC %.0f ms（阈值 %.0f ms）' % (
                 metrics['gc_max_ms'], thresholds['gc_single_ms']))
    if metrics.get('page_operation_max_ms', 0.0) > thresholds['page_operation_single_ms']:
        fail('page_operation_single_ms', metrics.get('page_operation_max_name', ''),
             metrics['page_operation_max_ms'],
             thresholds['page_operation_single_ms'],
             '单条 page.* 操作 %.0f ms（%s，阈值 %.0f ms）' % (
                 metrics['page_operation_max_ms'],
                 metrics.get('page_operation_max_name', '?'),
                 thresholds['page_operation_single_ms']))
    return violations


def compare_with_baseline(result, baseline):
    """Mark the breaches that are new relative to ``baseline``.

    ``baseline`` is a previous report (the JSON this tool writes).  Only the
    breaches that did not exist there count as new; the ones that were
    already there are listed separately, so nothing is hidden.
    """
    known = set()
    for violation in baseline.get('violations', []) or []:
        identifier = violation.get('id') or violation.get('check')
        if identifier:
            known.add(identifier)
    new = [violation for violation in result.get('violations', [])
           if (violation.get('id') or violation.get('check')) not in known]
    carried = [violation for violation in result.get('violations', [])
               if (violation.get('id') or violation.get('check')) in known]
    result['baseline'] = {
        'verdict': baseline.get('verdict'),
        'new_violations': new,
        'carried_over': carried,
        'metrics_delta': _metrics_delta(baseline.get('metrics', {}),
                                         result.get('metrics', {})),
    }
    result['violations'] = new
    result['verdict'] = 'FAIL' if new else 'PASS'
    return result


def _metrics_delta(before, after):
    delta = {}
    for key, value in sorted(after.items()):
        if not isinstance(value, (int, float)) or isinstance(value, bool):
            continue
        previous = before.get(key)
        if not isinstance(previous, (int, float)) or isinstance(previous, bool):
            continue
        delta[key] = round(value - previous, 3)
    return delta


def parse_threshold_overrides(items, base=None):
    """Parse ``--threshold name=value`` pairs into a threshold mapping."""
    thresholds = dict(base or DEFAULT_THRESHOLDS)
    for item in items or []:
        if '=' not in item:
            raise ValueError('threshold must be name=value, got %r' % item)
        name, _, raw = item.partition('=')
        name = name.strip()
        if name not in DEFAULT_THRESHOLDS:
            raise ValueError('unknown threshold %r (known: %s)' % (
                name, ', '.join(sorted(DEFAULT_THRESHOLDS))))
        thresholds[name] = float(raw)
    return thresholds


def main():
    parser = argparse.ArgumentParser(
        description='Summarise Prism performance logs and judge them PASS/FAIL.')
    parser.add_argument('logs', nargs='+')
    parser.add_argument('--output')
    parser.add_argument('--threshold', action='append', default=[],
                        metavar='NAME=VALUE',
                        help='override a threshold (repeatable): %s' % ', '.join(
                            sorted(DEFAULT_THRESHOLDS)))
    parser.add_argument('--threshold-file',
                        help='JSON file with threshold overrides')
    parser.add_argument('--baseline',
                        help='a previous report (JSON) to compare against; '
                             'only newly introduced breaches are reported')
    args = parser.parse_args()
    thresholds = None
    if args.threshold_file:
        thresholds = dict(DEFAULT_THRESHOLDS)
        thresholds.update(json.loads(
            Path(args.threshold_file).read_text(encoding='utf-8')))
    thresholds = parse_threshold_overrides(args.threshold, thresholds)
    result = analyse(args.logs, thresholds=thresholds)
    if args.baseline:
        baseline = json.loads(Path(args.baseline).read_text(encoding='utf-8'))
        compare_with_baseline(result, baseline)
    text = json.dumps(result, ensure_ascii=False, indent=2)
    if args.output:
        Path(args.output).write_text(text, encoding='utf-8')
    else:
        print(text)
    for warning in result.get('warnings', []):
        print('WARNING: %s' % warning, file=sys.stderr)
    print('verdict: %s (%d violation(s))' % (
        result.get('verdict'), len(result.get('violations', []))), file=sys.stderr)
    return 1 if result.get('verdict') == 'FAIL' else 0


if __name__ == '__main__':
    raise SystemExit(main())
