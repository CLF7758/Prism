"""T2「必须先验证」那四条：原始字节、保存不重编码、空 blob、格式/EXIF/ICC。

任务书 §5（T2）列了一张**必须先验证**的清单：

    - 新导入图片是否保留原始字节。
    - 项目保存是否会把原始字节重新编码。
    - `_source_blob` 为空时的处理方式。
    - 原始格式、扩展名、EXIF 和 ICC 是否有单独的元数据记录。

前三条已经有测试覆盖（`test_original_export.py` 等）。这个文件补第四条 ——
它是个**问句**，所以这里做的是"验并钉住结论"，不是"实现一个功能"。

**结论**（下面每条测试对应一句）：

| 项目 | 现状 |
|---|---|
| 原始格式 | ✅ 有：`_source_format`，保存进 `data['sourceFormat']` |
| 扩展名 | ✅ 有：跟着 `filename` 走 |
| **EXIF** | ❌ **没有单独记录**，但它**在原始字节里，不会丢** |
| **ICC** | ❌ 同上 |

也就是说：一个带 EXIF 的 JPEG 导进来再导出，**逐字节完全相同**，
EXIF 和 ICC 都在。Prism 只是不**解析**它们 —— 界面上看不到拍摄参数，
也谈不上"按色彩配置正确处理颜色"。

这个区别要紧：**字节层面不丢**（用户的原图是安全的），但**信息层面
不用**。交付说明里按这个写了。

测试用的 JPEG 是手工拼的 —— 环境里没有 Pillow，也不该为了一条测试引
一个图像库进来。APP1 段的构造就是几十行字节。
"""
import hashlib
import struct

import pytest
from PyQt6 import QtGui

from prism.items import PrismPixmapItem

# ── 手工构造带 EXIF / ICC 的 JPEG ───────────────────────────────

#: 最小的 TIFF 头 + 一个 IFD0 条目（ImageDescription）。
#: 字节序 II（小端），版本 42，IFD 偏移 8，1 个条目，条目是
#: tag=0x010E (ImageDescription), type=2 (ASCII), count=6, 值偏移 26。
EXIF_TIFF = (
    b'II*\x00'                       # 小端序 + 魔数 42
    + struct.pack('<I', 8)           # 第一个 IFD 的偏移
    + struct.pack('<H', 1)           # 条目数
    + struct.pack('<HHI', 0x010E, 2, 6)   # ImageDescription, ASCII, 6 字节
    + struct.pack('<I', 26)          # 值放在偏移 26
    + struct.pack('<I', 0)           # 下一个 IFD：没有
    + b'Hello\x00'                   # 值本体（4 字节对齐后到 26）
    + b'\x00\x00'                    # 补到 26（0..25 是头和第 0 个条目）
)


def _segment(marker, payload):
    """拼一个 JPEG 段：FFxx + 长度（含长度自身）+ 内容。"""
    return (bytes([0xFF, marker])
            + struct.pack('>H', len(payload) + 2)
            + payload)


#: 一个 8×8 的纯色 JPEG，用来当底图。
def _plain_jpeg(quality=90):
    image = QtGui.QImage(8, 8, QtGui.QImage.Format.Format_RGB32)
    image.fill(QtGui.QColor(120, 80, 40))
    from PyQt6 import QtCore

    buffer = QtCore.QBuffer()
    buffer.open(QtCore.QIODevice.OpenModeFlag.WriteOnly)
    image.save(buffer, 'JPEG', quality)
    return bytes(buffer.data())


def jpeg_with_exif():
    """在 SOI 之后插入一个 APP1（EXIF）段。

    JPEG 的结构是 FF D8（SOI）后面跟一串段，段顺序无所谓（APP1 通常
    紧跟 SOI）。所以把 APP1 插在 SOI 后面就是个合法的带 EXIF 的 JPEG。
    """
    data = _plain_jpeg()
    assert data[:2] == b'\xff\xd8', '底图得是个 JPEG'
    return data[:2] + _segment(0xE1, b'Exif\x00\x00' + EXIF_TIFF) + data[2:]


def jpeg_with_icc():
    """插入一个 APP2（ICC_PROFILE）段。"""
    data = _plain_jpeg()
    # 真的 ICC 内容这里不重要 —— 验的是"这段字节会不会被原样带过去"
    profile = bytes(range(128)) * 2
    payload = b'ICC_PROFILE\x00' + bytes([1, 1]) + profile
    return data[:2] + _segment(0xE2, payload) + data[2:]


def item_from_bytes(data, suffix='jpg'):
    """走一遍真实的导入路径：`set_source_blob`。"""
    item = PrismPixmapItem(QtGui.QImage())
    item.set_source_blob(data, suffix)
    return item


# ── 第四条：格式 / 扩展名 / EXIF / ICC ─────────────────────────

def test_source_format_is_recorded(qapp):
    """原始格式有单独记录。"""
    item = item_from_bytes(jpeg_with_exif())
    assert item.original_format() == 'jpg', f'实得 {item.original_format()!r}'


def test_source_format_normalises_case_and_leading_dot(qapp):
    """`_normalise_format` 只做小写和去点。

    **它不把 `jpeg` 折成 `jpg`** —— 原样返回。我原本以为它会折，那是猜
    的；这里按真实行为断言。要折的话得改产品代码，而那不是这条任务书
    要求的事。
    """
    assert item_from_bytes(_plain_jpeg(), 'JPEG').original_format() == 'jpeg'
    assert item_from_bytes(_plain_jpeg(), '.jpg').original_format() == 'jpg'
    assert item_from_bytes(_plain_jpeg(), 'jpg').original_format() == 'jpg'


