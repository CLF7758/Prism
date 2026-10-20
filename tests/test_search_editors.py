"""Exercise the bundled editors rather than only mocking their callbacks."""
import json
import pytest
from tests.test_unified_project_search import page
from prism.widgets.document_panel import WEBENGINE_AVAILABLE
from prism.widgets.mindmap_panel import content_signature

pytestmark = pytest.mark.skipif(not WEBENGINE_AVAILABLE, reason='QtWebEngine is unavailable')


def javascript(qtbot, web_page, code):
    result = []
    web_page.runJavaScript(code, result.append)
    qtbot.waitUntil(lambda: bool(result), timeout=8000)
    return result[0]


def test_import_into_loaded_mindmap_survives_immediate_flush(main_window, qtbot, monkeypatch):
    tree = {'data': {'text': 'old map', 'uid': 'old-root'}, 'children': []}
    record = page(main_window, 'mindmap', 'Import regression', tree)
    main_window.open_workspace_page('mindmap', record['id'])
    panel = main_window._page_panels[record['id']]
    qtbot.waitUntil(lambda: panel._ready and not getattr(panel, '_tree_loading', True), timeout=15000)
    imported = {'data': {'text': 'imported map', 'uid': 'new-root'}, 'children': [
        {'data': {'text': 'imported child', 'uid': 'new-child'}}]}
    monkeypatch.setattr('prism.view.QtWidgets.QFileDialog.getOpenFileName',
                        lambda **kwargs: ('import.xmind', ''))
    monkeypatch.setattr('prism.fileio.importers.xmind_to_tree', lambda filename: imported)
    main_window.view.on_action_import_mindmap()
    assert panel.store_to_scene() is True
    assert panel._scene_tree()['data']['uid'] == 'new-root'
    qtbot.waitUntil(lambda: not panel._tree_loading, timeout=8000)
    qtbot.wait(350)
    assert panel.store_to_scene() is True
    saved = panel._scene_tree()
    assert saved['data']['uid'] == 'new-root'
    assert saved['children'][0]['data']['uid'] == 'new-child'
    rendered = json.loads(javascript(qtbot, panel.web_view.page(), 'window.__searchSnapshot()'))['tree']
    assert rendered['data']['uid'] == 'new-root'
    assert rendered['children'][0]['data']['text'] == 'imported child'


def test_document_snapshot_and_all_occurrences(main_window, qtbot):
    record = page(main_window, 'document', '正文', '<p>旧字</p>')
    main_window.open_workspace_page('document', record['id'])
    panel = main_window._page_panels[record['id']]
    qtbot.waitUntil(lambda: panel._ready and not panel._pending_push, timeout=15000)
    javascript(qtbot, panel._page,
               'window.__quill.clipboard.dangerouslyPasteHTML("<p>雨<b>夜</b> 雨夜</p><p>红墙</p>"); true')
    controller = main_window.search_controller
    controller.input.setText('雨夜 红墙')
    controller.submit()
    qtbot.waitUntil(lambda: len(controller.results) == 1, timeout=8000)
    hit = controller.results[0]
    body_matches = [m for m in hit['matches'] if m['field'] == 'body']
    assert [(m['text'], m['occurrence']) for m in body_matches] == [('雨夜', 0), ('雨夜', 1), ('红墙', 0)]
    controller.open_result(hit)
    qtbot.waitUntil(lambda: not panel._pending_push, timeout=8000)
    controller.locate_match()
    assert javascript(qtbot, panel._page, 'window.__quill.getSelection()')['index'] == 0
    controller.navigate_match(1)
    assert javascript(qtbot, panel._page, 'window.__quill.getSelection()')['index'] == 3
    controller.navigate_match(1)
    assert javascript(qtbot, panel._page, 'window.__quill.getSelection()')['index'] == 6
    html = panel._html
    controller.return_to_results()
    controller.exit()
    assert panel._html == html


def test_mindmap_location_does_not_persist_temporary_expand(main_window, qtbot):
    tree = {'data': {'text': '根', 'uid': 'root', 'expand': True}, 'children': [
        {'data': {'text': '折叠', 'uid': 'parent', 'expand': False}, 'children': [
            {'data': {'text': '雨夜', 'uid': 'target'}}]}]}
    record = page(main_window, 'mindmap', '脑图', tree)
    main_window.open_workspace_page('mindmap', record['id'])
    panel = main_window._page_panels[record['id']]
    qtbot.waitUntil(lambda: panel._ready and panel._tree is not None, timeout=15000)
    qtbot.waitUntil(lambda: javascript(qtbot, panel.web_view.page(),
                      'Boolean(window.__mindMap && window.__mindMap.renderer.root)'), timeout=15000)
    # Let the initial editor normalization complete before comparing content.
    qtbot.wait(350)
    before = json.loads(javascript(qtbot, panel.web_view.page(), 'window.__searchSnapshot()'))['tree']
    signature = content_signature(before)
    panel.locate_search_result({'uid': 'target', 'path': [0, 0]})
    qtbot.wait(350)
    snapshot = json.loads(javascript(qtbot, panel.web_view.page(), 'window.__searchSnapshot()'))['tree']
    assert snapshot['children'][0]['data']['expand'] is False
    assert content_signature(snapshot) == signature
    panel.end_search_location()
    qtbot.wait(100)
    restored = json.loads(javascript(qtbot, panel.web_view.page(), 'window.__searchSnapshot()'))['tree']
    assert content_signature(restored) == signature


def test_explicit_user_expansion_during_search_is_preserved(main_window, qtbot):
    tree = {'data': {'text': 'Root', 'uid': 'root', 'expand': True}, 'children': [
        {'data': {'text': 'Parent', 'uid': 'parent', 'expand': False}, 'children': [
            {'data': {'text': 'Target', 'uid': 'target'}}]}]}
    record = page(main_window, 'mindmap', 'Explicit expansion', tree)
    main_window.open_workspace_page('mindmap', record['id'])
    panel = main_window._page_panels[record['id']]
    qtbot.waitUntil(lambda: panel._ready, timeout=15000)
    qtbot.waitUntil(lambda: javascript(qtbot, panel.web_view.page(),
        'Boolean(window.__mindMap && window.__mindMap.renderer.root)'), timeout=15000)
    panel.locate_search_result({'uid': 'target', 'path': [0, 0]})
    qtbot.wait(350)
    assert javascript(qtbot, panel.web_view.page(),
        "window.__mindMap.execCommand('SET_NODE_EXPAND', window.__mindMap.renderer.root.children[0], true); true")
    panel.end_search_location()
    qtbot.wait(150)
    snapshot = json.loads(javascript(qtbot, panel.web_view.page(), 'window.__searchSnapshot()'))['tree']
    assert snapshot['children'][0]['data']['expand'] is True
    panel.store_to_scene()
    assert record['content']['children'][0]['data']['expand'] is True

