"""Summarize Prism's existing operation log, including rotated JSONL parts."""
import argparse
from collections import Counter, defaultdict
import json
from pathlib import Path
import statistics


def distribution(values):
    values = sorted(values)
    return dict(count=len(values), median_ms=round(statistics.median(values), 3),
                p95_ms=round(values[min(len(values)-1, int(.95*len(values)))], 3),
                max_ms=round(values[-1], 3))


def summarize(path):
    rows = []
    for part in reversed([path, *sorted(path.parent.glob(path.name+'.*'))]):
        for line in part.read_text(encoding='utf-8').splitlines():
            try:
                rows.append(json.loads(line))
            except ValueError:
                continue
    rows.sort(key=lambda row: (row.get('time', 0), row.get('sequence', 0)))
    phases = defaultdict(lambda: defaultdict(list))
    phase = 'startup'
    stalls, gpu, browser, rss, cancelled = [], [], [], [], 0
    queue = []
    full_payloads, patches = [], []
    for row in rows:
        event = row['event']
        if event == 'benchmark_phase':
            phase = row['name']
        elif event == 'operation':
            phases[phase][row['name']].append(row['duration_ms'])
        elif event == 'gui_stall':
            stalls.append(dict(phase=phase, duration_ms=row['duration_ms'], stack=row.get('stack'), last_action=row.get('last_action')))
        elif event == 'material_browser':
            data = row['metrics']
            if 'gpu' in data:
                gpu.append(data)
            browser.append(dict(phase=phase, **data))
        elif event == 'material_task_start':
            queue.append(row['queue_ms'])
        elif event == 'material_task_cancelled':
            cancelled += 1
        elif event == 'process' and row.get('rss_bytes'):
            rss.append(row['rss_bytes'])
        elif event == 'material_cards_payload':
            full_payloads.append(row['bytes'])
        elif event == 'material_cards_patch':
            patches.append(row['bytes'])
    return dict(events=len(rows), phases={name:{op:distribution(values) for op,values in operations.items()}
                for name,operations in phases.items()}, gui_stalls=stalls, gpu=gpu,
                peak_python_rss_bytes=max(rss, default=0), queue_ms=distribution(queue) if queue else None,
                cancelled_obsolete_tasks=cancelled, full_payload_bytes=full_payloads,
                card_patch_bytes=(dict(count=len(patches), median_bytes=int(statistics.median(patches)),
                                       max_bytes=max(patches)) if patches else None), browser=browser)


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('log', type=Path)
    parser.add_argument('--output', type=Path)
    args = parser.parse_args()
    text = json.dumps(summarize(args.log), ensure_ascii=False, indent=2)
    if args.output:
        args.output.write_text(text, encoding='utf-8')
    else:
        print(text)
