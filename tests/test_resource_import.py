"""把外面的目录扫成导入计划：规则和「拖文件夹进画布」是同一套。

这里测的是**扫描**——目录树要变成什么形状、哪些文件被跳过、压缩包怎么
解。落地（建节点、灌内容、素材归属）在 `tests/test_resource_tree_import.py`。
"""
import os
import zipfile

import pytest

from prism.fileio.resource_import import (
    ResourceImportError, ResourceTreeScanner, extract_archive, scan_files)


def write_tree(root, files):
    """按 {相对路径: 字节} 造一棵目录树。"""
    for relative, payload in files.items():
        path = os.path.join(str(root), *relative.split('/'))
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, 'wb') as handle:
            handle.write(payload if isinstance(payload, bytes) else b'x')


def scan(root, section, name=None):
    source = os.path.join(str(root), name) if name else str(root)
    return ResourceTreeScanner(section).scan(source)


def shapes(plan):
    """{relative_path: node_type}，比对整棵结构用。"""
    return {node.relative_path: node.node_type for node in plan.nodes}


# ── 画布：一层目录一个画布 ─────────────────────────────────


def test_a_flat_folder_becomes_one_canvas(tmpdir):
    write_tree(tmpdir, {'参考/top.png': b'PNG', '参考/clip.mp4': b'MP4'})

    plan = scan(tmpdir, 'canvas', '参考')

    assert [node.node_type for node in plan.nodes] == ['page']
    assert plan.nodes[0].title == '参考'
    assert len(plan.nodes[0].assets) == 2
    assert plan.nodes[0].parent_path is None


def test_every_level_gets_its_own_canvas_and_parent(tmpdir):
    write_tree(tmpdir, {
        '参考/top.png': b'',
        '参考/Stray/ref_001.png': b'',
        '参考/Stray/Control/deep.png': b''})

    plan = scan(tmpdir, 'canvas', '参考')
    pages = {node.relative_path: node for node in plan.pages}
    folders = {node.relative_path for node in plan.folders}

    assert set(pages) == {'', 'Stray/', 'Stray/Control/'}
    assert folders == {'', 'Stray/'}
    assert pages[''].parent_path is None
    assert pages['Stray/'].parent_path == ''
    assert pages['Stray/Control/'].parent_path == 'Stray/'
    # 父在前：建节点时父节点必须先存在
    order = [node.relative_path for node in plan.nodes]
    assert order.index('Stray/') < order.index('Stray/Control/')


def test_a_folder_without_media_gets_no_empty_canvas(tmpdir):
    write_tree(tmpdir, {'参考/middle/Stray/x.png': b''})

    plan = scan(tmpdir, 'canvas', '参考')

    # middle 自己没有媒体：不建空画布，但层级要撑住，所以有文件夹
    assert [node.title for node in plan.pages] == ['Stray']
    assert {node.relative_path for node in plan.folders} == {'', 'middle/'}
    assert plan.pages[0].parent_path == 'middle/'


def test_a_leaf_folder_with_media_gets_only_a_canvas(tmpdir):
    write_tree(tmpdir, {'参考/Stray/x.png': b''})

    plan = scan(tmpdir, 'canvas', '参考')

    assert shapes(plan) == {'': 'folder', 'Stray/': 'page'}


def test_assets_are_collected_per_folder(tmpdir):
    write_tree(tmpdir, {'参考/a.png': b'', '参考/Stray/b.png': b''})

    plan = scan(tmpdir, 'canvas', '参考')
    by_path = {node.relative_path: node for node in plan.pages}

    assert len(by_path[''].assets) == 1
    assert len(by_path['Stray/'].assets) == 1
    assert plan.asset_files == tuple(
        path for node in plan.pages for path in node.assets)


# ── 文档 / 脑图：文件成页，目录成文件夹 ─────────────────────


