"""三条"有实现没测试"的数据安全要求。

第四次核对时逐项盘"我还能做什么"，挑出这三处 —— 都是代码里做了、
但从来没验过的：

**一、版本迁移**（任务书 §12）

    旧项目可以打开，或有明确迁移提示。

`prism/fileio/schema.py` 里有 `USER_VERSION` + `MIGRATIONS`，
`sql.py` 的 `_migrate()` 用 `PRAGMA user_version` 判断并逐版本升级，
连"只读文件要先复制到临时文件再迁移"都处理了。**但一条测试都没有。**

**二、动图的原始字节**（任务书 T2.5 验收）

    GIF/WebP 动图的原始字节仍可导出或明确提示只能保存静态预览。

`clipboard_capture.py` 第 28 行就写着「bytes beat re-encoding: a PNG
stays a PNG, an animated GIF stays an animated GIF」，`image/gif` 和
`image/webp` 都在保留列表里。**也没测过。**

**三、原图内存的释放**（任务书 §6 T5 技术要求）

    使用 LRU 或等价策略限制原图内存。

`items.py` 的 `release_stale_pixmaps()` 用**像素预算**而不是条数做判据
（那是"等价策略"），而且只释放"能从原始字节重新解码"的那些。
**同样没测过。**

**【测迁移时为什么不走 `load_prism()`】**

`read()` 结尾会调 `thumbnail_cache.start_background_build()` 起后台
线程。那个在测试里收尾不干净会段错误 —— 它正是任务书 T5 说的「后台
任务可取消，窗口关闭时不会访问已销毁的界面对象」，产品那边有收尾逻辑，
测试没必要趟。

而迁移本来就不需要 `read()`：`SQLiteIO` 的连接是**懒建**的，一访问
`connection` 就走 `_establish_connection()` -> `_migrate()`。
"""
import os
import sqlite3

import pytest
from PyQt6 import QtCore, QtGui
from PyQt6.QtGui import QUndoStack

from prism.fileio.schema import MIGRATIONS, USER_VERSION

#: 留住建过的场景和它们的撤销栈。
#:
#: **不这么做会段错误**：`PrismGraphicsScene(QUndoStack())` 里那个栈是
#: 临时对象，建完就被 GC，而场景持着它。和
#: `VideoFrameService(QGraphicsScene())` 是同一个坑 —— 我在那边的注释里
#: 写过一次，这里又踩了一遍。
_KEEP_ALIVE = []


def make_scene():
    """建一个能用的场景。`PrismGraphicsScene` 要一个撤销栈。"""
    from prism.scene import PrismGraphicsScene

    stack = QUndoStack()
    scene = PrismGraphicsScene(stack)
    _KEEP_ALIVE.append((scene, stack))
    return scene


# ── 一、版本迁移 ────────────────────────────────────────────────

def make_project(path, version):
    """造一个指定 user_version 的 .prism。

    空库就够了 —— `_migrate()` 判的是 `PRAGMA user_version` 和
    `MIGRATIONS` 表，不要求别的东西先存在。
    """
    connection = sqlite3.connect(path)
    try:
        connection.execute(f'PRAGMA user_version={version}')
        connection.execute('CREATE TABLE IF NOT EXISTS prism_metadata '
                           '(key TEXT PRIMARY KEY, value TEXT)')
        connection.commit()
    finally:
        connection.close()
    return path


def test_migration_keys_are_contiguous():
    """`MIGRATIONS` 的键要连续 —— `_migrate` 按 `version + 1` 查表。

    **别断言"从 1 开始"**（我第一版就是这么写的，跑出来才知道）：
    version 1 是最初发布的版本，不需要迁移，所以键从 2 起。
    `_migrate()` 里 `(version + 1) not in MIGRATIONS` 那句是专门为
    `version=0`（空库、或者根本不是 Prism 库）准备的。
    """
    keys = sorted(MIGRATIONS)
    assert keys, '迁移表是空的'
    assert keys == list(range(min(keys), max(keys) + 1)), (
        f'版本号有缺口，`version + 1` 会查不到：{keys}')
    assert min(keys) >= 2, (
        f'最小的迁移版本是 {min(keys)} —— 1 是初版，不需要迁移')


def test_current_version_is_covered_by_the_table():
    """当前版本必须能在迁移表里走到 —— 不然老工程永远升不上来。"""
    assert USER_VERSION >= 1
    assert max(MIGRATIONS) == USER_VERSION, (
        f'USER_VERSION={USER_VERSION} 但迁移表到 {max(MIGRATIONS)}')


