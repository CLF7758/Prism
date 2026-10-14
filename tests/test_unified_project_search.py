"""One project query covers content, not just the current browser's assets."""
from types import SimpleNamespace
from unittest.mock import patch

from PyQt6 import QtCore

from prism.project_search import search_project
from tests.test_asset_browser import photo


def page(window, kind, title, content=None):
    record = window.workspace_service.add(kind, title)
    record['content'] = content
    window.view.category_panel.rebuild_workspace_tree()
    return record


def query(window, text, panels=()):
    library = window.view.category_panel
    return search_project(library.scene, library.resource_service, text, panels,
                          page_records=library._pages_with_implicit_defaults())


def test_document_matches_are_aggregated_with_actual_terms(main_window):
    record = page(main_window, 'document', '雨夜记录',
                  '<p>雨<b>夜</b>街道</p><p>红墙与雨夜</p>')
    hits = [r for r in query(main_window, '雨夜 红墙')
            if r.get('page_id') == record['id']]
    assert len(hits) == 1
    assert hits[0]['match_text'] == '雨夜'
    assert {'雨夜', '红墙'} <= {m['text'] for m in hits[0]['matches']}
    assert hits[0]['result_id'] == 'page:' + record['id']


def test_mindmap_words_across_legacy_nodes_match_one_page(main_window):
    record = page(main_window, 'mindmap', '故事', {
        'topic': '根', 'nodes': [
            {'data': {'uid': 'rain', 'text': '雨夜'}},
            {'data': {'uid': 'wall', 'text': '红墙', 'note': '会面'}}]})
    hits = [r for r in query(main_window, '雨夜 红墙')
            if r.get('page_id') == record['id']]
    assert len(hits) == 1
    assert hits[0]['kind'] == 'mindmap_node'
    assert {m['node_uid'] for m in hits[0]['matches']} == {'rain', 'wall'}


def test_canvas_content_and_asset_metadata_share_the_index(main_window):
    record = page(main_window, 'canvas', '参考')
    item = photo(main_window, record['id'], '照片')
    item._notes = '雨夜'
    item._categories = ['场景/红墙']
    hits = query(main_window, '雨夜 红墙')
    assert any(r['kind'] == 'asset' and r.get('item') is item for r in hits)
    canvas = next(r for r in hits if r['kind'] == 'canvas')
    assert canvas['page_id'] == record['id']
    assert canvas['matches'][0]['item_id']
    assert [r.item for r in main_window.view.category_panel.catalog.query(
        text='雨夜 红墙')] == [item]


def test_live_empty_and_unsaved_mindmap_replace_saved_content(main_window):
    record = page(main_window, 'mindmap', '记录', {'topic': '旧字'})
    live = SimpleNamespace(page_id=record['id'], _tree={
        'data': {'uid': 'new', 'text': '新字'}})
    assert any(r.get('page_id') == record['id']
               for r in query(main_window, '新字', [live]))
    assert not query(main_window, '旧字', [live])
    live._tree = {}
    assert not query(main_window, '新字', [live])


def test_page_title_and_body_only_produce_one_document_result(main_window):
    record = page(main_window, 'document', '共同词', '<p>共同词 共同词</p>')
    assert len([r for r in query(main_window, '共同词')
                if r.get('page_id') == record['id']]) == 1


def test_query_does_not_edit_items_or_undo_history(main_window):
    record = page(main_window, 'canvas', '工作')
    item = photo(main_window, record['id'], '雨夜')
    item.setPos(120, 340)
    before = (item.pos(), item.isSelected(), item.isVisible(), item.grayscale,
              main_window.view.undo_stack.count(), main_window.view.is_dirty())
    assert query(main_window, '雨夜')
    assert before == (item.pos(), item.isSelected(), item.isVisible(), item.grayscale,
                      main_window.view.undo_stack.count(), main_window.view.is_dirty())


