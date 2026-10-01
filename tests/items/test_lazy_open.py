"""T5：打开工程时不要解码所有原图。

原来的 load_stored_image 在预览缓存未命中时会把原图整张解码。打开一个
上千张素材的工程时，每个素材都走这条路 —— 于是"打开"等于"解码一千张
全尺寸图"，正是任务书 T5 第一条明令禁止的行为，也是峰值内存的主要来源。

现在缓存未命中时只解码到预览尺寸：文件头拿原始尺寸（不解码像素），
然后让解码器边解边缩。
"""
import pytest
from PyQt6 import QtCore, QtGui

from prism import thumbnail_cache
from prism.items import PrismPixmapItem


def make_png(width, height):
    """造一张内容唯一的 PNG。

    内容必须是唯一的：缓存以内容摘要为键，如果两次造出的字节一样，
    第二次就会命中缓存，测的就不是"未命中"那条路了。
    """
    image = QtGui.QImage(width, height, QtGui.QImage.Format.Format_ARGB32)
    image.fill(QtCore.Qt.GlobalColor.transparent)
    for y in range(0, height, 5):
        for x in range(0, width, 5):
            image.setPixelColor(x, y, QtGui.QColor(
                (x * 7 + width) % 256,
                (y * 13 + height) % 256,
                (x * y + width * height) % 256))
    buffer = QtCore.QBuffer()
    buffer.open(QtCore.QIODevice.OpenModeFlag.WriteOnly)
    assert image.save(buffer, 'PNG'), '测试自己造的图必须能编码'
    return bytes(buffer.data())


@pytest.fixture
def isolated_cache(tmp_path, monkeypatch):
    """用临时目录的缓存，别碰用户的真实缓存。"""
    made = thumbnail_cache.ThumbnailCache(directory=str(tmp_path))
    monkeypatch.setattr(thumbnail_cache, 'cache', lambda: made)
    return made


@pytest.fixture
def item(qapp):
    """注意必须依赖 qapp。

    PrismPixmapItem 构造时会建 PrismSettings()，没有 QApplication 实例
    的话进程会直接崩掉（0xC0000409，连错误信息都来不及打印）。
    """
    blank = QtGui.QImage(1, 1, QtGui.QImage.Format.Format_ARGB32)
    return PrismPixmapItem(blank, 'stored.png')


# ── decode_preview_image：纯解码，不碰缓存 ────────────────────────────

def test_a_big_image_decodes_straight_down_to_preview_size():
    blob = make_png(640, 480)
    image, size = thumbnail_cache.decode_preview_image(blob, max_side=64)

    assert size == QtCore.QSize(640, 480), '原始尺寸要如实报出来'
    assert (image.width(), image.height()) == (64, 48), '要等比缩小'


def test_a_small_image_is_left_alone():
    blob = make_png(40, 30)
    image, size = thumbnail_cache.decode_preview_image(blob, max_side=256)

    assert size == QtCore.QSize(40, 30)
    assert (image.width(), image.height()) == (40, 30), '小图不该被放大'


def test_broken_data_yields_nothing():
    for blob in (b'', b'not an image at all', b'\x89PNG\r\n\x1a\n' + b'x' * 40):
        assert thumbnail_cache.decode_preview_image(blob) == (None, None)


def test_a_wide_image_keeps_its_aspect_ratio():
    blob = make_png(1000, 100)
    image, size = thumbnail_cache.decode_preview_image(blob, max_side=256)

    assert size == QtCore.QSize(1000, 100)
    assert image.width() == 256
    assert image.height() == pytest.approx(25.6, abs=1.5)


# ── load_stored_image：缓存未命中时 ──────────────────────────────────

def test_opening_does_not_decode_the_full_size_image(item, isolated_cache):
    """★ T5 的核心：没缓存时打开，也不该解出全尺寸位图。"""
    blob = make_png(900, 700)

    assert item.load_stored_image(blob) is True

    assert item.has_full_pixmap() is False, \
        '打开时解出全尺寸位图正是要消除的行为'
    assert item._preview_pixmap is not None, \
        '要有预览顶上，否则画布上是空的'


def test_the_original_pixel_size_is_remembered_for_layout(item, isolated_cache):
    """布局、裁剪、缩放都靠 _image_size，它必须是原始尺寸而不是预览尺寸。"""
    blob = make_png(900, 700)

    item.load_stored_image(blob)

    assert item._image_size == QtCore.QSize(900, 700)


def test_the_original_bytes_survive_for_export(item, isolated_cache):
    """用户硬要求：导出必须还是原图，不能降质量。"""
    blob = make_png(900, 700)

    item.load_stored_image(blob)

    assert item.original_bytes() == blob
    assert item.has_original_source() is True


