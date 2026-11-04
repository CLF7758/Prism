"""Measure the material preview of a real model on the real native worker.

The trace of the 100k project showed the first preview of a model taking
6.27 s: about a second for the artifact, about a second and a half for the
helper to load VTK, and the rest for the render itself.  The helper start-up
used to follow the artifact instead of overlapping it.

This probe opens the real project, opens one of its models in the material
preview and reports the measured stages.  Run it twice, in two processes, to
compare a cold helper:

    $env:AUDIT_NO_WARM='1'   # the old order: the helper starts after the artifact
    $env:AUDIT_NO_WARM='0'   # the new order: the helper starts before it
"""
from __future__ import annotations

import json
import os
import tempfile
import time
from pathlib import Path

import pytest
from PyQt6 import QtWidgets

PROJECT = Path(os.environ.get(
    'PRISM_AUDIT_PROJECT',
    r'C:\Users\admin\Desktop\Prism测试\Prism-100k-v1-新版.prism')).resolve()


def _open_project(qtbot, window):
    from prism.storage.managed import database_path
    expected = Path(database_path(PROJECT)).resolve()
    window.view.open_from_file(str(PROJECT), readonly=True)

    def loading():
        worker = getattr(window.view, 'worker', None)
        return (getattr(window.view, '_project_opening', False)
                or (worker is not None and worker.isRunning()))

    def opened():
        return (not loading() and bool(window.view.filename)
                and Path(window.view.filename).resolve() == expected
                and not getattr(window.view, '_paged_preparing', False))

    qtbot.waitUntil(lambda: loading() or opened(), timeout=60000)
    qtbot.waitUntil(opened, timeout=300000)
    assert not window.view._paged_open_failed, window.view._paged_open_failed_reason


def _resident_bytes():
    import ctypes
    from ctypes import wintypes

    class MEMORYSTATUSEX(ctypes.Structure):
        _fields_ = [('dwLength', wintypes.DWORD), ('dwMemoryLoad', wintypes.DWORD),
                    ('ullTotalPhys', ctypes.c_ulonglong), ('ullAvailPhys', ctypes.c_ulonglong),
                    ('ullTotalPageFile', ctypes.c_ulonglong), ('ullAvailPageFile', ctypes.c_ulonglong),
                    ('ullTotalVirtual', ctypes.c_ulonglong), ('ullAvailVirtual', ctypes.c_ulonglong),
                    ('ullAvailExtendedVirtual', ctypes.c_ulonglong)]

    status = MEMORYSTATUSEX()
    status.dwLength = ctypes.sizeof(status)
    ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status))
    return status.ullTotalPhys - status.ullAvailPhys


def _python_processes():
    """RSS of every python process, so a helper's idle cost is visible."""
    import subprocess
    try:
        out = subprocess.run(['tasklist', '/FI', 'IMAGENAME eq python.exe', '/FO', 'CSV',
                              '/NH'], capture_output=True, text=True, timeout=20).stdout
    except Exception:
        return []
    rows = []
    for line in out.splitlines():
        parts = [part.strip('" ') for part in line.split('","')]
        if len(parts) < 5 or not parts[0].lower().startswith('python'):
            continue
        try:
            rows.append((int(parts[1]), parts[4]))
        except ValueError:
            continue
    return rows


