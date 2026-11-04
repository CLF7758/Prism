from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from prism.aggregate_save_delta import changed_ids_since
from prism.project_open_scope import ProjectOpenScope
from prism.project_catalog import ProjectAssetCatalog
from tests.test_partial_save import project, rows
from tests.test_catalog_incremental_receipts import save
from tests.test_catalog_browser_partial import wait_complete
from tests.test_asset_browser import photo


def receipt(before, after, ids):
    return SimpleNamespace(source_before=before, source_after=after, catalog_item_ids=ids)


def test_chain_requires_exact_continuity_and_bounded_delta():
    a,b,c,d = ({'file': [i]} for i in range(4))
    chain=[receipt(a,b,(1,)),receipt(b,c,(2,)),receipt(c,d,(3,))]
    assert changed_ids_since(b,d,chain)==(2,3)
    assert changed_ids_since(a,d,[chain[0],chain[2]]) is None
    assert changed_ids_since(a,c,chain) is None
    assert changed_ids_since(a,d,chain,limit=2) is None
    assert changed_ids_since(None,d,chain) is None


def prepare(window, project, tmp_path, qtbot, monkeypatch):
    scope = ProjectOpenScope(project,tmp_path/'delta-index.sqlite')
    scope.build()
    view,scene,browser = window.view,window.view.scene,window.asset_browser
    scene._partial_project_source=str(project)
    scene.workspace_pages=[dict(id=k,kind='canvas',title=k) for k in ('A','B')]
    view._open_scope=scope
    panel=view.category_panel
    panel.catalog=ProjectAssetCatalog(scene,panel.resource_service,scope_provider=lambda:scope)
    item=photo(window,'A','current')
    item.save_id=2
    monkeypatch.setattr(scene,'_partial_project_session',SimpleNamespace(
        loaded=lambda:(2,),retained_item=lambda key:None),raising=False)
    panel.set_search_kind('asset')
    browser.open('all')
    wait_complete(qtbot,browser,11)
    assert item in browser.model.items
    return scope,view,browser,item


def test_confirmed_save_retains_model_and_unchanged_proxies(
        main_window,project,tmp_path,qtbot,monkeypatch):
    scope,view,browser,item=prepare(main_window,project,tmp_path,qtbot,monkeypatch)
    before=browser.model.items
    unchanged=next(i for i in before if i.save_id==3)
    original_bytes=rows(project,'sqlar')
    saved=save(project,2,title='current')
    monkeypatch.setattr(scope,'iter_query',Mock(side_effect=AssertionError('full catalog read after save')))
    reset=Mock()
    browser.model.modelReset.connect(reset)
    view._refresh_catalog_after_save(str(project),receipt=saved)
    qtbot.waitUntil(lambda:view._catalog_build_active is None and scope.ready
                   and browser.list.isEnabled(),timeout=10000)
    assert browser.model.items is before
    assert next(i for i in before if i.save_id==3) is unchanged
    reset.assert_not_called()
    assert rows(project,'sqlar')==original_bytes
    assert browser.model.presentation_evidence.final
    browser.open('all')
    assert browser._catalog_pending is None
    assert browser.model.items is before
    second=save(project,2,notes='second save')
    view._refresh_catalog_after_save(str(project),receipt=second)
    qtbot.waitUntil(lambda:view._catalog_build_active is None and scope.ready
                   and browser.list.isEnabled(),timeout=10000)
    assert browser.model.items is before
    reset.assert_not_called()


def test_deletion_already_filtered_by_live_overlay_keeps_remaining_model(
        main_window,project,tmp_path,qtbot,monkeypatch):
    scope,view,browser,item=prepare(main_window,project,tmp_path,qtbot,monkeypatch)
    item._deleted_at='saved deletion'
    view.scene.metadata_changed.emit()  # Normal delete commands publish invalidation.
    browser.open('all')
    wait_complete(qtbot,browser,10)
    previous=browser.model.items
    saved=save(project,2,deletedAt='saved deletion')
    reset=Mock()
    browser.model.modelReset.connect(reset)
    view._refresh_catalog_after_save(str(project),receipt=saved)
    qtbot.waitUntil(lambda:view._catalog_build_active is None and scope.ready
                   and browser.list.isEnabled(),timeout=10000)
    assert browser.model.items is previous
    assert all(i.save_id!=2 for i in previous)
    reset.assert_not_called()


