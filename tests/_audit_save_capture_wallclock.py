"""取证：保存捕获的墙钟时间——改动前 vs 改动后。

线上实测：一次保存捕获 1,020,000 步 / 7.77s。
本轮实测：894,477 步里只有 ~451ms 是真正的拷贝，其余全是事件循环往返。

``SavePreparation.SLICE_STEPS`` 以前是 256，于是每 256 步就要回一次事件
循环：894,477 / 256 = ~3,500 次往返。时间预算（4 ms）本来就能决定一次切片
走多远，步数上限只是把它顶掉了。把它放大到 16 384 之后，同一份数据应该
只需要 ~113 次往返。

这个探针用真实事件循环跑真实工程的数据形状，比较两档步数上限的：
  * 墙钟总时间
  * 切片次数（= 事件循环往返次数）
  * 纯工作时间
"""
from __future__ import annotations

import os
import sys
import time
from pathlib import Path

from PyQt6 import QtCore, QtWidgets

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from prism.fileio.sliced_copy import _copy_steps  # noqa: E402

PROJECT = Path(os.environ.get(
    'PRISM_AUDIT_PROJECT',
    r'C:\Users\admin\Desktop\Prism测试\Prism-100k-v1-新版.prism')).resolve()


def real_payload():
    """The material records of the real project, in their real shape."""
    from prism.storage.managed import database_path
    import json
    import sqlite3
    path = Path(database_path(PROJECT))
    connection = sqlite3.connect(path.as_uri() + '?mode=ro', uri=True)
    try:
        rows = connection.execute(
            'SELECT payload FROM material_records ORDER BY rowid').fetchall()
    finally:
        connection.close()
    records = [json.loads(payload) for (payload,) in rows]
    return records


def replay(value, slice_steps, slice_seconds):
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    generator = _copy_steps(value, {})
    state = dict(slices=0, steps=0, work=0.0, wait=0.0, done=False)
    timer = QtCore.QTimer()
    timer.setInterval(0)
    last_end = [time.perf_counter()]

    def drain():
        slice_start = time.perf_counter()
        state['wait'] += max(0.0, slice_start - last_end[0])
        start = time.perf_counter()
        try:
            for _ in range(slice_steps):
                next(generator)
                state['steps'] += 1
                if time.perf_counter() - start >= slice_seconds:
                    break
        except StopIteration:
            state['done'] = True
        end = time.perf_counter()
        state['work'] += end - slice_start
        state['slices'] += 1
        last_end[0] = end
        if state['done']:
            timer.stop()
            return
        timer.start(0)

    timer.timeout.connect(drain)
    started = time.perf_counter()
    timer.start(0)
    while not state['done']:
        app.processEvents(QtCore.QEventLoop.ProcessEventsFlag.AllEvents, 10)
    total = time.perf_counter() - started
    timer.deleteLater()
    return dict(total_s=round(total, 3), slices=state['slices'], steps=state['steps'],
                work_ms=round(state['work'] * 1000, 1),
                wait_ms=round(state['wait'] * 1000, 1))


def main():
    if not PROJECT.exists():
        print(f'真实工程不存在：{PROJECT}')
        return
    records = real_payload()
    print(f'真实材质记录：{len(records)} 条\n', flush=True)
    print(f'{"SLICE_STEPS":>12s} {"total_s":>9s} {"slices":>8s} '
          f'{"work_ms":>9s} {"wait_ms":>9s}', flush=True)
    for slice_steps in (256, 16384):
        report = replay(records, slice_steps, .004)
        print(f'{slice_steps:12,d} {report["total_s"]:9.3f} {report["slices"]:8,d} '
              f'{report["work_ms"]:9.1f} {report["wait_ms"]:9.1f}', flush=True)


if __name__ == '__main__':
    main()
