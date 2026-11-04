"""Audit a Prism trace for GC pauses, GUI stalls and their real cost.

The trace emits one ``slow_dispatch`` record per *sample group*, so several
records with the same timestamp are one stall, not several.  This probe
prints the event inventory, the timeline around the largest stalls and the
GC pauses, so a stall is attributed once and in context.

Run: ``python -u tests/_audit_trace_gc.py [trace.jsonl]``
"""
from __future__ import annotations

import json
import os
import sys
from collections import Counter, defaultdict
from pathlib import Path

DEFAULT_TRACE = Path(os.environ.get('APPDATA', '')) / 'Prism' / 'Prism-operations-156040.jsonl'


def load(path: Path) -> list[dict]:
    events = []
    with path.open('r', encoding='utf-8', errors='replace') as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            try:
                events.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return events


def name_of(event: dict) -> str:
    return str(event.get('event') or event.get('name') or event.get('kind') or '?')


def duration_of(event: dict) -> float:
    for key in ('duration_ms', 'ms', 'elapsed_ms', 'duration'):
        value = event.get(key)
        if isinstance(value, (int, float)):
            return float(value)
    return 0.0


def stamp_of(event: dict) -> float:
    for key in ('t', 'timestamp', 'time', 'ts'):
        value = event.get(key)
        if isinstance(value, (int, float)):
            return float(value)
    return 0.0


def describe(event: dict) -> str:
    parts = []
    for key in ('detail', 'func', 'label', 'slot', 'phase', 'stage', 'kind',
                'state', 'scope', 'reason', 'trigger', 'generation', 'name',
                'event_name', 'panel', 'surface'):
        value = event.get(key)
        if value not in (None, '', 0):
            parts.append(f'{key}={str(value)[:70]}')
    return ' '.join(parts)


def inventory(events: list[dict]) -> None:
    counts = Counter()
    totals = defaultdict(float)
    first, last = {}, {}
    for event in events:
        name = name_of(event)
        counts[name] += 1
        totals[name] += duration_of(event)
        stamp = stamp_of(event)
        first.setdefault(name, stamp)
        last[name] = stamp
    print(f'== event inventory ({len(events)} records) ==')
    for name, count in counts.most_common(40):
        print(f'  {count:5d}  {totals[name]:10.1f}ms  t={first[name]:.3f}..{last[name]:.3f}  {name}')


def timeline(events: list[dict], center: float, window: float) -> None:
    print(f'\n== timeline around t={center:.3f} (+/- {window:.1f}s) ==')
    rows = []
    for event in events:
        stamp = stamp_of(event)
        if abs(stamp - center) <= window:
            rows.append((stamp, duration_of(event), name_of(event), describe(event)))
    rows.sort()
    for stamp, duration, name, detail in rows:
        print(f'  t={stamp:.3f}  {duration:9.1f}ms  {name:28s} {detail}')


def gc_events(events: list[dict]) -> None:
    print('\n== gc events ==')
    rows = []
    for event in events:
        if 'gc' not in name_of(event).lower():
            continue
        rows.append((stamp_of(event), duration_of(event), describe(event)))
    rows.sort(key=lambda item: item[1], reverse=True)
    for stamp, duration, detail in rows[:20]:
        print(f'  t={stamp:.3f}  {duration:9.1f}ms  {detail}')
    if not rows:
        print('  (none)')


def stalls(events: list[dict]) -> None:
    print('\n== gui_stall / slow_dispatch records ==')
    grouped = defaultdict(list)
    for event in events:
        name = name_of(event)
        if name not in ('gui_stall', 'slow_dispatch'):
            continue
        grouped[round(stamp_of(event), 2)].append((name, duration_of(event), event))
    for stamp in sorted(grouped, key=lambda item: max(d[1] for d in grouped[item]), reverse=True)[:12]:
        entries = grouped[stamp]
        best = max(entries, key=lambda item: item[1])
        print(f'  t={stamp:.3f}  records={len(entries)}  max={best[1]:.1f}ms  {best[0]}')
        histogram = best[2].get('stack_histogram') or []
        for count, stack in histogram[:4]:
            print(f'       {count:4d}  {stack}')


def main() -> int:
    path = Path(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_TRACE
    if not path.exists():
        print(f'missing trace: {path}')
        return 1
    events = load(path)
    print(f'trace={path} events={len(events)}')
    if len(sys.argv) > 2:
        center = float(sys.argv[2])
        window = float(sys.argv[3]) if len(sys.argv) > 3 else 2.0
        if center < 0:
            # -1 dumps the raw records of the slowest dispatches, so the
            # sample count can be read next to the histogram.
            records = [event for event in events
                       if name_of(event) in ('gui_stall', 'slow_dispatch')]
            records.sort(key=duration_of, reverse=True)
            for event in records[:6]:
                print(json.dumps(event, ensure_ascii=False)[:1600])
                print()
            return 0
        timeline(events, center, window)
        return 0
    if events:
        stamps = [stamp_of(event) for event in events if stamp_of(event)]
        print(f'span t={min(stamps):.3f}..{max(stamps):.3f} ({max(stamps) - min(stamps):.1f}s)')
    inventory(events)
    gc_events(events)
    stalls(events)
    worst = max(events, key=lambda event: duration_of(event)
                if name_of(event) in ('gui_stall', 'slow_dispatch') else 0.0)
    if duration_of(worst) > 0:
        timeline(events, stamp_of(worst), 2.0)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
