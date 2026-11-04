"""取证：库面板在构造时**惰性导入**的模块，各自在 GUI 线程上花多少。

点击剖析（真实工程）显示首次点击材质库时 GUI 线程上有 ~100 个模块被导入，
`exec` 的 tottime 195ms —— 这是点击卡顿里仅次于 Chromium 的一块。
这些导入发生在控件构造内部（`from prism.material_parameters import RANGES`
之类），所以**预热的后台准备并没有覆盖它们**。

用法：
    $env:PYTHONIOENCODING='utf-8'
    .\\.venv\\Scripts\\python.exe tests\\_audit_library_import_cost.py material
    .\\.venv\\Scripts\\python.exe tests\\_audit_library_import_cost.py model
"""
from __future__ import annotations

import builtins
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

TARGETS = {
    'material': [
        'prism.widgets.material_library',
        'prism.widgets.material_cards',
        'prism.widgets.material_details',
        'prism.widgets.material_slots',
        'prism.material_parameters',
        'prism.widgets.material_render',
    ],
    'model': [
        'prism.widgets.model_library',
        'prism.widgets.model_details',
        'prism.widgets.model_material_editor',
        'prism.widgets.model_material_panel',
        'prism.widgets.material_slots',
        'prism.material_parameters',
    ],
}


def main() -> int:
    which = sys.argv[1] if len(sys.argv) > 1 else 'material'
    records = []
    original = builtins.__import__

    def hooked(name, *args, **kwargs):
        started = time.perf_counter()
        try:
            return original(name, *args, **kwargs)
        finally:
            elapsed = (time.perf_counter() - started) * 1000
            records.append((elapsed, name))

    builtins.__import__ = hooked
    started = time.perf_counter()
    for module in TARGETS[which]:
        original(module)
    total = (time.perf_counter() - started) * 1000
    builtins.__import__ = original

    print(f'== {which}: {len(TARGETS[which])} 个模块，总计 {total:.1f}ms ==')
    leaf = {}
    for elapsed, name in records:
        leaf[name] = leaf.get(name, 0.0) + elapsed
    for name, elapsed in sorted(leaf.items(), key=lambda kv: -kv[1])[:16]:
        print(f'  {elapsed:8.1f}ms  {name}')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
