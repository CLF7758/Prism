"""取证：真实工程下**第一次点击**材质库时，那 2.4 秒的 GUI 硬间隙是什么。

点击路径实测（`Prism-operations` 与离屏审计）：
* 点击会等面板构造完成（`ensure_created`），构造是切片的，所以"点击耗时"
  不等于"窗口冻结"；真正的冻结是**最长心跳间隙**。
* 修复前：材质首请求 747ms / 间隙 137.7ms；修复后：4168ms / 间隙 2424.1ms。

本脚本在点击那一刻挂上 cProfile，指出那 2.4 秒具体花在哪里。

用法：
    $env:PYTHONIOENCODING='utf-8'; $env:QT_QPA_PLATFORM='offscreen'
    .\\.venv\\Scripts\\python.exe -m pytest tests\\_audit_first_click_profile.py -q -s --no-header -p no:cacheprovider --no-cov
"""
from __future__ import annotations

import cProfile
import io
import json
import os
import pstats
import sqlite3
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
    assert view.scene.readonly_project
    print('AUDIT: project opened', flush=True)
    return view


def test_first_click_profile(main_window, qtbot, request, monkeypatch):
    if not PROJECT.exists():
        pytest.skip(f'真实工程不存在：{PROJECT}')
    window = main_window
    if os.environ.get('AUDIT_DETAIL') == '1':
        from prism.widgets.deferred_library import InspectorHost
        from prism.project_open_scope import ProjectOpenScope
        from prism.config import PrismSettings
        from prism.widgets.resource_tree_view import ResourceTreeView
        def timed(owner, name):
            original = getattr(owner, name)
            def call(*args, **kwargs):
                started = time.perf_counter()
                try:
                    return original(*args, **kwargs)
                finally:
                    elapsed = (time.perf_counter() - started) * 1000
                    if elapsed >= 40:
                        print(f'AUDIT_CALL {owner.__name__}.{name} {elapsed:.2f}ms', flush=True)
            monkeypatch.setattr(owner, name, call)
        for owner, name in ((InspectorHost, 'setVisible'),
                            (ProjectOpenScope, 'telemetry_state'),
                            (PrismSettings, 'value'), (PrismSettings, 'setValue'),
                            (ResourceTreeView, '_settings_store'),
                            (ResourceTreeView, '_expanded_node_ids')):
            timed(owner, name)
    from tests._audit_library_profile_real import _configure_audit
    trace = _configure_audit(window, request)
    window.resize(1440, 900)
    window.show()
    if os.environ.get('AUDIT_WARM_ENGINE', '1') == '1':
        from prism.webengine_runtime import warm_web_engine
        started = time.perf_counter()
        warm_web_engine(QtWidgets.QApplication.instance())
        print(f'\nAUDIT: WebEngine 预热 {(time.perf_counter()-started)*1000:.1f}ms', flush=True)
    window.view.project_open_finished.disconnect(window.schedule_library_prewarm)
    _open_project(qtbot, window)
    from prism.storage.managed import database_path
    metadata = Path(database_path(PROJECT))
    monitor = sqlite3.connect(metadata.as_uri() + '?mode=ro', uri=True)
    request.addfinalizer(monitor.close)
    source_version = monitor.execute('PRAGMA data_version').fetchone()[0]
    window.schedule_library_prewarm()
    reports = []
    audit_started = time.time()

    def measure(callback, ready, title):
        ticks = [time.perf_counter()]
        timer = QtCore.QTimer(window)
        timer.setInterval(16)
        timer.timeout.connect(lambda: ticks.append(time.perf_counter()))
        timer.start()
        profiler = cProfile.Profile()
        if os.environ.get('AUDIT_PROFILE') == '1':
            profiler.enable()
        from prism.user_activity import USER_ACTIVITY
        USER_ACTIVITY.note_input()
        callback()
        returned = time.perf_counter()
        qtbot.waitUntil(ready, timeout=120000)
        profiler.disable()
        timer.stop()
        timer.deleteLater()
        ticks.append(time.perf_counter())
        gap = max(b - a for a, b in zip(ticks, ticks[1:])) * 1000
        reports.append(dict(operation=title, call_ms=round((returned-ticks[0])*1000, 2),
                            ready_ms=round((ticks[-1]-ticks[0])*1000, 2),
                            max_heartbeat_gap_ms=round(gap, 2)))
        print(f'\nAUDIT: {title} call={(ticks[-1]-ticks[0])*1000:.1f}ms '
              f'最长心跳间隙={gap:.1f}ms ticks={len(ticks)-2}', flush=True)
        if os.environ.get('AUDIT_PROFILE') == '1':
            stream = io.StringIO()
            pstats.Stats(profiler, stream=stream).sort_stats('tottime').print_stats(22)
            print(stream.getvalue(), flush=True)

    order = ['material', 'model'] if os.environ.get('AUDIT_CLICK_TARGET', 'material') == 'material' else ['model', 'material']
    def ready(name):
        host = getattr(window, name + '_library')
        cards = host.cards if name == 'model' else host.materials
        return (cards is not None and cards.ready and bool(cards._record_ids)
                and window.tabs.currentWidget() is host and cards.isVisible())
    for name in order:
        measure(getattr(window, 'open_' + name + '_library'), lambda name=name: ready(name), '首次点击' + name)
    for name in order:
        measure(getattr(window, 'open_' + name + '_library'), lambda name=name: ready(name), '重复切换' + name)
    unchanged = monitor.execute('PRAGMA data_version').fetchone()[0] == source_version
    assert unchanged
    print('AUDIT_CLICK_JSON ' + json.dumps(dict(reports=reports, source_unchanged=unchanged,
          first_target=order[0], theme=True, trace=True, readonly=True), ensure_ascii=False), flush=True)
    qtbot.wait(80)
    trace.close()
    if os.environ.get('AUDIT_STACKS', '1') == '1':
        from tools.analyze_prism_trace import iter_entries, resolve_inputs
        for _path, _line, entry, error in iter_entries(resolve_inputs([str(trace.path)])):
            if error or not entry or entry.get('time', 0) < audit_started:
                continue
            if entry.get('event') not in ('gui_stall', 'gc_pause', 'slow_dispatch'):
                continue
            if max(entry.get('duration_ms', 0), entry.get('heartbeat_delay_ms', 0)) < 80:
                continue
            keys = ('event', 'duration_ms', 'heartbeat_delay_ms', 'thread', 'phase',
                    'classification', 'stack_histogram', 'generation', 'object_name',
                    'target', 'owner', 'event_type', 'samples')
            print('AUDIT_STALL ' + json.dumps({k: entry[k] for k in keys if k in entry}, ensure_ascii=False), flush=True)
