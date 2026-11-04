"""取证：回答三个问题
  Q1 模型库 page.library.construct 1723ms 的构成（哪些子事件、各占多少）
  Q2 catalog（目录索引）在本会话的生命周期：什么时候 ready、什么时候 not_ready
  Q3 两次点击（画布->模型库 / 画布->材质库）各自阻塞了多久、由哪个 operation 主导
"""
from __future__ import annotations

import json
import sys
from collections import Counter, defaultdict
from pathlib import Path


def load(path: Path) -> list[dict]:
    rows = []
    with path.open("r", encoding="utf-8", errors="replace") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except Exception:
                continue
    return rows


def main() -> int:
    path = Path(sys.argv[1])
    rows = load(path)
    by_seq = {r.get("sequence"): r for r in rows}

    print("=== Q1 模型库切换：seq 450-456 完整字段 ===")
    for seq in range(450, 457):
        r = by_seq.get(seq)
        if not r:
            continue
        keep = {k: v for k, v in r.items()
                if k not in ("schema_version", "session", "time", "monotonic", "project_token",
                             "open_epoch", "generation", "origin_action", "role")}
        print(f"  seq={seq}: " + json.dumps(keep, ensure_ascii=False)[:900])

    print("\n=== Q2 catalog 事件全览 ===")
    cat = [r for r in rows if "catalog" in str(r.get("event", "")).lower()
           or "catalog" in str(r.get("reason", "")).lower()
           or "catalog" in str(r.get("name", "")).lower()]
    print(f"  catalog 相关事件总数: {len(cat)}")
    c = Counter(str(r.get("event")) for r in cat)
    for name, n in c.most_common():
        print(f"    {name:<34} {n}")
    print("  -- not_ready / miss 明细（前 30）--")
    for r in cat:
        if str(r.get("reason", "")) == "not_ready" or r.get("event") == "catalog_scope_miss":
            keep = {k: v for k, v in r.items()
                    if k in ("sequence", "event", "reason", "partial", "scope", "folder",
                             "rows", "pages", "logical_rows", "stage", "name")}
            print("    " + json.dumps(keep, ensure_ascii=False))

    print("\n=== Q2b 索引 build / ready 相关 ===")
    for r in rows:
        ev = str(r.get("event", ""))
        nm = str(r.get("name", ""))
        if any(k in ev.lower() or k in nm.lower()
               for k in ("build", "ready", "index", "generation", "open_epoch")):
            keep = {k: v for k, v in r.items()
                    if k in ("sequence", "event", "name", "stage", "status", "duration_ms",
                             "reason", "partial", "rows", "generation", "open_epoch")}
            print("    " + json.dumps(keep, ensure_ascii=False)[:220])

    print("\n=== Q3 顶层 operation（role=end）按耗时排序 Top 25 ===")
    ends = []
    for r in rows:
        if r.get("role") == "end" and isinstance(r.get("duration_ms"), (int, float)):
            ends.append((float(r["duration_ms"]), r.get("sequence"), r.get("name") or r.get("event"),
                         r.get("parent_span_id"), r.get("trace_id")))
    ends.sort(reverse=True)
    for dur, seq, nm, parent, tid in ends[:25]:
        print(f"  {dur:10.1f}ms  seq={seq:<6} name={nm:<42} parent={parent}")

    print("\n=== Q4 每次输入的 input->paint 延迟（Top 12）===")
    lat = []
    for r in rows:
        if r.get("event") == "slow_input_to_paint":
            lat.append((float(r.get("latency_ms") or 0), r.get("sequence"), r.get("target")))
    lat.sort(reverse=True)
    for v, seq, tgt in lat[:12]:
        print(f"  {v:10.1f}ms  seq={seq:<6} target={tgt}")

    print("\n=== Q5 事件类型计数 Top 30 ===")
    c2 = Counter(str(r.get("event")) for r in rows)
    for name, n in c2.most_common(30):
        print(f"  {name:<36} {n}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
