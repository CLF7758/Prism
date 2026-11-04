"""D-3: keyword-search debounce by project size + the tag name-index cache.

Two things are pinned here.

**The keyword-search debounce is tiered by how much the search has to
look at.**  It used to be a fixed 180 ms: small projects waited for
nothing, big ones could not keep up with a burst of typing.  The
interval now follows the number of assets in the project.

**`prism.tags.name_index` is memoised.**  A search asks for the name
index of every asset it looks at, and the answer only depends on the
tags and on the project's mapping table.  The cache key carries both, so
a re-tagged asset never keeps the names of its old tags, a changed
synonym or implied parent never leaves an old name behind, and two
projects never share an entry.

The last test file also pins the things the media library had to keep:
the cross-canvas move entry and its shortcut, the three timers, and the
filter exemption for a photo the user asked for by name.
"""
import pytest
from PyQt6 import QtGui

from prism import tags
from prism.widgets.media_library import (
    MOVE_TO_CANVAS_SHORTCUT, SEARCH_DEBOUNCE_MAX, SEARCH_DEBOUNCE_TIERS,
    search_debounce_interval)


# ── The debounce tiers ─────────────────────────────────────────────────

@pytest.mark.parametrize('scale,expected', [
    (0, 50),
    (1, 50),
    (500, 50),
    (501, 200),
    (5000, 200),
    (5001, 250),
    (50000, 250),
    (50001, 300),
    (200000, 300),
])
def test_the_interval_follows_the_number_of_assets(scale, expected):
    assert search_debounce_interval(scale) == expected


def test_every_tier_has_its_own_interval_and_they_only_grow():
    intervals = [interval for _limit, interval in SEARCH_DEBOUNCE_TIERS]
    assert len(set(intervals)) == len(intervals), '两档用同一个间隔就不叫分档'
    assert intervals == sorted(intervals), '工程越大，等待只能更久，不能更短'
    assert intervals[-1] < SEARCH_DEBOUNCE_MAX


def test_a_bigger_project_always_waits_at_least_as_long():
    intervals = [search_debounce_interval(n)
                 for n in (10, 1000, 10000, 100000)]
    assert intervals == sorted(intervals)
    assert len(set(intervals)) == 4, '四个规模必须落在四个不同的档上'


def test_an_unknown_size_gets_the_longest_interval():
    """按页打开、目录还没建好时计数是 None —— 证明不了它小就按大的算。"""
    assert search_debounce_interval(None) == SEARCH_DEBOUNCE_MAX


def test_a_nonsense_size_gets_the_longest_interval_too():
    assert search_debounce_interval('not a number') == SEARCH_DEBOUNCE_MAX


# ── The panel really uses the tier ─────────────────────────────────────

def test_the_panel_timer_uses_the_interval_of_the_project_size(main_window):
    panel = main_window.view.category_panel
    seen = []
    for scale in (10, 1000, 10000, 100000):
        panel._search_scale = scale
        panel._start_search_timer()
        seen.append(panel._search_timer.interval())

    assert seen == [50, 200, 250, 300]
    assert len(set(seen)) == 4, '不同规模必须用不同的间隔'


def test_the_scale_is_the_number_of_assets_in_the_project(main_window):
    panel = main_window.view.category_panel
    panel.update_counts()
    assert panel._search_scale == panel.catalog.count_snapshot()['total']


def test_typing_starts_the_tiered_timer(main_window):
    panel = main_window.view.category_panel
    panel._enter_result_mode('search')
    panel._search_scale = 100000

    panel.search.setText('needle')

    assert panel._search_timer.isActive()
    assert panel._search_timer.interval() == 300


def test_the_search_still_answers_after_the_debounce_change(main_window):
    panel = main_window.view.category_panel
    panel._search_scale = 100000
    panel._start_search_timer()
    assert panel.project_search_results('needle') == []


# ── The name-index cache ───────────────────────────────────────────────

