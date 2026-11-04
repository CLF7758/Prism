"""取证：材质库首次构造期间，主线程上的目录索引写入来自哪里。

在真实审计工程上打开工程，然后 monkeypatch ``project_catalog_index`` 的索引写入，
把主线程上的调用栈打印出来 —— 只取证，不改产品代码。

运行：
    .\\.venv\\Scripts\\python.exe -m pytest tests/_audit_catalog_store_origin.py -q -s --no-cov
"""
from __future__ import annotations

import threading
import time
import traceback
from pathlib import Path

import pytest

PROJECT = Path('artifacts/full-audit/Test-project-audit.prism').resolve()
OUT = Path('artifacts/library-construct-20261029').resolve()


def test_catalog_store_origin(main_window, qtbot):
    if not PROJECT.exists():
        pytest.skip(f'审计副本不存在：{PROJECT}')
    from prism import project_catalog_index as pci

    stacks = []
    original = pci.ProjectCatalogIndex._store

    def traced(connection, state):
        if threading.current_thread() is threading.main_thread():
            stacks.append(''.join(traceback.format_stack()[-16:-1]))
        return original(connection, state)

    pci.ProjectCatalogIndex._store = staticmethod(traced)
    try:
        window = main_window
        window.resize(1440, 900)
        window.show()
        view = window.view
        finished = []
        try:
            view.project_open_finished.connect(lambda *a: finished.append(True))
        except Exception:
            pass
        view.open_from_file(str(PROJECT))

        def opened():
            worker = getattr(view, 'worker', None)
            return (bool(finished)
                    and not getattr(view, '_project_opening', False)
                    and (worker is None or not worker.isRunning()))

        qtbot.waitUntil(opened, timeout=180000)
        qtbot.wait(500)
        print('project opened: filename=%r' % (getattr(view, 'filename', None),), flush=True)
        stacks.clear()

        started = time.perf_counter()
        window.material_library.ensure_created()
        elapsed = (time.perf_counter() - started) * 1000
    finally:
        pci.ProjectCatalogIndex._store = staticmethod(original)

    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / 'catalog-store-origin.txt').write_text(
        'construct_ms=%.1f  main_thread_store_calls=%d\n\n%s'
        % (elapsed, len(stacks), '\n----------------\n'.join(stacks)),
        encoding='utf-8')
    print('construct_ms=%.1f main_thread_store_calls=%d' % (elapsed, len(stacks)), flush=True)
    for index, stack in enumerate(stacks[:2]):
        print('===== stack %d =====' % index, flush=True)
        print(stack, flush=True)
