"""S2: after "Save As" the whole-project catalogue must follow the **new** file.

A Save As only renames the project: the old file keeps its bytes, so the old
catalogue's fingerprint is still valid and the sidebar counts / the project
search would keep reading the *previous* project.  The session side already
knows that (``prism/partial_project.py``: ``requires_catalog_rebuild()`` /
``catalog_rebuild_notice()`` / ``mark_catalog_rebuilt()``); the view side has
to drop the stale scope, fall back to the in-memory scene for the transition
period and rebuild the catalogue for the new file.

These tests pin the view-side contract:

* ``_maybe_start_catalog_build`` grows an **optional** ``on_ready`` hook
  (``None`` by default: not passing it keeps today's behaviour byte for byte);
* the hook fires **after a successful build**, on the GUI thread;
* the Save As wiring drops the old scope, refreshes the counts, says
  "Rebuilding the project index…" through the existing ``_catalog_status()``
  channel and queues the rebuild under the **new file name**;
* the hook installs the new scope and closes the session state.

The view-side tests are skipped until the wiring lands in ``prism/view.py``
(main session).  The session-side contract is pinned unconditionally.
"""
import inspect
import json
import sqlite3
import threading
import time
import types
from types import SimpleNamespace

import pytest
from PyQt6 import QtCore, QtWidgets

from prism.partial_project import (
    CATALOG_REBUILDING_MESSAGE,
    CATALOG_SCOPE_CURRENT,
    CATALOG_SCOPE_STALE,
    PartialProjectSession,
)
from prism.view import PrismGraphicsView, _catalog_cache_path
from tests.test_partial_project_gui import PNG, SCHEMA

WIRING_METHOD = '_after_partial_save_catalog_rebind'

needs_wiring = pytest.mark.skipif(
    not hasattr(PrismGraphicsView, WIRING_METHOD),
    reason='S2 view-side wiring not landed yet (main session TODO)')

needs_on_ready = pytest.mark.skipif(
    'on_ready' not in inspect.signature(
        PrismGraphicsView._maybe_start_catalog_build).parameters,
    reason='_maybe_start_catalog_build has no on_ready hook yet (main session TODO)')


# ── helpers ─────────────────────────────────────────────────────────────

def _stale_session(source):
    """A session that has just been rebound to another file (Save As).

    Built without touching the disk: the three catalogue methods only read
    ``_catalog_scope_state`` and ``source``.
    """
    session = object.__new__(PartialProjectSession)
    session.source = str(source)
    session._catalog_scope_state = CATALOG_SCOPE_STALE
    return session


class _StubPanel:
    """The smallest stand-in for ``MediaLibraryPanel``."""

    def __init__(self):
        self.calls = 0

    def update_counts(self):
        self.calls += 1


class _StubBrowser:
    def __init__(self):
        self.invalidations = 0

    def invalidate_aggregate_result(self):
        self.invalidations += 1


class _StubView(QtCore.QObject):
    """Every view-side member the wiring touches.

    A real ``QObject``: the wiring parents the build-signal bridge to the
    view, so the stand-in has to be one too.
    """

    def __init__(self):
        super().__init__()
        self._open_scope = 'old-scope'      # sentinel: must not survive
        self.category_panel = _StubPanel()
        self.parent = SimpleNamespace(asset_browser=_StubBrowser())
        self.statuses = []
        self.build_calls = []
        self._catalog_to_build = None
        self._catalog_build_active = None
        self._catalog_build_pending = None
        self._catalog_build_cancel = None
        self._catalog_build_epoch = 0
        self._catalog_build_delivery = None

    def _catalog_status(self, text):
        self.statuses.append(text)


class _CapturingView(_StubView):
    """Captures the ``on_ready`` hook instead of running a background build."""

    def _maybe_start_catalog_build(self, filename, on_ready=None):
        self.build_calls.append((str(filename), on_ready))


class _IndexWarmupContract:
    def tag_groups(self, *, cancel=None):
        return []


@pytest.fixture
def prepared_test_scope(monkeypatch):
    # These tests isolate writer coordination, not source-file/scope readiness.
    monkeypatch.setattr(PrismGraphicsView, '_catalog_scope_instance',
        lambda self, filename, **kwargs: SimpleNamespace(ready=True))


