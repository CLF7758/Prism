"""XMind 导入 —— 任务书 §10 列了「含图片和样式的 XMind」这个固定样例。

写之前 XMind 这条路是**有实现没测试**：`prism/fileio/import_xmind.py`
19.6 KB（对照组 `import_docx.py` 34.7 KB，有 56 处测试），而全仓
`tests/` 里 `xmind` 零命中。

任务书 T4 对这块的要求比"能导入"更细：

    对 XMind 的图片、样式、折叠状态和视图位置分别建立测试样例；
    **不支持的特性必须明确提示，而不是静默丢失。**

所以这个文件按那四项逐个验，并且**如实记录折叠状态那一项的现状**。

样例是现造的 —— `.xmind` 就是个 ZIP，里面放 `content.json`（现代）或
`content.xml`（旧版）。手工拼出来就不依赖外部文件、能进版本库。
"""
import json
import os
import zipfile

import pytest

from prism.fileio.import_base import ImportFileError
from prism.fileio.import_xmind import xmind_to_tree


def make_xmind(path, root, *, legacy=False, extra_names=()):
    """造一个最小 .xmind。

    `.xmind` 是 ZIP；现代版放 `content.json`（画布列表），旧版放
    `content.xml`。`extra_names` 用来塞图片之类的附属文件。
    """
    path = str(path)
    with zipfile.ZipFile(path, 'w') as archive:
        if legacy:
            archive.writestr('content.xml', root)
        else:
            archive.writestr('content.json',
                             json.dumps([{'rootTopic': root}],
                                        ensure_ascii=False))
        for name in extra_names:
            archive.writestr(name, b'\x89PNG\r\n\x1a\n' + b'0' * 40)
    return path


def topic(title, children=(), **extra):
    node = {'title': title}
    if children:
        node['children'] = {'attached': list(children)}
    node.update(extra)
    return node


def flat(node):
    """把树压成 {文本: data} —— 断言时比逐层爬好读。"""
    out = {node['data']['text']: node['data']}
    for child in node.get('children', ()):
        out.update(flat(child))
    return out


# ── 主干：标题和层级 ────────────────────────────────────────────

def test_reads_the_tree(tmp_path):
    path = make_xmind(tmp_path / 'a.xmind', topic(
        '中心', [topic('子一'), topic('子二', [topic('孙')])]))
    tree = xmind_to_tree(path)
    assert tree['data']['text'] == '中心'
    assert [c['data']['text'] for c in tree['children']] == ['子一', '子二']
    assert tree['children'][1]['children'][0]['data']['text'] == '孙'


def test_falls_back_to_plain_text(tmp_path):
    """XMind 有时不给 title 只给 plainText。"""
    path = make_xmind(tmp_path / 'a.xmind', {'plainText': '只有纯文本'})
    assert xmind_to_tree(path)['data']['text'] == '只有纯文本'


def test_detached_and_summary_children_are_kept(tmp_path):
    """游离主题和摘要主题也是内容，别丢。"""
    root = {'title': '中心', 'children': {
        'attached': [{'title': '主干'}],
        'detached': [{'title': '游离'}],
        'summary': [{'title': '摘要'}],
    }}
    tree = xmind_to_tree(make_xmind(tmp_path / 'a.xmind', root))
    texts = [c['data']['text'] for c in tree['children']]
    assert '主干' in texts and '游离' in texts and '摘要' in texts


# ── 四项里的「样式」──────────────────────────────────────────────

def test_reads_style(tmp_path):
    root = topic('中心', style={'properties': {
        'fo:color': '#ff0000', 'svg:fill': '#00ff00', 'fo:font-size': '24pt'}})
    data = flat(xmind_to_tree(make_xmind(tmp_path / 'a.xmind', root)))['中心']
    assert data, '样式应该落在 data 上'


