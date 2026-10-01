"""T9-2：WorkspaceService（页面增删改查）。

服务只动 `scene.workspace_pages` 里的数据，不碰控件 —— 所以这些测试
不需要窗口，只要一个带那个属性的假场景。
"""
import copy

import pytest

from prism.workspace_service import DEFAULT_TITLES, KINDS, WorkspaceService


class FakeScene:
    def __init__(self, pages=None):
        self.workspace_pages = list(pages) if pages is not None else []


@pytest.fixture
def scene():
    return FakeScene()


@pytest.fixture
def service(scene):
    return WorkspaceService(scene)


def _page(page_id, kind='canvas', title='页面', parent=None, **extra):
    page = {'id': page_id, 'kind': kind, 'title': title,
            'content': None, 'tags': [], 'parent': parent}
    page.update(extra)
    return page


# ── pages 属性要容错 ─────────────────────────────────────────────

def test_pages_survives_a_missing_attribute(service, scene):
    del scene.workspace_pages
    assert service.pages == []
    assert scene.workspace_pages == [], '该顺手补上，不是每次新建'


def test_pages_survives_none(service, scene):
    scene.workspace_pages = None
    assert service.pages == []


# ── 增 ───────────────────────────────────────────────────────────

def test_add_puts_the_page_at_the_end(service):
    first = service.add('canvas', '一')
    second = service.add('canvas', '二')

    assert [p['id'] for p in service.pages] == [first['id'], second['id']]
    assert first['kind'] == 'canvas'
    assert first['tags'] == []
    assert first['content'] is None
    assert first['parent'] is None


def test_add_gives_each_page_its_own_id(service):
    ids = {service.add('canvas')['id'] for _ in range(20)}
    assert len(ids) == 20


def test_add_names_pages_without_repeating(service):
    titles = [service.add('canvas')['title'] for _ in range(3)]
    assert titles == [DEFAULT_TITLES['canvas'],
                      f"{DEFAULT_TITLES['canvas']} 2",
                      f"{DEFAULT_TITLES['canvas']} 3"]


def test_add_accepts_an_explicit_title(service):
    assert service.add('document', '  我的文档  ')['title'] == '我的文档'


def test_add_falls_back_when_the_title_is_blank(service):
    assert service.add('mindmap', '   ')['title'] == DEFAULT_TITLES['mindmap']


def test_add_rejects_an_unknown_kind(service):
    with pytest.raises(ValueError):
        service.add('spreadsheet')
    assert service.pages == [], '拒绝之后不该留下半个页面'


def test_add_can_take_a_parent(service):
    parent = service.add('canvas', '父')
    child = service.add('canvas', '子', parent=parent['id'])
    assert child['parent'] == parent['id']


def test_restore_page_keeps_data_but_not_the_callers_alias(service):
    original = _page('returned', 'document', '恢复的', content='<p>正文</p>',
                     tags=['稿件'], permanent=True)
    restored = service.restore_page(original)
    original['title'] = '调用者后来改了'

    assert restored == service.find('returned')
    assert restored['title'] == '恢复的'
    assert restored['content'] == '<p>正文</p>'
    assert restored['permanent'] is True


def test_every_kind_can_be_added(service):
    for kind in KINDS:
        assert service.add(kind)['kind'] == kind


# ── 复制 ─────────────────────────────────────────────────────────

def test_duplicate_copies_the_content(service):
    source = service.add('document', '原稿')
    source['content'] = '<p>正文</p>'

    copy_of = service.duplicate(source['id'])

    assert copy_of['id'] != source['id']
    assert copy_of['content'] == '<p>正文</p>'
    assert copy_of['kind'] == 'document'


def test_duplicate_content_is_a_deep_copy(service):
    """改副本不该动到原件 —— 浅拷贝会。"""
    source = service.add('mindmap', '图')
    source['content'] = {'root': 'A'}

    copy_of = service.duplicate(source['id'])
    copy_of['content']['root'] = 'B'

    assert source['content']['root'] == 'A'


