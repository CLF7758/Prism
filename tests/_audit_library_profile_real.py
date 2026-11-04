"""取证：**真实工程**下预热占满 GUI 线程的代码级构成。

线上会话 `Prism-operations-156040.jsonl` 的实测：
* 预热窗口 1.795s（模型库 1.59s + 材质库），其中 MainThread 实占 1374.1ms；
* 最长的一次**硬冻结** 536ms = `materials.cards.create_surface`（Chromium 一次性初始化）；
* 其余是 59 个 109~264ms 量级的间隙。

没有工程的探针（`_audit_library_profile_cprofile.py`）只能看到 ~440ms，
差的那几百毫秒是**工程相关**的工作。这里在真实工程上重跑 cProfile，
找出那部分热点函数。

用法：
    $env:PYTHONIOENCODING='utf-8'; $env:QT_QPA_PLATFORM='offscreen'
    .\\.venv\\Scripts\\python.exe -m pytest tests\\_audit_library_profile_real.py -q -s --no-header -p no:cacheprovider --no-cov
"""
from __future__ import annotations

import cProfile
from collections import Counter
import gc
import io
import json
import os
import pstats
import sqlite3
import tempfile
import threading
import time
from pathlib import Path

import pytest
from PyQt6 import QtCore, QtWidgets

PROJECT = Path(os.environ.get(
    'PRISM_AUDIT_PROJECT',
    r'C:\Users\admin\Desktop\Prism测试\Prism-100k-v1-新版.prism')).resolve()


def _open_project(qtbot, window):
    view = window.view
    from prism.storage.managed import database_path
    expected = Path(database_path(PROJECT)).resolve()
    view.open_from_file(str(PROJECT), readonly=True)

    def loading():
        worker = getattr(view, 'worker', None)
        return (getattr(view, '_project_opening', False)
                or (worker is not None and worker.isRunning()))

    def opened():
        return (not loading() and bool(view.filename)
                and Path(view.filename).resolve() == expected
                and not getattr(view, '_paged_preparing', False))

    qtbot.waitUntil(lambda: loading() or opened(), timeout=60000)
    qtbot.waitUntil(opened, timeout=300000)
    assert not view._paged_open_failed, view._paged_open_failed_reason
    assert view.scene.readonly_project
    print('AUDIT: project opened', flush=True)
    return view


def _dump(profiler, title):
    stream = io.StringIO()
    stats = pstats.Stats(profiler, stream=stream).sort_stats('tottime')
    stats.print_stats(30)
    print(f'\n===== {title} =====')
    print(stream.getvalue())


def _configure_audit(window, request):
    """Use the shipped theme and real diagnostics without retaining reports."""
    app = QtWidgets.QApplication.instance()
    original_theme = app.styleSheet()
    assets = Path(__file__).resolve().parents[1] / 'prism/assets'
    theme = (assets / 'theme.qss').read_text(encoding='utf-8')
    theme = theme.replace('url(assets/icons/', 'url(' + (assets / 'icons').as_posix() + '/')
    app.setStyleSheet(theme)
    trace_directory = tempfile.TemporaryDirectory(prefix='prism-prewarm-audit-')
    from prism.performance_trace import install
    trace = install(app, window, trace_directory.name)
    request.addfinalizer(lambda: app.setStyleSheet(original_theme))
    request.addfinalizer(trace_directory.cleanup)
    request.addfinalizer(trace.close)
    return trace


class _GcMeter:
    """Count the collector's passes and how long they took.

    The same ``gc.callbacks`` hook the shipped tracer uses, so the numbers
    here describe the same events the trace records.
    """

    def __init__(self):
        self.reset()
        self._started = 0.0

    def reset(self):
        self.passes = 0
        self.full_passes = 0
        self.seconds = 0.0
        self.longest = 0.0

    def callback(self, phase, info):
        if phase == 'start':
            self._started = time.perf_counter()
            return
        elapsed = time.perf_counter() - self._started
        self.passes += 1
        self.seconds += elapsed
        self.longest = max(self.longest, elapsed)
        if info.get('generation') == 2:
            self.full_passes += 1

    def __enter__(self):
        gc.callbacks.append(self.callback)
        return self

    def __exit__(self, *exc):
        if self.callback in gc.callbacks:
            gc.callbacks.remove(self.callback)
        return False


