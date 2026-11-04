"""Audit the model-library job pipeline in a Prism trace.

The question this probe answers: when ``model/job_preview`` takes seconds,
where does the time go?  It prints, in chronological order, every measured
library stage plus the native job lifecycle, so a queued preview can be
separated from the native install it waits for.

Run: ``python -u tests/_audit_model_queue.py [trace.jsonl]``
"""
from __future__ import annotations

import json
import os
import sys
from collections import Counter, defaultdict
from pathlib import Path

DEFAULT_TRACE = Path(os.environ.get('APPDATA', '')) / 'Prism' / 'Prism-operations-156040.jsonl'

LIFECYCLE = {
    'queued', 'started', 'source_prepared', 'native_loaded', 'rendered',
    'output_written', 'installed', 'completed', 'failed', 'cancelled', 'stale',
}


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


def main() -> int:
    path = Path(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_TRACE
    if not path.exists():
        print(f'missing trace: {path}')
        return 1
    events = load(path)
    stamps = [stamp_of(event) for event in events if stamp_of(event)]
    print(f'trace={path} events={len(events)} span={max(stamps) - min(stamps):.1f}s')

    stages = [event for event in events if name_of(event) == 'library.stage.duration']
    print(f'\n== library.stage.duration ({len(stages)} records) ==')
    totals = defaultdict(float)
    counts = Counter()
    for event in stages:
        key = f"{event.get('domain')}/{event.get('stage')}"
        totals[key] += duration_of(event)
        counts[key] += 1
    for key, total in sorted(totals.items(), key=lambda item: -item[1]):
        print(f'  {counts[key]:4d}  {total:10.1f}ms  {key}')

    print('\n== model stages in order ==')
    rows = []
    for event in stages:
        if event.get('domain') != 'model':
            continue
        rows.append((stamp_of(event), duration_of(event), str(event.get('stage')),
                     event.get('queue_ms'), event.get('job_id') or '',
                     event.get('model_id') or ''))
    rows.sort()
    for stamp, duration, stage, queue_ms, job_id, model_id in rows:
        queue = '' if queue_ms is None else f' queue={float(queue_ms):8.1f}ms'
        print(f'  t={stamp:9.3f} {duration:9.1f}ms {stage:24s}{queue} '
              f'{str(job_id)[:12]} {str(model_id)[:12]}')

    print('\n== native job lifecycle ==')
    life = []
    for event in events:
        name = name_of(event)
        if not (name.startswith('library.') or name.startswith('material.native')):
            continue
        tail = name.split('.')[-1]
        if tail not in LIFECYCLE:
            continue
        life.append((stamp_of(event), duration_of(event), name, event))
    life.sort(key=lambda item: item[0])
    for stamp, duration, name, event in life:
        extra = ''
        for key in ('action', 'error_code', 'reason', 'elapsed_ms', 'source_read_parse_ms'):
            value = event.get(key)
            if value not in (None, '', 0):
                extra += f' {key}={str(value)[:60]}'
        timing = event.get('timing')
        if isinstance(timing, dict):
            extra += ' timing=' + json.dumps(timing, ensure_ascii=False)[:300]
        print(f'  t={stamp:9.3f} {duration:9.1f}ms {name:44s}{extra}')

    print('\n== native timing events ==')
    for event in events:
        if name_of(event) != 'material.native.timing':
            continue
        print('  ' + json.dumps(event, ensure_ascii=False)[:1400])

    print('\n== scheduler snapshots ==')
    for event in events:
        if name_of(event) == 'library.scheduler.snapshot':
            print(f'  t={stamp_of(event):9.3f} ' + json.dumps(
                {k: event.get(k) for k in
                 ('action', 'submitted', 'replaced', 'completed', 'pending', 'active',
                  'heap_entries', 'model_id')}, ensure_ascii=False))

    print('\n== job_preview spans (paired start/end) ==')
    opens = {}
    spans = []
    QUEUE_MS = {}
    for event in events:
        name = name_of(event)
        if not name.startswith('library.job_preview.'):
            continue
        operation = event.get('operation_id')
        if not operation:
            continue
        stamp = stamp_of(event)
        if name.endswith('.started'):
            opens[operation] = (stamp, event)
            QUEUE_MS[operation] = float(event.get('queue_ms') or 0.0)
        elif name.endswith('.completed') and operation in opens:
            start, first = opens.pop(operation)
            spans.append((start, stamp, stamp - start, first.get('action'),
                          first.get('model_id') or '', operation))
    spans.sort()
    native = [(stamp_of(event), duration_of(event))
              for event in events if name_of(event) == 'library.stage.duration'
              and event.get('stage') == 'prepare_native_install']
    native.sort()
    wait_total = 0.0
    for start, end, duration, action, model_id, operation in spans:
        inside = [f'{d:.1f}ms@+{s - start:.0f}' for s, d in native if start <= s <= end]
        wait = QUEUE_MS.get(operation, 0.0)
        wait_total += wait
        print(f'  {start:9.3f} -> {end:9.3f} {duration:8.1f}ms wait={wait:8.1f}ms '
              f'{str(model_id)[:10]} native_install=[{",".join(inside)}]')
    print(f'  spans={len(spans)} total_work={sum(s[2] for s in spans):.1f}s '
          f'total_wait={wait_total:.1f}s')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