def test_duplicate_gives_it_a_distinct_title(service):
    source = service.add('canvas', '场景')
    copy_of = service.duplicate(source['id'])
    assert copy_of['title'] != source['title']
    assert '场景' in copy_of['title']


def test_duplicating_a_missing_page_returns_none(service):
    assert service.duplicate('没这个') is None


def test_duplicating_a_permanent_page_makes_an_ordinary_copy(service):
    scene = service._scene
    scene.workspace_pages = [_page('default-canvas', title='默认画布',
                                   permanent=True)]

    copy_of = service.duplicate('default-canvas')

    assert copy_of is not None
    assert not copy_of.get('permanent'), '副本该是普通页面，能删能改名'


# ── 删 ───────────────────────────────────────────────────────────

def test_remove_takes_the_page_out(service):
    page = service.add('canvas')
    assert service.remove(page['id']) == [page['id']]
    assert service.pages == []


def test_remove_reports_what_it_deleted(service):
    """返回值是给撤销用的 —— 得说清删了哪些。"""
    keep = service.add('canvas', '留')
    doomed = service.add('canvas', '删')
    assert service.remove(doomed['id']) == [doomed['id']]
    assert [p['id'] for p in service.pages] == [keep['id']]


def test_remove_takes_children_with_it(service):
    parent = service.add('canvas', '父')
    child = service.add('document', '子', parent=parent['id'])
    grand = service.add('mindmap', '孙', parent=child['id'])
    other = service.add('canvas', '无关')

    removed = service.remove(parent['id'])

    assert sorted(removed) == sorted([parent['id'], child['id'], grand['id']])
    assert [p['id'] for p in service.pages] == [other['id']]


def test_remove_refuses_permanent_pages(service, scene):
    scene.workspace_pages = [_page('default-canvas', permanent=True)]
    assert service.remove('default-canvas') == []
    assert len(service.pages) == 1


def test_removing_a_missing_page_is_harmless(service):
    service.add('canvas')
    assert service.remove('没这个') == []
    assert len(service.pages) == 1


# ── 排序 ─────────────────────────────────────────────────────────

def test_move_swaps_with_the_neighbour(service):
    a = service.add('canvas', 'A')
    b = service.add('canvas', 'B')

    assert service.move(b['id'], -1) is True
    assert [p['id'] for p in service.pages] == [b['id'], a['id']]


def test_move_refuses_to_walk_off_the_end(service):
    a = service.add('canvas', 'A')
    service.add('canvas', 'B')

    assert service.move(a['id'], -1) is False
    assert service.move(a['id'], 99) is False


def test_move_takes_multiple_steps(service):
    a = service.add('canvas', 'A')
    b = service.add('canvas', 'B')
    c = service.add('canvas', 'C')

    assert service.move(c['id'], -2) is True
    assert [p['id'] for p in service.pages] == [c['id'], a['id'], b['id']]


def test_move_to_a_position(service):
    a = service.add('canvas', 'A')
    b = service.add('canvas', 'B')
    c = service.add('canvas', 'C')

    assert service.move_to(c['id'], 0) is True
    assert [p['id'] for p in service.pages] == [c['id'], a['id'], b['id']]


def test_move_to_clamps_out_of_range(service):
    a = service.add('canvas', 'A')
    b = service.add('canvas', 'B')
    assert service.move_to(a['id'], 99) is True
    assert [p['id'] for p in service.pages] == [b['id'], a['id']]


def test_moving_nowhere_reports_false(service):
    a = service.add('canvas', 'A')
    service.add('canvas', 'B')
    assert service.move_to(a['id'], 0) is False
    assert service.move(a['id'], 0) is False


# ── 改名 ─────────────────────────────────────────────────────────

def test_rename_changes_the_title(service):
    page = service.add('canvas', '旧')
    assert service.rename(page['id'], '新') is True
    assert page['title'] == '新'