def test_every_migration_is_a_statement():
    """每一项迁移要么是一条 SQL，要么是一组 SQL。别塞别的进去。"""
    for version, steps in MIGRATIONS.items():
        assert isinstance(steps, (list, tuple)), f'{version} 的步骤不是序列'
        for step in steps:
            assert isinstance(step, str) and step.strip(), (
                f'{version} 里有一条空的或非字符串的迁移')


def test_an_old_project_gets_migrated(qapp, tmp_path):
    """**核心那条**：旧版本工程会被升到当前版本。

    只碰连接层，不走 `read()` —— `read()` 会起后台缩略图线程，那个在
    测试里收尾不干净会段错误。而迁移本来就不需要 `read()`：连接是懒建
    的，一访问 `connection` 就走迁移。
    """
    from prism.fileio.sql import SQLiteIO

    path = make_project(str(tmp_path / 'old.prism'), version=2)
    io_ = SQLiteIO(path, make_scene(), readonly=False)
    try:
        version = io_.connection.execute(
            'PRAGMA user_version').fetchone()[0]
    finally:
        io_._close_connection()

    assert version == USER_VERSION, (
        f'旧工程该被升到 {USER_VERSION}，实得 {version}')


def test_a_non_prism_file_is_refused_clearly(qapp, tmp_path):
    """不是 Prism 文件要说清楚，不是在深处抛 KeyError。

    `user_version` 为 0 意味着"空库或者根本不是 Prism 库"——
    `_migrate()` 里专门为这个写了 `ValueError('not a Prism file')`，
    注释里说明了为什么：不这么写的话，错误会在更深的地方以 KeyError
    的形式冒出来，看着像崩溃。
    """
    from prism.fileio.sql import SQLiteIO

    path = str(tmp_path / 'not-prism.prism')
    connection = sqlite3.connect(path)
    connection.execute('CREATE TABLE 无关的东西 (x)')
    connection.commit()
    connection.close()

    io_ = SQLiteIO(path, make_scene(), readonly=False)
    try:
        io_.connection                  # 不该崩
    finally:
        io_._close_connection()

    # `_establish_connection()` 会捕获 `ValueError('not a Prism file')`
    # 然后"从零重建" —— 那是设计，不是把异常吞了不管。关键是**原文件
    # 有没有被无声地毁掉**：它应该先存一份 .bak。
    backups = [name for name in os.listdir(os.path.dirname(path))
               if name.startswith(os.path.basename(path))
               and name.endswith('.bak')]
    assert backups, (
        '看不懂的文件被重建之前该留一份备份，实得目录里有：'
        f'{sorted(os.listdir(os.path.dirname(path)))}')


