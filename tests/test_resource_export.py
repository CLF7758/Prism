"""左侧树的「导出…」：把一棵子树算成文件，再写进文件夹或 .zip。

这一层只认数据（节点、页面、素材），不碰控件，所以这里测的是导出这件事
本身的约定：

* 文件夹 → 子目录，页面 → 文件，形状和左侧树一致
* 文档出 ``.docx``、脑图出 ``.xmind``
* 画布里的图片 / 视频按**原来那份字节**写出去（"无损导出"）
* 没有原始字节的素材照实说明，不假装无损
* 文件夹和 .zip 两条出口写出的是同一份东西
"""
import json
import os
import zipfile

import pytest

from PyQt6 import QtCore

from prism.fileio.resource_export import (
    SECTION_TITLES, ResourceExportError, ResourceSubtreeExporter,
    write_directory, write_zip)
from prism.workspace_service import ResourceTreeService


class FakeItem:
    """一个够用的素材：导出只需要它自己的字节和名字。"""

    TYPE = 'pixmap'

    def __init__(self, filename='photo.png', payload=b'PNG-BYTES',
                 save_id=1, image_format='png'):
        self.filename = filename
        self.save_id = save_id
        self._payload = payload
        self._format = image_format

    def original_bytes(self):
        return self._payload

    def original_format(self):
        return self._format

    def pixmap_to_bytes(self):
        return b'REENCODED', 'png'


class FakeVideo:
    TYPE = 'video'

    def __init__(self, filename='clip.mp4', blob=None, url=None, save_id=7):
        self.filename = filename
        self.save_id = save_id
        self._video_blob = blob
        self._video_url = url


class FakePenStroke:
    """画布上的标注（画笔线）：不是要带走的东西。"""

    TYPE = 'path'


def document_page(page_id, title='未命名文档', html='<p>你好</p>'):
    return {'id': page_id, 'kind': 'document', 'title': title,
            'content': html, 'tags': []}


def mindmap_page(page_id, title='未命名脑图', tree=None):
    return {'id': page_id, 'kind': 'mindmap', 'title': title,
            'content': tree if tree is not None else
            {'data': {'text': '根节点'}, 'children': []},
            'tags': []}


def canvas_page(page_id, title='未命名画布'):
    return {'id': page_id, 'kind': 'canvas', 'title': title,
            'content': None, 'tags': []}


def exporter_for(service, pages=(), assets=None):
    return ResourceSubtreeExporter(
        service, pages=pages,
        canvas_items=lambda page_id: (assets or {}).get(page_id, ()))


# ── 形状：文件夹变目录，页面变文件 ───────────────────────────────


def test_folder_becomes_a_directory_and_pages_become_files():
    service = ResourceTreeService('p1')
    folder = service.createFolder('document', '章节')
    service.createPage('document', '第一章', 'doc-1', folder['id'])
    exporter = exporter_for(service, [document_page('doc-1')])

    plan = exporter.build_plan('document')

    assert plan.root_name == SECTION_TITLES['document']
    assert plan.directories == ('章节',)
    assert [entry.relative_path for entry in plan.entries] == \
        ['章节/第一章.docx']


def test_exporting_a_folder_keeps_grandchildren():
    service = ResourceTreeService('p1')
    outer = service.createFolder('mindmap', '外层')
    inner = service.createFolder('mindmap', '内层', outer['id'])
    service.createPage('mindmap', '叶子', 'mind-1', inner['id'])
    exporter = exporter_for(service, [mindmap_page('mind-1')])

    plan = exporter.build_plan('mindmap', outer['id'])

    assert plan.root_name == '外层'
    assert plan.directories == ('内层',)
    assert [entry.relative_path for entry in plan.entries] == ['内层/叶子.xmind']


