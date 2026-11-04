"""
Delegation round 2026-10-23 -- P3: the catalog must follow the file a save wrote.

The owner's complaint: after editing a note and saving, the aggregation list
degrades (the catalog is `not_ready`, the counts fall back to the loaded page)
and only recovers after the app is restarted.

The contract this module pins down:

1. A CONFIRMED save re-points the read-only catalog at the file that was just
   written, and asks for a background rebuild.
2. A save that could NOT be confirmed must not touch the catalog version and
   must not clear the dirty flag.
3. The rebuild never fakes the fingerprint: the catalog stays honestly
   "not ready" until the rebuild finished.
4. While the catalog is being rebuilt the aggregation must not keep serving the
   previous rows as if they were the whole project.
5. A reply for the OLD catalog is rejected: the generation counter goes up.
"""
import json
from pathlib import Path
import threading

import pytest

EVIDENCE = Path(r"C:\Users\admin\Desktop\Prism-main\Prism-main\artifacts"
                r"\test-delegation-20261023\fix-20261023\evidence")
EVIDENCE.mkdir(parents=True, exist_ok=True)


class _FakeScope:
    """A catalog scope whose readiness is under the test's control."""

    def __init__(self, cache_path, ready, project_path=None):
        self.cache_path = str(cache_path)
        self.project_path = None if project_path is None else str(project_path)
        self._ready = ready

    @property
    def ready(self):
        return self._ready


def _instrument(monkeypatch, view, browser):
    state = {'invalidate': 0, 'build': None}

    def _invalidate():
        state['invalidate'] += 1

    def _build(filename):
        state['build'] = str(filename)

    monkeypatch.setattr(browser, 'invalidate_aggregate_result', _invalidate)
    monkeypatch.setattr(view, '_maybe_start_catalog_build', _build)
    return state


def test_p3_confirmed_save_requests_a_rebuild(main_window, monkeypatch, tmp_path):
    """A confirmed save asks for the rebuild and drops the stale rows."""
    window = main_window
    view = window.view
    browser = window.asset_browser

    # The file changed, so the catalog that describes the OLD file is no longer
    # ready -- that is the honest state, not something to paper over.
    view._open_scope = _FakeScope(tmp_path / 'cat.sqlite', ready=False)
    state = _instrument(monkeypatch, view, browser)

    view._refresh_catalog_after_save(str(tmp_path / 'project.prism'))

    assert state['invalidate'] == 1, (
        'the aggregate rows of the previous file version must be dropped')
    assert state['build'] is not None and state['build'].endswith('project.prism')
    pending = view._catalog_to_build
    assert pending['reason'] == 'source_changed_by_save'
    assert pending['cache'].endswith('cat.sqlite')
    assert pending['project'].endswith('project.prism')


def test_p3_save_as_invalidates_once_and_cancels_old_worker(main_window,
                                                             monkeypatch,
                                                             tmp_path):
    """Changing source drops the old model once and supersedes its worker."""
    view = main_window.view
    browser = main_window.asset_browser
    old_source = tmp_path / 'old' / 'project.prism'
    new_source = tmp_path / 'new' / 'project.prism'
    new_scope = _FakeScope(tmp_path / 'new.sqlite', ready=False,
                           project_path=new_source)
    view._open_scope = _FakeScope(tmp_path / 'old.sqlite', ready=True,
                                  project_path=old_source)
    old_cancel = threading.Event()
    view._catalog_build_active = {
        'cancel': old_cancel, 'callbacks': [lambda _path: None],
    }
    monkeypatch.setattr(view, '_catalog_scope_instance', lambda _filename: new_scope)
    state = _instrument(monkeypatch, view, browser)

    view._refresh_catalog_after_save(str(new_source))

    assert old_cancel.is_set()
    assert view._catalog_build_active['callbacks'] == []
    assert state['invalidate'] == 1
    assert state['build'] == str(new_source)
    assert view._catalog_to_build == {
        'project': str(new_source),
        'cache': str(tmp_path / 'new.sqlite'),
        'reason': 'source_changed_by_save',
    }


