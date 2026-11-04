"""任务 6 残余：保存 / 导出的内嵌视频物化改成后台。

七条验收断言在这里各有用例：

* **A** 后台流式提取 —— 请求方（GUI 线程 / 保存线程）不等提取。
* **B** 同源请求合并 —— 同一源版本只提取一次；不同版本不合并。
* **C** 可取消并清理半成品 —— 取消后不留半截文件、不进缓存。
* **D** 预览缓存键 —— 分得清源版本与规格。
* **E** 租约到期释放临时文件 —— 到期可回收；被持有的不回收。
* **F** 🔴 红线（必过项）—— 保存 / 导出读到的必须是 .prism 里的**原始字节**，
  工程挪走、原外部文件删掉之后仍然如此。
* **G** 打开工程不提取无关视频 —— 打开只登记源引用。

**为什么单开一个文件**：``tests/test_video_materialization.py`` 是既有契约的
回归盘（同步物化、取消、并发合并），这个文件只管「改成后台之后」的验收，
不去改那条盘上的任何断言。
"""
import os
import shutil
import sqlite3
import threading
import time
from pathlib import Path

import pytest

from prism import fileio, source_store
from prism.fileio.resource_export import (ResourceSubtreeExporter,
                                          write_directory)
from prism.items import PrismVideoItem
from prism.workspace_service import ResourceTreeService


# ── 夹具 ──────────────────────────────────────────────────────


@pytest.fixture
def archive(tmp_path):
    """一个只含 sqlar 的存档，用来测「提取」这一层。"""
    path = tmp_path / 'original.prism'
    data = b'original-video-bytes\x00' * 120000
    with sqlite3.connect(path) as db:
        db.execute('CREATE TABLE sqlar(item_id INTEGER, data BLOB)')
        db.execute('INSERT INTO sqlar VALUES(?,?)', (7, data))
    return (str(path), 7), data


def _temp_files():
    """source_store 临时目录里现在有哪些文件。"""
    directory = getattr(source_store, '_directory', None)
    if directory is None:
        return set()
    return set(Path(directory.name).iterdir())


def _video_item(source, filename='clip.mp4'):
    item = PrismVideoItem(filename=filename)
    item._prism_source = source
    return item


def _canvas_page(page_id='canvas-1', title='画布'):
    return {'id': page_id, 'kind': 'canvas', 'title': title,
            'content': None, 'tags': []}


def _make_archive(tmp_path, data, name='original.prism'):
    """造一个只含 sqlar 的存档，返回 ((路径, item_id), 原始字节)。"""
    path = tmp_path / name
    with sqlite3.connect(path) as db:
        db.execute('CREATE TABLE sqlar(item_id INTEGER, data BLOB)')
        db.execute('INSERT INTO sqlar VALUES(?,?)', (7, data))
    return (str(path), 7), data


# ── A 后台流式提取：请求方不等 ────────────────────────────────


def test_request_materialization_returns_before_the_bytes_are_ready(qapp, archive, monkeypatch):
    """A：请求立刻返回；提取在别的线程上发生。"""
    source, data = archive
    original = source_store.retain_stream
    entered, release = threading.Event(), threading.Event()
    threads = []

    def retain(blob, suffix):
        threads.append(threading.get_ident())
        entered.set()
        assert release.wait(10)
        return original(blob, suffix)

    monkeypatch.setattr(source_store, 'retain_stream', retain)
    item = _video_item(source)
    started = time.monotonic()
    request = item.request_materialization()
    elapsed = time.monotonic() - started
    try:
        assert request is not None
        assert elapsed < .5, f'请求方等了 {elapsed:.3f}s，那不是后台'
        assert entered.wait(10), '后台线程没有开始提取'
        assert threads and threads[0] != threading.get_ident(), '提取跑在请求方线程上'
        assert item._video_source_path is None, '还没好就不能有路径'
        release.set()
        path = request.wait(10)
        assert Path(path).read_bytes() == data
    finally:
        release.set()