def _resident_bytes():
    """The process' resident set size, so a threshold that trades memory for
    fewer pauses can be judged on both sides."""
    import ctypes
    from ctypes import wintypes

    class MEMORYSTATUSEX(ctypes.Structure):
        _fields_ = [('dwLength', wintypes.DWORD), ('dwMemoryLoad', wintypes.DWORD),
                    ('ullTotalPhys', ctypes.c_ulonglong),
                    ('ullAvailPhys', ctypes.c_ulonglong),
                    ('ullTotalPageFile', ctypes.c_ulonglong),
                    ('ullAvailPageFile', ctypes.c_ulonglong),
                    ('ullTotalVirtual', ctypes.c_ulonglong),
                    ('ullAvailVirtual', ctypes.c_ulonglong),
                    ('ullAvailExtendedVirtual', ctypes.c_ulonglong)]

    status = MEMORYSTATUSEX()
    status.dwLength = ctypes.sizeof(MEMORYSTATUSEX)
    ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status))
    return status.ullTotalPhys - status.ullAvailPhys


def test_real_project_prewarm_profile(main_window, qtbot, monkeypatch, request):
    if not PROJECT.exists():
        pytest.skip(f'真实工程不存在：{PROJECT}')
    gap = os.environ.get('AUDIT_SLICE_GAP_MS')
    if gap is not None:
        from prism.widgets import widget_construction
        widget_construction.WidgetConstruction.SLICE_GAP_MS = int(gap)
        print(f'\nAUDIT: SLICE_GAP_MS={gap}', flush=True)
    window = main_window
    _configure_audit(window, request)
    window.resize(1440, 900)
    window.show()
    # The collector's policy is the variable under test: the shipped one by
    # default, the old default (700, 10, 10) when the probe is asked for it.
    threshold2 = os.environ.get('AUDIT_GC_THRESHOLD2')
    if threshold2 is not None:
        gc.set_threshold(700, 10, int(threshold2))
    else:
        from prism.__main__ import tune_gc_for_large_projects
        tune_gc_for_large_projects()
    print(f'\nAUDIT: GC 阈值 {gc.get_threshold()}', flush=True)
    if os.environ.get('AUDIT_WARM_ENGINE', '1') == '1':
        # 生产启动路径就是这样的：窗口出现之前先把 Chromium 的启动成本付掉。
        from prism.webengine_runtime import warm_web_engine
        started = time.perf_counter()
        warm_web_engine(QtWidgets.QApplication.instance())
        print(f'\nAUDIT: WebEngine 预热 {(time.perf_counter()-started)*1000:.1f}ms', flush=True)
    # 断开"打开完成即预热"的接线：否则打开过程中面板已经建好，
    # 下面测的就是空操作。
    try:
        window.view.project_open_finished.disconnect(window.schedule_library_prewarm)
    except (TypeError, RuntimeError):
        pass
    # The collector's full passes happen while the project is being opened:
    # that is where 99,988 catalog rows become live objects.  Measuring the
    # open is therefore the only place the threshold shows up.
    open_meter = _GcMeter()
    open_started = time.perf_counter()
    rss_before = _resident_bytes()
    with open_meter:
        _open_project(qtbot, window)
    open_ms = (time.perf_counter() - open_started) * 1000
    rss_delta = (_resident_bytes() - rss_before) / 1024 / 1024
    live_objects = sum(gc.get_count())
    print(f'\nAUDIT: 打开工程 {open_ms:.1f}ms  GC 次数={open_meter.passes} '
          f'全量回收={open_meter.full_passes} GC 总耗时={open_meter.seconds*1000:.1f}ms '
          f'最长一次={open_meter.longest*1000:.1f}ms  存活对象={live_objects} '
          f'RSS 增量={rss_delta:.1f}MB', flush=True)
    # 让打开收尾的 GUI 工作先落地，再测预热。
    qtbot.wait(800)

    # A generator's cProfile ncalls includes resumptions, not widget instances.
    # Attribute row conversions only when the construction job is on the stack.
    from prism.project_catalog_index import ProjectCatalogIndex
    from prism.widgets.widget_construction import WidgetConstruction
    from prism.widgets.material_slots import MaterialSlot
    from prism.storage.managed import database_path
    active = threading.local()
    conversions = Counter()
    real_advance = WidgetConstruction._advance
    real_project_asset = ProjectCatalogIndex._project_asset

    def advance(job):
        active.value = True
        try:
            return real_advance(job)
        finally:
            active.value = False

    def project_asset(row):
        conversions[(threading.current_thread().name, getattr(active, 'value', False))] += 1
        return real_project_asset(row)

    monkeypatch.setattr(WidgetConstruction, '_advance', advance)
    monkeypatch.setattr(ProjectCatalogIndex, '_project_asset', staticmethod(project_asset))
    metadata = Path(database_path(PROJECT))
    monitor = sqlite3.connect(metadata.as_uri() + '?mode=ro', uri=True)
    before = monitor.execute('PRAGMA data_version').fetchone()[0]
    reports = []

    def measure(host, title):
        ticks = [time.perf_counter()]
        timer = QtCore.QTimer(window)
        timer.setInterval(16)
        timer.timeout.connect(lambda: ticks.append(time.perf_counter()))
        timer.start()
        profiler = cProfile.Profile()
        if os.environ.get('AUDIT_PROFILE') == '1':
            profiler.enable()
        host.prewarm()
        qtbot.waitUntil(lambda: host.initialized, timeout=180000)
        profiler.disable()
        timer.stop()
        timer.deleteLater()
        ticks.append(time.perf_counter())
        gap = max(b - a for a, b in zip(ticks, ticks[1:])) * 1000
        job = host._construction
        reports.append(dict(library=title, wall_ms=round((ticks[-1]-ticks[0])*1000, 2),
            max_heartbeat_gap_ms=round(gap, 2), slices=job.slices,
            max_slice_ms=round(job.max_slice_ms, 2),
            slot_instances=len(host.ensure_created().details.findChildren(MaterialSlot))))
        print(f'\nAUDIT: {title} wall={(ticks[-1]-ticks[0])*1000:.1f}ms '
              f'最长心跳间隙={gap:.1f}ms 切片={getattr(job, "slices", "-")} '
              f'max_slice={getattr(job, "max_slice_ms", "-")} '
              f'max_step={getattr(job, "max_step_ms", "-")}', flush=True)
        if os.environ.get('AUDIT_PROFILE') == '1':
            _dump(profiler, f'真实工程 · {title}（tottime）')

    try:
        meter = _GcMeter()
        with meter:
            measure(window.model_library, '模型库预热')
            measure(window.material_library, '材质库预热')
        unchanged = monitor.execute('PRAGMA data_version').fetchone()[0] == before
        print('AUDIT_JSON ' + json.dumps(dict(reports=reports, source_unchanged=unchanged,
            theme=True, trace=True, gap_ms=WidgetConstruction.SLICE_GAP_MS,
            open=dict(wall_ms=round(open_ms, 1), gc_passes=open_meter.passes,
                      gc_full_passes=open_meter.full_passes,
                      gc_ms=round(open_meter.seconds * 1000, 1),
                      longest_gc_ms=round(open_meter.longest * 1000, 1),
                      live_objects=live_objects,
                      rss_delta_mb=round(rss_delta, 1)),
            gc=dict(thresholds=gc.get_threshold(), passes=meter.passes,
                    full_passes=meter.full_passes,
                    gc_ms=round(meter.seconds * 1000, 1),
                    longest_gc_ms=round(meter.longest * 1000, 1)),
            row_conversions=[dict(thread=k[0], during_construction=k[1], count=v)
                             for k, v in conversions.items()]), ensure_ascii=False), flush=True)
        assert unchanged, 'The prewarm must not commit to the source project'
    finally:
        monitor.close()
