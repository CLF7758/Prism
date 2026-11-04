"""Reject delayed counts using the actual window consumption methods."""
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import Mock
import pytest
from prism.count_contract import CountPublication, CANVAS_DRAWN, LIBRARY_TOTAL
from prism.__main__ import PrismMainWindow


def make_window():
    identity = dict(project='A', open_epoch='open-1', query='all',
                    source_revision='source-1', overlay_revision=10)
    panel = SimpleNamespace(count_identity=lambda: dict(identity))
    window = SimpleNamespace(view=SimpleNamespace(category_panel=panel),
                             _render_status=Mock())
    for name in ('_accept_publication', '_with_external_counts', 'publish_count'):
        setattr(window, name, getattr(PrismMainWindow, name).__get__(window))
    return window, identity


@pytest.mark.parametrize('field,old', [
    ('project', 'B'), ('open_epoch', 'open-0'), ('query', 'tag:old'),
    ('source_revision', 'source-0'), ('overlay_revision', 9),
    ('overlay_revision', 11),
])
def test_later_token_cannot_publish_other_identity(field, old):
    window, identity = make_window()
    good = CountPublication(request_token=10, **identity, counts={LIBRARY_TOTAL: 37})
    assert window._accept_publication(good)
    stale = replace(good, request_token=100, **{field: old})
    assert not window._accept_publication(stale)
    assert window._latest_publication.value(LIBRARY_TOTAL) == 37


def test_external_count_is_retained_only_for_same_identity():
    window, identity = make_window()
    assert window._accept_publication(CountPublication(request_token=1, **identity))
    window.publish_count(CANVAS_DRAWN, 8, generation=100)
    token = window._latest_publication.request_token + 1
    assert window._accept_publication(CountPublication(request_token=token, **identity))
    assert window._latest_publication.value(CANVAS_DRAWN) == 8
    identity['query'] = 'tag:new'
    assert window._accept_publication(CountPublication(request_token=token+1, **identity))
    assert window._latest_publication.value(CANVAS_DRAWN) is None
    # A new identity starts its own generation sequence, even if smaller.
    window.publish_count(CANVAS_DRAWN, 2, generation=1)
    assert window._latest_publication.value(CANVAS_DRAWN) == 2


def test_explicit_old_async_identity_is_rejected_after_reopen():
    window, identity = make_window()
    old = dict(identity)
    window.publish_count(CANVAS_DRAWN, 8, identity=old)
    identity.update(project='B', open_epoch='open-2')
    window.publish_count(CANVAS_DRAWN, 1)
    window.publish_count(CANVAS_DRAWN, 999, identity=old)
    assert window._latest_publication.project == 'B'
    assert window._latest_publication.value(CANVAS_DRAWN) == 1


def test_older_generation_cannot_replace_newer_for_same_identity():
    window, identity = make_window()
    window.publish_count(CANVAS_DRAWN, 8, generation=10)
    window.publish_count(CANVAS_DRAWN, 999, generation=9)
    assert window._latest_publication.value(CANVAS_DRAWN) == 8


def test_count_identity_never_reads_source_store():
    from prism.widgets.media_library import MediaLibraryPanel
    panel = SimpleNamespace(scene=SimpleNamespace(project_id='A'),
                            _open_epoch=lambda: 'open-1',
                            _count_query_signature=lambda: 'all',
                            _count_revision=10,
                            _count_source_identity=('A', 'open-1', 'source-1'),
                            _source_revision_token=Mock(side_effect=AssertionError('I/O')))
    actual = MediaLibraryPanel.count_identity(panel)
    assert actual['source_revision'] == 'source-1'
    panel.scene.project_id = 'B'
    assert MediaLibraryPanel.count_identity(panel)['source_revision'] is None
    panel._source_revision_token.assert_not_called()


def test_real_window_rejects_old_query_and_keeps_current_count(main_window):
    panel = main_window.view.category_panel
    panel.update_counts()
    first = panel.count_publication()
    assert main_window._accept_publication(replace(first, request_token=10**12))
    panel._active_tags = {'new-tag'}
    stale = replace(first, request_token=10**12+1)
    assert not main_window._accept_publication(stale)
    current = replace(stale, request_token=10**12+2,
                      query=panel._count_query_signature())
    assert main_window._accept_publication(current)


def test_cancelled_window_close_does_not_stop_source_service(main_window, monkeypatch):
    from unittest.mock import Mock
    from prism import source_store
    stop = Mock()
    monkeypatch.setattr(source_store, 'stop_accepting_materialization', stop)
    monkeypatch.setattr(main_window, '_store_current_page', lambda: True)
    monkeypatch.setattr(main_window.view, 'get_confirmation_unsaved_changes',
                        lambda *args: False)
    event = Mock()
    main_window.closeEvent(event)
    event.ignore.assert_called_once()
    stop.assert_not_called()


def test_accepted_window_close_stops_source_service_without_waiting(main_window, monkeypatch):
    from unittest.mock import Mock
    from prism import source_store
    stop, shutdown = Mock(), Mock()
    monkeypatch.setattr(source_store, 'stop_accepting_materialization', stop)
    monkeypatch.setattr(source_store, 'shutdown_materialization_service', shutdown)
    monkeypatch.setattr(main_window, '_store_current_page', lambda: True)
    monkeypatch.setattr(main_window.view, 'get_confirmation_unsaved_changes',
                        lambda *args: True)
    event = Mock()
    main_window.closeEvent(event)
    event.accept.assert_called_once()
    stop.assert_called_once_with()
    shutdown.assert_not_called()