def test_synchronous_scope_change_during_data_notification_is_not_overwritten(
        main_window,project,tmp_path,qtbot,monkeypatch):
    scope,view,browser,item=prepare(main_window,project,tmp_path,qtbot,monkeypatch)
    saved=save(project,2,title='current')
    browser.suspend_aggregate_result()
    assert scope.index.apply_save_receipts([saved]).complete
    def navigate(*args):
        browser.mode='untagged'
        browser.breadcrumb.setText('new destination pending')
    browser.model.dataChanged.connect(navigate)
    assert browser.resume_saved_projection(dict(incremental_applied=True,
        catalog_deltas=(saved,),delta_summaries=scope.summaries([2])))
    assert browser.breadcrumb.text()=='new destination pending'


@pytest.mark.parametrize('mutation',['new_edit','broken_chain','deleted','new_source_row','filter',
                                     'metadata_changed','full_rebuild','missing_summary','moved'])
def test_unsafe_delta_declines_before_changing_model(
        main_window,project,tmp_path,qtbot,monkeypatch,mutation):
    scope,view,browser,item=prepare(main_window,project,tmp_path,qtbot,monkeypatch)
    before=browser.model.items
    saved=save(project,2,title='current')
    browser.suspend_aggregate_result()
    assert scope.index.apply_save_receipts([saved]).complete
    request=dict(incremental_applied=True,catalog_deltas=(saved,),
                 delta_summaries=scope.summaries([2]))
    if mutation=='new_edit':
        view.scene._asset_revision=getattr(view.scene,'_asset_revision',0)+1
    elif mutation=='broken_chain':
        request['catalog_deltas']=(replace(saved,source_before={'wrong':True}),)
    elif mutation=='deleted':
        item._deleted_at='now'
    elif mutation=='new_source_row':
        request['catalog_deltas']=(replace(saved,catalog_item_ids=(3,)),)
        request['delta_summaries']=scope.summaries([3])
    elif mutation=='filter':
        browser.saved_rule={'tag_expression':'keep'}
    elif mutation=='metadata_changed':
        browser._catalog_metadata_epoch+=1
    elif mutation=='full_rebuild':
        request['incremental_applied']=False
    elif mutation=='missing_summary':
        request['delta_summaries']=None
    else:
        item._canvas_id='B'
    assert browser.resume_saved_projection(request) is False
    assert browser.model.items is before


@pytest.mark.parametrize('mode,sort_key,rule', [
    ('untagged', 'default', None),
    ('unclassified', 'name', None),
    ('all', 'rating', {'text': 'current'}),
])
def test_stable_filtered_and_sorted_save_avoids_full_query(
        main_window, project, tmp_path, qtbot, monkeypatch, mode, sort_key, rule):
    scope, view, browser, item = prepare(main_window, project, tmp_path, qtbot, monkeypatch)
    if mode == 'unclassified':
        item._canvas_id = 'default-canvas'
        view.scene.metadata_changed.emit()
    browser.sort_key = sort_key
    browser.saved_rule = rule
    browser.open(mode)
    qtbot.waitUntil(lambda: browser._catalog_pending is None and browser.list._build_state is None
                   and item in browser.model.items, timeout=10000)
    previous = browser.model.items
    saved = save(project, 2, title='current', canvasId=item._canvas_id)
    monkeypatch.setattr(scope, 'iter_query', Mock(side_effect=AssertionError('unexpected full read')))
    view._refresh_catalog_after_save(str(project), receipt=saved)
    qtbot.waitUntil(lambda: view._catalog_build_active is None and scope.ready
                   and browser.list.isEnabled(), timeout=10000)
    assert browser.model.items is previous
    assert item in previous


def test_filter_changed_before_save_capture_cannot_bless_old_model(
        main_window, project, tmp_path, qtbot, monkeypatch):
    scope, view, browser, item = prepare(main_window, project, tmp_path, qtbot, monkeypatch)
    browser.saved_rule = {'text': 'different query not installed'}
    browser.suspend_aggregate_result()
    assert browser._save_display_snapshot is None


def test_saved_nonmember_does_not_reload_untagged_projection(
        main_window, project, tmp_path, qtbot, monkeypatch):
    scope, view, browser, item = prepare(main_window, project, tmp_path, qtbot, monkeypatch)
    item._tags = ['keep']
    view.scene.metadata_changed.emit()
    browser.open('untagged')
    qtbot.waitUntil(lambda: browser._catalog_pending is None and browser.list._build_state is None
                   and browser.model.items == [], timeout=10000)
    previous = browser.model.items
    saved = save(project, 2, title='current')
    monkeypatch.setattr(scope, 'iter_query', Mock(side_effect=AssertionError('unexpected full query')))
    view._refresh_catalog_after_save(str(project), receipt=saved)
    qtbot.waitUntil(lambda: view._catalog_build_active is None and scope.ready
                   and browser.list.isEnabled(), timeout=10000)
    assert browser.model.items is previous
