"""T6：查重服务 —— 显式范围、内容缓存、可取消。

这组测试不依赖界面状态：范围是传进去的参数，哈希只吃字节，
所以可以脱离窗口验证。
"""
import hashlib

import pytest
from PyQt6 import QtCore, QtGui

from prism.duplicate_scan import (HashCache, ImageSource, ScanScope,
                                  collect_sources, DuplicateScanService)


def _png(colour, size=(80, 60), marker=(10, 10, 30, 20)):
    image = QtGui.QImage(size[0], size[1], QtGui.QImage.Format.Format_ARGB32)
    image.fill(QtGui.QColor(*colour))
    painter = QtGui.QPainter(image)
    painter.fillRect(*marker, QtGui.QColor(255, 255, 255))
    painter.end()
    buffer = QtCore.QBuffer()
    buffer.open(QtCore.QIODevice.OpenModeFlag.WriteOnly)
    image.save(buffer, 'PNG')
    return bytes(buffer.data())


def _source(name, data, where=''):
    return ImageSource(hashlib.sha256(data).hexdigest(), name, data, where)


def _run(service, sources, scope=ScanScope.ALL_ASSETS):
    """同步跑一次扫描并返回 (groups, stats)。"""
    captured = {}
    service.finished.connect(lambda g, s: captured.update(groups=g, stats=s))
    service.scan(sources, scope)
    return captured.get('groups', []), captured.get('stats', {})


# ── 分组 ───────────────────────────────────────────────────────────────

def test_finds_a_duplicate_pair(qapp):
    red = _png((200, 60, 60))
    blue = _png((60, 60, 200))
    groups, stats = _run(DuplicateScanService(), [
        _source('a.png', red), _source('b.png', red), _source('c.png', blue)])

    assert len(groups) == 1
    assert {m.name for m in groups[0]} == {'a.png', 'b.png'}
    assert stats['scanned'] == 3
    assert stats['compared'] == 3


def test_different_colours_are_not_duplicates(qapp):
    """结构相同但颜色不同的两张图不能算重复。

    dHash 只看亮度结构，纯色块配同样位置的方块会得到一样的哈希，
    所以颜色签名这一层是必需的 —— 这条用例就是守住它的。
    """
    groups, _ = _run(DuplicateScanService(), [
        _source('red.png', _png((200, 60, 60))),
        _source('blue.png', _png((60, 60, 200)))])

    assert groups == [], '颜色不同不该被当成重复'


def test_three_copies_form_one_group(qapp):
    red = _png((200, 60, 60))
    groups, _ = _run(DuplicateScanService(), [
        _source('a.png', red), _source('b.png', red), _source('c.png', red)])
    assert len(groups) == 1 and len(groups[0]) == 3


def test_records_the_location_each_copy_came_from(qapp):
    """跨画布的副本要能说出各自来自哪里。"""
    red = _png((200, 60, 60))
    groups, _ = _run(DuplicateScanService(), [
        _source('a.png', red, '画布一'), _source('b.png', red, '画布二')])

    assert len(groups) == 1
    assert {m.location for m in groups[0]} == {'画布一', '画布二'}


def test_unreadable_entries_are_counted_not_crashed(qapp):
    _, stats = _run(DuplicateScanService(), [
        _source('broken.png', b'this is not an image'),
        _source('ok.png', _png((10, 200, 10)))])

    assert stats['unreadable'] == 1
    assert stats['compared'] == 1


# ── 缓存 ───────────────────────────────────────────────────────────────

def test_second_scan_hits_the_cache(qapp):
    # 用两张内容不同的图，否则第一次扫描里第二张就会命中缓存，
    # 断言反而看不清缓存有没有起作用
    red = _png((200, 60, 60))
    blue = _png((60, 60, 200))
    service = DuplicateScanService()
    sources = [_source('a.png', red), _source('b.png', blue)]

    _, first = _run(service, sources)
    assert first['cache_hits'] == 0, '第一次不该有命中'

    _, second = _run(service, sources)
    assert second['cache_hits'] == 2, '第二次应当全部命中'


def test_cache_is_keyed_by_content_not_by_name(qapp):
    """改名不算变内容，缓存应当照样命中。"""
    red = _png((200, 60, 60))
    service = DuplicateScanService()
    _run(service, [_source('original-name.png', red)])
    _, stats = _run(service, [_source('renamed-copy.png', red)])
    assert stats['cache_hits'] == 1


