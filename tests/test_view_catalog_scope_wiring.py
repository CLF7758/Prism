"""Behaviour-level guards for the catalog wiring on the default open path.

Why this file exists, in the main session's own words (17:10):

    The first version of these tests called ``view._catalog_scope_instance()``
    **directly** and then built its own ``ProjectAssetCatalog`` with a private
    lambda provider.  Group A proved that version worthless: delete the call
    site in ``PrismGraphicsView.open_from_file`` (``prism/view.py:1390``) or
    break the provider in ``MediaLibraryPanel``, and all four tests stayed
    green.  A test that cannot fail when the thing it guards is removed is not
    evidence.

So every test here drives the **real** production path:

* the entry point is ``view.open_from_file(...)`` -- not the helper;
* the consumer is ``view.category_panel.catalog`` -- the panel the view really
  hangs, with the provider it really installs.

``category_panel`` is the historical attribute name; it holds a
``MediaLibraryPanel`` (``prism/view.py:187``).
"""
import json
import sqlite3

import pytest

from prism.project_open_scope import ProjectOpenScope
from prism.view import _catalog_cache_path
from tests.test_partial_project_gui import PNG, SCHEMA


@pytest.fixture
def project(tmp_path):
    """A small but real .prism file: two canvases, three pixmaps."""
    source = tmp_path / 'wiring.prism'
    with sqlite3.connect(source) as db:
        db.executescript(SCHEMA)
        db.execute('INSERT INTO prism_metadata VALUES(?,?)', ('project_id', 'wiring'))
        for canvas in ('A', 'B'):
            db.execute('INSERT INTO workspace_pages VALUES(?,?,?,?,?)',
                       (canvas, 'canvas', canvas, '{}', 0))
        for key in range(1, 4):
            extra = {'canvasId': 'A' if key <= 2 else 'B',
                     'imageWidth': 16, 'imageHeight': 16,
                     'filename': f'{key}.png'}
            db.execute('INSERT INTO items VALUES(?,?,?,?,?,?,?,?,?)',
                       (key, 'pixmap', key, 0, 0, 1, 0, 1, json.dumps(extra)))
            original = PNG.read_bytes()
            db.execute('INSERT INTO sqlar(name,item_id,mode,sz,data) VALUES(?,?,?,?,?)',
                       (f'{key}.png', key, 0o644, len(original), original))
    return source


@pytest.fixture
def cache_root(tmp_path, monkeypatch):
    """Point the app's catalog cache at tmp_path -- never the shared APPDATA."""
    from prism import thumbnail_cache
    root = tmp_path / 'cache-root'
    root.mkdir()
    monkeypatch.setattr(thumbnail_cache, 'cache_directory',
                        lambda: str(root / 'thumbs'))
    return root


def _build_catalog(project, cache_root):
    cache = _catalog_cache_path(project)
    assert ProjectOpenScope(project, cache).build().complete
    return cache


def _open(main_window, project, monkeypatch):
    """Drive the real full-reader entry point with only its SQL reader stubbed."""
    view = main_window.view
    monkeypatch.setenv('PRISM_PAGED_OPEN', '0')
    monkeypatch.setattr(view, '_start_project_reader', lambda *a, **k: None)
    view.open_from_file(str(project))
    return view


@pytest.fixture
def opened(main_window, project, cache_root, monkeypatch):
    """Open ``project`` with **no catalog on disk yet** -- the honest
    first-open shape.  Scope preparation and every consumer below it run
    exactly as they do in the application.
    """
    return _open(main_window, project, monkeypatch)


@pytest.fixture
def opened_ready(main_window, project, cache_root, monkeypatch):
    """Open ``project`` with a catalog that already exists and is ready.

    Order matters and it is the whole point: **build first, open second**.
    Building after the open would leave ``_open_scope.ready`` False, because
    the scope instance is created at open time against whatever the cache
    holds at that moment.
    """
    _build_catalog(project, cache_root)
    return _open(main_window, project, monkeypatch)


def test_the_default_open_path_hands_a_ready_scope_to_the_real_panel(
        main_window, opened_ready):
    """The whole point of the wiring, asserted through the real panel.

    Removing ``self._catalog_scope_instance(filename)`` from
    ``PrismGraphicsView.open_from_file`` makes this red.
    """
    from prism.project_catalog import catalog_scope_counters, reset_catalog_scope_counters
    view = opened_ready

    assert view._open_scope is not None
    assert view._open_scope.ready is True
    # A ready catalog needs no queued build.
    assert view._catalog_to_build is None

    # The production consumer: the panel the view really hangs, with the
    # provider it really installs.
    panel = view.category_panel
    assert type(panel).__name__ == 'MediaLibraryPanel'
    reset_catalog_scope_counters()
    reached = panel.catalog._catalog_scope()

    assert reached is not None
    counters = catalog_scope_counters()
    assert counters.get('catalog_scope_hit') == 1
    # A miss counter that never fired is absent from the snapshot -- that is
    # the shape `catalog_scope_counters()` really has, so assert on absence.
    assert 'catalog_scope_miss' not in counters
    assert 'catalog_scope_miss:no_scope' not in counters


def test_a_missing_catalog_degrades_honestly_and_queues_a_build(project, opened):
    """No cache yet: attached but not ready, and a background build queued.

    Driven through the real entry point as well -- the helper must not be
    reachable only by direct call.
    """
    from prism.project_catalog import catalog_scope_counters, reset_catalog_scope_counters
    view = opened

    assert view._open_scope is not None
    # Not ready: the instance exists but must not answer as "the project is
    # empty". The catalog layer turns this into a `not_ready` miss.
    assert view._open_scope.ready is False
    pending = view._catalog_to_build
    assert pending is not None
    assert pending['reason'] == 'catalog_missing'
    assert pending['project'] == str(project)
    assert pending['cache'] == _catalog_cache_path(project)

    # And the real panel must report the miss honestly, not zero.
    reset_catalog_scope_counters()
    assert view.category_panel.catalog._catalog_scope() is None
    assert catalog_scope_counters().get('catalog_scope_miss:not_ready') == 1


def test_the_on_demand_path_keeps_its_own_scope_instance(
        main_window, project, cache_root, monkeypatch):
    """The paged open builds its scope in the preparation step; the default
    wiring must not take it over."""
    view = main_window.view
    monkeypatch.setenv('PRISM_PAGED_OPEN', '1')
    called = []
    monkeypatch.setattr(view, '_catalog_scope_instance',
                        lambda filename: called.append(filename))
    monkeypatch.setattr(view, '_start_project_reader', lambda *a, **k: None)
    monkeypatch.setattr(view, '_paged_preparer', lambda: _FakePreparer(view))

    view.open_from_file(str(project))

    assert called == []


class _FakePreparer:
    """Stands in for PagedOpenPreparation: records the start, does nothing."""

    def __init__(self, view):
        self.view = view
        self.progress = _FakeSignal()

    def start(self, *args, **kwargs):
        return None


class _FakeSignal:
    def emit(self, *args, **kwargs):
        return None