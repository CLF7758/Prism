import json
import sqlite3
from dataclasses import replace

import pytest
from prism.fileio.partial_save import RowPatch, capture_partial_save, write_partial_snapshot
from prism.project_catalog_index import CatalogBuildCancelled
from prism.project_open_scope import ProjectOpenScope
from tests.test_partial_save import project, rows
from tests.test_partial_project import project as project_with_assets, png


def save(path, key, **updates):
    snapshot = capture_partial_save(path, [RowPatch('items', key, json_updates=(('data', updates),))])
    return write_partial_snapshot(snapshot)


def test_one_row_save_updates_catalog_without_rebuilding(project, tmp_path, monkeypatch):
    scope = ProjectOpenScope(project, tmp_path/'catalog.sqlite')
    scope.build()
    originals = rows(project, 'sqlar')
    receipt = save(project, 2, tags=['changed'], title='needle')
    assert receipt.catalog_item_ids == (2,)
    assert receipt.source_before != receipt.source_after
    monkeypatch.setattr(scope.index, '_build', lambda **kw: pytest.fail('full rebuild'))
    result = scope.index.apply_save_receipts([receipt])
    assert result.complete and result.processed_records == 1
    assert scope.ready
    assert scope.summaries([2])[0]['tags'] == '["changed"]'
    assert [r['id'] for r in scope.iter_query(text='needle')] == [2]
    assert scope.grouped_counts()
    assert rows(project, 'sqlar') == originals
    clean = ProjectOpenScope(project, tmp_path/'fresh-catalog.sqlite')
    clean.build()
    assert scope.grouped_counts() == clean.grouped_counts()
    assert list(scope.iter_query()) == list(clean.iter_query())


def test_contiguous_saves_can_update_together_or_after_first_commit(project, tmp_path):
    scope = ProjectOpenScope(project, tmp_path/'catalog.sqlite')
    scope.build()
    first = save(project, 2, tags=['first'])
    second = save(project, 3, tags=['second'])
    result = scope.index.apply_save_receipts([first, second])
    assert result.processed_records == 2 and scope.ready
    third = save(project, 4, tags=['third'])
    assert scope.index.apply_save_receipts([first, second, third]).processed_records == 1
    assert scope.ready


def test_external_change_is_not_mistaken_for_a_trusted_save(project, tmp_path):
    scope = ProjectOpenScope(project, tmp_path/'catalog.sqlite')
    scope.build()
    with sqlite3.connect(project) as db:
        db.execute('UPDATE items SET x=x+1 WHERE id=9')
    receipt = save(project, 2, tags=['changed'])
    assert scope.index.apply_save_receipts([receipt]) is None
    assert not scope.ready


def test_cancelled_delta_leaves_old_index_rows_intact(project, tmp_path):
    scope = ProjectOpenScope(project, tmp_path/'catalog.sqlite')
    scope.build()
    receipt = save(project, 2, tags=['changed'])
    with sqlite3.connect(scope.cache_path) as db:
        before = db.execute('SELECT * FROM assets ORDER BY id').fetchall()
    with pytest.raises(CatalogBuildCancelled):
        scope.index.apply_save_receipts([receipt], cancel=lambda: True)
    with sqlite3.connect(scope.cache_path) as db:
        assert db.execute('SELECT * FROM assets ORDER BY id').fetchall() == before
    assert scope.index.apply_save_receipts([receipt]).complete


def test_cancellation_after_projection_rolls_back_derived_rows(project, tmp_path, monkeypatch):
    scope = ProjectOpenScope(project, tmp_path/'catalog.sqlite')
    scope.build()
    receipt = save(project, 2, title='cancelled projection')
    tables = ['assets', 'counts', 'counts_by_scope', 'catalog_state']

    def snapshot():
        with sqlite3.connect(scope.cache_path) as db:
            return {table: db.execute(f'SELECT * FROM {table} ORDER BY 1').fetchall() for table in tables}

    before = snapshot()
    cancelled = False
    project_row = scope.index._project_asset

    def cancel_after_projection(row):
        nonlocal cancelled
        value = project_row(row)
        cancelled = True
        return value

    with monkeypatch.context() as patch:
        patch.setattr(scope.index, '_project_asset', cancel_after_projection)
        with pytest.raises(CatalogBuildCancelled):
            scope.index.apply_save_receipts([receipt], cancel=lambda: cancelled)
    assert snapshot() == before
    assert scope.index.apply_save_receipts([receipt]).complete


