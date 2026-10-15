"""Summarise Prism JSONL interaction logs without loading them into memory."""
import argparse
from collections import Counter
import heapq
import json
from pathlib import Path


def analyse(paths, limit=20):
    counts = Counter()
    slow = []
    sessions = set()
    stalls = []
    dropped = 0
    malformed = 0
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
                milliseconds = entry.get('duration_ms', entry.get('blocked_ms', 0))
                if kind in ('operation', 'slow_dispatch', 'gui_stall', 'gui_recovered'):
                    entry.update(file=str(path), line=number)
                    # Bounded top entries; sequence avoids comparing dictionaries.
                    heapq.heappush(slow, (float(milliseconds), counts.total(), entry))
                    if len(slow) > limit:
                        heapq.heappop(slow)
                if kind == 'gui_stall':
                    stalls.append(entry)
                    stalls = stalls[-limit:]
    return dict(events=dict(counts), sessions=len(sessions), dropped_events=dropped,
                malformed_lines=malformed, slowest=[v[2] for v in sorted(slow, reverse=True)],
                recent_stalls=stalls,
                note='Stack samples identify where the GUI waited; correlate with operation timings before claiming a root cause.')


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('logs', nargs='+')
    parser.add_argument('--output')
    args = parser.parse_args()
    result = analyse(args.logs)
    text = json.dumps(result, ensure_ascii=False, indent=2)
    if args.output:
        Path(args.output).write_text(text, encoding='utf-8')
    else:
        print(text)


if __name__ == '__main__':
    main()