def test_cache_evicts_oldest_entries(qapp):
    cache = HashCache(max_entries=2)
    cache.put('a', (1, (0, 0, 0), 1.0))
    cache.put('b', (2, (0, 0, 0), 1.0))
    cache.get('a')                       # a 变成最近使用
    cache.put('c', (3, (0, 0, 0), 1.0))  # 应当挤掉 b

    assert len(cache) == 2
    assert cache.get('a') is not None
    assert cache.get('b') is None
    assert cache.get('c') is not None


# ── 取消与统计 ─────────────────────────────────────────────────────────

def test_cancel_during_a_scan_stops_it(qapp):
    """运行中取消要能停下来，并如实报告。"""
    red = _png((200, 60, 60))
    service = DuplicateScanService()
    captured = {}

    def on_progress(done, total):
        if done >= 1:
            service.cancel()

    service.progress.connect(on_progress)
    service.finished.connect(lambda g, s: captured.update(stats=s))
    service.scan([_source(f'{i}.png', red) for i in range(5)])

    assert captured['stats']['cancelled'] is True


def test_cancel_during_pair_comparison_discards_partial_groups(qapp):
    """Cancelling after fingerprinting must not show incomplete groups."""
    red = _png((200, 60, 60))
    service = DuplicateScanService()
    captured = {}
    checks = {'count': 0}
    original = service._close_enough

    def cancel_after_first_pair(left, right):
        checks['count'] += 1
        if checks['count'] == 1:
            service.cancel()
        return original(left, right)

    service._close_enough = cancel_after_first_pair
    service.finished.connect(
        lambda groups, stats: captured.update(groups=groups, stats=stats))
    service.scan([_source(f'{index}.png', red) for index in range(4)])

    assert checks['count'] == 1
    assert captured['stats']['cancelled'] is True
    assert captured['groups'] == []


def test_progress_reports_every_item(qapp):
    red = _png((200, 60, 60))
    service = DuplicateScanService()
    seen = []
    service.progress.connect(lambda done, total: seen.append((done, total)))
    _run(service, [_source(f'{i}.png', red) for i in range(3)])

    assert len(seen) == 3
    assert seen[-1] == (2, 3)


# ── 范围（不读界面状态）───────────────────────────────────────────────

def test_scope_labels_are_human_readable(qapp):
    for scope in ScanScope.ALL:
        label = ScanScope.label(scope)
        assert label and label != scope, f'{scope} 没有可读标签'


def test_collect_sources_respects_the_given_scope(view):
    """范围决定取哪些素材，collect_sources 不读任何控件。"""
    from prism.items import PrismPixmapItem

    def add(name, canvas_id, category):
        image = QtGui.QImage(40, 30, QtGui.QImage.Format.Format_ARGB32)
        image.fill(QtGui.QColor(90, 120, 40))
        item = PrismPixmapItem(image, name)
        item.set_source_blob(_png((90, 120, 40)), 'png')
        item._canvas_id = canvas_id
        item._categories = [category]
        view.scene.addItem(item)
        return item

    first = add('a.png', 'canvas-one', '分类一')
    second = add('b.png', 'canvas-two', '分类二')

    scene = view.scene
    everything = collect_sources(scene, ScanScope.ALL_ASSETS)
    assert len(everything) == 2

    only_one = collect_sources(scene, ScanScope.CURRENT_CANVAS,
                               current_canvas_id='canvas-one')
    assert len(only_one) == 1
    assert only_one[0].name == 'a.png'

    chosen = collect_sources(scene, ScanScope.SELECTION, selected=[second])
    assert len(chosen) == 1
    assert chosen[0].name == 'b.png'


def test_collect_reports_progress(view):
    """收集过程要报进度 —— 那一步在 GUI 线程读所有素材的字节。

    用户的工程有 1563 个素材，几秒钟。没有这个回调的话，调用方没法把
    进度画出来，用户看到的就是"点了查重，卡住几秒，窗口才出来"。
    """
    from tests.test_material_features import image_item

    for index in range(40):
        image_item(view.scene, [f'标签{index}'])

    seen = []

    def progress(done, total):
        seen.append((done, total))

    sources = collect_sources(view.scene, ScanScope.ALL_ASSETS,
                              progress=progress)
    assert len(sources) == 40
    assert seen, '一次进度都没报'
    # 最后一次要是"全部完成"，不然进度条会停在半路
    assert seen[-1] == (40, 40), f'最后报的是 {seen[-1]}'
    # 报的次数不能太密（改界面比读字节还贵），也不能太少（看着像卡住）
    assert len(seen) <= 8, f'报了 {len(seen)} 次，太密了'
