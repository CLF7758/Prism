"""T9-3：AssetImportService（分类、附件存储、汇总）。

服务不碰控件，所以这些测试不需要窗口。
"""
import pytest
from PyQt6 import QtCore

from prism.asset_import_service import (IMAGE, MODEL, OTHER, VIDEO,
                                        ImportReport, classify, classify_all,
                                        drop_attachments, mime_for_suffix,
                                        next_attachment_id, plan,
                                        referenced_attachments,
                                        store_attachment)


# ── 分类 ─────────────────────────────────────────────────────────

@pytest.mark.parametrize('name', ['a.png', 'b.JPG', 'c.webp', 'd.exr',
                                  'e.psd', 'f.tiff'])
def test_images_are_images(name):
    assert classify(name) == IMAGE


@pytest.mark.parametrize('name', ['v.mp4', 'v.MKV', 'v.mov', 'v.webm',
                                  'v.avi'])
def test_videos_are_videos(name):
    assert classify(name) == VIDEO


def test_glb_is_a_model_not_an_image():
    """`.glb` 也出现在 main_controls._IMAGE_EXTS 里。

    在那条路上那样没问题 —— 那个集合的意思是"能丢进来的文件"，而 3D
    模型确实是丢进来的。但做分类时它必须算 3D：它走的是 PrismGlbItem，
    和图片完全不是一回事。
    """
    assert classify('model.glb') == MODEL
    assert classify('model.GLB') == MODEL
    assert classify('scene.gltf') == MODEL


def test_unknown_extensions_are_other():
    for name in ('notes.txt', 'archive.zip', 'song.mp3', 'noextension'):
        assert classify(name) == OTHER


def test_full_paths_classify_the_same_as_names():
    assert classify(r'C:\some\folder\a.png') == IMAGE
    assert classify(r'C:\some\folder\v.mp4') == VIDEO


def test_remote_qurl_uses_its_url_path_for_classification():
    """浏览器采集传来的 QUrl 不能被它的 Python repr 误判。"""
    assert classify(QtCore.QUrl('https://example.com/photo.webp?size=large')) \
        == IMAGE


def test_classify_all_keeps_the_order_within_each_group():
    """用户拖进来的顺序常常就是他想看到的摆放顺序。"""
    paths = ['a.png', 'v.mp4', 'b.png', 'notes.txt', 'c.png']

    groups = classify_all(paths)

    assert groups[IMAGE] == ['a.png', 'b.png', 'c.png']
    assert groups[VIDEO] == ['v.mp4']
    assert groups[OTHER] == ['notes.txt']
    assert groups[MODEL] == []


# ── 计划与报告 ───────────────────────────────────────────────────

def test_plan_sorts_paths_without_touching_them():
    report = plan(['a.png', 'v.mp4', 'm.glb', 'readme.txt'])

    assert report.images == ['a.png']
    assert report.videos == ['v.mp4']
    assert report.models == ['m.glb']
    assert [p for p, _reason in report.skipped] == ['readme.txt']
    assert report.accepted == 3
    assert report.total == 4


def test_summary_names_what_it_found():
    report = plan(['a.png', 'b.png', 'v.mp4'])
    text = report.summary()
    assert '2 张图片' in text
    assert '1 个视频' in text


def test_summary_mentions_skipped_and_failed():
    report = plan(['a.png', 'readme.txt'])
    report.errors.append(('bad.png', '读不出来'))
    text = report.summary()
    assert '跳过 1 个' in text
    assert '1 个失败' in text


def test_summary_says_so_when_nothing_fits():
    assert '没有可导入' in plan(['notes.txt']).summary()


def test_failure_detail_lists_names_and_caps_the_list():
    report = ImportReport()
    for index in range(8):
        report.errors.append((f'file{index}.png', '读不出来'))

    lines = report.failure_detail(limit=5)

    assert len(lines) == 6, '五条明细加一行"还有几个"'
    assert 'file0.png' in lines[0]
    assert '读不出来' in lines[0]
    assert '还有 3 个' in lines[-1]


# ── 附件 id ──────────────────────────────────────────────────────

def test_the_first_attachment_id_is_one():
    assert next_attachment_id({}) == 1


