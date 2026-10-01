"""T7：标签的对象边界。

素材标签（item._tags）、页面标签（workspace_pages[i]['tags']）和标签目录
（scene.tag_names）是三个不同的东西。这个文件把它们的边界钉住：动其中一个
不能顺手改另一个。

真出过的问题类型是「改页标签结果把每个素材的元数据都重写了一遍」——
既慢又会把没打算改的素材标记成已修改。
"""
import copy

import pytest

from prism import tags
from prism.widgets.tag_panel import TagEdit, _retagged_items, rename_prefix

# ── 三个存放位置互相独立 ───────────────────────────────────────────────

def test_asset_pages_and_catalogue_are_separate_places(view):
    scene = view.scene
    scene.tag_names = ['目录里的']
    scene.workspace_pages = [{'id': 'p1', 'tags': ['页面的']}]

    from prism.items import PrismPixmapItem
    from PyQt6 import QtGui
    item = PrismPixmapItem(QtGui.QImage(8, 8,
                                        QtGui.QImage.Format.Format_ARGB32),
                           'a.png')
    item._tags = ['素材的']
    scene.addItem(item)

    assert scene.tag_names == ['目录里的']
    assert scene.workspace_pages[0]['tags'] == ['页面的']
    assert item._tags == ['素材的']


def test_scene_tag_names_only_reads(view):
    """合并三处名字用来展示，但这必须是只读的。"""
    scene = view.scene
    scene.tag_names = ['catalogue']
    scene.workspace_pages = [{'id': 'p', 'tags': ['page']}]
    before_pages = copy.deepcopy(scene.workspace_pages)
    before_names = list(scene.tag_names)

    names = tags.scene_tag_names(scene)

    assert 'catalogue' in names and 'page' in names
    assert scene.workspace_pages == before_pages, '只是看看，不该改动页面'
    assert scene.tag_names == before_names


# ── rename_prefix：纯函数，只算新名字 ──────────────────────────────────

def test_renaming_a_tag_and_its_children():
    assert rename_prefix('建筑/红墙', '建筑/红墙', '建筑/砖墙') == '建筑/砖墙'
    assert rename_prefix('建筑/红墙/砖', '建筑/红墙', '建筑/砖墙') == \
        '建筑/砖墙/砖'


def test_renaming_leaves_other_tags_alone():
    assert rename_prefix('人物/老人', '建筑/红墙', '建筑/砖墙') == '人物/老人'
    assert rename_prefix('建筑/白墙', '建筑/红墙', '建筑/砖墙') == '建筑/白墙'


def test_a_prefix_match_must_be_a_real_path_step():
    """「建筑/红墙」不该匹配到「建筑/红墙粉刷」。"""
    assert rename_prefix('建筑/红墙粉刷', '建筑/红墙', '建筑/砖墙') == \
        '建筑/红墙粉刷'


# ── _retagged_items：只挑受影响的素材 ─────────────────────────────────

@pytest.fixture
def three_items(view):
    from PyQt6 import QtGui
    from prism.items import PrismPixmapItem

    made = []
    for name, item_tags in (('a.png', ['建筑/红墙']),
                            ('b.png', ['建筑/红墙/砖']),
                            ('c.png', ['人物/老人'])):
        item = PrismPixmapItem(QtGui.QImage(8, 8,
                                            QtGui.QImage.Format.Format_ARGB32),
                               name)
        item._tags = list(item_tags)
        item._categories = []
        view.scene.addItem(item)
        made.append(item)
    return made


def test_only_matching_assets_are_retagged(view, three_items):
    retags = _retagged_items(view.scene,
                             lambda name: rename_prefix(name,
                                                        '建筑/红墙',
                                                        '建筑/砖墙'))

    assert len(retags) == 2, '只有两个素材挂了这个标签'
    assert sorted(retags[three_items[0]]) == ['建筑/砖墙']
    assert sorted(retags[three_items[1]]) == ['建筑/砖墙/砖']


def test_untouched_assets_are_not_in_the_retag_map(view, three_items):
    retags = _retagged_items(view.scene,
                             lambda name: rename_prefix(name,
                                                        '建筑/红墙',
                                                        '建筑/砖墙'))

    assert three_items[2] not in retags, \
        '没挂这个标签的素材不该被写进重命名表'