@pytest.fixture
def cache_root(tmp_path, monkeypatch):
    """Point the catalogue cache at tmp_path -- never the shared APPDATA."""
    from prism import thumbnail_cache
    root = tmp_path / 'cache-root'
    root.mkdir()
    monkeypatch.setattr(thumbnail_cache, 'cache_directory',
                        lambda: str(root / 'thumbs'))
    return root


@pytest.fixture
def project(tmp_path):
    """A small but real .prism file (two canvases, three pixmaps)."""
    source = tmp_path / 'saved-as.prism'
    with sqlite3.connect(source) as db:
        db.executescript(SCHEMA)
        db.execute('INSERT INTO prism_metadata VALUES(?,?)', ('project_id', 'rebind'))
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


def test_catalog_cache_path_distinguishes_equal_basenames(tmp_path, cache_root):
    first = tmp_path / 'one' / 'project.prism'
    second = tmp_path / 'two' / 'project.prism'

    first_cache = _catalog_cache_path(first)
    second_cache = _catalog_cache_path(second)

    assert first_cache != second_cache
    assert first_cache == _catalog_cache_path(first)
    assert second_cache == _catalog_cache_path(second)
    assert first_cache.endswith('.sqlite') and second_cache.endswith('.sqlite')


# ── session side (already landed; pinned unconditionally) ───────────────

def test_notice_reports_stale_counts_and_fallback_search(tmp_path):
    session = _stale_session(tmp_path / 'new.prism')

    notice = session.catalog_rebuild_notice()

    assert session.requires_catalog_rebuild() is True
    assert notice['counts'] == 'stale'
    assert notice['search'] == 'fallback'
    assert notice['message'] == CATALOG_REBUILDING_MESSAGE
    assert notice['message'] == 'Rebuilding the project index…'
    assert notice['source'] == str(tmp_path / 'new.prism')


def test_mark_catalog_rebuilt_clears_the_stale_state(tmp_path):
    session = _stale_session(tmp_path / 'new.prism')

    session.mark_catalog_rebuilt()

    assert session.requires_catalog_rebuild() is False
    assert session._catalog_scope_state == CATALOG_SCOPE_CURRENT
    assert session.catalog_rebuild_notice()['counts'] == 'current'
    assert session.catalog_rebuild_notice()['search'] == 'catalogue'


# ── view side (active once the wiring lands) ───────────────────────────

@needs_on_ready
def test_maybe_start_catalog_build_takes_an_optional_on_ready():
    signature = inspect.signature(PrismGraphicsView._maybe_start_catalog_build)

    assert 'on_ready' in signature.parameters
    assert signature.parameters['on_ready'].default is None


@needs_on_ready
def test_maybe_start_catalog_build_without_on_ready_keeps_todays_behaviour():
    view = _StubView()
    view._catalog_to_build = None

    assert PrismGraphicsView._maybe_start_catalog_build(view, 'x.prism') is None
    # No pending build -> nothing queued, nothing said.
    assert view._catalog_to_build is None
    assert view.statuses == []


@needs_on_ready
def test_on_ready_fires_after_a_successful_build(qtbot, monkeypatch, tmp_path, prepared_test_scope):
    from prism import project_catalog_index

    class _FakeIndex(_IndexWarmupContract):
        def __init__(self, project, cache):
            self.source = project
            self.cache = cache

        def source_revision(self):
            return (str(self.source), (1, 2, 3, 4), ())

        def build(self, cancel=None, lock_wait_seconds=0,
                  lock_poll_seconds=.05):
            return SimpleNamespace(complete=True, total_records=7, seconds=0.01,
                                   lock_wait_seconds=0, resumed=False, processed_records=7)

    monkeypatch.setattr(project_catalog_index, 'ProjectCatalogIndex', _FakeIndex)

    app = QtWidgets.QApplication.instance()
    calls = []

    def on_ready(cache_path):
        calls.append((cache_path,
                      QtCore.QThread.currentThread() is app.thread()))

    view = _StubView()
    view._catalog_to_build = {'project': str(tmp_path / 'p.prism'),
                              'cache': str(tmp_path / 'p.sqlite'),
                              'reason': 'source_changed'}

    PrismGraphicsView._maybe_start_catalog_build(view, 'p.prism',
                                                 on_ready=on_ready)

    qtbot.waitUntil(lambda: bool(calls), timeout=5000)
    assert calls == [(str(tmp_path / 'p.sqlite'), True)]


