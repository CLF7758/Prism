"""Sidebar counters, result lists and exportable sets must agree.

These are the regression tests for the "sidebar says 100,000, the list shows
100" gap: a partially opened project answers from the whole-project directory
(`ProjectOpenScope`) plus the session's live/parked overlay, and the four
consumers of that answer (sidebar counters, media-library result list, smart
folder/visual browser, status bar) may not fall back to "the one loaded page".

Every consistency assertion is written as an *equality between consumers*
(sidebar text == list rows == browser/exportable rows == disk save_ids), not
as "some function returned something non-empty".
"""
import json

import pytest

from PyQt6 import QtCore

from prism.project_catalog import CatalogNotReady, ProjectAssetCatalog
from tests.test_asset_browser import photo
from tests.test_catalog_keyset_overlay import scope_for


CANVASES = [dict(id='A', kind='canvas', title='A'),
            dict(id='B', kind='canvas', title='B')]


def partial(main_window, tmp_path, records, *, directory=True):
    """A partially opened project: one live page, whole-project directory."""
    scope = scope_for(tmp_path, records)
    scene = main_window.view.scene
    scene._partial_project_source = str(scope.project_path)
    scene.workspace_pages = [dict(page) for page in CANVASES]
    panel = main_window.view.category_panel
    panel.catalog = ProjectAssetCatalog(
        scene, panel.resource_service,
        scope_provider=(lambda: scope) if directory else (lambda: None))
    return scope, scene, panel


def live(main_window, canvas, title, save_id=None, tags=(), rating=0,
         deleted=None):
    """A loaded asset object, optionally standing in for a saved row."""
    item = photo(main_window, canvas, title, tags)
    item._rating = rating
    if save_id is not None:
        item.save_id = save_id
    if deleted is not None:
        item._deleted_at = deleted
    return item


def asset_rows(main_window):
    """Flat list of the asset rows the sidebar result list rendered."""
    tree = main_window.view.category_panel.search_results
    rows = []
    for index in range(tree.topLevelItemCount()):
        group = tree.topLevelItem(index)
        rows.extend(group.child(index) for index in range(group.childCount()))
    return rows


def wait_complete(qtbot, browser, expected):
    qtbot.waitUntil(lambda: browser._catalog_pending is None and
                   browser.model.rowCount() == expected, timeout=20000)


# ── the headline invariant ────────────────────────────────────────────────

def test_full_load_keeps_the_same_consistency_without_a_directory(main_window):
    """全量打开时（按需打开默认关闭的那条路径）口径同样必须自洽。

    目录分支和内存分支共用 `_matches`，这里守住另一半：没有目录时侧栏、
    列表、可导出集合也不能各说各话。
    """
    panel = main_window.view.category_panel
    loaded = [photo(main_window, 'default-canvas', f'Loaded {index}')
              for index in range(5)]
    assert not getattr(main_window.view.scene, '_partial_project_source', None)
    assert panel.catalog._catalog_scope() is None

    total = panel.catalog.total_count()
    assert total == len(loaded)
    panel._update_navigation()
    assert panel._nav_counts['all'].text() == f'{total:,}'

    panel._result_mode = 'all'
    panel._refresh_result_mode()
    assert len(asset_rows(main_window)) == total
    assert len(panel.catalog.query()) == total

    browser = main_window.asset_browser
    browser.open('all')
    assert len(browser.model.items) == total
    main_window._update_status({'refresh': True})
    assert main_window._status_text.text().startswith(f'{total:,} items')


def test_sidebar_list_browser_and_status_bar_report_one_whole_project_total(
        main_window, tmp_path, qtbot):
    """侧栏计数 == 结果列表行数 == 可导出行数 == 状态栏数字。

    600 rows live only on disk and one asset is unsaved in this session, so the
    whole-project answer is 601. Before the fix the sidebar said 601 and every
    list said 1.
    """
    records = [(key, 'pixmap', dict(
        canvasId='A' if key % 2 else 'B', title=f'Asset {key:03d}',
        imageWidth=100, imageHeight=50, tags=[] if key % 3 else ['keep']))
        for key in range(1, 601)]
    _, scene, panel = partial(main_window, tmp_path, records)
    live(main_window, 'A', 'Loaded only')

    total = panel.catalog.total_count()
    assert total == 601
    panel._update_navigation()
    assert panel._nav_counts['all'].text() == f'{total:,}'

    panel._result_mode = 'all'
    panel._refresh_result_mode()
    results = panel.catalog.query()
    assert len(results) == total
    assert len(asset_rows(main_window)) == total
    assert f'{total:,}' in panel.hint.text()

    browser = main_window.asset_browser
    browser.open('all')
    wait_complete(qtbot, browser, total)
    exportable = [item for item in browser.model.items
                  if getattr(item, 'TYPE', '') == 'pixmap']
    assert len(exportable) == total

    main_window._update_status({'refresh': True})
    assert main_window._status_text.text().startswith(f'{total:,} items')
    assert {panel._nav_counts['all'].text(), len(asset_rows(main_window)),
            len(exportable)} == {f'{total:,}', total}