def test_the_export_plan_does_not_wait_for_the_extraction(qapp, tmp_path, monkeypatch):
    """A：导出计划在 GUI 线程上构建，它只启动后台提取，不等它。"""
    source, data = _make_archive(tmp_path, b'EXPORT-BYTES\x00' * 5000,
                                 name='export-source.prism')
    original = source_store.retain_stream
    entered, release = threading.Event(), threading.Event()
    threads = []

    def retain(blob, suffix):
        threads.append(threading.get_ident())
        entered.set()
        assert release.wait(10)
        return original(blob, suffix)

    monkeypatch.setattr(source_store, 'retain_stream', retain)
    item = _video_item(source)
    service = ResourceTreeService('p1')
    page = service.createPage('canvas', '画布', 'canvas-1')
    exporter = ResourceSubtreeExporter(
        service, pages=[_canvas_page()],
        canvas_items=lambda page_id: [item])
    try:
        started = time.monotonic()
        plan = exporter.build_plan('canvas', page['id'])
        elapsed = time.monotonic() - started
        assert elapsed < .5, f'导出计划等了 {elapsed:.3f}s，那不是后台'
        assert entered.wait(10), '后台线程没有开始提取'
        assert threads and threads[0] != threading.get_ident()
        release.set()
        write_directory(plan, str(tmp_path / 'out'))
        exported = tmp_path / 'out' / '画布' / 'clip.mp4'
        assert exported.read_bytes() == data
    finally:
        release.set()


def test_the_save_thread_never_pulls_the_bytes_itself(main_window, tmp_path, monkeypatch):
    """A：完整保存路径上，提取也不在保存线程上发生。

    ``prism/fileio/sql.py`` 的保存分支调用 ``_ensure_materialized()``；
    它等的是后台线程的结果，不是自己提取。
    """
    scene = main_window.view.scene
    data = b'SAVED-BYTES\x00' * 20000
    item = PrismVideoItem(filename=str(tmp_path / 'clip.mp4'))
    item._video_blob = data
    scene.addItem(item)
    source = str(tmp_path / 'source.prism')
    fileio.save_prism(source, scene, create_new=True)

    # 重新打开：这一项只有 _prism_source，保存时必须去存档里取字节。
    scene.clear()
    fileio.load_prism(source, scene)
    scene.add_queued_items()
    restored = next(i for i in scene.items()
                     if getattr(i, 'TYPE', None) == 'video')
    assert restored._video_blob is None
    assert restored._prism_source is not None

    original = source_store.retain_stream
    threads = []

    def retain(blob, suffix):
        threads.append(threading.get_ident())
        return original(blob, suffix)

    monkeypatch.setattr(source_store, 'retain_stream', retain)
    fileio.save_prism(str(tmp_path / 'out.prism'), scene, create_new=True)
    assert threads, '保存没有触发提取'
    assert threads[0] != threading.get_ident(), '提取跑在保存线程上'


# ── B 同源请求合并 ────────────────────────────────────────────


def test_two_requests_for_the_same_version_extract_once(archive, monkeypatch):
    """B：同一 (archive, item_id, 版本) 只产生一次实际提取。"""
    source, data = archive
    original = source_store.retain_stream
    entered, release = threading.Event(), threading.Event()
    calls = []

    def retain(blob, suffix):
        calls.append(True)
        entered.set()
        assert release.wait(10)
        return original(blob, suffix)

    monkeypatch.setattr(source_store, 'retain_stream', retain)
    service = source_store.materialization_service()
    first = service.request(source, '.mp4')
    second = service.request(source, '.mp4')
    try:
        assert entered.wait(10)
        release.set()
        assert first.wait(10) == second.wait(10)
        assert len(calls) == 1, f'提取了 {len(calls)} 次'
    finally:
        release.set()


