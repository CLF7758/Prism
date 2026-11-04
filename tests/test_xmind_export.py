"""Verify actual XMind archives independently of Prism's XMind importer."""
import hashlib
import io
import json
from types import SimpleNamespace
import zipfile

from PIL import Image
import pytest

from prism.fileio.attachments import attachment_url
from prism.fileio.mindmap_export import MindMapExportError, export_mindmap, node_text


def _png():
    output = io.BytesIO()
    Image.new('RGB', (16, 8), '#38a7cb').save(output, format='PNG')
    return output.getvalue()


def _tree():
    return {
        '_prism_layout': 'logicalStructure',
        'data': {'text': '计划 <主角> & 音乐\n第二行', 'note': '第一行\n示例 <shader> & 参数',
                 'hyperlink': 'https://example.com/?a=1&b=2', 'hyperlinkTitle': '参考文档',
                 'tag': ['角色', '动画'], 'image': attachment_url(7),
                 'imageSize': {'width': 16, 'height': 8}},
        'children': [
            {'data': {'text': '制作', 'expand': False}, 'children': [
                {'data': {'text': '模型'}, 'children': [
                    {'data': {'text': '贴图 😀', 'image': attachment_url(7)}}]}]},
            {'data': {'text': '<p>渲染 &amp; 光照</p><p>测试 &#x2605;</p>', 'richText': True}},
        ],
    }


def _read(path):
    with zipfile.ZipFile(path) as archive:
        assert archive.testzip() is None
        content = json.loads(archive.read('content.json'))
        manifest = json.loads(archive.read('manifest.json'))['file-entries']
        assert set(manifest) == set(archive.namelist())
        return content[0], {name: archive.read(name) for name in archive.namelist()}


def test_real_archive_preserves_tree_text_notes_links_fold_and_original_images(tmp_path):
    path, png = tmp_path / '测试脑图.xmind', _png()
    export_mindmap(_tree(), path, attachments={7: ('参考.png', 'image/png', png)})
    sheet, files = _read(path)
    root = sheet['rootTopic']
    assert root['title'] == '计划 <主角> & 音乐\n第二行'
    assert root['notes']['plain']['content'] == '第一行\n示例 <shader> & 参数'
    assert root['hyperlink'] == 'https://example.com/?a=1&b=2'
    assert root['hyperlinkTitle'] == '参考文档'
    assert root['labels'] == ['角色', '动画']
    assert root['structureClass'] == 'org.xmind.ui.logic.right'
    children = root['children']['attached']
    assert [node['title'] for node in children] == ['制作', '渲染 & 光照\n测试 ★']
    assert children[0]['collapsed'] is True
    branch = children[0]['children']['attached'][0]
    assert branch['title'] == '模型'
    leaf = branch['children']['attached'][0]
    assert leaf['title'] == '贴图 😀'
    stack, ids = [root], set()
    while stack:
        topic = stack.pop()
        assert topic['id'] not in ids
        ids.add(topic['id'])
        stack.extend(topic.get('children', {}).get('attached', []))
    assert len(ids) == 5
    resources = [name for name in files if name.startswith('resources/')]
    assert len(resources) == 1  # Shared source picture is packaged only once.
    assert root['image']['src'] == leaf['image']['src'] == 'xap:' + resources[0]
    assert root['image']['width'] == 48 and root['image']['height'] == 24
    assert hashlib.sha256(files[resources[0]]).digest() == hashlib.sha256(png).digest()
    with Image.open(io.BytesIO(files[resources[0]])) as decoded:
        assert decoded.size == (16, 8)


def test_plain_text_is_not_guessed_to_be_html():
    text = 'x <shader> y &amp; &#9733;\n  keep spaces'
    assert node_text({'data': {'text': text}}) == text
    assert node_text({'data': {'text': text, 'richText': False}}) == text
    assert node_text({'data': {'text': '<p>A&nbsp;B</p><p>C<br>D</p>', 'richText': True}}) == 'A B\nC\nD'