# ── TagEdit：改素材，不碰页面 ─────────────────────────────────────────

def test_retagging_assets_leaves_page_tags_alone(view, three_items):
    """这就是 T7 要守住的那条线。"""
    scene = view.scene
    scene.tag_names = ['建筑/红墙', '建筑/红墙/砖', '人物/老人']
    scene.workspace_pages = [{'id': 'p1', 'tags': ['建筑/红墙'],
                              'kind': 'canvas'}]
    pages_before = copy.deepcopy(scene.workspace_pages)

    retags = _retagged_items(scene,
                             lambda name: rename_prefix(name,
                                                        '建筑/红墙',
                                                        '建筑/砖墙'))
    command = TagEdit(view, 'rename tag', names=['建筑/砖墙',
                                                 '建筑/砖墙/砖',
                                                 '人物/老人'],
                      table={'siblings': {}, 'parents': {}},
                      retags=retags)
    command.redo()

    assert scene.workspace_pages == pages_before, \
        '重命名素材标签不该动页面标签'

    command.undo()
    assert scene.workspace_pages == pages_before


def test_retagging_assets_updates_the_assets(view, three_items):
    scene = view.scene
    scene.tag_names = ['建筑/红墙']
    scene.workspace_pages = []

    retags = _retagged_items(scene,
                             lambda name: rename_prefix(name,
                                                        '建筑/红墙',
                                                        '建筑/砖墙'))
    TagEdit(view, 'rename tag', names=['建筑/砖墙'], table={},
            retags=retags).redo()

    assert sorted(three_items[0]._tags) == ['建筑/砖墙']
    assert sorted(three_items[1]._tags) == ['建筑/砖墙/砖']
    assert sorted(three_items[2]._tags) == ['人物/老人'], '无关素材必须原样'


def test_undo_puts_the_asset_tags_back(view, three_items):
    scene = view.scene
    scene.tag_names = ['建筑/红墙']
    scene.workspace_pages = []

    retags = _retagged_items(scene,
                             lambda name: rename_prefix(name,
                                                        '建筑/红墙',
                                                        '建筑/砖墙'))
    command = TagEdit(view, 'rename tag', names=['建筑/砖墙'], table={},
                      retags=retags)
    command.redo()
    command.undo()

    assert sorted(three_items[0]._tags) == ['建筑/红墙']
    assert sorted(three_items[1]._tags) == ['建筑/红墙/砖']


def test_undo_restores_the_catalogue(view, three_items):
    scene = view.scene
    scene.tag_names = ['建筑/红墙', '人物/老人']
    scene.workspace_pages = []

    retags = _retagged_items(scene,
                             lambda name: rename_prefix(name,
                                                        '建筑/红墙',
                                                        '建筑/砖墙'))
    command = TagEdit(view, 'rename tag',
                      names=['建筑/砖墙', '人物/老人'], table={},
                      retags=retags)
    command.redo()
    assert scene.tag_names == ['建筑/砖墙', '人物/老人']

    command.undo()
    assert scene.tag_names == ['建筑/红墙', '人物/老人']


# ── 筛选是只读的 ──────────────────────────────────────────────────────

def test_filtering_never_writes_metadata(view, three_items):
    scene = view.scene
    scene.tag_names = ['建筑/红墙', '人物/老人']
    scene.workspace_pages = [{'id': 'p', 'tags': ['页面的']}]
    assets_before = [list(item._tags) for item in three_items]
    pages_before = copy.deepcopy(scene.workspace_pages)
    catalogue_before = list(scene.tag_names)

    scene.filter_items(tags=['建筑/红墙'])
    assert [list(item._tags) for item in three_items] == assets_before
    assert scene.workspace_pages == pages_before
    assert scene.tag_names == catalogue_before

    scene.filter_items(tags=['人物/老人', '页面的'])
    assert [list(item._tags) for item in three_items] == assets_before
    assert scene.workspace_pages == pages_before
    assert scene.tag_names == catalogue_before
