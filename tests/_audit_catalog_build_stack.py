"""取证：谁在 GUI 线程上同步跑了 ProjectCatalogIndex 的建库循环。

不改源码：用 monkeypatch 在 build/_build 入口打印调用栈与线程名。

运行：
    $env:PYTHONIOENCODING='utf-8'
    $env:QT_QPA_PLATFORM='offscreen'
    .\\.venv\\Scripts\\python.exe -m pytest tests/_audit_catalog_build_stack.py -q -s --no-cov
"""
from __future__ import annotations

import json
import threading
import time
import traceback
from pathlib import Path

import pytest

PROJECT = Path(r'C:\Users\admin\Desktop\Prism测试\Prism-100k-v1-新版.prism').resolve()
OUT = Path('artifacts/library-open-profile-20261029').resolve()


def test_catalog_build_stack(main_window, qtbot):
    if not PROJECT.exists():
        pytest.skip(f'真实工程不存在：{PROJECT}')
    from prism.project_catalog_index import ProjectCatalogIndex

    stacks = []
    original_build = ProjectCatalogIndex.build
    original_inner = ProjectCatalogIndex._build

    def patched_build(self, *args, **kwargs):
        stacks.append({
            'where': 'build',
            'thread': threading.current_thread().name,
            'main_thread': threading.current_thread() is threading.main_thread(),
            'stack': ''.join(traceback.format_stack()[-12:]),
        })
        return original_build(self, *args, **kwargs)

    def patched_inner(self, *args, **kwargs):
        stacks.append({
            'where': '_build',
            'thread': threading.current_thread().name,
            'main_thread': threading.current_thread() is threading.main_thread(),
            'stack': ''.join(traceback.format_stack()[-12:]),
        })
        return original_inner(self, *args, **kwargs)

    ProjectCatalogIndex.build = patched_build
    ProjectCatalogIndex._build = patched_inner
    try:
        window = main_window
        window.resize(1440, 900)
        window.show()
        view = window.view
        view.open_from_file(str(PROJECT))

        def loading():
            worker = getattr(view, 'worker', None)
            return (getattr(view, '_project_opening', False)
                    or (worker is not None and worker.isRunning()))

        def opened():
            worker = getattr(view, 'worker', None)
            return (not loading()
                    and str(getattr(view, 'filename', '') or '').endswith('.prism'))

        # The open is asynchronous: wait until it really started before
        # waiting for it to end, otherwise "not opening yet" looks like "opened".
        qtbot.waitUntil(lambda: loading() or opened(), timeout=30000)
        qtbot.waitUntil(opened, timeout=300000)
        qtbot.wait(500)
        print('project opened:', getattr(view, 'filename', None), flush=True)

        stacks.clear()
        started = time.perf_counter()
        window.open_model_library()
        model_ms = (time.perf_counter() - started) * 1000
        qtbot.wait(300)
    finally:
        ProjectCatalogIndex.build = original_build
        ProjectCatalogIndex._build = original_inner

    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / 'catalog-build-stack.json').write_text(
        json.dumps({'model_open_ms': round(model_ms, 1), 'stacks': stacks},
                   ensure_ascii=False, indent=2), encoding='utf-8')
    print('\n===== CATALOG BUILD STACKS =====')
    for item in stacks:
        print(f"--- {item['where']} thread={item['thread']} main={item['main_thread']}")
        print(item['stack'])
    print(f'model_open_ms={model_ms:.1f}  stack_count={len(stacks)}', flush=True)
