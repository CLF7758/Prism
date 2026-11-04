import threading
from unittest.mock import Mock

from prism.catalog_browser import CatalogRows, MAX_CACHED_PAGES
from prism.presentation_evidence import QueryEvidence, fingerprint
from tests.test_catalog_keyset_overlay import scope_for
from tests.test_catalog_browser_partial import prepare, wait_complete


def test_evicted_directory_pages_replay_without_sql_reads(tmp_path):
    scope = scope_for(tmp_path, [(i, 'pixmap', dict(canvasId='A', title=str(i))) for i in range(1200)])
    rows = CatalogRows(scope, page_size=32).prepare()
    try:
        assert [r['id'] for r in rows] == list(range(1200))
        read = rows.stats()['rows_read']
        rows.bind_evidence(QueryEvidence(), lambda: False)
        assert [r['id'] for r in rows] == list(range(1200))
        assert rows.stats()['rows_read'] == read
        assert rows.stats()['cached_pages'] <= MAX_CACHED_PAGES
        assert rows.stats()['replay_bytes'] <= rows.stats()['replay_limit']
    finally:
        rows.close()
    assert rows._spool.closed


def test_gui_slice_yields_when_next_page_is_not_available(tmp_path, monkeypatch):
    scope = scope_for(tmp_path, [(i, 'pixmap', {}) for i in range(20)])
    rows = CatalogRows(scope, page_size=2).prepare()
    try:
        monkeypatch.setattr(rows, '_start_producer', lambda: None)
        rows._page = Mock(side_effect=AssertionError('GUI called blocking page API'))
        assert rows.try_slice(18,20) is None
        assert rows._wanted >= 9
    finally:
        rows.close()


def test_sparse_replay_pages_do_not_duplicate_or_skip_ids(tmp_path, monkeypatch):
    scope = scope_for(tmp_path, [(i, 'pixmap', {}) for i in range(60)])
    rows = CatalogRows(scope, page_size=2)
    save_page = rows._save_page
    monkeypatch.setattr(rows, '_save_page', lambda number, batch: save_page(number, batch) if number%2 else None)
    rows.prepare()
    try:
        assert [r['id'] for r in rows] == list(range(60))
        rows.bind_evidence(QueryEvidence(), lambda: False)
        assert [r['id'] for r in rows] == list(range(60))
    finally:
        rows.close()


def test_cancelled_install_resumes_from_consumed_checkpoint(main_window, tmp_path, qtbot, monkeypatch):
    scope, scene, browser = prepare(main_window, tmp_path,
        [(i, 'pixmap', dict(canvasId='A', title=str(i), tags=['tag'] if i % 2 else []))
         for i in range(1400)])
    queued = []
    original = browser._queue_catalog_install
    monkeypatch.setattr(browser, '_queue_catalog_install', lambda pending: queued.append(pending))
    browser.open('all')
    qtbot.waitUntil(lambda: bool(queued), timeout=5000)
    prefix = queued[-1]
    assert 0 < prefix['offset'] < 1400
    offset, identities = prefix['offset'], [id(i) for i in prefix['items']]
    key = fingerprint(prefix['requested_key'])
    browser.open('untagged')
    assert key in browser._catalog_checkpoints
    browser.open('all')
    resumed = browser._catalog_pending
    assert resumed is not prefix
    assert resumed['offset'] == offset
    assert [id(i) for i in resumed['items']] == identities
    monkeypatch.setattr(browser, '_queue_catalog_install', original)
    browser._install_catalog_rows(resumed)
    wait_complete(qtbot, browser, 1400)
    assert len({i.save_id for i in browser.model.items}) == 1400


def test_equivalent_page_scope_order_does_not_invalidate_result(main_window, tmp_path, qtbot, monkeypatch):
    scope, scene, browser = prepare(main_window, tmp_path, [
        (1, 'pixmap', dict(canvasId='A')), (2, 'pixmap', dict(canvasId='B'))])
    browser.open('all', scope_pages=['A', 'B'])
    wait_complete(qtbot, browser, 2)
    matching = Mock(wraps=browser._catalog_matches)
    monkeypatch.setattr(browser, '_catalog_matches', matching)
    browser.open('all', scope_pages=['B', 'A'])
    assert browser._catalog_pending is None
    assert matching.call_count == 0