def test_section_root_exports_every_child_of_that_section():
    service = ResourceTreeService('p1')
    service.createPage('canvas', '甲画布', 'canvas-1')
    service.createFolder('canvas', '乙组')
    service.createPage('mindmap', '别的分区', 'mind-1')
    exporter = exporter_for(
        service, [canvas_page('canvas-1'), mindmap_page('mind-1')],
        assets={'canvas-1': [FakeItem()]})

    plan = exporter.build_plan('canvas')

    names = sorted([*plan.directories, *(e.relative_path
                                         for e in plan.entries)])
    assert names == ['乙组', '甲画布', '甲画布/photo.png']


def test_empty_folder_is_still_created_on_disk(tmpdir):
    service = ResourceTreeService('p1')
    service.createFolder('document', '空文件夹')
    exporter = exporter_for(service)

    write_directory(exporter.build_plan('document'), str(tmpdir))

    assert os.path.isdir(os.path.join(str(tmpdir), '文档', '空文件夹'))


def test_missing_node_is_refused():
    service = ResourceTreeService('p1')
    exporter = exporter_for(service)

    with pytest.raises(ResourceExportError):
        exporter.build_plan('canvas', 'nope')


def test_deleted_node_is_refused():
    service = ResourceTreeService('p1')
    folder = service.createFolder('canvas', '旧文件夹')
    service.trash([folder['id']], '2026-01-01T00:00:00Z')
    exporter = exporter_for(service)

    with pytest.raises(ResourceExportError):
        exporter.build_plan('canvas', folder['id'])


def test_titles_with_characters_windows_refuses_are_cleaned():
    service = ResourceTreeService('p1')
    service.createPage('document', 'A:B?C*D', 'doc-1')
    exporter = exporter_for(service, [document_page('doc-1')])

    plan = exporter.build_plan('document')

    assert [entry.relative_path for entry in plan.entries] == ['A_B_C_D.docx']


def test_two_nodes_that_would_collide_get_numbered():
    """画布页和文件夹同名时，两者都会变成目录 —— 第二个要错开。"""
    service = ResourceTreeService('p1')
    service.createFolder('canvas', '同名')
    service.createPage('canvas', '同名', 'canvas-1')
    exporter = exporter_for(service, [canvas_page('canvas-1')],
                            assets={'canvas-1': [FakeItem()]})

    plan = exporter.build_plan('canvas')

    assert plan.directories == ('同名', '同名 (2)')
    assert [entry.relative_path for entry in plan.entries] == \
        ['同名 (2)/photo.png']


# ── 右键一个页面：导出的就是它自己 ───────────────────────────


def test_exporting_one_document_page_exports_that_page():
    service = ResourceTreeService('p1')
    page = service.createPage('document', '第一章', 'doc-1')
    exporter = exporter_for(service, [document_page('doc-1')])

    plan = exporter.build_plan('document', page['id'])

    assert plan.root_name == '第一章'
    assert [entry.relative_path for entry in plan.entries] == ['第一章.docx']


def test_exporting_one_mindmap_page_exports_that_page():
    service = ResourceTreeService('p1')
    page = service.createPage('mindmap', '计划', 'mind-1')
    exporter = exporter_for(service, [mindmap_page('mind-1')])

    plan = exporter.build_plan('mindmap', page['id'])

    assert plan.root_name == '计划'
    assert [entry.relative_path for entry in plan.entries] == ['计划.xmind']


def test_exporting_one_canvas_page_puts_its_assets_in_the_root(tmpdir):
    service = ResourceTreeService('p1')
    page = service.createPage('canvas', '素材画布', 'canvas-1')
    exporter = exporter_for(
        service, [canvas_page('canvas-1')],
        assets={'canvas-1': [FakeItem()]})

    plan = exporter.build_plan('canvas', page['id'])
    write_directory(plan, str(tmpdir))

    assert plan.directories == ()
    assert [entry.relative_path for entry in plan.entries] == ['photo.png']
    assert os.path.isfile(os.path.join(str(tmpdir), '素材画布', 'photo.png'))