def test_a_thousand_previews_cost_tens_of_megabytes_not_hundreds(
        qapp, isolated_cache):
    """内存账：这是 T5 要省下来的东西，用数字钉住。"""
    from prism.thumbnail_cache import PREVIEW_MAX_SIDE

    blank = QtGui.QImage(1, 1, QtGui.QImage.Format.Format_ARGB32)
    item = PrismPixmapItem(blank, 'stored.png')
    item.load_stored_image(make_png(1200, 900))

    preview = item._preview_pixmap
    pixels = preview.width() * preview.height()
    assert pixels <= PREVIEW_MAX_SIDE ** 2

    # 如实记录实测数字：256px 长边的预览，一千张约 190 MB；同样一千张
    # 按 1200x900 全尺寸解码是 4 GB，差了二十多倍。
    # 但 190 MB 本身也不小 —— 1563 张素材的工程里预览是内存大头之一，
    # 所以 PREVIEW_MAX_SIDE 的取值值得单独评估，别当成已经解决了。
    thousand_mb = pixels * 4 * 1000 / 1024 / 1024
    assert thousand_mb < 250, f'一千张预览要 {thousand_mb:.0f} MB，太多了'

    full_mb = 1200 * 900 * 4 * 1000 / 1024 / 1024
    assert thousand_mb < full_mb / 10, '预览至少要比全尺寸省一个数量级'


# ── 缓存命中那条路不能坏 ──────────────────────────────────────────────

def test_a_cached_preview_is_used_and_keeps_the_pixel_size(
        item, isolated_cache):
    blob = make_png(800, 600)
    image = QtGui.QImage(200, 150, QtGui.QImage.Format.Format_ARGB32)
    image.fill(QtCore.Qt.GlobalColor.blue)
    assert isolated_cache.store(thumbnail_cache.digest(blob), image,
                                800, 600)

    assert item.load_stored_image(blob) is True
    assert item.has_full_pixmap() is False
    assert item._image_size == QtCore.QSize(800, 600)


def test_the_blob_is_queued_so_the_next_open_is_faster(item, isolated_cache):
    """这次解码出来的预览要进缓存队列，下次打开就不用再做一遍。"""
    blob = make_png(900, 700)
    before = thumbnail_cache.pending_count()

    item.load_stored_image(blob)

    assert thumbnail_cache.pending_count() == before + 1
    assert thumbnail_cache.digest(blob) in [key for key, _ in
                                            thumbnail_cache._pending[-1:]]


def test_broken_data_does_not_load(item, isolated_cache):
    assert item.load_stored_image(b'nonsense') is False


# ── T5 验收：连续切换页面后内存不会无限增长 ──────────────────────────

def test_switching_canvases_does_not_grow_the_scene(main_window):
    """T5 验收要求之一。

    所有画布共用一个 scene，靠 item._canvas_id 区分归属，切换只是改
    current_canvas_id 再过滤一遍 —— 所以来回切多少次，场景里的对象数
    都该原样。如果哪天改成切换时重建画布对象，这条会立刻红。
    """
    from prism.items import PrismPixmapItem

    window = main_window
    view = window.view
    scene = view.scene

    scene.workspace_pages = [
        {'id': 'canvas-a', 'kind': 'canvas', 'title': 'A'},
        {'id': 'canvas-b', 'kind': 'canvas', 'title': 'B'},
    ]
    window._reload_workspace_pages()

    for canvas in ('canvas-a', 'canvas-b'):
        for index in range(3):
            blank = QtGui.QImage(4, 4, QtGui.QImage.Format.Format_ARGB32)
            made = PrismPixmapItem(blank, f'{canvas}-{index}.png')
            made._canvas_id = canvas
            scene.addItem(made)

    before = len(scene.items())

    counts = []
    for _ in range(10):
        window.open_workspace_page('canvas', 'canvas-a')
        counts.append(len(scene.items()))
        window.open_workspace_page('canvas', 'canvas-b')
        counts.append(len(scene.items()))

    assert set(counts) == {before}, \
        f'切换画布后场景对象数变了：{before} → {sorted(set(counts))}'


def test_switching_canvases_keeps_the_previews_shared(main_window):
    """切回来不该重新构造素材对象（也就不会多占一份预览内存）。"""
    from prism.items import PrismPixmapItem

    window = main_window
    view = window.view
    scene = view.scene

    scene.workspace_pages = [
        {'id': 'canvas-a', 'kind': 'canvas', 'title': 'A'},
        {'id': 'canvas-b', 'kind': 'canvas', 'title': 'B'},
    ]
    window._reload_workspace_pages()

    blank = QtGui.QImage(4, 4, QtGui.QImage.Format.Format_ARGB32)
    mine = PrismPixmapItem(blank, 'a.png')
    mine._canvas_id = 'canvas-a'
    scene.addItem(mine)
    identity = id(mine)

    for _ in range(5):
        window.open_workspace_page('canvas', 'canvas-b')
        window.open_workspace_page('canvas', 'canvas-a')

    same = [i for i in scene.items() if id(i) == identity]
    assert len(same) == 1, '同一个素材在切换后不该变成两个对象'
