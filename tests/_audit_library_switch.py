"""取证：切换到材质库/模型库时，**切过去那一刻面板有没有内容**。

对照两个量：

* ``click_to_switch_ms`` —— 从发起切换到页签真正换过去用了多久；
* ``blank_after_switch_ms`` —— 换过去之后，面板还要多久才有内容（这就是
  用户看到的那一下"闪"）。

只取证，不改产品代码；不打开真实工程，不写任何数据。

运行：
    .\\.venv\\Scripts\\python.exe -m pytest tests/_audit_library_switch.py -q -s --no-cov
"""
from __future__ import annotations

import json
import time
from pathlib import Path

OUT = Path('artifacts/library-construct-20261029').resolve()


def _cards_in_dom(page, qtbot, timeout=15000):
    """卡片墙在网页里出现要多久（毫秒）。"""
    result = []
    started = time.perf_counter()
    page.materials.web.page().runJavaScript(
        "document.querySelectorAll('.asset-card').length", result.append)
    qtbot.waitUntil(lambda: bool(result), timeout=timeout)
    return (time.perf_counter() - started) * 1000, (result[0] if result else None)


def test_library_switch_content_readiness(main_window, qtbot):
    window = main_window
    window.resize(1440, 900)
    window.show()
    report = {'generated_at': time.strftime('%Y-%m-%dT%H:%M:%S')}

    # ── 材质库 ───────────────────────────────────────────────
    started = time.perf_counter()
    window.open_material_library()
    click_to_switch_ms = (time.perf_counter() - started) * 1000
    page = window.material_library
    ready_at_switch = bool(page.materials.ready)
    blank_ms, card_count = _cards_in_dom(page, qtbot)
    report['material'] = {
        'click_to_switch_ms': round(click_to_switch_ms, 1),
        'content_ready_at_switch': ready_at_switch,
        'blank_after_switch_ms': round(blank_ms, 1),
        'cards_in_dom': card_count,
    }

    # ── 模型库 ───────────────────────────────────────────────
    started = time.perf_counter()
    window.open_model_library()
    model_switch_ms = (time.perf_counter() - started) * 1000
    report['model'] = {
        'click_to_switch_ms': round(model_switch_ms, 1),
        'content_ready_at_switch': bool(window.model_library.is_content_ready()),
    }

    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / 'library-switch-readiness.json').write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps(report, ensure_ascii=False, indent=2), flush=True)