def test_broken_receipt_chain_is_refused(project, tmp_path):
    scope = ProjectOpenScope(project, tmp_path/'catalog.sqlite')
    scope.build()
    first = save(project, 2, tags=['one'])
    second = save(project, 3, tags=['two'])
    assert scope.index.apply_save_receipts([first, replace(second, source_before={'broken': True})]) is None
    assert not scope.ready


def test_formal_after_save_worker_keeps_labelled_frame_and_updates_delta(
        main_window, project, tmp_path, qtbot, monkeypatch):
    from unittest.mock import Mock
    from prism.project_catalog import ProjectAssetCatalog
    from tests.test_catalog_browser_partial import wait_complete
    scope = ProjectOpenScope(project, tmp_path/'catalog.sqlite')
    scope.build()
    view, browser = main_window.view, main_window.asset_browser
    scene = view.scene
    scene._partial_project_source = str(project)
    scene.workspace_pages = [dict(id='A', kind='canvas', title='A'), dict(id='B', kind='canvas', title='B')]
    view._open_scope = scope
    view.category_panel.catalog = ProjectAssetCatalog(scene, view.category_panel.resource_service,
                                                     scope_provider=lambda: view._open_scope)
    browser.open('all')
    wait_complete(qtbot, browser, 13)
    assert {item['result_id'] for item in browser.model.items if isinstance(item, dict)} == {'page:A', 'page:B'}
    assert sum(not isinstance(item, dict) for item in browser.model.items) == 11
    previous = browser.model.items
    receipt = save(project, 2, title='changed by confirmed save')
    from prism.project_catalog_index import ProjectCatalogIndex
    full = Mock(side_effect=AssertionError('confirmed delta unexpectedly rebuilt everything'))
    monkeypatch.setattr(ProjectCatalogIndex, '_build', full)
    view._refresh_catalog_after_save(str(project), receipt=receipt)
    assert browser.model.items is previous
    assert not browser.list.isEnabled()
    qtbot.waitUntil(lambda: scope.ready and view._catalog_build_active is None
                   and browser._catalog_pending is None and browser.list.isEnabled(), timeout=10000)
    assert full.call_count == 0
    assert next(i for i in browser.model.items if getattr(i, 'save_id', None) == 2)._title == 'changed by confirmed save'


def test_later_save_retains_chain_after_previous_worker_has_retired(
        main_window, project, tmp_path, monkeypatch):
    scope = ProjectOpenScope(project, tmp_path/'catalog.sqlite')
    scope.build()
    view, browser = main_window.view, main_window.asset_browser
    view._open_scope = scope
    monkeypatch.setattr(browser, 'suspend_aggregate_result', lambda: None)
    monkeypatch.setattr(view, '_maybe_start_catalog_build', lambda filename: None)
    first = save(project, 2, tags=['first'])
    view._refresh_catalog_after_save(str(project), receipt=first)
    view._catalog_build_active = None  # An interrupted worker already delivered terminal.
    second = save(project, 3, tags=['second'])
    view._refresh_catalog_after_save(str(project), receipt=second)
    assert view._catalog_to_build['catalog_deltas'] == (first, second)
    assert scope.index.apply_save_receipts(view._catalog_to_build['catalog_deltas']).processed_records == 2


def test_new_asset_id_and_original_bytes_reach_incremental_catalog(project_with_assets, tmp_path, png):
    from tests.test_partial_project import opened, loaded_items, FakeScene, FakeItem, pages_of
    source = project_with_assets
    scope = ProjectOpenScope(source, tmp_path/'catalog.sqlite')
    scope.build()
    before = rows(source, 'sqlar')
    session = opened(source)
    items = loaded_items(source)
    fresh = FakeItem(save_id=None, data={'filename': 'new-original.png', 'tags': ['new']}, source=png)
    scene = FakeScene(items + [fresh], pages_of(source))
    snapshot = session.capture(scene)
    receipt = write_partial_snapshot(snapshot)
    assert receipt.catalog_item_ids == (13,)
    result = scope.index.apply_save_receipts([receipt])
    assert result.processed_records == 1 and result.total_records == 13
    assert scope.ready and scope.summaries([13])[0]['tags'] == '["new"]'
    with sqlite3.connect(source) as db:
        assert db.execute('SELECT data FROM sqlar WHERE item_id=13').fetchone()[0] == png
    assert rows(source, 'sqlar')[:len(before)] == before