def test_two_versions_of_one_item_do_not_merge(tmp_path):
    """B：不同版本不能错误合并 —— 键里带着 size / mtime / inode。"""
    first = tmp_path / 'a.prism'
    second = tmp_path / 'b.prism'
    data_a, data_b = b'A' * 4096, b'B' * 8192
    for path, data in ((first, data_a), (second, data_b)):
        with sqlite3.connect(path) as db:
            db.execute('CREATE TABLE sqlar(item_id INTEGER, data BLOB)')
            db.execute('INSERT INTO sqlar VALUES(?,?)', (7, data))
    service = source_store.materialization_service()
    path_a = service.request((str(first), 7), '.mp4').wait(10)
    path_b = service.request((str(second), 7), '.mp4').wait(10)
    assert path_a != path_b
    assert Path(path_a).read_bytes() == data_a
    assert Path(path_b).read_bytes() == data_b


def test_a_changed_archive_is_a_new_version(tmp_path):
    """B：同一个文件被改过之后就是一个新版本，不能拿旧字节。"""
    path = tmp_path / 'project.prism'
    with sqlite3.connect(path) as db:
        db.execute('CREATE TABLE sqlar(item_id INTEGER, data BLOB)')
        db.execute('INSERT INTO sqlar VALUES(?,?)', (7, b'OLD-BYTES'))
    service = source_store.materialization_service()
    old = service.request((str(path), 7), '.mp4').wait(10)
    assert Path(old).read_bytes() == b'OLD-BYTES'
    with sqlite3.connect(path) as db:
        db.execute('UPDATE sqlar SET data=? WHERE item_id=?', (b'NEW-BYTES', 7))
    # 改过时间戳：版本键里的 mtime 变了，就算字节数一样也是新版本。
    stat = path.stat()
    os.utime(path, (stat.st_atime, stat.st_mtime + 5))
    new = service.request((str(path), 7), '.mp4').wait(10)
    assert new != old
    assert Path(new).read_bytes() == b'NEW-BYTES'


# ── C 可取消并清理半成品 ──────────────────────────────────────


def test_cancelled_extraction_leaves_no_file_and_is_not_cached(archive, monkeypatch):
    """C：取消后不留半截文件、不进缓存，再要一次能成功。"""
    source, data = archive
    original = source_store.retain_stream
    entered, release = threading.Event(), threading.Event()

    def retain(blob, suffix):
        entered.set()
        assert release.wait(10)
        return original(blob, suffix)

    monkeypatch.setattr(source_store, 'retain_stream', retain)
    before = _temp_files()
    service = source_store.materialization_service()
    request = service.request(source, '.mp4', cancel_event=threading.Event())
    try:
        assert entered.wait(10)
        request.cancel()
        release.set()
        with pytest.raises(source_store.MaterializationCancelled):
            request.wait(10)
        assert _temp_files() == before, '取消后留下了半截文件'
        key = source_store.archive_video_key(source, '.mp4')
        with source_store._lock:
            assert key not in source_store._materialized_archives, '失败的提取进了缓存'
    finally:
        release.set()
    # 再要一次：必须成功
    path = service.request(source, '.mp4').wait(10)
    assert Path(path).read_bytes() == data


def test_cancelling_an_item_extraction_leaves_the_item_without_a_path(qapp, archive, monkeypatch):
    """C：被取消的项不能留下指向半写文件的路径。"""
    source, data = archive
    original = source_store.retain_stream
    entered, release = threading.Event(), threading.Event()

    def retain(blob, suffix):
        entered.set()
        assert release.wait(10)
        return original(blob, suffix)

    monkeypatch.setattr(source_store, 'retain_stream', retain)
    before = _temp_files()
    item = _video_item(source)
    request = item.request_materialization()
    try:
        assert entered.wait(10)
        item.cleanup()
        release.set()
        with pytest.raises(source_store.MaterializationCancelled):
            request.wait(10)
        assert item._video_source_path is None
        assert _temp_files() == before
    finally:
        release.set()


# ── D 预览缓存键 ──────────────────────────────────────────────