def test_ids_do_not_collide_after_a_deletion():
    """用长度当 id 会在删掉中间某个之后撞上已有的。"""
    attachments = {1: ('a', 'image/png', b'x'),
                   2: ('b', 'image/png', b'x'),
                   3: ('c', 'image/png', b'x')}
    del attachments[2]

    assert next_attachment_id(attachments) == 4


# ── 存附件 ───────────────────────────────────────────────────────

def test_storing_returns_the_new_id_and_writes_the_entry():
    attachments = {}
    attachment_id = store_attachment(attachments, b'bytes', 'png')

    assert attachment_id == 1
    name, mime, blob = attachments[1]
    assert name == 'imported.png'
    assert mime == 'image/png'
    assert blob == b'bytes'


def test_jpeg_suffix_maps_to_the_jpeg_mime():
    mime, suffix = mime_for_suffix('.JPG')
    assert mime == 'image/jpeg'
    assert suffix == 'jpg', 'jpg 和 jpeg 要归一到同一个后缀'


def test_svg_gets_its_own_mime():
    assert mime_for_suffix('svg')[0] == 'image/svg+xml'


def test_a_missing_suffix_defaults_to_png():
    assert mime_for_suffix(None) == ('image/png', 'png')


def test_storing_rejects_empty_data():
    with pytest.raises(ValueError):
        store_attachment({}, b'', 'png')


def test_storing_rejects_a_missing_table():
    with pytest.raises(ValueError):
        store_attachment(None, b'x', 'png')


def test_storing_accepts_an_explicit_name():
    attachments = {}
    store_attachment(attachments, b'x', 'jpg', name='photo.jpg')
    assert attachments[1][0] == 'photo.jpg'


def test_storing_works_with_the_lazy_attachment_store():
    """文档/脑图的附件表现在是 StoredAttachments（按需读）。

    写进去的条目必须立刻可读 —— 它本来就在内存里，不该再去读一次文件。
    """
    from prism.fileio.attachments_store import StoredAttachments

    store = StoredAttachments(loader=None)
    attachment_id = store_attachment(store, b'bytes', 'png')

    assert attachment_id == 1
    assert store[attachment_id] == ('imported.png', 'image/png', b'bytes')
    assert store.loaded_count() == 1


# ── 删附件 ───────────────────────────────────────────────────────

def test_dropping_removes_them_and_counts():
    attachments = {1: ('a', 'm', b'x'), 2: ('b', 'm', b'x')}
    assert drop_attachments(attachments, [1, 2, 99]) == 2
    assert attachments == {}


def test_dropping_missing_ids_is_harmless():
    attachments = {1: ('a', 'm', b'x')}
    assert drop_attachments(attachments, [99]) == 0
    assert drop_attachments(attachments, []) == 0
    assert drop_attachments(attachments, None) == 0


# ── 找引用 ───────────────────────────────────────────────────────

def test_references_are_found_in_html():
    html = ('<p>看图</p><img src="prism://attach/3">'
            '<img src="prism://attach/7">')
    assert referenced_attachments(html) == {3, 7}


def test_references_are_found_in_a_nested_tree():
    """脑图存的是嵌套 dict，不是字符串。"""
    tree = {'root': 'A', 'image': 'prism://attach/12',
            'children': [{'root': 'B', 'image': 'prism://attach/13'}]}
    assert referenced_attachments(tree) == {12, 13}


def test_no_references_gives_an_empty_set():
    assert referenced_attachments('<p>没有图</p>') == set()
    assert referenced_attachments(None) == set()


def test_the_pattern_follows_the_shared_prefix():
    """前缀来自附件模块，不在这里写死 —— 改一处要跟着变。"""
    from prism.fileio.attachments import ATTACHMENT_PREFIX

    assert ATTACHMENT_PREFIX == 'prism://attach/'
    assert referenced_attachments(f'{ATTACHMENT_PREFIX}5') == {5}


def test_a_lookalike_url_is_not_a_reference():
    """别的 scheme 上的同一个 id 不该被算进来。"""
    assert referenced_attachments('https://example.com/attach/5') == set()
    assert referenced_attachments('file:///C:/attach/5.png') == set()