def test_browser_and_sidebar_query_the_same_document_body(main_window):
    record = page(main_window, 'document', '分镜', '<p>正文独有词</p>')
    browser = main_window.asset_browser
    browser.open('all')
    browser.search.setText('正文独有词')
    browser.refresh()
    hit = next((r for r in browser.model.items
                if isinstance(r, dict) and r.get('page_id') == record['id']), None)
    assert hit is not None
    library = main_window.view.category_panel
    assert {r['result_id'] for r in browser.search_hits} == {
        r['result_id'] for r in library.project_search_results('正文独有词')
        if r['kind'] not in ('folder', 'tag')}
    with patch.object(main_window, 'open_workspace_page') as opened:
        browser.locate(browser.model.index(browser.model.items.index(hit)))
    opened.assert_called_once_with('document', record['id'])


def test_removed_page_and_its_assets_are_excluded(main_window):
    record = page(main_window, 'canvas', '待删除词')
    item = photo(main_window, record['id'], '待删除词')
    library = main_window.view.category_panel
    node = next(n for n in library.resource_service.nodes
                if n.get('pageId') == record['id'])
    library.resource_service.trash([node['id']], '2026-10-12T00:00:00Z')
    assert not query(main_window, '待删除词')
    assert item.scene() is main_window.view.scene


def test_type_dropdown_filters_same_keyword_and_syncs_both_surfaces(main_window):
    doc = page(main_window, 'document', '文档', '<p>共享词</p>')
    mind = page(main_window, 'mindmap', '脑图', {'topic': '共享词'})
    canvas = page(main_window, 'canvas', '共享词画布')
    photo(main_window, canvas['id'], '共享词素材')
    browser = main_window.asset_browser
    library = main_window.view.category_panel
    browser.open('all')
    browser.search.setText('共享词')
    for kind, target in [('document', doc), ('mindmap', mind), ('canvas', canvas)]:
        browser.search_type.setCurrentIndex(browser.search_type.findData(kind))
        browser.refresh()
        assert library.search_type.currentData() == kind
        assert len(browser.model.items) == 1
        assert browser.model.items[0]['page_id'] == target['id']
    library.search_type.setCurrentIndex(library.search_type.findData('asset'))
    assert browser.search_type.currentData() == 'asset'
    assert len(browser.model.items) == 1
    assert not isinstance(browser.model.items[0], dict)


def test_clear_query_keeps_selected_document_type(main_window):
    record = page(main_window, 'document', '记录', '<p>清空词</p>')
    browser = main_window.asset_browser
    browser.open('all')
    browser.search_type.setCurrentIndex(browser.search_type.findData('document'))
    browser.search.setText('清空词')
    browser.refresh()
    browser.search.clear()
    browser.refresh()
    assert browser.model.items
    assert all(isinstance(r, dict) and r['page_kind'] == 'document' for r in browser.model.items)
    assert any(r['page_id'] == record['id'] for r in browser.model.items)


def test_page_card_selection_and_render_do_not_modify_source(main_window):
    record = page(main_window, 'document', '记录', '<p>卡片词</p>')
    browser = main_window.asset_browser
    browser.open('all')
    browser.search.setText('卡片词')
    browser.refresh()
    before = main_window.view.undo_stack.count()
    browser.list.setCurrentIndex(browser.model.index(0))
    browser.select_assets()
    browser.grab()
    assert main_window.view.undo_stack.count() == before
    assert record['content'] == '<p>卡片词</p>'


def test_sidebar_type_is_respected_in_existing_all_results_mode(main_window):
    record = page(main_window, 'document', '文件', '<p>侧栏词</p>')
    library = main_window.view.category_panel
    library._enter_result_mode('all')
    library.search_type.setCurrentIndex(library.search_type.findData('document'))
    library.search.setText('侧栏词')
    library._on_search_timer()
    assert library.search_results.topLevelItemCount() == 1
    hit = library.search_results.topLevelItem(0).child(0).data(0, QtCore.Qt.ItemDataRole.UserRole)
    assert hit['page_id'] == record['id']
