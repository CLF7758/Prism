"""A failed or cancelled scoped open must never look like an opened project.

Regression cover for the case where ``open_from_file`` cleared the canvas and
reported success before preparation ran: the view ended up pointing at a real
``filename`` with an empty scene, so the next save could have rewritten the
project with nothing in it.
"""
import hashlib
import json
import sqlite3

import pytest

from prism import paged_open_preparation as preparation
from tests.test_paged_canvas_loader import pages  # noqa: F401  (fixture)
from tests.test_partial_project_gui import SCHEMA, PNG


@pytest.fixture
def project(tmp_path):
    """A real project on disk that a scoped open will be pointed at."""
    source = tmp_path / 'honest.prism'
    with sqlite3.connect(source) as db:
        db.executescript(SCHEMA)
        db.execute('INSERT INTO prism_metadata VALUES(?,?)', ('project_id', 'p1'))
        db.execute('INSERT INTO workspace_pages VALUES(?,?,?,?,?)',
                   ('A', 'canvas', 'A', '{}', 0))
        for key in range(1, 4):
            extra = {'canvasId': 'A', 'imageWidth': 16, 'imageHeight': 16,
                     'filename': f'{key}.png'}
            db.execute('INSERT INTO items VALUES(?,?,?,?,?,?,?,?,?)',
                       (key, 'pixmap', key, 0, 0, 1, 0, 1, json.dumps(extra)))
            blob = PNG.read_bytes()
            db.execute('INSERT INTO sqlar(name,item_id,mode,sz,data) VALUES(?,?,?,?,?)',
                       (f'{key}.png', key, 0o644, len(blob), blob))
    return source


def _digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _enable_scoped_open(view, source, monkeypatch):
    from prism import thumbnail_cache
    from prism.project_open_scope import ProjectOpenScope
    monkeypatch.setattr(thumbnail_cache, 'cache_directory',
                        lambda: str(source.parent / 'thumbs'))
    from pathlib import Path
    from prism.view import _catalog_cache_path
    cache = Path(_catalog_cache_path(source))
    cache.parent.mkdir(exist_ok=True)
    ProjectOpenScope(source, cache).build()
    monkeypatch.setenv('PRISM_PAGED_OPEN', '1')


def test_failed_scoped_open_never_claims_the_project_is_open(
        pages, project, qtbot, monkeypatch):  # noqa: F811
    view, _scope, _session = pages
    _enable_scoped_open(view, project, monkeypatch)
    before = _digest(project)

    def boom(*args, **kwargs):
        raise RuntimeError('Project catalog is not ready after preparation')

    monkeypatch.setattr(preparation, 'resolve_scope', boom)
    result = view.open_from_file(str(project))

    # "Not open yet" - the caller must not adopt the filename either.
    assert result is False
    qtbot.waitUntil(lambda: view._paged_open_failed, timeout=15000)
    assert view.filename is None, 'an empty scene must not point at a real project'
    assert list(view.scene.iter_assets()) == []
    assert view._paged_open_failed_reason
    assert _digest(project) == before, 'a failed open must not touch the file'


def test_failed_open_refuses_to_save_over_the_real_project(
        pages, project, qtbot, monkeypatch):  # noqa: F811
    view, _scope, _session = pages
    _enable_scoped_open(view, project, monkeypatch)

    def boom(*args, **kwargs):
        raise RuntimeError('Project catalog is not ready after preparation')

    monkeypatch.setattr(preparation, 'resolve_scope', boom)
    view.open_from_file(str(project))
    qtbot.waitUntil(lambda: view._paged_open_failed, timeout=15000)

    before = _digest(project)
    view.do_save(str(project), create_new=False)
    worker = getattr(view, 'worker', None)
    assert worker is None or not worker.isRunning(), (
        'saving an empty scene after a failed open must not start a writer')
    assert _digest(project) == before, 'a refused save must not touch the file'
    assert 'unavailable' in view.parent.statusBar().currentMessage()


def test_cancelled_scoped_open_is_not_reported_as_a_project_failure(
        pages, project, qtbot, monkeypatch):  # noqa: F811
    view, _scope, _session = pages
    _enable_scoped_open(view, project, monkeypatch)

    def cancelled(*args, **kwargs):
        raise preparation.PreparationCancelled('Opening was cancelled')

    monkeypatch.setattr(preparation, 'resolve_scope', cancelled)
    assert view.open_from_file(str(project)) is False
    qtbot.waitUntil(lambda: not view._paged_preparing, timeout=15000)

    assert not view._paged_open_failed, 'a user cancellation is not a project error'
    assert view.filename is None
    assert 'cancelled' in view.parent.statusBar().currentMessage().lower()


def test_cancelled_open_also_refuses_to_save_an_empty_canvas(
        pages, project, qtbot, monkeypatch):  # noqa: F811
    """A cancelled open leaves no project either, so it must not be savable."""
    view, _scope, _session = pages
    _enable_scoped_open(view, project, monkeypatch)
    before = _digest(project)

    def cancelled(*args, **kwargs):
        raise preparation.PreparationCancelled('Opening was cancelled')

    monkeypatch.setattr(preparation, 'resolve_scope', cancelled)
    view.open_from_file(str(project))
    qtbot.waitUntil(lambda: view._paged_open_cancelled, timeout=15000)

    view.do_save(str(project), create_new=False)
    worker = getattr(view, 'worker', None)
    assert worker is None or not worker.isRunning(), (
        'a cancelled open must not leave an empty canvas that can be written')
    assert _digest(project) == before
    assert 'cancelled' in view.parent.statusBar().currentMessage().lower()


def test_scoped_open_setting_defaults_on_and_env_overrides(pages, monkeypatch):  # noqa: F811
    view, _scope, _session = pages
    monkeypatch.delenv('PRISM_PAGED_OPEN', raising=False)
    view.settings.remove('Project/paged_open')
    assert view._paged_open_enabled() is True, (
        'paged open is on by default since 2026-10-23 '
        '(coordination/决策-20261023.md)')

    view.settings.setValue('Project/paged_open', 'off')
    assert view._paged_open_enabled() is False

    # The environment stays a hard override in both directions.
    monkeypatch.setenv('PRISM_PAGED_OPEN', '1')
    assert view._paged_open_enabled() is True
    monkeypatch.delenv('Project/paged_open', raising=False)
    monkeypatch.setenv('PRISM_PAGED_OPEN', '0')
    assert view._paged_open_enabled() is False