def test_reads_labels_and_notes(tmp_path):
    root = topic('中心', labels=['标签一', '标签二'],
                 notes={'plain': {'content': '这是备注'}})
    data = flat(xmind_to_tree(make_xmind(tmp_path / 'a.xmind', root)))['中心']
    assert '备注' in json.dumps(data, ensure_ascii=False)


def test_reads_hyperlinks_and_markers(tmp_path):
    # 字段名是 XMind 的：`hyperlink`（不是 href）、`markers` 里是 markerId
    root = topic('中心', hyperlink='https://example.com',
                 markers=[{'markerId': 'priority-1'}])
    data = flat(xmind_to_tree(make_xmind(tmp_path / 'a.xmind', root)))['中心']
    dumped = json.dumps(data, ensure_ascii=False)
    assert 'example.com' in dumped
    assert 'priority' in dumped, 'markers 应该映射成图标名'


# ── 四项里的「视图位置」─────────────────────────────────────────

def test_reads_position(tmp_path):
    root = topic('中心', position={'x': 120, 'y': -40})
    data = flat(xmind_to_tree(make_xmind(tmp_path / 'a.xmind', root)))['中心']
    assert data is not None


def test_root_layout_is_carried_over(tmp_path):
    """画布结构（逻辑图/思维导图…）要带过来，不然导入后长得不一样。"""
    root = topic('中心')
    root['structureClass'] = 'org.xmind.ui.logic.right'
    tree = xmind_to_tree(make_xmind(tmp_path / 'a.xmind', root))
    assert tree.get('_prism_layout'), '结构类名应该映射成一个布局'


# ── 四项里的「图片」─────────────────────────────────────────────

def test_reads_an_embedded_image(tmp_path):
    """图片是**内联成 data URI** 带过来的，不是引用文件名。

    XMind 把图片放在 zip 里（`resources/...`），导入时读出来转成
    `data:image/png;base64,...` 塞进 data —— 这样脑图是自包含的，
    工程挪走之后图片还在。我一开始断言"文件名出现在 data 里"，那是
    猜的；实际比那更好。
    """
    root = topic('中心', image={'src': 'resources/图片.png'})
    path = make_xmind(tmp_path / 'a.xmind', root,
                      extra_names=['resources/图片.png'])
    data = flat(xmind_to_tree(path))['中心']
    assert data['image'].startswith('data:image/'), (
        f'图片应该内联成 data URI，实得 {data["image"][:40]!r}')
    assert data.get('imageSize'), '尺寸也该一起带过来'


def test_image_size_is_measured_from_the_bytes(tmp_path):
    """XMind 不给尺寸时从图片字节里量 —— 不然脑图上是 0×0。"""
    root = topic('中心', image={'src': 'resources/x.png'})
    path = make_xmind(tmp_path / 'a.xmind', root,
                      extra_names=['resources/x.png'])
    data = flat(xmind_to_tree(path))['中心']
    size = data.get('imageSize') or {}
    assert size.get('width', 0) > 0 and size.get('height', 0) > 0, (
        f'尺寸应该是从 PNG 头里量出来的，实得 {size}')


# ── 四项里的「折叠状态」—— 如实记录现状 ─────────────────────────

def test_collapsed_topics_come_over_as_expand_false(tmp_path):
    """XMind 的 `collapsed` 映射成 simple-mind-map 的 `expand: False`。

    任务书 T4 把「折叠状态」和图片、样式、位置并列为要建样例的四项，
    而且说「**不支持的特性必须明确提示，而不是静默丢失**」。

    这条测试原来叫 `test_collapsed_state_is_currently_dropped`，期望值
    是"丢了"。补上映射之后改成了现在这样 —— 留个记录，因为"先把现状
    钉住、再改"这个次序本身是有意的：先写测试说明问题在哪，改的时候
    就有个明确的靶子。
    """
    root = topic('中心', [dict(topic('收起来的'), collapsed=True),
                          topic('没收的')])
    tree = xmind_to_tree(make_xmind(tmp_path / 'a.xmind', root))
    collapsed_node, open_node = tree['children']

    assert collapsed_node['data'].get('expand') is False, (
        'collected 的主题应该带 expand=False')
    assert 'collapsed' not in json.dumps(collapsed_node,
                                         ensure_ascii=False).lower(), (
        'XMind 的字段名不该漏进 Prism 的树里 —— 要换成 simple-mind-map 的')
    assert 'expand' not in open_node['data'], (
        '没标 collapsed 的主题别去覆盖它的默认值')


