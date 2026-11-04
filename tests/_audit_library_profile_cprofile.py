"""取证：库面板构造的**代码级**耗时构成（cProfile）。

为什么需要：线上日志 `Prism-operations-156040.jsonl` 显示预热在 MainThread 上
合计占用 1374.1ms（模型库 914ms / 材质库 460ms），最大单片 445.5ms
（= `materials.cards.create_surface`，Chromium 一次性初始化）。
切片级探针（`_audit_library_step_profile.py`）显示：把 Chromium 挪走后，
剩下的是 12~15 个 4~22ms 的步骤。本脚本用 cProfile 指出这些步骤
到底是 Qt 控件创建、Python import，还是数据读取。

用法：
    $env:PYTHONIOENCODING='utf-8'; $env:QT_QPA_PLATFORM='offscreen'
    .\\.venv\\Scripts\\python.exe -m pytest tests\\_audit_library_profile_cprofile.py -q -s --no-header -p no:cacheprovider --no-cov
"""
from __future__ import annotations

import cProfile
import io
import pstats

import pytest


def _dump(profiler, title):
    stream = io.StringIO()
    stats = pstats.Stats(profiler, stream=stream).sort_stats('tottime')
    stats.print_stats(28)
    print(f'\n===== {title} =====')
    print(stream.getvalue())


def test_library_profile(main_window, qtbot):
    window = main_window
    window.resize(1440, 900)
    window.show()

    profiler = cProfile.Profile()
    profiler.enable()
    window.model_library.prewarm()
    qtbot.waitUntil(lambda: window.model_library.initialized, timeout=120000)
    profiler.disable()
    _dump(profiler, '模型库构造（cProfile，按 tottime）')

    profiler = cProfile.Profile()
    profiler.enable()
    window.material_library.prewarm()
    qtbot.waitUntil(lambda: window.material_library.initialized, timeout=120000)
    profiler.disable()
    _dump(profiler, '材质库构造（cProfile，按 tottime）')
