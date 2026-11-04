"""Late page-read failures must terminate a query, not the Qt application."""
from prism.project_catalog_index import CatalogNotReady
from tests.test_catalog_browser_partial import prepare


def records():
    return [(key, 'pixmap', dict(canvasId='A', title=f'Asset {key}',
                               imageWidth=100, imageHeight=50)) for key in range(1,1201)]


def test_index_change_after_first_page_recovers_without_uncaught_qt_error(
        main_window, tmp_path, qtbot, monkeypatch):
    scope, scene, browser = prepare(main_window, tmp_path, records())
    real_query, failed = scope.query, []
    def query(**kwargs):
        if kwargs.get('after_id') and not failed:
            failed.append(kwargs['after_id'])
            raise CatalogNotReady('source changed between pages')
        return real_query(**kwargs)
    monkeypatch.setattr(scope, 'query', query)
    browser.open('all')
    qtbot.waitUntil(lambda: bool(failed) and browser._catalog_pending is None
                   and browser.model.presentation_evidence is not None
                   and browser.model.presentation_evidence.final
                   and len([item for item in browser.model.items
                            if getattr(item, '_catalog_proxy', False)]) == 1200, timeout=15000)
    assert browser.list.isEnabled()
    assert {item.save_id for item in browser.model.items if getattr(item, '_catalog_proxy', False)} == set(range(1,1201))
    assert not list(scene.iter_assets())  # query failure never materializes data


def test_disk_failure_after_first_page_shows_failure_without_retry_loop(
        main_window, tmp_path, qtbot, monkeypatch):
    scope, scene, browser = prepare(main_window, tmp_path, records())
    real_query, failures = scope.query, []
    def query(**kwargs):
        if kwargs.get('after_id'):
            failures.append(kwargs['after_id'])
            raise OSError('read failed')
        return real_query(**kwargs)
    monkeypatch.setattr(scope, 'query', query)
    browser.open('all')
    qtbot.waitUntil(lambda: bool(failures) and browser._catalog_pending is None
                   and 'read failed' in browser.summary.text(), timeout=15000)
    assert browser.model.items == []
    assert browser.list.isEnabled()
    qtbot.wait(200)
    assert len(failures) == 1


def test_late_error_of_retired_query_cannot_clear_current_results(main_window):
    browser = main_window.asset_browser
    before = browser.model.items
    browser._catalog_install_failed({'evidence': None}, CatalogNotReady('old query'))
    assert browser.model.items is before


def test_model_commit_does_not_requery_hidden_asset_list(main_window, monkeypatch):
    from unittest.mock import Mock
    from PyQt6.QtGui import QShowEvent
    browser = main_window.asset_browser
    assert not browser.isVisible()
    refresh = Mock()
    monkeypatch.setattr(browser, 'refresh', refresh)
    browser.invalidate_aggregate_result('model_metadata_committed')
    refresh.assert_not_called()
    browser.showEvent(QShowEvent())
    refresh.assert_called_once()