@needs_on_ready
def test_same_catalog_target_merges_without_cancel_or_restart(qtbot, monkeypatch,
                                                              tmp_path, prepared_test_scope):
    from prism import project_catalog_index

    entered = threading.Event()
    release = threading.Event()
    starts = []

    class _ControlledIndex(_IndexWarmupContract):
        def __init__(self, project, cache):
            self.source = project
            self.cache = cache

        def source_revision(self):
            return (str(self.source), (10, 20, 30, 40), ())

        def build(self, cancel=None, **_options):
            starts.append(str(self.source))
            entered.set()
            assert release.wait(5)
            return SimpleNamespace(complete=True, total_records=7, seconds=.01,
                                   lock_wait_seconds=0, resumed=False, processed_records=7)

    monkeypatch.setattr(project_catalog_index, 'ProjectCatalogIndex', _ControlledIndex)
    view = _StubView()
    ready = []
    pending = {'project': str(tmp_path / 'same.prism'),
               'cache': str(tmp_path / 'same.sqlite'),
               'reason': 'source_changed_by_save'}
    view._catalog_to_build = dict(pending)
    PrismGraphicsView._maybe_start_catalog_build(view, pending['project'],
                                                 on_ready=lambda cache: ready.append(('a', cache)))
    assert entered.wait(2)
    first = view._catalog_build_active

    view._catalog_to_build = dict(pending)
    PrismGraphicsView._maybe_start_catalog_build(view, pending['project'],
                                                 on_ready=lambda cache: ready.append(('b', cache)))

    assert starts == [pending['project']]
    assert view._catalog_build_active is first
    assert not first['cancel'].is_set()
    assert view._catalog_build_pending is None
    release.set()
    qtbot.waitUntil(lambda: len(ready) == 2, timeout=5000)
    assert [name for name, _cache in ready] == ['a', 'b']


@needs_on_ready
def test_new_catalog_target_starts_only_after_old_worker_terminal(qtbot,
                                                                  monkeypatch,
                                                                  tmp_path, prepared_test_scope):
    from prism import project_catalog_index

    first_entered = threading.Event()
    first_release = threading.Event()
    first_ended = threading.Event()
    second_entered = threading.Event()
    second_release = threading.Event()
    order = []

    class _ControlledIndex(_IndexWarmupContract):
        def __init__(self, project, cache):
            self.source = project
            self.cache = cache

        def source_revision(self):
            return (str(self.source), (len(str(self.source)), 2, 3, 4), ())

        def build(self, cancel=None, **_options):
            name = str(self.source)
            order.append(('start', name, first_ended.is_set()))
            if name.endswith('old.prism'):
                first_entered.set()
                assert first_release.wait(5)
                first_ended.set()
            else:
                second_entered.set()
                assert second_release.wait(5)
            return SimpleNamespace(complete=True, total_records=3, seconds=.01,
                                   lock_wait_seconds=0, resumed=False, processed_records=3)

    monkeypatch.setattr(project_catalog_index, 'ProjectCatalogIndex', _ControlledIndex)
    view = _StubView()
    callbacks = []
    old = {'project': str(tmp_path / 'old.prism'),
           'cache': str(tmp_path / 'old.sqlite'), 'reason': 'initial'}
    new = {'project': str(tmp_path / 'new.prism'),
           'cache': str(tmp_path / 'new.sqlite'), 'reason': 'source_changed_by_save'}
    view._catalog_to_build = dict(old)
    PrismGraphicsView._maybe_start_catalog_build(
        view, old['project'], on_ready=lambda cache: callbacks.append(('old', cache)))
    assert first_entered.wait(2)

    view._catalog_to_build = dict(new)
    PrismGraphicsView._maybe_start_catalog_build(
        view, new['project'], on_ready=lambda cache: callbacks.append(('new', cache)))
    assert view._catalog_build_active['cancel'].is_set()
    assert view._catalog_build_pending['project'] == new['project']
    assert not second_entered.is_set()

    # Even if the superseded worker ignores cancellation and returns a complete
    # result, it may neither publish nor start the replacement until it ended.
    first_release.set()
    qtbot.waitUntil(second_entered.is_set, timeout=5000)
    assert first_ended.is_set()
    assert ('start', new['project'], True) in order
    assert callbacks == []
    second_release.set()
    qtbot.waitUntil(lambda: callbacks == [('new', new['cache'])], timeout=5000)