def test_current_page_menu_export_resolves_scene_attachments(tmp_path, monkeypatch, qapp):
    from prism.actions import export_workflow
    target, png = tmp_path / 'menu.xmind', _png()
    tree = _tree()
    panel = SimpleNamespace(_scene_tree=lambda: tree)
    view = SimpleNamespace(_current_panel_of=lambda kind: panel,
                           scene=SimpleNamespace(attachments={7: ('source.png', 'image/png', png)}))
    monkeypatch.setattr(export_workflow, '_store_open_panel', lambda view: True)
    monkeypatch.setattr(export_workflow.QtWidgets.QFileDialog, 'getSaveFileName',
                        lambda *args: (str(target), 'XMind (*.xmind)'))
    monkeypatch.setattr(export_workflow.widgets, 'PrismNotification', lambda *args: None)
    export_workflow.export_mindmap_action(view)
    sheet, files = _read(target)
    assert files[sheet['rootTopic']['image']['src'][4:]] == png


def test_missing_image_is_an_explicit_failure_and_does_not_replace_existing_export(tmp_path):
    target = tmp_path / 'existing.xmind'
    target.write_bytes(b'previous export')
    with pytest.raises(MindMapExportError, match='图片数据缺失'):
        export_mindmap(_tree(), target, attachments={})
    assert target.read_bytes() == b'previous export'


def test_the_export_survives_a_round_trip(tmp_path):
    """超链接、折叠状态、纯文本里的 <…> 和换行都得原样回来。"""
    from prism.fileio.import_xmind import xmind_to_tree

    path, png = tmp_path / '往返.xmind', _png()
    export_mindmap(_tree(), path, attachments={7: ('参考.png', 'image/png', png)})
    back = xmind_to_tree(path)

    root = back['data']
    assert root['text'] == '计划 <主角> & 音乐\n第二行'
    assert root['note'] == '第一行\n示例 <shader> & 参数'
    assert root['hyperlink'] == 'https://example.com/?a=1&b=2'
    assert root['hyperlinkTitle'] == '参考文档'
    assert root['tag'] == ['角色', '动画']
    children = back['children']
    assert children[0]['data']['expand'] is False, '折叠状态没有回来'
    assert children[0]['children'][0]['data']['text'] == '模型'
    assert children[1]['data']['text'] == '渲染 & 光照\n测试 ★'


def test_the_menu_export_snapshots_the_attachment_table(tmp_path, monkeypatch, qapp):
    """菜单导出不能拿着活的附件表去写 —— 资源树那条路是冻结过的。

    两条入口得一样：导出期间用户还在改图的话，活表会边写边变。
    """
    from prism.actions import export_workflow
    target, png = tmp_path / 'snap.xmind', _png()
    attachments = {7: ('source.png', 'image/png', png)}
    panel = SimpleNamespace(_scene_tree=_tree)
    view = SimpleNamespace(_current_panel_of=lambda kind: panel,
                           scene=SimpleNamespace(attachments=attachments))
    monkeypatch.setattr(export_workflow, '_store_open_panel', lambda view: True)
    monkeypatch.setattr(export_workflow.QtWidgets.QFileDialog, 'getSaveFileName',
                        lambda *args: (str(target), 'XMind (*.xmind)'))
    monkeypatch.setattr(export_workflow.widgets, 'PrismNotification', lambda *args: None)

    captured = {}
    from prism.fileio import mindmap_export
    real = mindmap_export.export_mindmap

    def spy(tree, path, fmt=None, attachments=None, **kwargs):
        captured['attachments'] = attachments
        return real(tree, path, fmt, attachments, **kwargs)

    monkeypatch.setattr(mindmap_export, 'export_mindmap', spy)
    export_workflow.export_mindmap_action(view)

    assert captured['attachments'] is not attachments, \
        '菜单导出拿的是活的附件表，不是快照'
    assert captured['attachments'] == attachments, '快照的内容必须一样'
    sheet, files = _read(target)
    assert files[sheet['rootTopic']['image']['src'][4:]] == png

