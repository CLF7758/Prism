"""取证：库面板构造的**每一步**耗时与它停在源码的哪一行。

目的：预热把构造成本提前了，但没有消除冻结（用户实测：模型面板约 1.59s、
材质面板约 1.87s；线上日志 `Prism-operations-156040.jsonl` 显示
`page.library.construct_slice` 在 MainThread 上合计 1374.1ms，最大单片 445.5ms，
其中 `materials.cards.create_surface` 占 445.3ms）。本脚本把切片拆到源码行，
指出"哪些行必须搬走、哪些行只需要再切细"。

用法：
    $env:PYTHONIOENCODING='utf-8'; $env:QT_QPA_PLATFORM='offscreen'
    .\\.venv\\Scripts\\python.exe -m pytest tests\\_audit_library_step_profile.py -q -s --no-header -p no:cacheprovider --no-cov
"""
from __future__ import annotations

import time

import pytest


@pytest.fixture(autouse=True)
def _instrument(monkeypatch):
    from prism.widgets import widget_construction

    steps = []

    class Probe(widget_construction.WidgetConstruction):
        def _advance(self):
            before = getattr(self.steps, 'gi_frame', None)
            line_before = before.f_lineno if before is not None else -1
            where = before.f_code.co_filename if before is not None else ''
            func = before.f_code.co_name if before is not None else ''
            started = time.perf_counter()
            super()._advance()
            elapsed = (time.perf_counter() - started) * 1000
            after = getattr(self.steps, 'gi_frame', None)
            line_after = after.f_lineno if after is not None else -1
            steps.append((elapsed, line_before, line_after, where, func))

    monkeypatch.setattr(widget_construction, 'WidgetConstruction', Probe)
    yield steps


def _report(steps, title):
    print(f'\n===== {title} =====')
    total = sum(item[0] for item in steps)
    print(f'  总切片 {len(steps)} 片，GUI 占用 {total:.1f}ms')
    for elapsed, line_before, line_after, where, func in sorted(steps, reverse=True)[:18]:
        short = where.replace('C:\\Users\\admin\\Desktop\\Prism-main\\Prism-main\\', '')
        print(f'  {elapsed:8.1f}ms  {short}:{line_after}  ({func})')


def test_library_step_profile(main_window, qtbot, _instrument):
    window = main_window
    window.resize(1440, 900)
    window.show()

    steps = _instrument
    window.model_library.prewarm()
    qtbot.waitUntil(lambda: window.model_library.initialized, timeout=120000)
    _report(steps, '模型库构造')
    steps.clear()

    window.material_library.prewarm()
    qtbot.waitUntil(lambda: window.material_library.initialized, timeout=120000)
    _report(steps, '材质库构造')


def test_qsettings_native_lock(tmp_path,qapp):
 from PyQt6 import QtCore
 import threading,subprocess,sys
 path=str(tmp_path/'probe.ini')
 reader=QtCore.QSettings(path,QtCore.QSettings.Format.IniFormat)
 reader.setValue('one','original')
 reader.sync()
 created=threading.Event()
 go=threading.Event()
 entered=threading.Event()
 finished=threading.Event()
 timing={}
 def sync():
  writer=QtCore.QSettings(path,QtCore.QSettings.Format.IniFormat)
  created.set()
  assert go.wait(4)
  entered.set()
  started=time.perf_counter()
  writer.sync()
  timing['sync_ms']=(time.perf_counter()-started)*1000
  finished.set()
 worker=threading.Thread(target=sync)
 worker.start()
 assert created.wait(2)
 code="from PyQt6.QtCore import QLockFile; import sys,time; lock=QLockFile(sys.argv[1]+'.lock'); assert lock.tryLock(0); print('READY',flush=True); time.sleep(.5); lock.unlock()"
 holder=subprocess.Popen([sys.executable,'-c',code,path],stdout=subprocess.PIPE,text=True)
 assert holder.stdout.readline().strip()=='READY'
 reader.setValue('one','latest')
 go.set()
 assert entered.wait(2)
 time.sleep(.02)
 started=time.perf_counter()
 result=reader.value('one')
 timing['gui_value_ms']=(time.perf_counter()-started)*1000
 assert result=='latest'
 holder.wait(timeout=4)
 worker.join(4)
 assert finished.is_set()
 print('NATIVE_SHARED_LOCK',timing,flush=True)