def test_preview_cache_key_separates_source_version_and_spec(tmp_path):
    """D：键必须同时认源版本和规格，两者都不能互相污染。"""
    path = tmp_path / 'project.prism'
    with sqlite3.connect(path) as db:
        db.execute('CREATE TABLE sqlar(item_id INTEGER, data BLOB)')
        db.execute('INSERT INTO sqlar VALUES(?,?)', (7, b'BYTES'))
    archive = (str(path), 7)
    key = source_store.preview_cache_key(archive, '.mp4', 'opening-640')
    assert key == source_store.preview_cache_key(archive, '.mp4', 'opening-640')
    # 规格不同 → 键不同
    assert key != source_store.preview_cache_key(archive, '.mp4', 'opening-320')
    # 源版本里带着路径、item_id、size、mtime、inode
    assert key[0] == str(Path(path).resolve())
    assert key[1] == 7
    assert key[2] == Path(path).stat().st_size
    assert key[3] == Path(path).stat().st_mtime_ns
    # 换一版存档 → 键不同
    other = tmp_path / 'other.prism'
    with sqlite3.connect(other) as db:
        db.execute('CREATE TABLE sqlar(item_id INTEGER, data BLOB)')
        db.execute('INSERT INTO sqlar VALUES(?,?)', (7, b'BYTES'))
    assert key != source_store.preview_cache_key((str(other), 7), '.mp4', 'opening-640')


def test_the_extracted_file_knows_which_version_it_came_from(archive):
    """D：提取件记得自己是哪一版抽出来的。"""
    source, data = archive
    path = source_store.retain_archive_video(source, '.mp4')
    version = source_store.stream_source_version(path)
    assert version is not None
    assert version[0] == str(Path(source[0]).resolve())
    assert version[1] == 7


# ── E 租约到期释放临时文件 ────────────────────────────────────


def test_an_expired_lease_is_reclaimed(archive):
    """E：租约到期 → 临时文件可以被回收。"""
    source, data = archive
    path = source_store.retain_archive_video(source, '.mp4')
    assert Path(path).is_file()
    assert source_store.reclaim_expired_materializations() == ()
    assert Path(path).is_file(), '租约没到期就回收了'
    later = time.monotonic() + source_store.MATERIALIZATION_LEASE_SECONDS + 1
    assert path in source_store.reclaim_expired_materializations(now=later)
    assert not Path(path).exists()


def test_a_held_extraction_is_never_reclaimed(archive):
    """E：正在被读的文件不回收 —— 保存读一半被删掉就是数据丢失。"""
    source, data = archive
    path = source_store.retain_archive_video(source, '.mp4')
    later = time.monotonic() + source_store.MATERIALIZATION_LEASE_SECONDS + 1
    with source_store.materialization_hold(path):
        assert path not in source_store.reclaim_expired_materializations(now=later)
        assert Path(path).is_file()
    assert Path(path).is_file()


def test_an_item_that_points_at_the_extraction_keeps_it_alive(qapp, archive):
    """E：还有 item 指着的提取件不会被回收。"""
    source, data = archive
    item = _video_item(source)
    path = item._ensure_materialized()
    later = time.monotonic() + source_store.MATERIALIZATION_LEASE_SECONDS + 1
    assert path not in source_store.reclaim_expired_materializations(now=later)
    assert Path(path).is_file()


# ── F 红线（必过项）：保存 / 导出必须是存档里的原始字节 ────────


def _project_with_video(main_window, tmp_path):
    """造一个内嵌了视频的工程，并返回 (工程路径, 原始字节, 外部文件)。"""
    scene = main_window.view.scene
    data = b'ORIGINAL-VIDEO-BYTES\x00' * 30000
    external = tmp_path / 'clip.mp4'
    external.write_bytes(data)
    item = PrismVideoItem(filename=str(external))
    item._video_blob = data
    scene.addItem(item)
    project = str(tmp_path / 'source.prism')
    fileio.save_prism(project, scene, create_new=True)
    return project, data, external


