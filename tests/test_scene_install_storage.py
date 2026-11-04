from types import SimpleNamespace
from unittest.mock import patch
import hashlib
import sqlite3
import threading

import pytest

from prism import source_store
from tests.test_project_catalog_index import catalog  # noqa: F401


def test_file_size_uses_one_stat_per_unique_path_per_batch(tmp_path):
    path = tmp_path / 'original.png'
    path.write_bytes(b'original')
    items = [SimpleNamespace(_source_path=str(path)) for _ in range(10000)]
    original = source_store.os.stat
    calls = []

    def stat(filename, *args, **kw):
        calls.append(filename)
        return original(filename, *args, **kw)

    with patch.object(source_store.os, 'stat', stat):
        assert source_store.storage_bytes(items, {}) == 80000
    assert len(calls) == 1
    path.write_bytes(b'changed-size')
    assert source_store.storage_bytes(items[:1], {}) == len(b'changed-size')


def test_large_scope_summary_does_not_stat_on_gui(main_window, qtbot, monkeypatch):
    browser = main_window.asset_browser
    items = [SimpleNamespace(_source_path='unused') for _ in range(1000)]
    browser.model.items = items
    monkeypatch.setattr(browser, 'isVisible', lambda: True)
    gui = threading.get_ident()
    workers = []

    def calculate(rows, cache, **kw):
        workers.append(threading.get_ident())
        assert threading.get_ident() != gui
        return 1234

    monkeypatch.setattr(source_store, 'storage_bytes', calculate)
    seen = []
    monkeypatch.setattr(browser.view._detail_panel, 'show_collection', lambda *a: seen.append(a))
    browser.show_scope_info(items)
    assert seen[-1][2] is None
    qtbot.waitUntil(lambda: getattr(browser, '_storage_worker', None) is None)
    assert workers and seen[-1][2] == 1234


def test_storage_summary_keeps_only_one_running_and_latest_pending(main_window, monkeypatch):
    from prism.widgets import asset_browser as module
    browser = main_window.asset_browser
    monkeypatch.setattr(browser, 'isVisible', lambda: True)
    jobs = []
    monkeypatch.setattr(module, 'storage_thread_pool', lambda: SimpleNamespace(start=jobs.append))
    monkeypatch.setattr(source_store, 'storage_bytes', lambda *a, **kw: 999)
    monkeypatch.setattr(browser.view._detail_panel, 'show_collection', lambda *a: None)
    first, second, latest = ([SimpleNamespace(_source_path='unused') for _ in range(300)]
                             for _ in range(3))
    for items in (first, second, latest):
        browser.model.items = items
        browser.show_scope_info(items)
    assert len(jobs) == 1
    assert jobs[0].cancelled.is_set()
    assert browser._storage_pending[0] is latest
    jobs[0].run()
    assert len(jobs) == 2
    assert jobs[1].items is latest
    jobs[1].run()
    assert browser._storage_worker is None


def test_debounced_refresh_skips_only_identical_scene_input(main_window, monkeypatch):
    browser = main_window.asset_browser
    browser._last_scene_refresh_key = browser._scene_refresh_key()
    called = []
    monkeypatch.setattr(browser, 'refresh', lambda: called.append(True))
    browser._refresh_if_changed()
    assert called == []
    browser.view.scene._asset_revision += 1
    browser._refresh_if_changed()
    assert called == [True]


def test_corrupt_cache_opt_in_quarantines_only_cache(catalog):
    before = hashlib.sha256(catalog.source.read_bytes()).hexdigest()
    catalog.cache.write_bytes(b'broken catalog file')
    assert catalog.build(recover_corrupt=True).complete
    preserved = list(catalog.cache.parent.glob(catalog.cache.name + '.corrupt-*'))
    assert len(preserved) == 1
    assert preserved[0].read_bytes() == b'broken catalog file'
    assert hashlib.sha256(catalog.source.read_bytes()).hexdigest() == before


def test_unrelated_database_is_never_quarantined(catalog):
    with sqlite3.connect(catalog.cache) as db:
        db.execute('CREATE TABLE valuable(data)')
        db.execute("INSERT INTO valuable VALUES ('keep')")
    before = catalog.cache.read_bytes()
    with pytest.raises(ValueError):
        catalog.build(recover_corrupt=True)
    assert catalog.cache.read_bytes() == before
    assert not list(catalog.cache.parent.glob(catalog.cache.name + '.corrupt-*'))