def test_the_same_tags_under_the_same_table_hit_the_cache():
    system = tags.TagSystem()
    tags.clear_name_index_cache()
    before = tags.name_index_cache_stats()

    first = tags.name_index(['建筑/红墙'], system)
    after_first = tags.name_index_cache_stats()
    second = tags.name_index(['建筑/红墙'], system)
    after_second = tags.name_index_cache_stats()

    assert after_first['misses'] == before['misses'] + 1
    assert after_second['hits'] == after_first['hits'] + 1
    assert after_second['size'] == 1
    assert first == second


def test_the_cache_is_keyed_by_the_tag_not_by_the_tag_set():
    """The cached unit is **one tag**.

    Fifty assets carrying the same two tags have to reuse two cache
    entries, not fifty - that is what makes the cache pay on a project
    with many assets.
    """
    system = tags.TagSystem()
    tags.clear_name_index_cache()
    before = tags.name_index_cache_stats()
    for _ in range(50):
        tags.name_index(['建筑/红墙', '材质/砖墙'], system)

    stats = tags.name_index_cache_stats()
    assert stats['size'] == 2, '缓存的条目是两个标签，不该是 50 个标签组合'
    assert stats['misses'] == before['misses'] + 2, '只有两个标签要建'
    assert stats['hits'] == before['hits'] + 98, '其余 98 次查找都该命中'


def test_the_cached_index_is_the_same_as_a_freshly_built_one():
    system = tags.TagSystem(siblings=[('霓虹', 'neon')],
                            parents=[('红墙', '建筑')])
    tags.name_index(['霓虹', '红墙'], system)      # fill the cache

    # A twin with the same table but an identity of its own: its answer is
    # built from scratch, so the two can be compared.
    twin = tags.TagSystem(siblings=[('霓虹', 'neon')],
                          parents=[('红墙', '建筑')])

    assert tags.name_index(['霓虹', '红墙'], system) == \
        tags.name_index(['霓虹', '红墙'], twin)


def test_the_result_is_still_a_plain_mutable_set():
    system = tags.TagSystem()
    index = tags.name_index(['建筑'], system)
    assert type(index) is set
    index.add('调用方自己加的名字')
    assert '调用方自己加的名字' in index
    # The cache still holds the original answer.
    assert tags.name_index(['建筑'], system) == {'建筑'}


def test_a_retagged_asset_keeps_no_name_of_its_old_tag():
    system = tags.TagSystem()
    before = tags.name_index(['建筑/红墙'], system)
    assert '建筑' in before and '红墙' in before

    after = tags.name_index(['材质/砖墙'], system)

    assert '建筑' not in after, '旧标签绝不能留在结果里'
    assert '红墙' not in after
    assert '材质' in after and '砖墙' in after


def test_a_new_synonym_shows_up_in_the_index():
    system = tags.TagSystem()
    assert 'neon' not in tags.name_index(['霓虹'], system)

    system.add_sibling('霓虹', 'neon')

    assert 'neon' in tags.name_index(['霓虹'], system)


def test_a_removed_synonym_leaves_no_old_name_behind():
    system = tags.TagSystem(siblings=[('霓虹', 'neon')])
    assert 'neon' in tags.name_index(['霓虹'], system)

    system.remove_sibling('霓虹')

    assert 'neon' not in tags.name_index(['霓虹'], system)


def test_a_new_implied_parent_shows_up_in_the_index():
    system = tags.TagSystem()
    assert '建筑' not in tags.name_index(['红墙'], system)

    system.add_parent('红墙', '建筑')

    assert '建筑' in tags.name_index(['红墙'], system)


def test_a_removed_implied_parent_leaves_no_old_name_behind():
    system = tags.TagSystem(parents=[('红墙', '建筑')])
    assert '建筑' in tags.name_index(['红墙'], system)

    system.remove_parent('红墙', '建筑')

    assert '建筑' not in tags.name_index(['红墙'], system)


def test_a_renamed_tag_leaves_no_old_path_behind():
    system = tags.TagSystem(parents=[('建筑/红墙', '室外')])
    assert '室外' in tags.name_index(['建筑/红墙'], system)

    system.rename_prefix('建筑', '构筑')

    assert '室外' not in tags.name_index(['建筑/红墙'], system)
    assert '室外' in tags.name_index(['构筑/红墙'], system)


