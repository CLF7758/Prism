"""T2 验收：导入什么，导出就必须是什么。

这是 v5 任务书 T2 的核心验收 —— 用户导入大图，导出时不能被降质、
不能被改格式、不能丢字节。做法是走完整链路后比对 sha256：

    造真实文件 → 导入 → 保存工程 → 清空场景 → 重开工程 → 导出原图 → 比哈希

只测函数是不够的：真正可能丢字节的地方在「保存工程」和「重开」这两步，
所以矩阵必须覆盖它们。
"""
import hashlib

import pytest
from PyQt6 import QtGui

from prism.fileio.export import ImagesToDirectoryExporter
from prism.fileio.sql import SQLiteIO
from prism.items import PrismPixmapItem

#: 覆盖有损（jpeg）、无损（png/tiff/bmp）和现代格式（webp）
FORMATS = ['.png', '.jpg', '.webp', '.tiff', '.bmp']


def _make_image(suffix, tmp_path, size=(120, 90)):
    """写一个真实格式的图片文件，返回 (路径, 原始字节)。"""
    image = QtGui.QImage(size[0], size[1], QtGui.QImage.Format.Format_ARGB32)
    image.fill(QtGui.QColor(30, 90, 160))
    painter = QtGui.QPainter(image)
    painter.fillRect(10, 10, 40, 40, QtGui.QColor(240, 200, 60))
    painter.end()
    path = tmp_path / f'source{suffix}'
    assert image.save(str(path)), f'无法写出 {suffix} 测试图（Qt 缺插件？）'
    return path, path.read_bytes()


def _sha256(data):
    return hashlib.sha256(data).hexdigest()


def _import(view, path, data):
    """按现有测试的模式导入：先构造，再挂原始字节。"""
    item = PrismPixmapItem(QtGui.QImage(str(path)), filename=str(path))
    item.set_source_blob(data, path.suffix.lstrip('.'))
    item._canvas_id = 'default-canvas'
    view.scene.addItem(item)
    return item


def _pixmap_items(scene):
    return [i for i in scene.items() if isinstance(i, PrismPixmapItem)]


@pytest.mark.parametrize('suffix', FORMATS)
def test_import_keeps_original_bytes(qapp, view, tmp_path, suffix):
    """导入后原始字节必须一个字节都不变。"""
    path, original = _make_image(suffix, tmp_path)
    item = _import(view, path, original)

    assert item.has_original_source(), '导入后应当保留原始字节'
    assert _sha256(item.original_bytes()) == _sha256(original), \
        f'{suffix}: 导入过程改动了原始字节'


@pytest.mark.parametrize('suffix', FORMATS)
def test_save_and_reopen_keeps_original_bytes(qapp, view, tmp_path, suffix):
    """保存工程 → 清空 → 重开，原始字节仍然不变。

    这是最容易丢字节的一步：项目保存如果按「图片存储偏好」重新编码，
    原始字节就在这里没了。
    """
    path, original = _make_image(suffix, tmp_path)
    _import(view, path, original)

    project = str(tmp_path / 'roundtrip.prism')
    SQLiteIO(project, view.scene, create_new=True).write()

    # 清空场景，模拟重新打开工程
    view.clear_scene()
    assert not _pixmap_items(view.scene), '清空后不该还有素材'

    SQLiteIO(project, view.scene, readonly=True).read()
    view.scene.add_queued_items()

    reopened = _pixmap_items(view.scene)
    assert reopened, f'{suffix}: 重开后没有读到素材'
    item = reopened[0]
    assert item.has_original_source(), f'{suffix}: 重开后原始字节丢了'
    assert _sha256(item.original_bytes()) == _sha256(original), \
        f'{suffix}: 保存或重开过程改动了原始字节'


@pytest.mark.parametrize('suffix', FORMATS)
def test_export_original_matches_source_file(qapp, view, tmp_path, suffix):
    """导出「原始素材」，导出文件必须与源文件逐字节一致。

    这是用户提的硬要求：导入什么，导出就该是什么。
    """
    path, original = _make_image(suffix, tmp_path)
    _import(view, path, original)

    outdir = tmp_path / 'exported'
    outdir.mkdir()
    ImagesToDirectoryExporter(
        view.scene, str(outdir), adjusted=False).export()

    written = list(outdir.glob('*'))
    assert written, f'{suffix}: 导出没有产生文件'
    exported = written[0].read_bytes()
    assert _sha256(exported) == _sha256(original), (
        f'{suffix}: 导出把原图改动了\n'
        f'  源文件 {len(original)} 字节  {_sha256(original)[:16]}\n'
        f'  导出物 {len(exported)} 字节  {_sha256(exported)[:16]}')


@pytest.mark.parametrize('suffix', FORMATS)
def test_full_roundtrip_export_matches_source(qapp, view, tmp_path, suffix):
    """完整链路：导入 → 保存 → 重开 → 导出，导出物 == 源文件。

    这是最接近用户真实用法的一条：存盘之后隔天再导出，也不能变。
    """
    path, original = _make_image(suffix, tmp_path)
    _import(view, path, original)

    project = str(tmp_path / 'roundtrip.prism')
    SQLiteIO(project, view.scene, create_new=True).write()

    view.clear_scene()
    SQLiteIO(project, view.scene, readonly=True).read()
    view.scene.add_queued_items()

    outdir = tmp_path / 'exported'
    outdir.mkdir()
    ImagesToDirectoryExporter(
        view.scene, str(outdir), adjusted=False).export()

    written = list(outdir.glob('*'))
    assert written, f'{suffix}: 重开后导出没有产生文件'
    exported = written[0].read_bytes()
    assert _sha256(exported) == _sha256(original), (
        f'{suffix}: 完整来回之后原图变了\n'
        f'  源文件 {len(original)} 字节  {_sha256(original)[:16]}\n'
        f'  导出物 {len(exported)} 字节  {_sha256(exported)[:16]}')


def test_generated_image_reports_no_original(qapp, view):
    """没有原始字节的素材必须能明确报出来，而不是假装可以无损导出。"""
    image = QtGui.QImage(40, 30, QtGui.QImage.Format.Format_ARGB32)
    image.fill(QtGui.QColor(10, 10, 10))
    item = PrismPixmapItem(image)

    assert not item.has_original_source(), \
        '程序生成的图片不该声称有原始来源'
    assert item.original_bytes() is None


def test_preview_adjustment_does_not_clear_original(qapp, view, tmp_path):
    """预览调整（曝光/通道/灰度）绝不能清空原始字节。"""
    path, original = _make_image('.png', tmp_path)
    item = _import(view, path, original)

    adjusted = QtGui.QPixmap(item.pixmap())
    adjusted.fill(QtGui.QColor(255, 255, 255))
    item.set_preview_pixmap(adjusted)

    assert item.has_original_source(), '预览调整把原始字节清掉了'
    assert _sha256(item.original_bytes()) == _sha256(original), \
        '预览调整改动了原始字节'


def test_exported_extension_matches_source(qapp, view, tmp_path):
    """导出的扩展名要跟原始格式走，不能按「图片存储偏好」猜。

    否则会出现 .png 里装着 JPEG 的情况。
    """
    path, original = _make_image('.png', tmp_path)
    _import(view, path, original)

    outdir = tmp_path / 'exported'
    outdir.mkdir()
    ImagesToDirectoryExporter(
        view.scene, str(outdir), adjusted=False).export()

    written = list(outdir.glob('*'))
    assert written, '导出没有产生文件'
    assert written[0].suffix.lower() == '.png', (
        f'原图是 PNG，导出却是 {written[0].suffix}')