def test_p3_ready_catalog_is_not_rebuilt(main_window, monkeypatch, tmp_path):
    """A catalog that still describes this exact file needs no rebuild."""
    window = main_window
    view = window.view
    browser = window.asset_browser

    view._open_scope = _FakeScope(tmp_path / 'cat.sqlite', ready=True)
    state = _instrument(monkeypatch, view, browser)

    view._refresh_catalog_after_save(str(tmp_path / 'project.prism'))

    assert state['build'] is None, 'a ready catalog must not trigger a rebuild'
    assert state['invalidate'] == 0, 'a ready catalog must not drop the rows'


def test_p3_no_catalog_means_nothing_to_do(main_window, monkeypatch, tmp_path):
    """A project without a catalog has nothing to re-point."""
    window = main_window
    view = window.view
    browser = window.asset_browser

    view._open_scope = None
    state = _instrument(monkeypatch, view, browser)

    view._refresh_catalog_after_save(str(tmp_path / 'project.prism'))

    assert state['build'] is None
    assert state['invalidate'] == 0


def test_p3_unconfirmed_save_leaves_the_catalog_alone(main_window, monkeypatch,
                                                     tmp_path):
    """The bytes are on disk but the session could not confirm them.

    The catalog version must NOT be switched and the dirty flag must stay:
    the user keeps editing and assumes the next save is safe.
    """
    window = main_window
    view = window.view
    browser = window.asset_browser

    view._open_scope = _FakeScope(tmp_path / 'cat.sqlite', ready=False)
    state = _instrument(monkeypatch, view, browser)

    # A partial snapshot for a DIFFERENT project: the acknowledgement is
    # refused, which is the "could not confirm" path.
    class _OtherProject:
        project_id = 'some-other-project'

    view._save_partial_snapshot = _OtherProject()
    view._content_dirty = True

    view.on_saving_finished(str(tmp_path / 'project.prism'), errors=[])

    assert state['build'] is None, (
        'an unconfirmed save must not switch the catalog version')
    assert state['invalidate'] == 0
    assert view._content_dirty is True, (
        'an unconfirmed save must keep the dirty flag')


def test_p3_failed_save_keeps_dirty_and_catalog(main_window, monkeypatch,
                                                tmp_path):
    """A save that reported errors changes nothing."""
    window = main_window
    view = window.view
    browser = window.asset_browser

    view._open_scope = _FakeScope(tmp_path / 'cat.sqlite', ready=False)
    state = _instrument(monkeypatch, view, browser)
    view._content_dirty = True

    view.on_saving_finished(str(tmp_path / 'project.prism'), errors=['boom'])

    assert state['build'] is None
    assert state['invalidate'] == 0
    assert view._content_dirty is True


def test_p3_invalidate_bumps_the_generation(main_window, tmp_path):
    """A reply for the old catalog must be rejected when it lands."""
    window = main_window
    browser = window.asset_browser

    before = browser._catalog_generation
    browser.invalidate_aggregate_result()
    after = browser._catalog_generation

    assert after > before, (
        'the generation counter must go up so an in-flight reply for the old '
        'catalog is rejected')
    assert browser._catalog_scalar_cache is None
    assert browser._catalog_pending is None
    assert browser._catalog_result_key is None
    # The rows of the previous version must be gone, not merely re-labelled.
    assert len(browser.model.items) == 0


def test_p3_stale_reply_is_rejected(main_window, monkeypatch, tmp_path):
    """A reply carrying the old generation must not install anything."""
    window = main_window
    browser = window.asset_browser

    browser._catalog_generation = 7
    monkeypatch.setattr(browser, 'model', type('M', (), {
        'items': [], 'replace': lambda self, rows: None})())

    called = []
    monkeypatch.setattr(browser, '_install_catalog_rows',
                        lambda *a, **kw: called.append(True))

    # A reply for generation 6 (the old catalog) arrives.  The payload shape is
    # (token, rows, error) and the token itself is (generation, project id).
    browser._catalog_rows_ready(((6, window.view.scene.project_id), [], None))

    assert not called, 'a reply for a superseded generation must be dropped'