def test_the_first_material_preview_of_a_real_model(main_window, qtbot, monkeypatch, request):
    if not PROJECT.exists():
        pytest.skip(f'真实工程不存在：{PROJECT}')
    no_warm = os.environ.get('AUDIT_NO_WARM', '0') == '1'
    eager = os.environ.get('AUDIT_EAGER_WARM', '0') == '1'
    if no_warm:
        from prism.model_library.native_jobs import NativeModelJobController
        # The old order: nothing asks for the helper before the artifact.
        monkeypatch.setattr(NativeModelJobController, 'warm', lambda self: None)

    window = main_window
    window.resize(1440, 900)
    window.show()
    trace_directory = tempfile.TemporaryDirectory(prefix='prism-material-audit-')
    from prism.performance_trace import install
    logger = install(QtWidgets.QApplication.instance(), window, trace_directory.name)
    request.addfinalizer(trace_directory.cleanup)
    request.addfinalizer(logger.close)
    _open_project(qtbot, window)

    composition = window.model_composition
    rss_before_bind = _resident_bytes()
    composition.ensure()
    qtbot.waitUntil(lambda: composition.service is not None, timeout=180000)
    rss_after_bind = (_resident_bytes() - rss_before_bind) / 1024 / 1024
    service = composition.service
    material = getattr(service, 'multimaterial', None)
    assert material is not None, 'the material service was not created'
    print(f'\nAUDIT: 模型库接入 RSS 增量={rss_after_bind:.1f}MB（模型库自己的 F3D worker）',
          flush=True)
    try:
        print('AUDIT: repository.count() = %r' % (service.repository.count(),), flush=True)
    except Exception as error:
        print('AUDIT: repository.count() 失败 %r' % (error,), flush=True)

    from prism.model_library.contracts import ModelQuery
    records = list(service.query(ModelQuery()).items)
    print(f'\nAUDIT: 模型 {len(records)} 个', flush=True)
    assert records, '真实工程里没有模型'

    if eager:
        # The helper is asked for before the user picks a model.  The user
        # needs a moment to browse, and that is the time the start-up buys.
        before = _python_processes()
        started = time.perf_counter()
        material.controller.warm()
        print(f'\nAUDIT: 预热调用返回 {(time.perf_counter()-started)*1000:.1f}ms', flush=True)
        time.sleep(float(os.environ.get('AUDIT_EAGER_WAIT_S', '3')))
        after = _python_processes()
        fresh = {pid: mem for pid, mem in after if pid not in {p for p, _ in before}}
        print('AUDIT: 预热后的新进程 ' + json.dumps(fresh, ensure_ascii=False), flush=True)

    # The user browses the library before picking a model.  Both runs get the
    # same time, so the only difference is whether the helper was started.
    browse = float(os.environ.get('AUDIT_BROWSE_S', '4'))
    if browse:
        time.sleep(browse)

    record = records[0]
    # The query returns summaries; the preview needs the full record.
    full = service.get(record.model_id)
    rss_before = _resident_bytes()
    started = time.perf_counter()
    job = material.open_model(full)
    material._jobs[job].result(timeout=300)
    wall = (time.perf_counter() - started) * 1000
    rss_delta = (_resident_bytes() - rss_before) / 1024 / 1024
    print(f'\nAUDIT: 第一次预览 wall={wall:.1f}ms  model={record.model_id} '
          f'RSS 增量={rss_delta:.1f}MB', flush=True)

    logger.close()
    rows = [json.loads(line) for line in
            logger.path.read_text(encoding='utf-8').splitlines()]
    stages = [row for row in rows if row.get('event') == 'library.stage.duration']
    installs = [row for row in stages if row.get('stage') == 'prepare_native_install']
    workers = [row for row in rows if row.get('event') == 'model_native_worker_started']
    timings = [row for row in rows if row.get('event') == 'model_material.native.timing']
    resources = [row for row in rows if row.get('event') == 'model_material.native.resources']
    started_at = min((row.get('time') for row in rows
                      if isinstance(row.get('time'), (int, float))), default=0.0)
    for row in installs:
        print(f"AUDIT: prepare_native_install {row.get('duration_ms'):.1f}ms "
              f"结束于 +{row.get('time') - started_at:.1f}s", flush=True)
    for row in workers:
        print(f"AUDIT: native_worker_started +{row.get('time') - started_at:.1f}s "
              f"backend={row.get('backend_version')}", flush=True)
    for row in timings:
        print(f"AUDIT: native job action={row.get('action')} "
              f"elapsed={row.get('elapsed_ms'):.1f}ms "
              f"parse={row.get('source_read_parse_ms'):.1f}ms "
              f"timing={json.dumps(row.get('timing'), ensure_ascii=False)}", flush=True)
    for row in resources:
        print(f"AUDIT: worker rss={row.get('process_rss_bytes',0)/1024/1024:.1f}MB "
              f"actors={row.get('actors')}", flush=True)
    print('AUDIT_JSON ' + json.dumps(dict(
        no_warm=no_warm, eager=eager, wall_ms=round(wall, 1),
        rss_delta_mb=round(rss_delta, 1),
        install_ms=[round(row.get('duration_ms'), 1) for row in installs],
        worker_start_s=[round(row.get('time') - started_at, 2) for row in workers],
        native_ms=[round(row.get('elapsed_ms'), 1) for row in timings],
        worker_rss_mb=[round(row.get('process_rss_bytes', 0) / 1024 / 1024, 1)
                       for row in resources],
    ), ensure_ascii=False), flush=True)
