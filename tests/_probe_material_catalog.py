"""真实工程：打开材质库时到底读了什么？（只读探针，不改业务代码）

运行：
    $env:PYTHONIOENCODING='utf-8'
    $env:QT_QPA_PLATFORM='offscreen'
    .\.venv\Scripts\python.exe -m pytest tests/_probe_material_catalog.py -q -s --no-cov
"""
from __future__ import annotations

import collections
import os
import time
from pathlib import Path

import pytest
from PyQt6 import QtCore

PROJECT = Path(os.environ.get(
    'PRISM_AUDIT_PROJECT',
    r'C:\Users\admin\Desktop\Prism测试\Prism-100k-v1-新版.prism')).resolve()

READS = collections.Counter()
EVENTS = []


def _install_hooks():
    from prism import project_catalog_index as pci
    from prism import catalog_browser as cb
    from prism import material_service as ms

    for name in ('page', 'query', 'summaries', 'summaries_grouped',
                 'summaries_buckets', 'summaries_uncovered', 'display_summaries',
                 'details', 'count', 'grouped_counts'):
        original = getattr(pci.ProjectCatalogIndex, name)

        def make(name=name, original=original):
            def wrapper(self, *args, **kwargs):
                started = time.perf_counter()
                try:
                    result = original(self, *args, **kwargs)
                except Exception as error:
                    READS[name + ':error'] += 1
                    raise
                elapsed = (time.perf_counter() - started) * 1000
                size = len(result) if hasattr(result, '__len__') else 0
                READS[f'{name}:calls'] += 1
                READS[f'{name}:rows'] += size
                READS[f'{name}:ms'] += elapsed
                EVENTS.append(('index.' + name, size, round(elapsed, 1),
                               args[1:] if len(args) > 1 else ()))
                return result
            return wrapper
        setattr(pci.ProjectCatalogIndex, name, make())

    original_batches = ms.MaterialLibraryService.iter_card_record_batches

    def batches(self, folder_id=None, query='', *, chunk_size=64):
        count = 0
        for batch in original_batches(self, folder_id, query, chunk_size=chunk_size):
            count += len(batch)
            READS['material_batches:chunks'] += 1
            yield batch
        READS['material_batches:rows'] += count
        EVENTS.append(('material.iter_card_record_batches', count, 0,
                       (folder_id, query)))
    ms.MaterialLibraryService.iter_card_record_batches = batches

    def observer(event, **fields):
        EVENTS.append(('browser.' + event, fields.get('rows', 0),
                       fields.get('pages', 0), fields.get('scope', '')))
    cb.add_catalog_browser_read_observer(observer)


def _open_project(qtbot, window):
    view = window.view
    view.open_from_file(str(PROJECT))

    def loading():
        worker = getattr(view, 'worker', None)
        return (getattr(view, '_project_opening', False)
                or (worker is not None and worker.isRunning()))

    def opened():
        return (not loading()
                and bool(view.filename)
                and Path(view.filename).resolve() == PROJECT.resolve()
                and not getattr(view, '_paged_preparing', False))

    qtbot.waitUntil(lambda: loading() or opened(), timeout=60000)
    qtbot.waitUntil(opened, timeout=300000)
    assert not view._paged_open_failed, view._paged_open_failed_reason
    qtbot.wait(500)
    return view


def test_probe(main_window, qtbot):
    if not PROJECT.exists():
        pytest.skip(f'真实工程不存在：{PROJECT}')
    window = main_window
    window.resize(1440, 900)
    window.show()
    _install_hooks()
    view = _open_project(qtbot, window)
    print('OPENED', flush=True)

    READS.clear()
    EVENTS.clear()
    from prism.catalog_browser import reset_catalog_browser_counters
    reset_catalog_browser_counters()

    started = time.perf_counter()
    window.open_material_library()
    qtbot.waitUntil(lambda: bool(window.material_library.materials._record_ids),
                    timeout=60000)
    elapsed = (time.perf_counter() - started) * 1000
    print(f'\n=== 打开材质库耗时 {elapsed:.0f} ms ===', flush=True)
    print('card records:', len(window.material_library.materials._record_ids), flush=True)
    print('\n--- READS ---', flush=True)
    for key, value in sorted(READS.items()):
        print('   ', key, value, flush=True)
    from prism.catalog_browser import catalog_browser_counters
    print('\n--- catalog_browser_counters ---', flush=True)
    for key, value in sorted(catalog_browser_counters().items()):
        print('   ', key, value, flush=True)
    print('\n--- EVENTS (first 40) ---', flush=True)
    for event in EVENTS[:40]:
        print('   ', event, flush=True)
    print('event count', len(EVENTS), flush=True)
