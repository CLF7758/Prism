"""The tag model: hierarchy, namespaces, synonyms, parents and queries."""
import pytest

from prism import tags


# ── Conventions ───────────────────────────────────────────────────────

def test_paths_split_and_join():
    assert tags.split_path('建筑/红墙') == ['建筑', '红墙']
    assert tags.split_path('/建筑//红墙/') == ['建筑', '红墙']
    assert tags.join_path(['建筑', '红墙']) == '建筑/红墙'
    assert tags.leaf('建筑/红墙') == '红墙'
    assert tags.leaf('红墙') == '红墙'
    assert tags.ancestors('a/b/c') == ['a/b', 'a']


def test_namespace_is_read_from_the_first_level():
    assert tags.namespace('场景:雨夜') == '场景'
    assert tags.namespace('场景:雨夜/内景') == '场景'
    assert tags.namespace('红墙') == ''
    assert tags.namespace_value('场景:雨夜') == '雨夜'
    assert tags.namespace_value('红墙') == ''


def test_namespace_colour_is_stable_and_optional():
    assert tags.namespace_color('场景:雨夜') == tags.namespace_color('场景:白天')
    assert tags.namespace_color('场景:雨夜') != tags.namespace_color('来源:x')
    assert tags.namespace_color('红墙') == ''


# ── Queries ───────────────────────────────────────────────────────────

def find(query, own_tags, extra_tags=(), system=None):
    system = system or tags.TagSystem()
    index = tags.name_index(list(own_tags) + list(extra_tags), system)
    return tags.parse_query(query).matches(index)


def test_query_matches_exact_tag():
    assert find('红墙', ['红墙'])
    assert not find('红墙', ['砖墙'])


def test_query_is_case_insensitive():
    assert find('Neon', ['neon'])
    assert find('neon', ['NEON'])


def test_query_finds_a_tag_by_its_leaf_and_by_its_path():
    assert find('红墙', ['建筑/红墙'])
    assert find('建筑/红墙', ['建筑/红墙'])
    assert not find('砖墙', ['建筑/红墙'])


def test_query_uses_implied_parents():
    assert find('建筑', ['建筑/红墙'])
    assert not find('建筑', ['材质/红墙'])


def test_query_uses_namespace_and_wildcard():
    assert find('场景:*', ['场景:雨夜'])
    assert find('场景:*', ['场景:雨夜/内景'])
    assert find('场景', ['场景:雨夜'])
    assert not find('场景:*', ['来源:Pinterest'])
    assert not find('场景:*', ['雨夜'])


def test_query_requires_all_terms():
    assert find('雨夜 红墙', ['雨夜', '红墙'])
    assert not find('雨夜 红墙', ['雨夜'])


def test_query_or_group():
    assert find('雨夜 | 雪天', ['雪天'])
    assert find('雨夜，雪天', ['雨夜'])
    assert not find('雨夜 | 雪天', ['晴天'])


def test_query_minus_excludes():
    assert find('建筑 -草图', ['建筑'])
    assert not find('建筑 -草图', ['建筑', '草图'])
    # A term that is ruled out must not be satisfied by the OR group it
    # would otherwise belong to.
    assert not find('-草图', ['建筑/草图'])


def test_query_of_nothing_matches_everything():
    query = tags.parse_query('')
    assert not query
    assert query.matches(set())


def test_query_ignores_a_lone_minus():
    assert tags.parse_query('-').terms == []


# ── Synonyms ──────────────────────────────────────────────────────────

def test_siblings_fold_two_names_together():
    system = tags.TagSystem(siblings=[('霓虹', 'neon')])
    assert system.canonical('霓虹') == 'neon'
    assert system.canonical('neon') == 'neon'
    assert system.group('霓虹') == ['neon', '霓虹']
    assert system.aliases('neon') == ['霓虹']
    # Either name finds the other one's items.
    assert find('霓虹', ['neon'], system=system)
    assert find('neon', ['霓虹'], system=system)


def test_siblings_follow_a_chain():
    system = tags.TagSystem(siblings=[('霓虹', 'neon'), ('neon', 'lights')])
    assert system.canonical('霓虹') == 'lights'
    assert system.group('霓虹') == ['lights', 'neon', '霓虹']


def test_sibling_chain_is_kept_when_the_middle_name_is_added_later():
    system = tags.TagSystem()
    system.add_sibling('霓虹', 'neon')
    system.add_sibling('neon', 'lights')
    assert system.canonical('霓虹') == 'lights'


def test_a_synonym_cycle_is_refused():
    system = tags.TagSystem(siblings=[('a', 'b')])
    assert not system.add_sibling('b', 'a')
    assert system.canonical('b') == 'b'


