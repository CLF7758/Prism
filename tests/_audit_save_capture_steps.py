"""取证：保存快照的 102 万步到底花在哪个字段上。

线上实测：一次保存捕获跑了 1,020,000 步、耗时 7.77s。
每一步都是 `sliced_copy` 里的一次 `yield None`，也就是一次 GUI 调度往返。
这里用真实工程的数据形状，量出每一步来自哪个顶层字段、各自多少步。

用法：
    $env:PYTHONIOENCODING='utf-8'; $env:QT_QPA_PLATFORM='offscreen'
    .\\.venv\\Scripts\\python.exe -m pytest tests\\_audit_save_capture_steps.py -q -s --no-header -p no:cacheprovider --no-cov
"""
from __future__ import annotations

import json
import os
import time
from collections import Counter
from pathlib import Path

import pytest

PROJECT = Path(os.environ.get(
    'PRISM_AUDIT_PROJECT',
    r'C:\Users\admin\Desktop\Prism测试\Prism-100k-v1-新版.prism')).resolve()

DEFAULT_KEYS = ('note_html', 'mindmap_tree', 'workspace_pages', 'workspace_nodes',
                'material_records', 'model_records', 'material_library_state',
                'category_names', 'tag_names', 'library_queries')


def _count_steps(value):
    """Count the checkpoints ``sliced_copy`` yields for one value."""
    from prism.fileio.sliced_copy import _copy_steps
    steps = 0
    generator = _copy_steps(value, {})
    try:
        while True:
            next(generator)
            steps += 1
    except StopIteration:
        pass
    return steps


def _shape(value, depth=0):
    """Describe the container shape without walking atomic leaves."""
    kind = type(value).__name__
    if isinstance(value, dict):
        return f'dict[{len(value)}]'
    if isinstance(value, (list, tuple, set, frozenset)):
        return f'{kind}[{len(value)}]'
    if isinstance(value, str):
        return f'str[{len(value)}]'
    return kind


def test_save_capture_step_distribution(main_window, qtbot, request):
    if not PROJECT.exists():
        pytest.skip(f'真实工程不存在：{PROJECT}')
    from prism.storage.managed import database_path
    window = main_window
    view = window.view
    view.open_from_file(str(PROJECT), readonly=True)

    def loading():
        worker = getattr(view, 'worker', None)
        return (getattr(view, '_project_opening', False)
                or (worker is not None and worker.isRunning()))

    def opened():
        return (not loading() and bool(view.filename)
                and Path(view.filename).resolve() == Path(database_path(PROJECT)).resolve()
                and not getattr(view, '_paged_preparing', False))

    qtbot.waitUntil(lambda: loading() or opened(), timeout=60000)
    qtbot.waitUntil(opened, timeout=300000)
    qtbot.wait(500)
    scene = view.scene
    print('\nAUDIT: 工程已打开', flush=True)

    report = {}
    total_steps = 0
    for key in DEFAULT_KEYS:
        value = getattr(scene, key, None)
        if value is None:
            report[key] = dict(shape='missing', steps=0)
            continue
        started = time.perf_counter()
        steps = _count_steps(value)
        elapsed = (time.perf_counter() - started) * 1000
        total_steps += steps
        report[key] = dict(shape=_shape(value), steps=steps,
                           copy_ms=round(elapsed, 2))
        print(f'AUDIT: {key:24s} {_shape(value):14s} steps={steps:9,d} '
              f'copy={elapsed:9.2f}ms', flush=True)

    # The tag-system mapping is copied by a second sliced_copy call.
    system = getattr(scene, 'tag_system', None)
    if system is not None:
        started = time.perf_counter()
        steps = _count_steps((system._siblings, system._parents))
        elapsed = (time.perf_counter() - started) * 1000
        total_steps += steps
        report['tag_mappings'] = dict(shape='tuple', steps=steps,
                                      copy_ms=round(elapsed, 2))
        print(f'AUDIT: {"tag_mappings":24s} {"tuple":14s} steps={steps:9,d} '
              f'copy={elapsed:9.2f}ms', flush=True)

    # Where do the steps come from inside the worst field?
    worst = max((k for k in report if report[k]['steps'] > 0),
                key=lambda k: report[k]['steps'], default=None)
    detail = {}
    if worst is not None:
        value = getattr(scene, worst, None)
        if isinstance(value, dict):
            for key, item in value.items():
                steps = _count_steps(item)
                detail[str(key)] = dict(steps=steps, shape=_shape(item))
                print(f'AUDIT:   {worst}.{key} → steps={steps:9,d} {_shape(item)}',
                      flush=True)
        elif isinstance(value, (list, tuple)):
            for index, item in enumerate(value[:20]):
                steps = _count_steps(item)
                detail[str(index)] = dict(steps=steps, shape=_shape(item))
                print(f'AUDIT:   {worst}[{index}] → steps={steps:9,d} {_shape(item)}',
                      flush=True)

    print('\nAUDIT_JSON ' + json.dumps(dict(
        total_steps=total_steps, fields=report, worst_field=worst,
        worst_detail=detail), ensure_ascii=False), flush=True)
