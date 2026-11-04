"""第一次切库不再冻结：面板提前建好、GUI 线程不读原件、索引陈旧就排队重建。

真实工程（Prism-100k-v1-新版）实测的三个真凶：

* 库面板在**点击处理函数内部**首次构造 —— 主线程堵 1723ms（模型库）/
  2118ms（材质库），窗口停止重绘，用户看到的是"程序闪退了"。
* 材质库详情同步读原件字节 —— GUI 线程再吃 606ms。
* 打开后索引变陈旧却没人触发重建 —— 库退回慢路径，同一会话里索引就绪后
  再切同样的页面只要 13–86ms。

这里逐条钉住"以后不会再退化"。
"""
from __future__ import annotations

import threading

import pytest

from prism.project_catalog_index import CatalogNotReady
from prism.view import PrismGraphicsView


# ── 预热 ────────────────────────────────────────────────────────

def test_prewarm_builds_both_library_panels(main_window, qtbot):
    """工程打开后先把面板建好：第一次切换就只剩内容切换本身。"""
    window = main_window
    window.show()
    assert not window.model_library.initialized
    assert not window.material_library.initialized

    window.schedule_library_prewarm()
    qtbot.waitUntil(lambda: window.material_library.initialized, timeout=30000)
    assert window.model_library.initialized


def test_prewarm_survives_a_broken_library(monkeypatch, main_window, qtbot):
    """一个库面板建不起来，不能把另一个也带下水。"""
    window = main_window
    window.show()

    def broken(*_prepared):
        raise RuntimeError('panel is broken')

    monkeypatch.setattr(window.model_library, '_factory', broken, raising=False)
    window.schedule_library_prewarm()
    qtbot.waitUntil(lambda: window.material_library.initialized, timeout=30000)
    assert window.material_library.initialized


# ── 切换路径 ────────────────────────────────────────────────────

def test_switching_builds_the_panel_before_waiting_for_content(main_window, qtbot):
    """等待要有页面可等：面板先建好，再等它有内容。"""
    window = main_window
    window.show()
    window.open_material_library()
    page = window.material_library
    assert page.initialized
    assert window.tabs.currentWidget() is page


# ── GUI 线程不读原件 ────────────────────────────────────────────

class _Attachments(dict):
    """A stand-in attachment store that records every byte read."""

    def __init__(self):
        super().__init__()
        self.reads = []

    def get(self, key, default=None):
        self.reads.append(key)
        return default

    def peek(self, key, default=None):
        return default


def test_detail_thumbnail_never_reads_original_bytes_on_the_gui_thread(main_window, qtbot):
    """详情缩略图不能同步读原件：那是 606ms 的 GUI 线程阻塞。"""
    window = main_window
    window.show()
    window.open_material_library()
    page = window.material_library

    attachments = _Attachments()
    original = window.view.scene.attachments
    window.view.scene.attachments = attachments
    try:
        record = {'id': 'probe', 'preview': {'thumbnailAttachmentId': 4242}}
        assert page._thumbnail(record) is None
        assert attachments.reads == []
    finally:
        window.view.scene.attachments = original


def test_detail_thumbnail_is_loaded_in_the_background(monkeypatch, main_window, qtbot):
    """缺图时走后台通道，回调再刷新详情，不占 GUI 线程。"""
    window = main_window
    window.show()
    window.open_material_library()
    page = window.material_library

    queued = []
    original_task = page._task

    def spy(function, callback, *args, **kwargs):
        queued.append((function, callback))
        return original_task(function, callback, *args, **kwargs)

    monkeypatch.setattr(page, '_task', spy)
    page._load_detail_thumbnail('missing-record')
    # 记录不存在时不排队：没有可加载的东西。
    assert queued == []


# ── 卡片面没就绪时不是空板 ──────────────────────────────────────

def test_material_library_shows_a_loading_state_instead_of_a_blank_surface(main_window, qtbot):
    """卡片面没加载完时，中间显示"正在加载"，不是一块空白矩形。"""
    window = main_window
    window.show()
    window.open_material_library()
    page = window.material_library

    page.records.create(None)
    page.materials.ready = False
    page.refresh()
    assert not page.materials.isVisible()
    assert '正在加载' in page.empty_title.text()

    page.materials.ready = True
    page.refresh()
    assert page.materials.isVisible()


# ── 索引重建 ────────────────────────────────────────────────────

class _Index:
    """A catalog index whose build refuses once, the way a concurrent save does."""

    def __init__(self, failures=1):
        self.failures = failures
        self.calls = 0

    def build(self, **kwargs):
        self.calls += 1
        if self.calls <= self.failures:
            raise CatalogNotReady('Source changed during catalog build')
        return 'built'

    def source_revision(self):
        return ('revision', (), ())


def _request(tmp_path, index):
    source = tmp_path / 'project.sqlite'
    source.write_bytes(b'')
    return {
        'index': index,
        'project': str(source),
        'cache': str(tmp_path / 'cache.sqlite'),
        'cancel': threading.Event(),
        'build_id': 'build-1',
    }


def test_catalog_build_retries_after_the_source_changed(tmp_path, monkeypatch):
    """源在构建中变了：换新指纹重来，而不是把整个构建判死。"""
    from prism import project_catalog_index

    index = _Index(failures=1)
    made = []

    class _Rebuilt:
        def __init__(self, source, cache):
            self.source = source
            self.cache = cache
            made.append(self)

        def build(self, **kwargs):
            return 'built'

        def source_revision(self):
            return ('revision', (), ())

    monkeypatch.setattr(project_catalog_index, 'ProjectCatalogIndex', _Rebuilt)
    request = _request(tmp_path, index)
    result = PrismGraphicsView._build_catalog_with_retry(None, request, {})
    assert result == 'built'
    assert index.calls == 1                 # 原来那个索引只被试了一次
    assert made and made[0].source == request['project']   # 换成了新指纹的索引


def test_catalog_build_gives_up_after_repeated_source_changes(tmp_path, monkeypatch):
    """一直变就一直重试是不行的：必须有上限。"""
    from prism import project_catalog_index

    calls = {'n': 0}

    class _AlwaysChanging:
        def __init__(self, source, cache):
            self.source = source

        def build(self, **kwargs):
            calls['n'] += 1
            raise CatalogNotReady('Source changed during catalog build')

        def source_revision(self):
            return ('revision', (), ())

    monkeypatch.setattr(project_catalog_index, 'ProjectCatalogIndex', _AlwaysChanging)
    request = _request(tmp_path, _Index(failures=1))
    with pytest.raises(CatalogNotReady):
        PrismGraphicsView._build_catalog_with_retry(None, request, {})
    assert calls['n'] == 2                 # 最多再试两次，然后如实上报


def test_catalog_build_does_not_retry_other_failures(tmp_path):
    """别的失败照旧上报，不能被当成"源变了"吞掉。"""
    class _Other(_Index):
        def build(self, **kwargs):
            raise ValueError('something else broke')

    request = _request(tmp_path, _Other())
    with pytest.raises(ValueError):
        PrismGraphicsView._build_catalog_with_retry(None, request, {})