# ── Implied parents ───────────────────────────────────────────────────

def test_parents_are_virtual_and_transitive():
    system = tags.TagSystem(parents=[('红墙', '建筑'), ('建筑', '场景')])
    assert system.implied_parents('红墙') == {'建筑', '场景'}
    assert find('建筑 场景', ['红墙'], system=system)


def test_parent_cycle_is_survived():
    system = tags.TagSystem(parents=[('a', 'b'), ('b', 'a')])
    assert system.implied_parents('a') == {'b'}


def test_path_parents_and_table_parents_are_merged():
    system = tags.TagSystem(parents=[('建筑/红墙', '室外')])
    assert system.implied_parents('建筑/红墙') == {'建筑', '室外'}


# ── The tree ──────────────────────────────────────────────────────────

def build(names, counts=None, system=None):
    return tags.build_tree(names, counts or {}, system or tags.TagSystem())


def test_tree_nests_tags_under_their_path():
    tree = build(['建筑', '建筑/红墙'], {'建筑': 1, '建筑/红墙': 2})
    assert [node.name for node in tree] == ['建筑', '建筑/红墙']
    assert [node.depth for node in tree] == [0, 1]
    assert tree[0].count == 3
    assert tree[0].direct == 1
    assert tree[1].count == 2


def test_tree_adds_the_parent_a_tag_needs():
    # 建筑 was never used and is not in the catalogue, but its child is.
    tree = build(['建筑/红墙'], {'建筑/红墙': 4})
    assert [node.name for node in tree] == ['建筑', '建筑/红墙']
    assert tree[0].count == 4
    assert tree[0].virtual
    assert tree[0].children == [tree[1]]


def test_tree_folds_synonyms_into_one_row():
    system = tags.TagSystem(siblings=[('霓虹', 'neon')])
    tree = build(['neon', '霓虹'], {'neon': 1, '霓虹': 2}, system)
    assert [node.name for node in tree] == ['neon']
    assert tree[0].count == 3
    assert tree[0].aliases == ['霓虹']


def test_tree_lists_virtual_nodes_for_implied_parents():
    system = tags.TagSystem(parents=[('红墙', '建筑')])
    tree = build(['红墙'], {'红墙': 2}, system)
    assert [node.name for node in tree] == ['建筑', '红墙']
    assert tree[0].count == 2
    assert tree[1].count == 2


def test_tree_keeps_catalogue_tags_without_items():
    tree = build(['场景:雨夜'], {})
    assert [node.name for node in tree] == ['场景:雨夜']
    assert tree[0].count == 0
    assert tree[0].virtual


# ── Editing ───────────────────────────────────────────────────────────

def test_move_path_keeps_the_leaf():
    assert tags.move_path('建筑/红墙', '材质') == '材质/红墙'
    assert tags.move_path('红墙', '') == '红墙'
    assert tags.rename_path('建筑/红墙', '砖墙') == '建筑/砖墙'


def test_is_descendant_and_descendants():
    names = ['建筑', '建筑/红墙', '建筑/红墙/旧', '砖墙']
    assert tags.is_descendant('建筑/红墙', '建筑')
    assert not tags.is_descendant('砖墙', '建筑')
    assert tags.descendants(names, '建筑') == ['建筑/红墙', '建筑/红墙/旧']


def test_rename_prefix_follows_children_and_mappings():
    system = tags.TagSystem(siblings=[('建筑/霓虹', 'neon')],
                            parents=[('建筑/红墙', '室外')])
    system.rename_prefix('建筑', '构筑')
    assert system.siblings() == {'构筑/霓虹': 'neon'}
    assert system.parents() == {'构筑/红墙': {'室外'}}


def test_rename_prefix_rewrites_the_canonical_side():
    system = tags.TagSystem(siblings=[('霓虹', '建筑/灯光')])
    system.rename_prefix('建筑', '构筑')
    assert system.canonical('霓虹') == '构筑/灯光'


# ── Storage ───────────────────────────────────────────────────────────

def test_mapping_table_round_trip():
    system = tags.TagSystem(siblings=[('霓虹', 'neon')],
                            parents=[('红墙', '建筑')])
    restored = tags.TagSystem.from_dict(system.to_dict())
    assert restored.siblings() == system.siblings()
    assert restored.parents() == system.parents()
    assert restored.to_dict() == system.to_dict()