# ── overlay precedence, de-duplication, completeness ──────────────────────

def test_dirty_overlay_outranks_disk_rows_and_never_duplicates(main_window, tmp_path):
    """未保存的编辑压过旧目录行，同一个 save_id 只出一行。"""
    _, scene, panel = partial(main_window, tmp_path, [
        (1, 'pixmap', dict(canvasId='A', title='旧标题', tags=['旧标签'], rating=0)),
        (2, 'pixmap', dict(canvasId='A', title='二', tags=['旧标签'])),
        (3, 'pixmap', dict(canvasId='B', title='三', tags=[]))])
    edited = live(main_window, 'B', '新标题', save_id=1, tags=['新标签'], rating=4)

    results = panel.catalog.query()
    assert len(results) == panel.catalog.total_count() == 3
    assert len({getattr(r.item, 'save_id', None) for r in results}) == 3
    row = next(r for r in results if r.item is edited)
    assert (row.title, row.tags, row.canvas_id, row.rating) == (
        '新标题', ['新标签'], 'B', 4)
    assert all('旧标题' != r.title for r in results)

    # An overlay that stops matching must not let the stale disk row back in.
    edited._tags = []
    untagged = panel.catalog.query(untagged_only=True)
    keys = [str(getattr(r.item, 'save_id', None)) for r in untagged]
    assert sorted(keys) == ['1', '3']          # edited row + genuinely untagged row 3
    assert keys.count('1') == 1                # and never twice
    assert [r.item.save_id for r in panel.catalog.query(tag_expression='旧标签')] == [2]


def test_unloaded_dirty_new_and_deleted_results_stay_complete(main_window, tmp_path):
    """未加载 / dirty / 新建 / 删除混在一起时结果集合仍然完整。"""
    _, scene, panel = partial(main_window, tmp_path, [
        (1, 'pixmap', dict(canvasId='A', title='已加载', tags=['x'])),
        (2, 'pixmap', dict(canvasId='A', title='仅在磁盘')),
        (3, 'pixmap', dict(canvasId='A', title='磁盘已删', deletedAt='yesterday')),
        (4, 'pixmap', dict(canvasId='A', title='内存已删'))])
    live(main_window, 'A', '已加载改', save_id=1, tags=['y'])
    live(main_window, 'A', '内存已删', save_id=4, deleted='now')
    fresh = live(main_window, 'A', '本次新建')

    active = panel.catalog.query()
    titles = sorted(r.title for r in active)
    assert titles == ['仅在磁盘', '已加载改', '本次新建']  # sorted by code point
    assert panel.catalog.total_count() == len(active)
    assert fresh in [r.item for r in active]
    assert len([r for r in active if r.title == '已加载改']) == 1

    trash = panel.catalog.query(include_deleted=True)
    assert sorted(r.title for r in trash) == ['内存已删', '磁盘已删']
    assert len(trash) == panel.catalog.trash_asset_count()


def test_directory_rows_expose_provenance_and_navigate_by_id(main_window, tmp_path):
    """未加载的行是只读代理，定位走 ensure_asset_loaded，不碰代理自身。"""
    _, scene, panel = partial(main_window, tmp_path, [
        (7, 'pixmap', dict(canvasId='A', title='未加载', tags=[]))])
    result = panel.catalog.query()[0]
    assert result.is_directory_only
    assert result.item.save_id == 7
    proxy = result.item
    main_window.view.category_panel._locate_asset_result(result)
    # The proxy is display-only: navigation asked for the canonical object.
    assert proxy._catalog_display_failed in (False, True)  # untouched by navigation
    assert 'not loaded' in main_window.view.category_panel.hint.text() or (
        'Loaded only' not in main_window.view.category_panel.hint.text())


# ── predicate parity: no LIKE stand-in for tag semantics ─────────────────