def test_save_after_the_project_moved_keeps_the_original_bytes(main_window, tmp_path):
    """F 🔴 必过项：工程挪走、外部原件删掉，保存仍然写进原始字节。"""
    project, data, external = _project_with_video(main_window, tmp_path)
    moved_dir = tmp_path / 'moved'
    moved_dir.mkdir()
    moved = moved_dir / 'moved.prism'
    shutil.copyfile(project, moved)
    external.unlink()
    assert not external.exists()

    scene = main_window.view.scene
    scene.clear()
    fileio.load_prism(str(moved), scene)
    scene.add_queued_items()
    restored = next(i for i in scene.items()
                     if getattr(i, 'TYPE', None) == 'video')
    assert restored._prism_source[0] == str(moved)
    assert not os.path.exists(restored.filename), '外部原件应当已经没了'

    out = str(tmp_path / 'out.prism')
    fileio.save_prism(out, scene, create_new=True)
    with sqlite3.connect(out) as db:
        rows = db.execute('SELECT data FROM sqlar').fetchall()
    assert [row[0] for row in rows] == [data], '保存没有写进存档里的原始字节'


def test_export_after_the_project_moved_keeps_the_original_bytes(main_window, tmp_path):
    """F 🔴 必过项：导出同样必须来自 .prism 存档，不是外部旧路径。"""
    project, data, external = _project_with_video(main_window, tmp_path)
    moved_dir = tmp_path / 'moved'
    moved_dir.mkdir()
    moved = moved_dir / 'moved.prism'
    shutil.copyfile(project, moved)
    external.unlink()

    scene = main_window.view.scene
    scene.clear()
    fileio.load_prism(str(moved), scene)
    scene.add_queued_items()
    restored = next(i for i in scene.items()
                     if getattr(i, 'TYPE', None) == 'video')
    assert not os.path.exists(restored.filename)

    service = ResourceTreeService('p1')
    page = service.createPage('canvas', '画布', 'canvas-1')
    exporter = ResourceSubtreeExporter(
        service, pages=[_canvas_page()],
        canvas_items=lambda page_id: [restored])
    plan = exporter.build_plan('canvas', page['id'])
    write_directory(plan, str(tmp_path / 'out'))
    exported = tmp_path / 'out' / '画布' / 'clip.mp4'
    assert exported.read_bytes() == data, '导出没有用存档里的原始字节'


def test_a_video_without_an_archive_is_not_exported_from_an_outside_file(qapp, tmp_path):
    """F 🔴 必过项：没有存档来源时宁可不导出，也不拿外部文件凑数。"""
    outside = tmp_path / 'outside.mp4'
    outside.write_bytes(b'OUTSIDE-BYTES')
    item = PrismVideoItem(filename=str(outside))
    item._video_url = None
    service = ResourceTreeService('p1')
    service.createPage('canvas', '画布', 'canvas-1')
    exporter = ResourceSubtreeExporter(
        service, pages=[_canvas_page()],
        canvas_items=lambda page_id: [item])
    plan = exporter.build_plan('canvas')
    write_directory(plan, str(tmp_path / 'out'))
    assert not (tmp_path / 'out' / '画布' / 'outside.mp4').exists()


# ── G 打开工程不提取无关视频 ──────────────────────────────────


def test_opening_a_project_extracts_no_video_bytes(main_window, tmp_path):
    """G：打开工程只登记源引用，不产生任何视频临时文件。"""
    project, data, external = _project_with_video(main_window, tmp_path)
    before = _temp_files()
    scene = main_window.view.scene
    scene.clear()
    fileio.load_prism(project, scene)
    scene.add_queued_items()
    assert _temp_files() == before, '打开工程提取了视频字节'
    video = next(i for i in scene.items()
                 if getattr(i, 'TYPE', None) == 'video')
    assert video._video_source_path is None
    assert video._prism_source is not None
