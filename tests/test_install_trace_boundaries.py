"""T07: finalization is not installation; the time budget is cooperative."""
from contextlib import contextmanager
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from PyQt6 import QtGui


def test_completion_runs_outside_install_trace(view, monkeypatch):
    from prism import view as module
    events = []

    @contextmanager
    def span(name, **fields):
        events.append(('enter', name))
        yield SimpleNamespace(set=lambda **kw: None)
        events.append(('exit', name))

    monkeypatch.setattr(module, 'trace_span', span)
    scheduler = view._load_scheduler
    scheduler.finish(lambda: events.append(('callback', 'done')))
    assert events == [('enter', 'project.gui_install'),
                      ('exit', 'project.gui_install'),
                      ('enter', 'project.gui_complete'),
                      ('callback', 'done'), ('exit', 'project.gui_complete')]


def test_cooperative_overrun_includes_signals_without_losing_items(view, monkeypatch):
    from prism import scene as module
    clock = [0.0]
    monkeypatch.setattr(module.time, 'perf_counter', lambda: clock[0])
    scene = view.scene
    for text in ('one', 'two'):
        scene.add_item_later({'type': 'text', 'data': {'text': text}})

    def installed(item):
        clock[0] += .020

    def signals():
        clock[0] += .030

    scene.assets_changed.connect(signals)
    try:
        scene.add_queued_items(max_items=128, time_budget_ms=6, on_installed=installed)
        metrics = scene._last_install_metrics
        assert metrics['installed_items'] == 1
        assert metrics['max_item_ms'] == pytest.approx(20)
        assert metrics['signal_ms'] == pytest.approx(30)
        assert metrics['total_ms'] == pytest.approx(50)
        assert metrics['budget_overrun_ms'] == pytest.approx(44)
        assert metrics['budget_kind'] == 'cooperative_between_items'
        assert scene.items_to_add.qsize() == 1
        scene.add_queued_items(on_installed=installed)
        assert scene.items_to_add.empty()
        assert scene._item_count == 2
        assert scene._last_install_metrics['budget_overrun_ms'] is None
    finally:
        scene.assets_changed.disconnect(signals)


def test_bundled_icon_stats_once_but_returns_independent_handles(qapp, monkeypatch, tmp_path):
    from prism.widgets.components import icons
    from pathlib import Path
    (tmp_path / 'star.svg').write_text('<svg xmlns="http://www.w3.org/2000/svg"/>')
    monkeypatch.setattr(icons, 'ICON_DIR', tmp_path)
    original = Path.is_file
    calls = []

    def is_file(path):
        calls.append(path.name)
        return original(path)

    icons._bundled_icon.cache_clear()
    try:
        with patch.object(Path, 'is_file', is_file):
            first = icons.icon('star')
            others = [icons.icon('star') for _ in range(5)]
            assert calls == ['star.svg']
            with pytest.raises(ValueError):
                icons.icon('../star')
        old_key = others[0].cacheKey()
        image = QtGui.QPixmap(8, 8)
        image.fill(QtGui.QColor('red'))
        first.addPixmap(image)
        assert first.cacheKey() != old_key
        assert all(icon.cacheKey() == old_key for icon in others)
    finally:
        icons._bundled_icon.cache_clear()


def test_card_surface_creation_is_idempotent(main_window):
    cards = main_window.material_library.materials
    original = cards.web
    cards._ensure_web()
    assert cards.web is original


def test_real_trace_has_sibling_install_and_completion_spans(main_window, qapp, tmp_path):
    import json
    from prism import performance_trace as trace_module
    trace = trace_module.install(qapp, main_window, tmp_path)
    try:
        main_window.view._load_scheduler.finish(lambda: trace_module.record('test_completion'))
    finally:
        trace.close()
    rows = [json.loads(line) for line in trace.path.read_text(encoding='utf-8').splitlines()]
    install = next(r for r in rows if r.get('name') == 'project.gui_install'
                   and r['event'] == 'operation')
    complete = next(r for r in rows if r.get('name') == 'project.gui_complete'
                    and r['event'] == 'operation_start')
    assert install['sequence'] < complete['sequence']
    assert complete['parent_span_id'] != install['span_id']
