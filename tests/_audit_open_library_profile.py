"""真实工程离屏打开、自动预热、首次请求和重复切换的链路检查。

不禁止生产预热来制造基线；初始化状态和内容就绪等待分别记录。
这里只测 Python/Qt 离屏链路，不能证明桌面首帧或 GPU 呈现速度。

运行：
    $env:PYTHONIOENCODING='utf-8'
    $env:QT_QPA_PLATFORM='offscreen'
    .\\.venv\\Scripts\\python.exe -m pytest tests/_audit_open_library_profile.py -q -s --no-cov
"""
from __future__ import annotations

import json
import faulthandler
import os
import time
from pathlib import Path

import pytest
from PyQt6 import QtCore

OUT = Path(os.environ.get('AUDIT_OUT',
                          'artifacts/library-open-profile-20261029')).resolve()
OUT.mkdir(parents=True, exist_ok=True)
_STACK = (OUT / 'audit-stacks.txt').open('w', encoding='utf-8')
faulthandler.enable(file=_STACK)
faulthandler.dump_traceback_later(int(os.environ.get('AUDIT_DUMP_AFTER', '90')),
                                   exit=False, file=_STACK)

PROJECT = Path(os.environ.get(
    'PRISM_AUDIT_PROJECT',
    r'C:\Users\admin\Desktop\Prism测试\Prism-100k-v1-新版.prism')).resolve()


def _switch(window, callback):
    """A nested readiness wait is elapsed time, not necessarily GUI starvation."""
    ticks = [time.perf_counter()]
    timer = QtCore.QTimer(window)
    timer.setInterval(16)
    timer.timeout.connect(lambda: ticks.append(time.perf_counter()))
    timer.start()
    try:
        callback()
    finally:
        timer.stop()
        timer.deleteLater()
    ticks.append(time.perf_counter())
    return {'call_ms': round((ticks[-1] - ticks[0]) * 1000, 1),
            'gui_heartbeat_count': len(ticks) - 2,
            'longest_heartbeat_gap_ms': round(max(b - a for a, b in zip(ticks, ticks[1:])) * 1000, 1)}


def _open_project(qtbot, window):
    view = window.view
    from prism.storage.managed import database_path
    expected = Path(database_path(PROJECT)).resolve()
    print('AUDIT: opening project...', flush=True)
    view.open_from_file(str(PROJECT))

    def loading():
        worker = getattr(view, 'worker', None)
        return (getattr(view, '_project_opening', False)
                or (worker is not None and worker.isRunning()))

    def opened():
        return (not loading()
                and bool(view.filename)
                and Path(view.filename).resolve() == expected
                and not getattr(view, '_paged_preparing', False))

    qtbot.waitUntil(lambda: loading() or opened(), timeout=60000)
    print('AUDIT: open started, waiting for it to finish...', flush=True)
    qtbot.waitUntil(opened, timeout=300000)
    assert not view._paged_open_failed, view._paged_open_failed_reason
    assert view.scene._partial_project_session is not None
    print('AUDIT: project opened', flush=True)
    qtbot.wait(500)
    return view


def test_open_library_profile(main_window, qtbot):
    if not PROJECT.exists():
        pytest.skip(f'真实工程不存在：{PROJECT}')
    window = main_window
    window.resize(1440, 900)
    window.show()

    opened_at = time.perf_counter()
    view = _open_project(qtbot, window)
    open_ms = (time.perf_counter() - opened_at) * 1000

    report = {'project': str(PROJECT), 'open_ms': round(open_ms, 1),
              'basis': 'offscreen-qt-chain',
              'model_initialized_before_first_request': window.model_library.initialized,
              'material_initialized_before_first_request': window.material_library.initialized}

    print('AUDIT: first model request...', flush=True)
    report['model_first_request'] = _switch(window, window.open_model_library)
    qtbot.waitUntil(lambda: window.model_library.cards is not None
                   and bool(window.model_library.cards._record_ids), timeout=15000)
    assert window.tabs.currentWidget() is window.model_library
    report['model_card_count'] = len(window.model_library.cards._record_ids)

    print('AUDIT: first material request...', flush=True)
    report['material_first_request'] = _switch(window, window.open_material_library)
    qtbot.waitUntil(lambda: bool(window.material_library.materials._record_ids), timeout=15000)
    assert window.tabs.currentWidget() is window.material_library
    report['material_card_count'] = len(window.material_library.materials._record_ids)

    print('AUDIT: repeated model switch...', flush=True)
    started = time.perf_counter()
    window.open_model_library()
    report['model_repeat_switch_ms'] = round((time.perf_counter() - started) * 1000, 1)

    print('AUDIT: repeated material switch...', flush=True)
    started = time.perf_counter()
    window.open_material_library()
    report['material_repeat_switch_ms'] = round((time.perf_counter() - started) * 1000, 1)

    # 卡片面是不是真的有内容（不是空板）
    report['material_surface_ready'] = bool(
        getattr(window.material_library, 'materials', None) is not None
        and window.material_library.materials.ready)
    report['material_visible_records'] = window.material_library.materials.isVisible()
    assert report['material_surface_ready'] and report['material_visible_records']

    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / 'open-library-profile.json').write_text(
        json.dumps(report, ensure_ascii=False, indent=2, default=str), encoding='utf-8')
    print('\n===== REPORT =====')
    print(json.dumps(report, ensure_ascii=False, indent=2), flush=True)