def test_documents_become_pages_inside_folders(tmpdir):
    write_tree(tmpdir, {'报告/周报.docx': b'',
                        '报告/月报/三月.docx': b''})

    plan = scan(tmpdir, 'document', '报告')
    pages = {node.title: node for node in plan.pages}

    assert pages['周报'].source_file.endswith('周报.docx')
    assert pages['周报'].parent_path == ''
    assert pages['三月'].parent_path == '月报/'
    assert {node.relative_path for node in plan.folders} == {'', '月报/'}


def test_mindmaps_become_pages(tmpdir):
    write_tree(tmpdir, {'计划/大纲.xmind': b''})

    plan = scan(tmpdir, 'mindmap', '计划')

    assert [node.title for node in plan.pages] == ['大纲']
    assert plan.pages[0].page_kind == 'mindmap'


def test_files_of_another_section_are_skipped(tmpdir):
    write_tree(tmpdir, {'报告/周报.docx': b'', '报告/pic.png': b''})

    plan = scan(tmpdir, 'document', '报告')

    assert [path for path, _reason in plan.skipped] == ['pic.png']
    assert 'Skipped 1 file' in plan.notes[0]


def test_a_folder_holding_only_other_files_is_refused(tmpdir):
    write_tree(tmpdir, {'随便/note.txt': b''})

    with pytest.raises(ResourceImportError):
        scan(tmpdir, 'document', '随便')


def test_titles_are_cleaned_and_trimmed(tmpdir):
    write_tree(tmpdir, {f'{"长" * 100}.docx': b''})

    plan = scan(tmpdir, 'document')

    assert len(plan.nodes[0].title) <= 76


def test_a_missing_folder_is_refused(tmpdir):
    with pytest.raises(ResourceImportError):
        ResourceTreeScanner('document').scan(
            os.path.join(str(tmpdir), '不存在'))


# ── 散装文件 ───────────────────────────────────────────────


def test_scan_files_makes_one_page_per_file(tmpdir):
    write_tree(tmpdir, {'a.docx': b'', 'b.docx': b''})
    paths = [os.path.join(str(tmpdir), name) for name in ('a.docx', 'b.docx')]

    plan = scan_files('document', paths)

    assert [node.title for node in plan.nodes] == ['a', 'b']
    assert all(node.parent_path is None for node in plan.nodes)


def test_scan_files_refuses_foreign_types(tmpdir):
    write_tree(tmpdir, {'a.txt': b''})

    with pytest.raises(ResourceImportError):
        scan_files('document', [os.path.join(str(tmpdir), 'a.txt')])


def test_scan_files_refuses_the_canvas_section():
    with pytest.raises(ResourceImportError):
        scan_files('canvas', ['x.png'])


# ── 压缩包 ─────────────────────────────────────────────────


def make_archive(tmpdir, entries, name='x.zip'):
    path = os.path.join(str(tmpdir), name)
    with zipfile.ZipFile(path, 'w') as archive:
        for entry, payload in entries.items():
            archive.writestr(entry, payload)
    return path


def test_extract_strips_the_single_top_folder(tmpdir):
    """导出的 zip 里有一层「根名/」，导入时剥掉它，不再套一层。"""
    archive = make_archive(tmpdir, {'文档/笔记.docx': b'x'})

    root = extract_archive(archive, os.path.join(str(tmpdir), 'out'))

    assert root == os.path.join(str(tmpdir), 'out', '文档')
    assert os.path.isfile(os.path.join(root, '笔记.docx'))


def test_extract_keeps_several_top_entries(tmpdir):
    archive = make_archive(tmpdir, {'a.docx': b'x', 'b/c.docx': b'x'})

    root = extract_archive(archive, os.path.join(str(tmpdir), 'out'))

    assert root == os.path.join(str(tmpdir), 'out')
    assert sorted(os.listdir(root)) == ['a.docx', 'b']


