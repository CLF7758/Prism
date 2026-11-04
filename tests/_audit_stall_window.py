"""取证：展开指定 sequence 区间内的全部日志事件，用于定位主线程冻结的真正调用者。

用法：
  $env:PYTHONIOENCODING='utf-8'
  python tests/_audit_stall_window.py <logfile> <start_seq> <end_seq> [--full]
"""
from __future__ import annotations

import json
import sys
from pathlib import Path


def main() -> int:
    path = Path(sys.argv[1])
    lo = int(sys.argv[2])
    hi = int(sys.argv[3])
    full = "--full" in sys.argv

    rows = []
    with path.open("r", encoding="utf-8", errors="replace") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except Exception:
                continue
            seq = row.get("sequence")
            if isinstance(seq, int) and lo <= seq <= hi:
                rows.append(row)

    base = None
    for row in rows:
        t = row.get("time")
        if isinstance(t, (int, float)):
            base = t if base is None else base
            rel = t - base
        else:
            rel = 0.0
        seq = row.get("sequence")
        ev = row.get("event")
        dur = row.get("duration_ms")
        if dur is None:
            dur = row.get("elapsed_ms")
        if dur is None:
            dur = row.get("duration")
        head = f"seq={seq:<6} +{rel:8.3f}s {ev:<26}"
        if dur is not None:
            head += f" dur={dur}"
        if full:
            print(head)
            print("    " + json.dumps(row, ensure_ascii=False))
        else:
            rest = {k: v for k, v in row.items()
                    if k not in ("schema_version", "event", "sequence", "session", "time",
                                 "monotonic", "pid", "thread_id", "thread_name", "priority")}
            print(head + "  " + json.dumps(rest, ensure_ascii=False)[:300])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