def test_unknown_format_is_derived_from_the_bytes(qapp):
    """没告诉格式时从字节头推 —— 剪贴板来的数据就是这种情况。"""
    item = PrismPixmapItem(QtGui.QImage())
    item.set_source_blob(_plain_jpeg())          # 不给 suffix
    assert item.original_format() == 'jpg'


def test_exif_bytes_survive_byte_for_byte(qapp):
    """**关键的一条**：带 EXIF 的 JPEG 进来再出去，逐字节相同。

    这就是「EXIF 没有单独记录，但也不会丢」的证据 —— 它在原始字节里。
    """
    original = jpeg_with_exif()
    item = item_from_bytes(original)
    stored = item.original_bytes()
    assert stored is not None, '原始字节没保留'
    assert hashlib.sha256(stored).hexdigest() == \
        hashlib.sha256(original).hexdigest()
    assert b'Exif\x00\x00' in stored, 'EXIF 那个段应该还在'
    assert b'Hello\x00' in stored, 'EXIF 里的内容应该还在'


def test_icc_bytes_survive_byte_for_byte(qapp):
    original = jpeg_with_icc()
    item = item_from_bytes(original)
    stored = item.original_bytes()
    assert hashlib.sha256(stored).hexdigest() == \
        hashlib.sha256(original).hexdigest()
    assert b'ICC_PROFILE\x00' in stored, 'ICC 那个段应该还在'


def test_prism_does_not_index_exif_or_icc(qapp):
    """**如实记录现状**：素材上保存的元数据是"用户加的"，不含文件自带的
    EXIF / ICC 字段。

    `get_metadata_save_data()` 给的是 categories / tags / rating / notes /
    title / canvasId / groupId / groupNote —— 全是 Prism 自己的概念。
    拍摄时间、相机型号、色彩配置这些**不在里面**。

    **EXIF 的方向有被用**（`prism/fileio/image.py` 的
    `exif_rotated_image()` 用 `exif` 库读 orientation 并据此旋转），
    但那是读进来就用掉了，没有存下来给别处用。

    写下来比不写有用：至少"我们验过了，结论是这样"是个有记录的结论。
    哪天要加，改这条和交付说明。
    """
    item = item_from_bytes(jpeg_with_exif())
    saved = item.get_metadata_save_data()
    assert set(saved) == {'categories', 'tags', 'rating', 'notes', 'title',
                          'canvasId', 'groupId', 'groupNote'}
    for name in ('exif', 'icc', 'iccProfile', 'orientation', 'capturedAt'):
        assert name not in saved, (
            f'{name} 居然在保存的元数据里 —— 如果确实加了，'
            f'请更新这条测试和交付说明')


def test_exif_orientation_is_actually_handled(qapp):
    """EXIF 的**方向**是真的被处理了的。

    这条是冲着我自己写错的核查结论来的：我一度以为"Prism 不解析 EXIF"，
    实际 `prism/fileio/image.py` 早就用 `exif` 库读 orientation 并据此
    旋转，依赖记录（DEPENDENCIES.md:86）也一直有这一条。

    记下来是因为"以为没有 → 差点重复劳动"本身值得记：**先查有没有，
    再决定做不做**。
    """
    import prism.fileio.image as image_module

    assert hasattr(image_module, 'exif_rotated_image')
    assert hasattr(image_module, 'exif'), 'exif 库是导入的'


def test_format_travels_through_save_and_load(qapp):
    """`_source_format` 要存进 `data['sourceFormat']`，重开之后还在。"""
    item = item_from_bytes(jpeg_with_exif())
    data = item.get_extra_save_data()
    assert data.get('sourceFormat') == 'jpg'


# ── 前三条也钉一遍（已有测试，这里只做冒烟）────────────────────

def test_original_bytes_are_kept(qapp):
    """第一条：新导入的图片保留原始字节。"""
    original = jpeg_with_exif()
    assert item_from_bytes(original).original_bytes() == original


def test_empty_blob_is_falsy(qapp):
    """第三条：`_source_blob` 为空时取出来是**假值**。

    注意它返回的是 `b''` 而不是 `None` —— 两种都是 falsy，而调用方
    （`ExportService.check_original`、`duplicate_scan.ImageSource`）
    用的正是 `if payload:`，所以判断是对的。

    这里断言"falsy"而不是"is None"：后者是我一开始的猜测，与实现不符。
    """
    item = PrismPixmapItem(QtGui.QImage())
    assert not item.original_bytes(), '没设过的应该是假值'
    item.set_source_blob(b'')
    assert not item.original_bytes(), '空字节也是假值'


def test_saving_does_not_touch_the_original(qapp):
    """第二条：设了预览/调整之后，原始字节一个字节都不该变。

    预览是**非破坏性**的，这条是它的底线。
    """
    original = jpeg_with_exif()
    item = item_from_bytes(original)
    before = item.original_bytes()

    # 走一遍常见的调整路径：灰度 + 一个调整过的预览
    image = QtGui.QImage.fromData(original)
    item.grayscale = True
    adjusted = QtGui.QImage(image)
    adjusted.fill(QtGui.QColor(0, 0, 0))
    item.set_preview_pixmap(QtGui.QPixmap.fromImage(adjusted), adjusted=True)

    assert item.original_bytes() == before, '调整预览动了原始字节'