@needs_on_ready
def test_cancel_catalog_builds_invalidates_callbacks_without_join(monkeypatch,
                                                                  tmp_path):
    cancelled = threading.Event()
    callback = lambda _cache: None
    view = _StubView()
    view._catalog_build_active = {'cancel': cancelled, 'callbacks': [callback]}
    view._catalog_build_pending = {'target': 'pending'}
    before = view._catalog_build_epoch

    PrismGraphicsView._cancel_catalog_builds(view)

    assert cancelled.is_set()
    assert view._catalog_build_active['callbacks'] == []
    assert view._catalog_build_pending is None
    assert view._catalog_build_epoch == before + 1


@needs_wiring
def test_save_as_drops_the_old_scope_and_queues_a_rebuild(project, cache_root):
    view = _CapturingView()
    view._catalog_scope_instance = types.MethodType(
        PrismGraphicsView._catalog_scope_instance, view)
    session = _stale_session(project)

    PrismGraphicsView._after_partial_save_catalog_rebind(view, session)

    # The old scope is gone: during the transition the catalogue counts are
    # unavailable, so they fall back instead of showing the numbers of the
    # project we came from.
    scope = view._open_scope
    assert scope is None or scope.ready is False
    if scope is not None:
        assert str(scope.project_path) == str(project)
    # The counts are refreshed right away (update_counts -> counts_changed ->
    # _update_navigation -> catalog.count_snapshot()).
    assert view.category_panel.calls == 1
    # The status bar keeps using the existing _catalog_status() channel.
    assert CATALOG_REBUILDING_MESSAGE in view.statuses
    # The cache is keyed by the complete normalized source path, so Save As
    # cannot collide with an equally named project in another directory.
    pending = view._catalog_to_build
    assert pending['project'] == str(project)
    assert pending['cache'] == _catalog_cache_path(project)
    # The rebuild is queued with the on_ready hook attached.
    assert len(view.build_calls) == 1
    assert view.build_calls[0][0] == str(project)
    assert callable(view.build_calls[0][1])


@needs_wiring
def test_on_ready_installs_the_new_scope_and_closes_the_session(project,
                                                                 cache_root):
    from prism.project_open_scope import ProjectOpenScope

    # A cache that is valid for the new file, built somewhere else than the
    # one the wiring queues: the hook must install *this* scope.
    cache = cache_root / 'built' / 'saved-as.sqlite'
    cache.parent.mkdir(parents=True, exist_ok=True)
    assert ProjectOpenScope(str(project), str(cache)).build().complete

    view = _CapturingView()
    view._catalog_scope_instance = types.MethodType(
        PrismGraphicsView._catalog_scope_instance, view)
    session = _stale_session(project)

    PrismGraphicsView._after_partial_save_catalog_rebind(view, session)
    _, on_ready = view.build_calls[0]

    on_ready(str(cache))

    assert view._open_scope is not None
    assert view._open_scope.ready is True
    assert str(view._open_scope.project_path) == str(project)
    # The session knows the catalogue describes this file again.
    assert session.requires_catalog_rebuild() is False
    assert 'Project index ready.' in view.statuses


@needs_wiring
def test_a_valid_cache_for_the_new_name_is_not_rebuilt(project, cache_root):
    """Same file name, cache already valid: no second build, session closed."""
    from prism.project_open_scope import ProjectOpenScope

    cache = _catalog_cache_path(project)
    assert ProjectOpenScope(str(project), str(cache)).build().complete

    view = _CapturingView()
    view._catalog_scope_instance = types.MethodType(
        PrismGraphicsView._catalog_scope_instance, view)
    old_cancel = threading.Event()
    old_callback = lambda _cache: None
    view._catalog_build_active = {
        'cancel': old_cancel, 'callbacks': [old_callback],
    }
    session = _stale_session(project)

    PrismGraphicsView._after_partial_save_catalog_rebind(view, session)

    # Nothing to rebuild: the catalogue already describes the new file. The
    # old-source worker subscription is still cancelled before this fast path,
    # so a late complete result cannot publish into the Save As destination.
    assert view.build_calls == []
    assert old_cancel.is_set()
    assert view._catalog_build_active['callbacks'] == []
    assert view.parent.asset_browser.invalidations == 1
    assert view._open_scope is not None
    assert view._open_scope.ready is True
    assert session.requires_catalog_rebuild() is False
    assert 'Project index ready.' in view.statuses