def test_switching_scene_does_not_carry_the_names_over(view):
    scene = view.scene
    first = tags.tag_system(scene)
    first.add_parent('红墙', '建筑')
    assert '建筑' in tags.name_index(['红墙'], first)

    # Another project: another scene, another table, another identity.
    other = tags.TagSystem()

    assert '建筑' not in tags.name_index(['红墙'], other)
    assert '建筑' in tags.name_index(['红墙'], first)


def test_a_copied_table_gets_an_identity_of_its_own():
    system = tags.TagSystem(parents=[('红墙', '建筑')])
    clone = system.copy()
    assert '建筑' in tags.name_index(['红墙'], clone)

    clone.remove_parent('红墙', '建筑')

    assert '建筑' not in tags.name_index(['红墙'], clone)
    assert '建筑' in tags.name_index(['红墙'], system), '原表不该被副本的改动带走'


def test_the_cache_is_bounded(monkeypatch):
    monkeypatch.setattr(tags, 'NAME_INDEX_CACHE_LIMIT', 4)
    tags.clear_name_index_cache()
    system = tags.TagSystem()

    for number in range(20):
        tags.name_index([f'标签{number}'], system)

    stats = tags.name_index_cache_stats()
    assert stats['size'] <= 4
    assert stats['evictions'] >= 1


def test_a_broken_tag_list_still_raises_the_same_error():
    system = tags.TagSystem()
    with pytest.raises(TypeError):
        tags.name_index(5, system)


def test_the_query_language_still_answers_the_same():
    system = tags.TagSystem(siblings=[('霓虹', 'neon')],
                            parents=[('红墙', '建筑')])
    index = tags.name_index(['霓虹', '红墙'], system)
    assert tags.parse_query('neon 建筑').matches(index)
    assert not tags.parse_query('neon 材质').matches(index)


# ── What the media library had to keep ─────────────────────────────────

def test_the_move_entry_and_its_shortcut_are_still_there(main_window):
    panel = main_window.view.category_panel
    assert MOVE_TO_CANVAS_SHORTCUT == 'Ctrl+Shift+C'
    action = panel.move_to_canvas_action
    assert action.shortcut() == QtGui.QKeySequence(MOVE_TO_CANVAS_SHORTCUT)
    assert action in main_window.actions()
    for name in ('move_items_to_canvas', '_setup_move_to_canvas',
                 '_install_tree_move_entry', '_add_tree_move_action',
                 'move_selected_to_canvas'):
        assert callable(getattr(panel, name)), name


def test_the_three_timers_keep_their_own_meaning(main_window):
    panel = main_window.view.category_panel
    # The recount debounce is the count recalculation - 300 ms, unchanged.
    assert panel._recount_timer.interval() == 300
    assert panel._recount_timer.isSingleShot()
    # The keyword-search debounce is the one that is tiered.
    assert panel._search_timer.isSingleShot()
    # The material tag debounce is 100 ms and is created on demand.
    panel._on_material_records_changed({'operation': 'tags'})
    assert panel._material_tag_timer.interval() == 100
    assert panel._material_tag_timer.isSingleShot()


def test_a_photo_the_user_asked_for_keeps_its_filter_exemption(main_window):
    """「在画布中定位」找到的那张图不能被随后的重算藏起来。

    用户报过的「闪一下又消失」就是这条豁免防的。改了筛选之后豁免就该
    结束 —— 那是 `keep_pinned` 告诉 apart 的。
    """
    from tests.test_material_features import image_item

    panel = main_window.view.category_panel
    item = image_item(main_window.view.scene)
    item._keep_visible = True
    # A filter that hides the photo: a category the item does not carry.
    panel._active_filter = ('category', '没有这个分类')

    panel._apply_filter(keep_pinned=True)
    assert item._keep_visible is True, '重算不该解除用户指名的那张图的豁免'

    panel._apply_filter(keep_pinned=False)
    assert item._keep_visible is False, '用户自己改了筛选，豁免就该结束'
