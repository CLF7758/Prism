"""取证：真实工程下，材质库 / 模型库首次构造的耗时构成。

空窗口的数字（tests/_audit_library_construct.py）和真实会话差很多 —— 真实会话里
``page.library.construct`` 是 1625.6ms（模型库）/ 1716.1ms（材质库）。这个脚本
在**真实审计副本**上重跑同一段测量，并额外回答：

* 构造之后，卡片面还要多久才 ``ready``（即"面板真正有内容"）；
* 真实工程里构造的时间落在哪些函数上。

只读打开审计副本，不做任何保存类动作。

运行：
    .\\.venv\\Scripts\\python.exe -m pytest tests/_audit_library_construct_real.py -q -s --no-cov
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
PROJECT = Path('artifacts/full-audit/Test-project-audit.prism').resolve()


def _profile(call, top=26):
    profiler = cProfile.Profile()
    started = time.perf_counter()
    profiler.enable()
    result = call()
    profiler.disable()
    elapsed = (time.perf_counter() - started) * 1000
    stream = io.StringIO()
    stats = pstats.Stats(profiler, stream=stream).sort_stats('cumulative')
    stats.print_stats(top)
    callers = io.StringIO()
    pstats.Stats(profiler, stream=callers).sort_stats('cumulative').print_callers(
        'sqlite3|settings|notify|execute')
    return result, elapsed, stream.getvalue() + '\n===== CALLERS =====\n' + callers.getvalue()


def _wait_ready(qtbot, predicate, timeout=30000):
    started = time.perf_counter()
    try:
        qtbot.waitUntil(predicate, timeout=timeout)
    except Exception:
        return None
    return (time.perf_counter() - started) * 1000


def test_library_construct_real_project(main_window, qtbot):
    if not PROJECT.exists():
        pytest.skip(f'审计副本不存在：{PROJECT}')
    window = main_window
    window.resize(1440, 900)
    window.show()

    view = window.view
    view.open_from_file(str(PROJECT))

    def opened():
        worker = getattr(view, 'worker', None)
        return (not getattr(view, '_project_opening', False)
                and (worker is None or not worker.isRunning()))

    qtbot.waitUntil(opened, timeout=180000)
    qtbot.wait(300)

    report = {'generated_at': time.strftime('%Y-%m-%dT%H:%M:%S'),
              'project': str(PROJECT)}

    material_page, material_ms, material_profile = _profile(
        lambda: window.material_library.ensure_created())
    material_ready_ms = _wait_ready(
        qtbot, lambda: bool(getattr(material_page.materials, 'ready', False)))
    report['material'] = {
        'construct_ms': round(material_ms, 1),
        'content_ready_after_construct_ms': (
            round(material_ready_ms, 1) if material_ready_ms is not None else None),
        'cards_created_during_construct': material_page.materials.web is not None,
        'records': len(material_page.records.all_records()
                       if hasattr(material_page.records, 'all_records') else []),
        'profile': material_profile,
    }

    model_page, model_ms, model_profile = _profile(
        lambda: window.model_library.ensure_created())
    model_cards_ms = _wait_ready(
        qtbot, lambda: getattr(window.model_library, 'cards', None) is not None)
    report['model'] = {
        'construct_ms': round(model_ms, 1),
        'cards_created_after_construct_ms': (
            round(model_cards_ms, 1) if model_cards_ms is not None else None),
        'cards_created_during_construct':
            getattr(window.model_library, 'cards', None) is not None,
        'profile': model_profile,
    }

    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / 'library-construct-real.json').write_text(
        json.dumps(report, ensure_ascii=False, indent=2, default=str), encoding='utf-8')
    (OUT / 'material-construct-real.txt').write_text(material_profile, encoding='utf-8')
    (OUT / 'model-construct-real.txt').write_text(model_profile, encoding='utf-8')
    print(json.dumps(
        {key: {k: v for k, v in value.items() if k != 'profile'}
         for key, value in report.items() if key != 'generated_at' and key != 'project'},
        ensure_ascii=False, indent=2), flush=True)
