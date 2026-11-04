"""取证：保存捕获的墙钟时间到底被什么决定。

实测（真实工程）：894,477 步，纯拷贝只用 ~451ms，但捕获总耗时 7.77s。
每步是一次 `next()`，每片最多 SLICE_STEPS 步；每片结束都要
`timer.start(0)` 回事件循环，下一片再被调度回来。

这个探针用真实的事件循环重放同一份数据，比较不同 SLICE_STEPS 下：
  * 墙钟总时间
  * 每片实际工作时间
  * 事件循环往返次数
"""
from __future__ import annotations

import os
import time
from pathlib import Path

from PyQt6 import QtCore, QtWidgets

import sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from prism.fileio.sliced_copy import _copy_steps  # noqa: E402


def build_payload():
    """A payload with the same shape as the measured one: 5001 records of 15 keys."""
    records = []
    for index in range(5001):
        textures = {}
        for slot in range(8):
            textures[f'slot{slot}'] = {'width': 1024, 'height': 1024}
        records.append({
            'id': f'{index:032x}', 'name': f'材质 {index}', 'notes': '说明文字' * 20,
            'folderId': None, 'tags': ['a', 'b', 'c'],
            'textures': textures,
            'preview': {'signature': 'x' * 64, 'thumbnailAttachmentId': index,
                        'discardedAdjustments': {}},
            'nativePackages': [{'engineVersion': '1.0'}] * 3,
            'files': [{'path': f'/x/{index}.png'}] * 4,
            'status': 'ready', 'createdAt': '2026-01-01', 'updatedAt': '2026-01-01',
            'deletedAt': None,
        })
    return {'material_records': records, 'workspace_pages': [], 'workspace_nodes': []}


def replay(payload, slice_steps, slice_seconds):
    """Drive the copy through a real Qt event loop, exactly like the save does."""
    app = QtWidgets.QApplication.instance()
    generator = _copy_steps(payload, {})
    state = dict(slices=0, steps=0, work=0.0, wait=0.0, done=False)
    timer = QtCore.QTimer()
    timer.setInterval(0)
    clock = [time.perf_counter()]
    last_end = [clock[0]]

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
            clock[0] = end
            return
        timer.start(0)

    timer.timeout.connect(drain)
    started = time.perf_counter()
    timer.start(0)
    while not state['done']:
        app.processEvents(QtCore.QEventLoop.ProcessEventsFlag.AllEvents, 10)
    total = time.perf_counter() - started
    timer.deleteLater()
    return dict(total_s=round(total, 3), slices=state['slices'],
                steps=state['steps'], work_ms=round(state['work'] * 1000, 1),
                wait_ms=round(state['wait'] * 1000, 1))


def main():
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    payload = build_payload()
    print(f'payload: {len(payload["material_records"])} records\n', flush=True)
    print(f'{"SLICE_STEPS":>12s} {"total_s":>9s} {"slices":>8s} '
          f'{"work_ms":>9s} {"wait_ms":>9s}', flush=True)
    for slice_steps in (256, 1024, 4096, 16384, 65536):
        report = replay(payload, slice_steps, .004)
        print(f'{slice_steps:12,d} {report["total_s"]:9.3f} {report["slices"]:8,d} '
              f'{report["work_ms"]:9.1f} {report["wait_ms"]:9.1f}', flush=True)


if __name__ == '__main__':
    main()