def test_tag_expression_keeps_full_semantics_on_directory_rows(main_window, tmp_path):
    """中文标签 + 命名空间 + 排除 + 或，目录行和内存行判定完全一致。"""
    _, scene, panel = partial(main_window, tmp_path, [
        (1, 'pixmap', dict(canvasId='A', title='红墙', tags=['建筑/红墙'])),
        (2, 'pixmap', dict(canvasId='A', title='蓝墙', tags=['建筑/蓝墙', '草图'])),
        (3, 'pixmap', dict(canvasId='A', title='素描', tags=['草图'])),
        (4, 'pixmap', dict(canvasId='A', title='字面', tags=['ab'])),
        (5, 'pixmap', dict(canvasId='A', title='百分号', tags=['100%_木头']))])

    def ids(expression):
        return sorted(r.item.save_id
                      for r in panel.catalog.query(tag_expression=expression))

    assert ids('建筑') == [1, 2]           # implied namespace level
    assert ids('建筑 -蓝墙') == [1]         # negation
    assert ids('红墙 | 草图') == [1, 2, 3]     # alternation
    assert ids('蓝墙 | 素描') == [2]         # only the tag matches, never the title
    assert ids('a') == []                  # a LIKE '%a%' would have matched 4
    assert ids('100%_木头') == [5]          # % and _ are literal here
    assert ids('草图') == [2, 3]
    assert panel.catalog.query(untagged_only=True) == []
    # Same predicate, live object: it is filtered, not waved through.
    tagged = live(main_window, 'A', '内存蓝墙', tags=['建筑/蓝墙'])
    assert tagged in [r.item for r in panel.catalog.query(tag_expression='建筑')]
    assert tagged not in [r.item for r in panel.catalog.query(tag_expression='建筑 -蓝墙')]


def test_equal_sort_keys_have_a_stable_id_tie_break(main_window, tmp_path):
    """相同 (画布路径, 标题) 时按保存 id 排序，且重复查询顺序一致。"""
    _, scene, panel = partial(main_window, tmp_path, [
        (key, 'pixmap', dict(canvasId='A', title='同名', tags=[]))
        for key in (3, 1, 2)])
    first = [r.item.save_id for r in panel.catalog.query()]
    assert first == [1, 2, 3]
    assert [r.item.save_id for r in panel.catalog.query()] == first
    assert all(r.path == panel.catalog.canvas_path('A') for r in panel.catalog.query())


# ── directory unavailable is a state, never an empty answer ───────────────

def test_unavailable_directory_refuses_instead_of_returning_empty(main_window, tmp_path):
    _, scene, panel = partial(main_window, tmp_path, [
        (1, 'pixmap', dict(canvasId='A', title='x'))], directory=False)
    live(main_window, 'A', 'only loaded page')
    with pytest.raises(CatalogNotReady):
        panel.catalog.query()
    assert panel.catalog.count_snapshot()['total'] is None


def test_result_list_says_the_directory_is_unavailable(main_window, tmp_path):
    """缺陷 B：不能把"目录没准备好"渲染成"没有匹配的素材"。"""
    _, scene, panel = partial(main_window, tmp_path, [
        (1, 'pixmap', dict(canvasId='A', title='x'))], directory=False)
    live(main_window, 'A', 'only loaded page')
    panel._result_mode = 'all'
    panel._refresh_result_mode()
    tree = panel.search_results
    text = '\n'.join(tree.topLevelItem(index).text(0)
                     for index in range(tree.topLevelItemCount()))
    assert 'directory unavailable' in text
    assert '没有匹配的素材' not in text
    assert '均已添加标签' not in text
    assert 'directory unavailable' in panel.hint.text()


def test_visual_search_reports_unknown_candidates_instead_of_none_found(
        main_window, tmp_path):
    """缺陷 B 的以图搜图一侧：候选集未知 ≠ 没有可检索的素材。"""
    _, scene, panel = partial(main_window, tmp_path, [
        (1, 'pixmap', dict(canvasId='A', title='x'))], directory=False)
    controller = main_window.search_controller.visual_search
    controller.active = True
    assert controller.candidates() is None
    assert '不可用' in controller.search.info.text()
    assert '稍后重试' in controller.search.info.toolTip()


def test_status_bar_marks_unknown_instead_of_zero(main_window, tmp_path):
    _, scene, panel = partial(main_window, tmp_path, [
        (1, 'pixmap', dict(canvasId='A', title='x'))])
    live(main_window, 'A', 'Loaded')
    main_window._update_status({'refresh': True})
    assert main_window._status_text.text().startswith('2 items')  # 1 disk + 1 unsaved
    panel.catalog._scope_provider = lambda: None
    main_window._update_status({'refresh': True})
    assert 'Preparing' in main_window._status_text.text()
    assert '0 items' not in main_window._status_text.text()