# ── 文档 → .docx，脑图 → .xmind ──────────────────────────────


def test_document_is_written_as_a_word_file(tmpdir):
    pytest.importorskip('docx')
    service = ResourceTreeService('p1')
    service.createPage('document', '第一章', 'doc-1')
    exporter = exporter_for(service, [document_page('doc-1', html='<p>你好</p>')])

    write_directory(exporter.build_plan('document'), str(tmpdir))

    target = os.path.join(str(tmpdir), '文档', '第一章.docx')
    assert os.path.isfile(target)
    with zipfile.ZipFile(target) as archive:
        assert 'word/document.xml' in archive.namelist()


def test_mindmap_is_written_as_an_xmind_archive(tmpdir):
    service = ResourceTreeService('p1')
    service.createPage('mindmap', '计划', 'mind-1')
    exporter = exporter_for(service, [mindmap_page('mind-1')])

    write_directory(exporter.build_plan('mindmap'), str(tmpdir))

    target = os.path.join(str(tmpdir), '脑图', '计划.xmind')
    with zipfile.ZipFile(target) as archive:
        content = json.loads(archive.read('content.json').decode('utf-8'))
    assert content[0]['rootTopic']['title'] == '根节点'


def test_empty_mindmap_is_skipped_with_a_note(tmpdir):
    service = ResourceTreeService('p1')
    service.createPage('mindmap', '空的', 'mind-1')
    page = mindmap_page('mind-1')
    page['content'] = None
    exporter = exporter_for(service, [page])

    plan = exporter.build_plan('mindmap')

    assert plan.entries == ()
    assert any('empty mind map' in note for note in plan.notes)
    assert not os.path.exists(os.path.join(str(tmpdir), '脑图', '空的.xmind'))


# ── 画布素材：原格式、原字节 ────────────────────────────────


def test_canvas_assets_are_written_byte_for_byte(tmpdir):
    source = b'\x89PNG\r\n\x1a\n' + bytes(range(256))
    service = ResourceTreeService('p1')
    service.createPage('canvas', '素材画布', 'canvas-1')
    exporter = exporter_for(
        service, [canvas_page('canvas-1')],
        assets={'canvas-1': [FakeItem('风景.png', source, save_id=1)]})

    plan = exporter.build_plan('canvas')
    write_directory(plan, str(tmpdir))

    assert plan.lossless_assets == 1
    assert plan.reencoded_assets == 0
    with open(os.path.join(str(tmpdir), '画布', '素材画布', '风景.png'),
              'rb') as handle:
        assert handle.read() == source


def test_asset_without_original_bytes_is_reported_not_pretended():
    service = ResourceTreeService('p1')
    service.createPage('canvas', '素材画布', 'canvas-1')
    exporter = exporter_for(
        service, [canvas_page('canvas-1')],
        assets={'canvas-1': [FakeItem('画出来的.png', payload=None)]})

    plan = exporter.build_plan('canvas')

    assert plan.reencoded_assets == 1
    assert plan.lossless_assets == 0
    assert any('no original bytes' in warning for warning in plan.warnings)


def test_referenced_video_is_copied_untouched(tmpdir):
    clip = os.path.join(str(tmpdir), 'clip.mp4')
    with open(clip, 'wb') as handle:
        handle.write(b'MP4-DATA')
    service = ResourceTreeService('p1')
    service.createPage('canvas', '素材画布', 'canvas-1')
    exporter = exporter_for(
        service, [canvas_page('canvas-1')],
        assets={'canvas-1': [FakeVideo(
            url=QtCore.QUrl.fromLocalFile(clip))]})

    plan = exporter.build_plan('canvas')
    entry = plan.entries[0]
    assert os.path.normpath(entry.source_path) == os.path.normpath(clip)

    write_directory(plan, str(tmpdir))
    with open(os.path.join(str(tmpdir), '画布', '素材画布', 'clip.mp4'),
              'rb') as handle:
        assert handle.read() == b'MP4-DATA'


