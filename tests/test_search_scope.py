"""搜索的范围（用户的第 3 条）。

用户的原话：

    搜索功能只能适用于画布，也要匹配文档和脑图，但是这个比如有一个标签
    叫做古风，但是文档和脑图里也有古风的文字，这个功能你要好好想想，
    而且所有功能比如说我搜索一个标签，他应该展示的是我这个项目里所有
    分类里这个标签的图片。

拆成两件事：

  1. **搜索要匹配文档正文和脑图节点** —— 以前只比画布素材的
     标题/文件名/备注/标签，文档和脑图的文字根本不在搜索范围里
  2. **搜索要跨所有分类** —— 以前选了某个分类之后，搜索就只在那个分类
     里找，别处明明有也看不到
"""
import pytest

from prism.widgets.media_library import plain_text_of, text_of_tree


def hits(panel):
    from prism.project_search import search_project
    query, expression = panel._split_query(panel.search.text())
    return search_project(panel.scene, panel.resource_service, query,
                          tag_expression=expression)


# ── 纯文本抽取 ──────────────────────────────────────────────────

def test_html_tags_are_stripped():
    assert plain_text_of('<p>古风</p>') == '古风'


def test_a_word_split_by_tags_is_still_found():
    """`古<b>风</b>` 也要能被"古风"搜到。

    不去标签的话，正文里的词被格式标签拆开时就搜不到了 —— 而用户
    恰恰是拿这种词来搜的。
    """
    text = plain_text_of('<p>古<b>风</b>配色</p>')
    assert '古风' in text.replace(' ', '')


def test_entities_are_decoded():
    """`&amp;` / `&lt;` 这些要还原成字符，搜 `&` 才命中。"""
    text = plain_text_of('<p>甲 &amp; 乙 &lt;丙&gt;</p>')
    assert '&' in text
    assert '<丙>' in text


def test_empty_html_is_empty_text():
    assert plain_text_of('') == ''
    assert plain_text_of(None) == ''


def test_repeated_calls_are_consistent():
    """第二次走缓存，结果要一样。"""
    html = '<p>缓存测试</p>'
    assert plain_text_of(html) == plain_text_of(html) == '缓存测试'


def test_tree_text_collects_every_node():
    tree = {'title': '根', 'children': [
        {'title': '古风', 'children': [{'title': '山水'}]},
        {'title': '配色'},
    ]}
    text = text_of_tree(tree)
    for word in ('根', '古风', '山水', '配色'):
        assert word in text, f'{word} 没被收进来'


def test_tree_text_handles_odd_shapes():
    """树可能是空、可能是 None、孩子可能是别的字段名 —— 别炸。"""
    assert text_of_tree(None) == ''
    assert text_of_tree({}) == ''
    assert '甲' in text_of_tree({'topic': '甲'})
    assert '乙' in text_of_tree({'nodes': [{'text': '乙'}]})


# ── 面板里的搜索范围 ────────────────────────────────────────────

def make_document_page(view, page_id='doc-1', title='秋雨笔记', html=''):
    view.scene.workspace_pages = [{
        'id': page_id, 'kind': 'document', 'title': title,
        'content': html, 'tags': []}]
    panel = view.category_panel
    panel.rebuild_workspace_tree()
    return page_id


def test_a_document_page_row_dims_when_it_does_not_match(view):
    make_document_page(view, html='<p>全是无关的话</p>')
    panel = view.category_panel
    panel.search.setText('古风')
    assert not hits(panel)


def test_a_document_page_row_is_lit_when_its_body_matches(view):
    """**核心那条**：搜索要能匹配**文档正文**，不只是页面名。

    页面叫"秋雨笔记"，正文里有"古风" —— 搜"古风"该把它点亮。
    """
    make_document_page(view, html='<p>这幅画是古风的意境</p>')
    panel = view.category_panel
    panel.search.setText('古风')
    assert any(r['kind'] == 'document_text' for r in hits(panel))


def test_a_mindmap_page_row_is_lit_when_a_node_matches(view):
    """脑图节点文字也要能搜到。"""
    view.scene.workspace_pages = [{
        'id': 'map-1', 'kind': 'mindmap', 'title': '配色方案',
        'content': {'title': '配色', 'children': [{'title': '古风配色'}]},
        'tags': []}]
    panel = view.category_panel
    panel.rebuild_workspace_tree()
    panel.search.setText('古风')
    assert any(r['kind'] == 'mindmap_node' for r in hits(panel))


def test_a_page_matches_on_its_title_too(view):
    make_document_page(view, title='古风笔记', html='<p>无关</p>')
    panel = view.category_panel
    panel.search.setText('古风')
    assert any(r['kind'] == 'document' for r in hits(panel))


def test_clearing_the_search_restores_every_row(view):
    make_document_page(view, html='<p>无关</p>')
    panel = view.category_panel
    panel.search.setText('古风')
    assert not hits(panel)
    panel.search.setText('')
    panel._apply_filter()
    assert panel.search.text() == ''
    assert panel.search_results.isHidden()