def test_extract_refuses_a_path_traversal(tmpdir):
    archive = make_archive(tmpdir, {'../evil.txt': b'x'})

    with pytest.raises(ResourceImportError):
        extract_archive(archive, os.path.join(str(tmpdir), 'out'))


def test_extract_refuses_an_absolute_path(tmpdir):
    archive = make_archive(tmpdir, {'/evil.txt': b'x'})

    with pytest.raises(ResourceImportError):
        extract_archive(archive, os.path.join(str(tmpdir), 'out'))


def test_extract_refuses_too_many_entries(tmpdir, monkeypatch):
    monkeypatch.setattr('prism.fileio.resource_import.MAX_ARCHIVE_ENTRIES', 2)
    archive = make_archive(tmpdir, {'a.docx': b'', 'b.docx': b'',
                                    'c.docx': b''})

    with pytest.raises(ResourceImportError):
        extract_archive(archive, os.path.join(str(tmpdir), 'out'))


def test_extract_refuses_a_broken_archive(tmpdir):
    path = os.path.join(str(tmpdir), 'broken.zip')
    with open(path, 'wb') as handle:
        handle.write(b'not a zip at all')

    with pytest.raises(ResourceImportError):
        extract_archive(path, os.path.join(str(tmpdir), 'out'))


# ── 导数：导出的东西再导入回来，还是原来那样 ────────────────


def test_a_document_subtree_survives_a_round_trip(tmpdir):
    from prism.fileio.resource_export import (ResourceSubtreeExporter,
                                              write_zip)
    from prism.workspace_service import ResourceTreeService

    service = ResourceTreeService('p1')
    folder = service.createFolder('document', '报告')
    service.createPage('document', '周报', 'doc-1', folder['id'])
    service.createPage('document', '月报', 'doc-2', folder['id'])
    pages = [{'id': 'doc-1', 'kind': 'document', 'title': '周报',
              'content': '<p>a</p>', 'tags': []},
             {'id': 'doc-2', 'kind': 'document', 'title': '月报',
              'content': '<p>b</p>', 'tags': []}]

    plan = ResourceSubtreeExporter(service, pages=pages).build_plan(
        'document', folder['id'])
    archive = os.path.join(str(tmpdir), 'out.zip')
    write_zip(plan, archive)

    root = extract_archive(archive, os.path.join(str(tmpdir), 'unpacked'))
    back = ResourceTreeScanner('document').scan(root,
                                                os.path.basename(root))

    assert [node.title for node in back.folders] == ['报告']
    assert sorted(node.title for node in back.pages) == ['周报', '月报']
    assert all(node.parent_path == '' for node in back.pages)


def test_a_canvas_subtree_survives_a_round_trip(tmpdir):
    from prism.fileio.resource_export import (ResourceSubtreeExporter,
                                              write_zip)
    from prism.workspace_service import ResourceTreeService

    service = ResourceTreeService('p1')
    service.createPage('canvas', '素材画布', 'canvas-1')
    payload = b'\x89PNG\r\n\x1a\n' + b'0' * 32
    asset = os.path.join(str(tmpdir), 'top.png')
    with open(asset, 'wb') as handle:
        handle.write(payload)

    class Item:
        TYPE = 'pixmap'
        filename = asset
        save_id = 1

        def original_bytes(self):
            return payload

        def original_format(self):
            return 'png'

    plan = ResourceSubtreeExporter(
        service, canvas_items=lambda page_id: [Item()] if page_id ==
        'canvas-1' else []).build_plan('canvas')
    archive = os.path.join(str(tmpdir), 'canvas.zip')
    write_zip(plan, archive)

    root = extract_archive(archive, os.path.join(str(tmpdir), 'unpacked'))
    back = ResourceTreeScanner('canvas').scan(root, os.path.basename(root))

    assert [node.title for node in back.pages] == ['素材画布']
    assert len(back.pages[0].assets) == 1
    with open(back.pages[0].assets[0], 'rb') as handle:
        assert handle.read() == payload
