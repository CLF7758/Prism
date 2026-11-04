"""取证：把"画布 -> 模型库/材质库"切换那一刻的日志时间轴完整切出来。

目的：区分两种用户观感
  A. 真崩溃退出又重开  -> 会看到 session_start/session_end 成对出现 + 进程号变化
  B. GUI 冻结 + 整窗重绘 -> 单次 session 内 gui_stall / slow_dispatch 尖峰

用法：
  $env:PYTHONIOENCODING='utf-8'
  python tests/_audit_tab_switch_timeline.py [logfile]
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

INTEREST = (
    "page.library.construct",
    "page.model.open",
    "page.material.set_widget",
    "page.tab_changed",
    "materials.cards",
    "model_library",
    "gui_stall",
    "slow_dispatch",
    "slow_input_to_paint",
    "catalog",
    "session_start",
    "session_end",
    "window.",
)


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


def pick(row: dict) -> str:
    for key in ("event", "name", "op", "operation", "kind", "type", "msg", "message"):
        val = row.get(key)
        if isinstance(val, str):
            return val
    return ""


def main() -> int:
    if len(sys.argv) > 1:
        path = Path(sys.argv[1])
    else:
        appdata = Path(os.environ["APPDATA"]) / "Prism"
        files = sorted(appdata.glob("Prism-operations-*.jsonl"), key=lambda p: p.stat().st_mtime)
        path = files[-1]

    rows = load(path)
    print(f"log   : {path}")
    print(f"rows  : {len(rows)}")
    if not rows:
        return 1

    # 1) 进程/会话生命周期：真崩溃重开会表现为多段
    print("\n=== 1. 会话/进程生命周期 ===")
    life = [r for r in rows if pick(r) in ("session_start", "session_end") or "session_start" in pick(r)]
    for r in life[:20]:
        print(f"  {r.get('ts') or r.get('time') or r.get('timestamp')}  {pick(r)}  {json.dumps({k: v for k, v in r.items() if k not in ('ts', 'time', 'timestamp')}, ensure_ascii=False)[:220]}")
    pids = {r.get("pid") for r in rows if r.get("pid")}
    print(f"  distinct pid in file: {sorted(x for x in pids if x)}")
    sess = {r.get("session_id") or r.get("session") for r in rows}
    print(f"  distinct session keys: {len([x for x in sess if x])}")

    # 2) 所有 construct / stall 尖峰
    print("\n=== 2. 构造与卡顿尖峰 ===")
    spikes = []
    for r in rows:
        ev = pick(r)
        if not ev:
            continue
        dur = r.get("duration_ms") or r.get("elapsed_ms") or r.get("ms") or r.get("duration")
        if ev in ("page.library.construct", "gui_stall", "slow_dispatch", "slow_input_to_paint"):
            spikes.append((r.get("seq", r.get("ts", 0)), ev, dur, r))
    for _, ev, dur, r in spikes:
        extra = json.dumps(r, ensure_ascii=False)
        print(f"  {ev:<28} dur={dur!s:<12} {extra[:260]}")

    # 3) 切页事件时间轴（按 seq 排序）
    print("\n=== 3. 切页/库构造时间轴（seq 排序） ===")
    seq_rows = []
    for idx, r in enumerate(rows):
        ev = pick(r)
        if ev and any(ev.startswith(p) or p in ev for p in INTEREST):
            seq_rows.append((r.get("seq", idx), ev, r))
    seq_rows.sort(key=lambda x: (x[0] if isinstance(x[0], int) else 0))
    for seq, ev, r in seq_rows:
        dur = r.get("duration_ms") or r.get("elapsed_ms") or r.get("ms")
        tail = json.dumps(r, ensure_ascii=False)
        print(f"  seq={seq!s:<7} {ev:<34} {('dur=' + str(dur)) if dur else '':<16} {tail[:200]}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