# ── 搜索跨分类 ──────────────────────────────────────────────────

def test_searching_ignores_the_selected_category(view):
    """**核心那条**：选了"古风"分类之后搜别的词，别处的结果也要出来。

    用户的原话："我搜索一个标签，他应该展示的是我这个项目里所有分类里
    这个标签的图片。"
    """
    from tests.test_material_features import image_item

    inside = image_item(view.scene, ['古风'])
    inside._categories = ['古风']
    outside = image_item(view.scene, ['雨夜'])
    outside._categories = ['风景']

    panel = view.category_panel
    panel._active_filter = ('category', '古风')
    panel.search.setText('雨夜')
    panel._apply_filter()

    assert any(r.get('item') is outside for r in hits(panel))
    assert inside.isVisible() and not outside.isVisible(), '项目搜索不修改画布分类筛选'


def test_clearing_the_search_brings_the_category_filter_back(view):
    """搜完清空，回到当前分类的视图（不是回到第一个分类）。"""
    from tests.test_material_features import image_item

    inside = image_item(view.scene, ['古风'])
    inside._categories = ['古风']
    outside = image_item(view.scene, ['雨夜'])
    outside._categories = ['风景']

    panel = view.category_panel
    panel._active_filter = ('category', '古风')
    panel.search.setText('雨夜')
    panel._apply_filter()
    assert any(r.get('item') is outside for r in hits(panel))

    panel.search.setText('')
    panel._apply_filter()
    assert inside.isVisible(), '清空之后该只看当前分类'
    assert not outside.isVisible(), '别的分类该重新被挡住'


# ── 合并后的搜索框（第 11 条）──────────────────────────────────

def test_the_tag_query_box_is_gone(view):
    """界面上只剩一个搜索框 —— 用户要的就是"少一个盒子"。"""
    panel = view.category_panel
    assert not hasattr(panel, 'tag_query'), '那个盒子该没了'
    assert panel.search is not None


@pytest.mark.parametrize('typed,is_tag', [
    ('tag:古风', True),
    ('场景:*', True),
    ('雨夜 | 红墙', True),
    ('红墙 -草图', True),
    ('-*', True),
    ('古风', False),
    ('雨夜 红墙', False),
    ('秋雨参考', False),
])
def test_plain_words_go_to_text_and_operators_go_to_tags(
        view, typed, is_tag):
    """分派规则：**只有明显是运算符的才当标签查询**。

    猜错的代价不对称 —— 把普通词当成标签查询会让它搜不到东西（比如
    「秋雨参考」这种名字），而当文字搜最多是结果多一些，用户一眼能看出
    来再改。所以判定故意保守。
    """
    query, tag_expression = view.category_panel._split_query(typed)
    if is_tag:
        assert not query, f'{typed!r} 该走标签查询，实得文字={query!r}'
        assert tag_expression, f'{typed!r} 该走标签查询'
    else:
        assert query, f'{typed!r} 该走文字搜，实得标签={tag_expression!r}'


def test_quotes_force_plain_text(view):
    """引号里即使有运算符也当纯文字 —— 那是"我就是要搜这串字"的意思。"""
    query, tag_expression = view.category_panel._split_query('"雨夜 | 红墙"')
    assert query == '雨夜 | 红墙'
    assert not tag_expression


def test_an_empty_box_is_both_empty(view):
    panel = view.category_panel
    assert panel._split_query('') == ('', '')
    assert panel._split_query('   ') == ('', '')


def test_several_words_all_have_to_match(view):
    """空格 = 与，文字搜索这边也一样。

    整串匹配的话"雨夜 红墙"永远搜不到东西 —— 而用户敲空格的意思是
    "两个都要"，标签查询那边也是这么理解的。两边行为一致，用户不用记
    "空格在哪个盒子里是什么意思"。
    """
    from tests.test_material_features import image_item

    both = image_item(view.scene, ['雨夜', '红墙'])
    only_one = image_item(view.scene, ['雨夜'])

    panel = view.category_panel
    panel.search.setText('雨夜 红墙')
    panel._apply_filter()
    assert both.isVisible(), '两个词都有，该留下'
    assert [r['item'] for r in hits(panel) if r['kind'] == 'asset'] == [both]
    assert only_one.isVisible(), '搜索不能隐藏原画布素材'


def test_a_tag_operator_still_works_through_the_single_box(view):
    """标签运算符在合并后的框里照旧能用。"""
    from tests.test_material_features import image_item

    wall = image_item(view.scene, ['红墙'])
    wall_and_sketch = image_item(view.scene, ['红墙', '草图'])

    panel = view.category_panel
    panel.search.setText('红墙 -草图')
    panel._apply_filter()
    assert wall.isVisible(), '没打草图的该留下'
    assert [r['item'] for r in hits(panel) if r['kind'] == 'asset'] == [wall]
    assert wall_and_sketch.isVisible()