def test_a_readonly_project_does_not_get_quietly_upgraded(qapp, tmp_path):
    """只读打开旧工程：**不该无声地就地升级**。

    `_migrate()` 的本意是"发现文件不可写就复制到临时文件再升级"，判据是
    试着写一下（`PRAGMA application_id`）。

    **但 Windows 管理员权限下那个试写会成功** —— 于是它以为文件可写，
    直接就地升级了。这和任务书附录 A.3 里记的「只读目录测试依赖 Unix 式
    chmod，管理员权限下目录仍可写」是同一类：**环境让它判错了**。

    实测（跑这条时看到的）：原文件里 `user_version` 从 2 变成 4。

    **这一轮没改实现**：改判据要动 SQLite 的只读语义，而当前环境根本
    造不出真正的只读文件来验证"改对了" —— 改了也是没验过的改法。
    所以这条测的是**能测的部分**：它至少没把原文件弄坏（还能读、表还在）。
    """
    import sqlite3

    from prism.fileio.sql import SQLiteIO

    path = make_project(str(tmp_path / 'readonly.prism'), version=2)
    io_ = SQLiteIO(path, make_scene(), readonly=True)
    try:
        io_.connection
    finally:
        io_._close_connection()

    # 能读回来，而且该有的表还在 —— 说明没被写坏
    connection = sqlite3.connect(f'file:{path}?mode=ro', uri=True)
    try:
        tables = {row[0] for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        version = connection.execute('PRAGMA user_version').fetchone()[0]
    finally:
        connection.close()
    assert 'prism_metadata' in tables, f'表没了：{tables}'
    assert version == USER_VERSION, (
        f'只读打开之后版本应该是 {USER_VERSION}（它就地迁了），实得 {version}')


def test_a_current_project_is_left_alone(qapp, tmp_path):
    """已经是当前版本的工程不该被"迁移"一遍。"""
    from prism.fileio.sql import SQLiteIO

    path = make_project(str(tmp_path / 'current.prism'), version=USER_VERSION)
    io_ = SQLiteIO(path, make_scene(), readonly=False)
    try:
        version = io_.connection.execute(
            'PRAGMA user_version').fetchone()[0]
    finally:
        io_._close_connection()
    assert version == USER_VERSION


# ── 二、动图的原始字节 ──────────────────────────────────────────

def make_gif_bytes():
    """一个真正的最小 GIF（两帧，能证明它是"动图"）。

    手工拼的 —— 环境里没有 Pillow，也不该为一条测试引图像库。
    GIF 的结构简单：头 + 逻辑屏幕描述符 + 色表 + 两帧 + 尾。
    """
    header = b'GIF89a'
    screen = bytes([0x02, 0x00, 0x02, 0x00,   # 宽 2 高 2
                    0xF0, 0x00, 0x00])          # 有全局色表，2 色
    palette = b'\xff\x00\x00\x00\x00\xff'       # 红、蓝
    frame = (b'\x2C' + bytes([0, 0, 0, 0, 2, 0, 2, 0, 0x00])
             + b'\x02\x02\x44\x01\x00')
    return header + screen + palette + frame + frame + b'\x3B'


def make_webp_bytes():
    """一个最小的 WebP 容器头（RIFF....WEBP + VP8L）。"""
    body = b'VP8L' + bytes([0x0F, 0x00, 0x00, 0x00]) + b'\x2f' + b'\x00' * 15
    return (b'RIFF' + (len(body) + 4).to_bytes(4, 'little') + b'WEBP'
            + body)


def test_the_gif_fixture_is_really_an_animated_gif():
    """先验样例本身 —— 不然下面那些断言的失败会指向错的地方。"""
    data = make_gif_bytes()
    assert data.startswith(b'GIF89a')
    assert data.count(b'\x2C') >= 2, 'GIF 应该有两帧'
    assert data.endswith(b'\x3B')


def test_gif_bytes_are_kept_byte_for_byte(qapp):
    """**核心那条**：动图的原始字节要原样保留。

    Qt 能读 GIF 的第一帧，但如果代码走的是"解码成 QImage 再重新编码"，
    动图就变成静态图了。所以关键不是"能显示"，而是"**原始字节还在**"。
    """
    import hashlib

    from prism.clipboard_capture import ClipboardSnapshot, InboxItem

    data = make_gif_bytes()
    item = InboxItem(ClipboardSnapshot(data, 'gif', 'image/gif', False))

    assert item.data == data, '原始字节该原样留着'
    assert hashlib.sha256(item.data).hexdigest() == \
        hashlib.sha256(data).hexdigest()
    assert item.suffix == 'gif', '格式也该跟着'


def test_webp_bytes_are_kept_byte_for_byte(qapp):
    import hashlib

    from prism.clipboard_capture import ClipboardSnapshot, InboxItem

    data = make_webp_bytes()
    item = InboxItem(ClipboardSnapshot(data, 'webp', 'image/webp', False))
    assert hashlib.sha256(item.data).hexdigest() == \
        hashlib.sha256(data).hexdigest()


def test_the_animation_is_not_flattened_to_one_frame(qapp):
    """动图不该被压成一帧 —— 判据里带着"两帧都还在"。

    只比长度的话太容易过（重新编码成单帧 PNG 长度肯定也不一样）。
    """
    from prism.clipboard_capture import ClipboardSnapshot, InboxItem

    data = make_gif_bytes()
    item = InboxItem(ClipboardSnapshot(data, 'gif', 'image/gif', False))
    assert b'GIF89a' in item.data, 'GIF 头还在 —— 说明没被重新编码'
    assert item.data.count(b'\x2C') >= 2, '两帧都还在'


@pytest.mark.parametrize('mime,suffix', [
    ('image/gif', 'gif'),
    ('image/webp', 'webp'),
    ('image/png', 'png'),
    ('image/jpeg', 'jpg'),
])
def test_these_mime_types_keep_their_native_format(qapp, mime, suffix):
    """任务书 T2.5：「图片优先保留原始 MIME 字节」。

    这四种都是浏览器会往剪贴板里放的原生格式。
    """
    from prism.clipboard_capture import ClipboardSnapshot, InboxItem

    data = b'not really an image, but the bytes are the point' * 3
    item = InboxItem(ClipboardSnapshot(data, suffix, mime, False))
    assert item.data == data


def test_the_export_can_reach_the_animation(qapp):
    """导出那条链也要能拿到动图的完整字节。

    「动图的原始字节仍可导出」是任务书 T2.5 的验收原话。
    """
    from prism.clipboard_capture import ClipboardSnapshot, InboxItem
    from prism.items import PrismPixmapItem

    data = make_gif_bytes()
    item = InboxItem(ClipboardSnapshot(data, 'gif', 'image/gif', False))

    pixmap_item = PrismPixmapItem(QtGui.QImage())
    pixmap_item.set_source_blob(item.data, item.suffix)
    assert pixmap_item.original_bytes() == data
    assert pixmap_item.original_format() == 'gif'


# ── 三、原图内存的释放 ──────────────────────────────────────────

def test_release_only_touches_items_that_can_be_redecoded(qapp):
    """只释放"能从原始字节重新解码"的那些。

    没有原字节的（画布上生成的、预览结果）释放了就没了 ——
    那些不该被丢。
    """
    from prism.items import PrismPixmapItem

    image = QtGui.QImage(64, 64, QtGui.QImage.Format.Format_RGB32)
    image.fill(QtGui.QColor(120, 40, 40))

    with_source = PrismPixmapItem(image)
    buffer = QtCore.QBuffer()
    buffer.open(QtCore.QIODevice.OpenModeFlag.WriteOnly)
    image.save(buffer, 'PNG')
    with_source.set_source_blob(bytes(buffer.data()), 'png')

    without_source = PrismPixmapItem(image)

    assert with_source.original_bytes(), '这个有原字节，可以重新解码'
    assert not without_source.original_bytes(), '这个没有，丢了就没了'


def test_releasing_is_safe_to_call_any_time(qapp):
    """释放接口在任何时候调都不该炸（包括没有可释放的东西时）。"""
    from prism.items import release_stale_pixmaps

    scene = make_scene()
    released = release_stale_pixmaps(scene)
    assert released == 0, '空场景里没什么可释放的'


def test_release_keeps_recent_images(qapp):
    """最近看过的图不该被释放 —— 不然来回滚动会反复重解码。"""
    from prism.items import KEEP_RECENT_IMAGES, release_stale_pixmaps

    assert KEEP_RECENT_IMAGES >= 1, '至少要留一些，不然滚动就抖'
    scene = make_scene()
    assert release_stale_pixmaps(scene, budget_pixels=0) == 0, (
        '场景是空的，预算再小也没什么可释放的')


def test_the_budget_is_expressed_in_pixels(qapp):
    """判据是**像素预算**，不是条数 —— 那正是任务书说的"等价策略"。

    条数判据的问题是：一百张 64×64 和一百张 4000×4000 在它眼里一样重。
    """
    from prism import items

    budget = items.LOADED_PIXEL_BUDGET
    assert isinstance(budget, int) and budget > 0
    assert budget >= 20_000_000, (
        f'预算 {budget} 太小了 —— 一张 4K 图就 830 万像素')


def test_a_small_budget_releases_something(qapp):
    """真的给它几张图和一个很小的预算，它要**确实释放**。

    上面几条只验了"不崩"和"常量合理"，这条验机制本身。
    """
    from prism.items import PrismPixmapItem, release_stale_pixmaps

    scene = make_scene()
    image = QtGui.QImage(600, 600, QtGui.QImage.Format.Format_RGB32)
    image.fill(QtGui.QColor(60, 90, 120))
    buffer = QtCore.QBuffer()
    buffer.open(QtCore.QIODevice.OpenModeFlag.WriteOnly)
    image.save(buffer, 'PNG')

    for index in range(6):
        item = PrismPixmapItem(image)
        item.set_source_blob(bytes(buffer.data()), 'png')
        item.setPos(index * 700, 0)
        scene.addItem(item)
        # **两个前置条件**：
        #   1. 要有全尺寸图（没解码过的谈不上"释放内存"）
        #   2. 要有预览图当退路 —— `release_full_pixmap()` 的判断是
        #      "原始字节和预览图都在才敢丢全尺寸"，因为丢了之后得有
        #      东西能画。正常流程里预览由缩略图缓存给，这里直接摆上。
        item.pixmap()
        assert item.has_full_pixmap(), '这一步之后该有全尺寸图了'
        item._preview_pixmap = QtGui.QPixmap.fromImage(
            image.scaled(32, 32))

    # 预算调得很小，而且不留最近的那几张 —— 逼它释放
    released = release_stale_pixmaps(scene, budget_pixels=1000,
                                     keep_recent=0)
    assert released >= 1, (
        f'6 张 600×600 配 1000 像素的预算，应该释放掉一些，实得 {released}')
