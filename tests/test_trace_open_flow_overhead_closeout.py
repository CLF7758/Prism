"""Interleaved real open-flow comparison, not a GPU/visual acceptance."""
import json
import os
from pathlib import Path
import platform
import statistics
import time

from PyQt6 import QtCore
from prism import performance_trace as pt
from prism.project_open_scope import ProjectOpenScope
from tests.test_zz_deleg_b1_open_scope import build_project, _page


def test_interleaved_real_open_flow(main_window, qtbot, tmp_path, monkeypatch):
    from prism import thumbnail_cache
    source = tmp_path / 'overhead.prism'
    build_project(source, [_page('A', 'First', 0), _page('B', 'Second', 1)],
                  [dict(page_id='A' if i < 100 else 'B', filename=f'{i}.png', tags=[])
                   for i in range(150)])
    cache_root = tmp_path / 'cache'
    monkeypatch.setattr(thumbnail_cache, 'cache_directory', lambda: str(cache_root / 'thumbs'))
    cache = cache_root / 'catalog' / (source.name + '.sqlite')
    cache.parent.mkdir(parents=True)
    assert ProjectOpenScope(source, cache).build().complete
    monkeypatch.setenv('PRISM_PAGED_OPEN', '1')
    trace = pt.install(main_window.view.app, main_window, tmp_path / 'logs')
    view = main_window.view
    records = []

    def opened():
        view.open_from_file(str(source))
        qtbot.waitUntil(lambda: getattr(view.scene, '_partial_project_session', None) is not None
                        and not view._project_opening and not view._paged_preparing,
                        timeout=15000)
        # A first canvas that fits the initial scoped page has no continuation
        # loader at all. Keep the exact resident-count assertion below.
        qtbot.waitUntil(lambda: getattr(view, '_paged_canvas_loader', None) is None
                        or not view._paged_canvas_loader.active, timeout=10000)
        assert main_window.current_page_id == 'A'
        assert len(list(view.scene.iter_assets())) == 100

    try:
        pt.set_mode('off')
        opened()  # all modes use a prepared catalog and warmed source
        modes = ('off', 'normal', 'diagnostic')
        for round_index in range(3):
            order = modes[round_index:] + modes[:round_index]
            for mode in order:
                pt.set_mode(mode)
                started = time.perf_counter()
                opened()
                records.append(dict(round=round_index, mode=mode,
                                    elapsed_ms=(time.perf_counter()-started)*1000,
                                    loaded=100, catalog_ready=view._open_scope.ready))
    finally:
        trace.close()
        pt.set_mode('normal')
    result = dict(scenario='real-open-flow-only', source_items=150, current_page_items=100,
                  qt_platform=main_window.view.app.platformName(), python=platform.python_version(),
                  qt=QtCore.QT_VERSION_STR, visual_gpu_acceptance=False,
                  cache_condition='prepared catalog; one warm-up', samples=records, summaries={})
    for mode in ('off', 'normal', 'diagnostic'):
        values = [r['elapsed_ms'] for r in records if r['mode'] == mode]
        result['summaries'][mode] = dict(n=len(values), median_ms=statistics.median(values),
                                       min_ms=min(values), max_ms=max(values))
    base = result['summaries']['off']['median_ms']
    for mode in ('normal', 'diagnostic'):
        result['summaries'][mode]['median_delta_percent'] = (result['summaries'][mode]['median_ms']/base-1)*100
    result['qualification'] = 'small-sample open-flow observation; not whole-app <=3% proof'
    dest = Path('artifacts/closeout-20261023/open-flow-overhead.json')
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