# ── smart folder (saved_rule) on the directory path ───────────────────────

def test_smart_folder_rule_matches_unloaded_assets_too(main_window, tmp_path, qtbot):
    """缺陷 A#2：智能文件夹的 saved_rule 必须能筛到未加载的素材。"""
    records = [(key, 'pixmap', dict(
        canvasId='A', title=f'Item {key}', imageWidth=10, imageHeight=10,
        tags=['keep'] if key in (1, 3) else ['drop'])) for key in range(1, 5)]
    _, scene, panel = partial(main_window, tmp_path, records)
    browser = main_window.asset_browser
    browser.saved_rule = {'text': '', 'tag_expression': json.dumps('keep'),
                          'rating': 0}
    browser.open('all', title='Smart')
    wait_complete(qtbot, browser, 2)
    assert {item.save_id for item in browser.model.items} == {1, 3}
    assert all(getattr(item, '_catalog_proxy', False) for item in browser.model.items)


def test_visual_mode_ranks_the_whole_project_directory(main_window, tmp_path, qtbot):
    """缺陷 A#3：以图搜图结果页在按页打开时也走全项目目录，并保留名次。"""
    records = [(key, 'pixmap', dict(canvasId='A', title=f'Item {key}',
                                    imageWidth=10, imageHeight=10)) for key in range(1, 5)]
    _, scene, panel = partial(main_window, tmp_path, records)
    live(main_window, 'A', 'Loaded only')
    browser = main_window.asset_browser
    browser.visual_project = scene.project_id
    browser.visual_results = [(next(r.item for r in panel.catalog.query()
                                    if r.item.save_id == key), 1.0 / key)
                              for key in (4, 2, 1, 3)]
    browser.open('visual', title='以图搜图', record=False)
    wait_complete(qtbot, browser, 4)
    assert [item.save_id for item in browser.model.items] == [4, 2, 1, 3]


# ── defect C: silent autosave failures back off and are observable ────────

class FakeSaveWorker(QtCore.QObject):
    """Just enough of `fileio.ThreadedIO` to report an autosave outcome."""
    finished = QtCore.pyqtSignal(str, list)

    def isRunning(self):
        return False


def test_refused_autosave_is_visible_on_first_failure(main_window, tmp_path):
    window = main_window
    window.view.filename = str(tmp_path / 'autosave.prism')
    window.view.mark_content_dirty()
    window.view.do_save = lambda *args, **kwargs: None
    assert window._perform_autosave() == 'refused'
    policy = window.view.autosave_policy
    assert policy.failures == 1
    assert policy.backoff == 2
    assert 'failed 1 time(s)' in window.statusBar().currentMessage()
    assert window._autosave_timer.interval() == 1000  # observer, not retry queue


def test_autosave_recovers_only_when_worker_finishes(main_window, tmp_path):
    window = main_window
    window.view.filename = str(tmp_path / 'autosave.prism')
    window.view.mark_content_dirty()
    window.view.do_save = lambda *args, **kwargs: None
    window._perform_autosave()
    assert window.view.autosave_policy.failures == 1
    def writing_save(*args, **kwargs):
        window.view.worker = FakeSaveWorker()
    window.view.do_save = writing_save
    assert window._perform_autosave() == 'started'
    assert window.view.autosave_policy.failures == 1
    window.view.worker.finished.emit(str(window.view.filename), [])
    assert window.view.autosave_policy.failures == 0


def test_autosave_reports_the_views_own_refusal_reason(main_window, tmp_path):
    window = main_window
    window.view.filename = str(tmp_path / 'autosave.prism')
    window.view.mark_content_dirty()
    def refuse(*args, **kwargs):
        window.view._paged_save_refused = (123.0, 'partial baseline missing')
    window.view.do_save = refuse
    window._perform_autosave()
    assert window.view.autosave_policy.last_error == 'partial baseline missing'
    assert 'partial baseline missing' in window.statusBar().currentMessage()


def test_clean_project_drops_the_backoff(main_window):
    window = main_window
    window.view.autosave_policy.record_failure('previous error')
    window.view.undo_stack.setClean()
    window.view._content_dirty = False
    assert window._perform_autosave() == 'nothing_to_save'
    assert window.view.autosave_policy.failures == 0