def test_reading_a_mapping_table_ignores_rubbish():
    system = tags.TagSystem.from_dict(
        {'siblings': [['a', 'b'], 'nonsense', ['c'], ['d', 'd']],
         'parents': [['x', 'y'], None]})
    assert system.siblings() == {'a': 'b'}
    assert system.parents() == {'x': {'y'}}
    assert tags.TagSystem.from_dict(None).is_empty()


def test_an_empty_system_is_reported_as_empty():
    assert tags.TagSystem().is_empty()
    assert not tags.TagSystem(siblings=[('a', 'b')]).is_empty()


# ── Importing and exporting a tag tree ────────────────────────────────

def test_parse_tree_text_reads_indentation_and_paths():
    text = """
    # a comment
    场景
        雨夜
        - 白天
    建筑/红墙
    """
    assert tags.parse_tree_text(text) == [
        '场景', '场景/雨夜', '场景/白天', '建筑/红墙']


def test_parse_tree_text_handles_tabs_and_bullets():
    assert tags.parse_tree_text('* 场景\n\t+ 雨夜\n') == ['场景', '场景/雨夜']


def test_tree_text_can_be_read_back():
    names = ['场景', '场景/雨夜', '建筑/红墙']
    text = tags.tree_text(names)
    # The export writes the levels a path needs, so reading it back gives
    # the same tags plus the parents that were only implied.
    assert tags.parse_tree_text(text) == [
        '场景', '场景/雨夜', '建筑', '建筑/红墙']
    assert set(names) <= set(tags.parse_tree_text(text))


# ── Attached to a scene ───────────────────────────────────────────────

def test_tag_system_is_created_on_the_scene_once(view):
    first = tags.tag_system(view.scene)
    assert isinstance(first, tags.TagSystem)
    assert tags.tag_system(view.scene) is first


def test_scene_tag_names_merges_catalogue_items_and_pages(view):
    view.scene.tag_names = ['catalogue']
    view.scene.workspace_pages = [{'id': 'p', 'tags': ['page']}]
    assert 'catalogue' in tags.scene_tag_names(view.scene)
    assert 'page' in tags.scene_tag_names(view.scene)


def test_scene_tag_names_ignores_blank_entries(view):
    view.scene.tag_names = ['', '  ', 'ok']
    assert tags.scene_tag_names(view.scene) == ['ok']


def test_tag_names_of_items_counts_usage(view):
    from tests.test_material_features import image_item
    image_item(view.scene, ['a', 'b'])
    image_item(view.scene, ['a'])
    assert tags.tag_names_of_items(view.scene) == {'a': 2, 'b': 1}


def test_unhashable_term_is_rejected():
    with pytest.raises(ValueError):
        tags.Term('   ')


# ── Stored inside the project ─────────────────────────────────────────

def test_the_mapping_table_survives_a_save_and_reload(view, tmp_path):
    from prism.fileio.sql import SQLiteIO
    from tests.test_material_features import image_item
    scene = view.scene
    image_item(scene, ['霓虹', '红墙'])
    system = tags.tag_system(scene)
    system.add_sibling('霓虹', 'neon')
    system.add_parent('红墙', '建筑')
    path = str(tmp_path / 'tags.prism')
    SQLiteIO(path, scene, create_new=True).write()
    scene.clear()
    SQLiteIO(path, scene, readonly=True).read()
    scene.add_queued_items()
    restored = tags.tag_system(scene)
    assert restored.canonical('霓虹') == 'neon'
    assert restored.implied_parents('红墙') == {'建筑'}
    # The item still carries the tag it was given - the table is virtual.
    assert sorted(list(scene.items_for_save())[0].tags) == ['红墙', '霓虹']


def test_a_project_without_a_mapping_table_still_opens(view, tmp_path):
    from prism.fileio.sql import SQLiteIO
    path = str(tmp_path / 'plain.prism')
    SQLiteIO(path, view.scene, create_new=True).write()
    view.scene.clear()
    SQLiteIO(path, view.scene, readonly=True).read()
    assert tags.tag_system(view.scene).is_empty()


def test_a_damaged_mapping_table_does_not_block_the_project(view, tmp_path):
    import json
    import sqlite3
    from prism.fileio.sql import SQLiteIO
    path = str(tmp_path / 'damaged.prism')
    SQLiteIO(path, view.scene, create_new=True).write()
    connection = sqlite3.connect(path)
    connection.execute(
        'INSERT OR REPLACE INTO prism_metadata VALUES (?, ?)',
        ('tag_system', json.dumps({'siblings': 'not a list',
                                   'parents': [['a', 'b']]})))
    connection.commit()
    connection.close()
    view.scene.clear()
    SQLiteIO(path, view.scene, readonly=True).read()
    assert tags.tag_system(view.scene).parents() == {'a': {'b'}}