def test_rename_trims_whitespace(service):
    page = service.add('canvas', '旧')
    service.rename(page['id'], '  新  ')
    assert page['title'] == '新'


def test_rename_refuses_a_blank_title(service):
    page = service.add('canvas', '原名')
    assert service.rename(page['id'], '   ') is False
    assert page['title'] == '原名'


def test_rename_to_the_same_name_is_not_a_change(service):
    page = service.add('canvas', '同名')
    assert service.rename(page['id'], '同名') is False


def test_renaming_a_missing_page_is_harmless(service):
    assert service.rename('没这个', '随便') is False


# ── 快照与恢复 ───────────────────────────────────────────────────

def test_snapshot_is_deep(scene, service):
    """浅拷贝会让"改动前"的快照跟着后续改动一起变，撤销就撤不回去。"""
    page = service.add('document', '原来')
    page['content'] = '<p>初稿</p>'
    snapshot = service.snapshot()

    service.rename(page['id'], '改了')
    page['content'] = '<p>改了</p>'
    service.add('canvas', '新的')

    assert snapshot[0]['title'] == '原来'
    assert snapshot[0]['content'] == '<p>初稿</p>'
    assert len(snapshot) == 1


def test_restore_puts_everything_back(scene, service):
    page = service.add('document', '原来')
    page['content'] = '<p>初稿</p>'
    snapshot = service.snapshot()

    service.rename(page['id'], '改了')
    service.remove(page['id'])
    assert service.pages == []

    service.restore(snapshot)

    assert len(service.pages) == 1
    assert service.pages[0]['title'] == '原来'
    assert service.pages[0]['content'] == '<p>初稿</p>'


def test_restore_does_not_alias_the_snapshot(scene, service):
    """恢复之后改页面，不该改到快照本身 —— 否则同一个快照只能用一次。"""
    service.add('canvas', '原')
    snapshot = service.snapshot()

    service.restore(snapshot)
    service.pages[0]['title'] = '改过了'

    assert snapshot[0]['title'] == '原'


# ── 查 ───────────────────────────────────────────────────────────

def test_find_returns_the_page(service):
    page = service.add('canvas', '找得到')
    assert service.find(page['id']) is page
    assert service.find('没这个') is None


def test_by_kind_filters(service):
    service.add('canvas', 'A')
    service.add('document', 'B')
    service.add('canvas', 'C')

    assert [p['title'] for p in service.by_kind('canvas')] == ['A', 'C']
    assert [p['title'] for p in service.by_kind('mindmap')] == []


def test_children_of(service):
    parent = service.add('canvas', '父')
    child = service.add('document', '子', parent=parent['id'])
    service.add('canvas', '无关')

    assert [p['id'] for p in service.children_of(parent['id'])] == \
        [child['id']]


# ── 页面标签 ─────────────────────────────────────────────────────

def test_set_tags_cleans_and_dedupes(service):
    page = service.add('canvas')
    assert service.set_tags(page['id'], ['  建筑 ', '', '建筑', None, '夜景'])
    assert page['tags'] == ['建筑', '夜景']


def test_set_tags_empty_clears_them(service):
    page = service.add('canvas')
    service.set_tags(page['id'], ['甲'])
    service.set_tags(page['id'], [])
    assert page['tags'] == []


def test_setting_tags_on_a_missing_page_is_harmless(service):
    assert service.set_tags('没这个', ['甲']) is False


# ── 信号 ─────────────────────────────────────────────────────────

def test_changes_announce_themselves(qtbot, service):
    """界面靠这个信号决定要不要重建页面树。"""
    kinds = []
    service.changed.connect(kinds.append)

    service.add('canvas')
    service.add('document')
    assert kinds == ['add', 'add']

    kinds.clear()
    page = service.pages[0]
    service.rename(page['id'], '新名')
    service.move(page['id'], 1)
    service.set_tags(page['id'], ['甲'])
    service.remove(page['id'])
    service.restore([])

    assert kinds == ['rename', 'move', 'tags', 'remove', 'restore']