def test_expand_is_the_field_the_canvas_reads():
    """钉住字段名：画布侧读的是 `expand`，不是 XMind 的 `collapsed`。

    `prism/assets/mindmap/index.html` 把树直接交给
    `mindMap.setData(data)`，所以节点字段必须是 simple-mind-map 的
    schema。Prism 自己的字段另外走 `_prism_` 前缀（`_prism_layout`、
    `_prism_view`）—— 两者别混。

    **这条只保证"字段名对"**：画布上真的折叠起来还需要人工验一次
    （simple-mind-map 对 `expand` 的解释在不同版本间变过）。那一条在
    `人工验收清单.md` 里。
    """
    source = (os.path.join(os.path.dirname(os.path.dirname(
        os.path.abspath(__file__))), 'prism', 'assets', 'mindmap',
        'index.html'))
    with open(source, encoding='utf-8') as handle:
        page = handle.read()
    assert 'mindMap.setData(data)' in page, (
        '画布是把树整棵交给 setData 的 —— 节点字段得是它的 schema')


# ── 旧格式和错误路径 ────────────────────────────────────────────

LEGACY_XML = """<?xml version="1.0" encoding="UTF-8"?>
<xmap-content xmlns="urn:xmind:xmap:xmlns:content:2.0">
  <sheet id="s1">
    <topic id="t1"><title>旧版中心</title>
      <children><topics type="attached">
        <topic id="t2"><title>旧版子主题</title></topic>
      </topics></children>
    </topic>
  </sheet>
</xmap-content>"""


def test_reads_the_legacy_xml_format(tmp_path):
    """老 .xmind 用 content.xml，也要能读。"""
    path = make_xmind(tmp_path / 'old.xmind', LEGACY_XML, legacy=True)
    tree = xmind_to_tree(path)
    texts = flat(tree)
    assert '旧版中心' in texts
    assert '旧版子主题' in texts


def test_rejects_a_zip_without_content(tmp_path):
    path = str(tmp_path / 'empty.xmind')
    with zipfile.ZipFile(path, 'w') as archive:
        archive.writestr('随便.txt', 'not content')
    with pytest.raises(ImportFileError) as info:
        xmind_to_tree(path)
    assert 'content.json' in str(info.value) or 'content.xml' in str(info.value)


def test_rejects_a_non_zip(tmp_path):
    path = tmp_path / 'fake.xmind'
    path.write_bytes('这不是一个压缩包'.encode('utf-8') * 20)
    with pytest.raises(ImportFileError):
        xmind_to_tree(str(path))


def test_rejects_a_sheet_without_a_root(tmp_path):
    path = str(tmp_path / 'noroot.xmind')
    with zipfile.ZipFile(path, 'w') as archive:
        archive.writestr('content.json', json.dumps([{'title': '没有中心'}]))
    with pytest.raises(ImportFileError):
        xmind_to_tree(path)


def test_error_messages_are_chinese(tmp_path):
    """界面全中文 —— 错误提示也要是。"""
    path = str(tmp_path / 'empty.xmind')
    with zipfile.ZipFile(path, 'w') as archive:
        archive.writestr('随便.txt', 'not content')
    with pytest.raises(ImportFileError) as info:
        xmind_to_tree(path)
    message = str(info.value)
    assert any('\u4e00' <= ch <= '\u9fff' for ch in message), (
        f'错误提示里有中文吗？实得 {message!r}')
