"""取证：材质库 / 模型库首次构造的内部耗时构成与"内容就绪"延迟。

这是**取证脚本**，不改产品代码。它回答三个问题：

1. ``DeferredLibrary.ensure_created()`` 这一次同步构造到底花了多久，
   时间主要落在哪些函数上（cProfile 累计耗时前 N）；
2. 构造结束之后，卡片面（Chromium + cards.html）还要多久才 ``ready``，
   即"面板真正有内容"的延迟；
3. 材质库与模型库是否对称（模型库的 Chromium 面是懒创建的）。

不打开任何真实工程、不写任何数据；只造空窗口。

运行：
    .\\.venv\\Scripts\\python.exe -m pytest tests/_audit_library_construct.py -q -s --no-cov
"""
from __future__ import annotations

import cProfile
import io
import json
import pstats
import time
from pathlib import Path

import pytest

OUT = Path('artifacts/library-construct-20261029').resolve()


def _profile(label, call, top=22):
    profiler = cProfile.Profile()
    started = time.perf_counter()
    profiler.enable()
    result = call()
    profiler.disable()
    elapsed = (time.perf_counter() - started) * 1000
    stream = io.StringIO()
    stats = pstats.Stats(profiler, stream=stream)
    stats.sort_stats('cumulative')
    stats.print_stats(top)
    return result, elapsed, stream.getvalue()


def _wait_ready(qtbot, predicate, timeout=20000):
    """等到 predicate 为真，返回它变真所花的毫秒数（超时返回 None）。"""
    started = time.perf_counter()
    try:
        qtbot.waitUntil(predicate, timeout=timeout)
    except Exception:
        return None
    return (time.perf_counter() - started) * 1000


def test_library_construct_profile(main_window, qtbot):
    window = main_window
    window.resize(1440, 900)
    window.show()
    report = {'generated_at': time.strftime('%Y-%m-%dT%H:%M:%S')}

    # ── 材质库 ───────────────────────────────────────────────
    material_page, material_ms, material_profile = _profile(
        'material',
        lambda: window.material_library.ensure_created())
    material_ready_ms = _wait_ready(
        qtbot, lambda: bool(getattr(material_page.materials, 'ready', False)))
    report['material'] = {
        'construct_ms': round(material_ms, 1),
        'content_ready_after_construct_ms': (
            round(material_ready_ms, 1) if material_ready_ms is not None else None),
        'cards_created_during_construct': material_page.materials.web is not None,
        'profile': material_profile,
    }

    # ── 模型库 ───────────────────────────────────────────────
    model_page, model_ms, model_profile = _profile(
        'model',
        lambda: window.model_library.ensure_created())
    model_cards_ms = _wait_ready(
        qtbot, lambda: getattr(window.model_library, 'cards', None) is not None)
    model_ready_ms = None
    cards = getattr(window.model_library, 'cards', None)
    if cards is not None:
        model_ready_ms = _wait_ready(qtbot, lambda: bool(cards.ready))
    report['model'] = {
        'construct_ms': round(model_ms, 1),
        'cards_created_after_construct_ms': (
            round(model_cards_ms, 1) if model_cards_ms is not None else None),
        'content_ready_after_cards_ms': (
            round(model_ready_ms, 1) if model_ready_ms is not None else None),
        'cards_created_during_construct': cards is not None,
        'profile': model_profile,
    }

    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / 'library-construct.json').write_text(
        json.dumps(report, ensure_ascii=False, indent=2, default=str), encoding='utf-8')
    (OUT / 'material-construct.txt').write_text(material_profile, encoding='utf-8')
    (OUT / 'model-construct.txt').write_text(model_profile, encoding='utf-8')
    print(json.dumps(
        {key: {k: v for k, v in value.items() if k != 'profile'}
         for key, value in report.items() if key != 'generated_at'},
        ensure_ascii=False, indent=2), flush=True)