def test_embedded_video_keeps_its_bytes(tmpdir):
    service = ResourceTreeService('p1')
    service.createPage('canvas', '素材画布', 'canvas-1')
    exporter = exporter_for(
        service, [canvas_page('canvas-1')],
        assets={'canvas-1': [FakeVideo(blob=b'EMBEDDED-MP4')]})

    write_directory(exporter.build_plan('canvas'), str(tmpdir))

    with open(os.path.join(str(tmpdir), '画布', '素材画布', 'clip.mp4'),
              'rb') as handle:
        assert handle.read() == b'EMBEDDED-MP4'


def test_video_whose_file_is_gone_is_reported():
    missing = os.path.join('Z:', os.sep, 'nowhere', 'gone.mp4')
    service = ResourceTreeService('p1')
    service.createPage('canvas', '素材画布', 'canvas-1')
    exporter = exporter_for(
        service, [canvas_page('canvas-1')],
        assets={'canvas-1': [FakeVideo(
            url=QtCore.QUrl.fromLocalFile(missing))]})

    plan = exporter.build_plan('canvas')

    assert plan.entries == ()
    assert plan.skipped_assets == ('clip.mp4',)
    assert any('could not be exported' in warning for warning in plan.warnings)


def test_annotations_are_not_part_of_the_export():
    service = ResourceTreeService('p1')
    service.createPage('canvas', '素材画布', 'canvas-1')
    exporter = exporter_for(
        service, [canvas_page('canvas-1')],
        assets={'canvas-1': [FakePenStroke(), FakeItem()]})

    plan = exporter.build_plan('canvas')

    assert [entry.relative_path for entry in plan.entries] == \
        ['素材画布/photo.png']


def test_asset_without_a_name_falls_back_to_its_save_id():
    service = ResourceTreeService('p1')
    service.createPage('canvas', '素材画布', 'canvas-1')
    exporter = exporter_for(
        service, [canvas_page('canvas-1')],
        assets={'canvas-1': [FakeItem(filename='', save_id=42)]})

    plan = exporter.build_plan('canvas')

    assert plan.entries[0].relative_path == '素材画布/0042.png'


# ── 两个出口写出同一份东西 ─────────────────────────────────


def _relative_paths(root):
    found = set()
    for directory, _dirs, files in os.walk(root):
        for name in files:
            full = os.path.join(directory, name)
            found.add(os.path.relpath(full, root).replace(os.sep, '/'))
    return found


def test_zip_holds_exactly_what_the_folder_holds(tmpdir):
    service = ResourceTreeService('p1')
    folder = service.createFolder('canvas', '组')
    service.createPage('canvas', '画布甲', 'canvas-1', folder['id'])
    service.createPage('document', '文档甲', 'doc-1')
    pages = [canvas_page('canvas-1'), document_page('doc-1')]
    exporter = exporter_for(
        service, pages,
        assets={'canvas-1': [FakeItem('图.png', b'X' * 10)]})

    plan = exporter.build_plan('canvas', folder['id'])
    folder_root = write_directory(plan, os.path.join(str(tmpdir), 'out'))
    zip_path = os.path.join(str(tmpdir), 'out.zip')
    write_zip(plan, zip_path)

    with zipfile.ZipFile(zip_path) as archive:
        names = {name for name in archive.namelist()
                 if not name.endswith('/')}
    assert names == {f'{plan.root_name}/{relative}'
                     for relative in _relative_paths(folder_root)}


def test_zip_keeps_empty_folders(tmpdir):
    service = ResourceTreeService('p1')
    service.createFolder('document', '空文件夹')
    exporter = exporter_for(service)

    zip_path = os.path.join(str(tmpdir), 'out.zip')
    write_zip(exporter.build_plan('document'), zip_path)

    with zipfile.ZipFile(zip_path) as archive:
        assert '文档/空文件夹/' in archive.namelist()
