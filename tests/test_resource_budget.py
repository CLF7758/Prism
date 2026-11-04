"""Shared resident-cache pressure must evict real bytes, not only count them."""
import gc
import threading
import weakref

from PyQt6 import QtGui

from prism.resource_budget import CacheBudget
from prism.material_cache import MaterialCache


class Cache:
    def __init__(self, size, pinned=False):
        self.bytes = size
        self.pinned = pinned
        self.trim_threads = []

    def _trim_cache_to(self, target):
        self.trim_threads.append(threading.get_ident())
        if not self.pinned:
            self.bytes = min(self.bytes, target)


def test_global_pressure_reclaims_lower_priority_first():
    budget = CacheBudget(100)
    image, video = Cache(80), Cache(60)
    budget.register(image, category='image', priority=80)
    budget.register(video, category='video', priority=20)
    assert budget.enforce() == 40
    assert image.bytes == 80
    assert video.bytes == 20
    assert budget.snapshot()['bytes'] == 100


def test_equal_priority_uses_last_access():
    budget = CacheBudget(100)
    old, recently_used = Cache(80), Cache(60)
    budget.register(recently_used, category='cache')
    budget.register(old, category='cache')
    budget.touch(recently_used)
    budget.enforce()
    assert old.bytes == 40
    assert recently_used.bytes == 60


def test_pinned_content_reports_real_pressure_instead_of_hiding_it():
    budget = CacheBudget(10)
    visible = Cache(30, pinned=True)
    budget.register(visible, category='image')
    assert budget.enforce() == 0
    assert budget.snapshot()['over_budget'] == 20


def test_registration_does_not_retain_cache_or_its_window():
    budget = CacheBudget(100)
    cache = Cache(50)
    reference = weakref.ref(cache)
    budget.register(cache, category='image')
    del cache
    gc.collect()
    assert reference() is None
    assert budget.snapshot()['bytes'] == 0
    assert budget.snapshot()['caches'] == 0


def test_destroyed_qt_wrapper_is_pruned_without_destroyed_callback(qapp):
    from PyQt6 import QtCore, sip
    budget = CacheBudget(100)
    owner = QtCore.QObject()
    owner.bytes = 30
    owner._trim_cache_to = lambda _target: None
    budget.register(owner, category='qt')
    sip.delete(owner)
    assert budget.snapshot()['bytes'] == 0
    assert budget.snapshot()['caches'] == 0


def test_foreign_thread_never_executes_gui_eviction():
    budget = CacheBudget(0)
    ready, finish = threading.Event(), threading.Event()
    cache = Cache(30)

    def worker():
        budget.register(cache, category='worker')
        ready.set()
        assert finish.wait(3)
        budget.enforce()

    task = threading.Thread(target=worker)
    task.start()
    try:
        assert ready.wait(3)
        budget.enforce()
        assert cache.trim_threads == []
        assert budget.snapshot()['over_budget'] == 30
    finally:
        finish.set()
        task.join(3)
    assert not task.is_alive()
    assert cache.trim_threads == [task.ident]
    assert budget.snapshot()['bytes'] == 0


def test_material_caches_share_one_resident_limit(monkeypatch):
    from prism import resource_budget
    budget = CacheBudget(10)
    monkeypatch.setattr(resource_budget, '_shared', budget)
    first, second = MaterialCache(10, 100), MaterialCache(10, 100)
    first['one'] = b'1234567'
    second['two'] = b'12345678'
    assert first.bytes == 0
    assert second.bytes == 8
    assert budget.snapshot()['bytes'] == 8
    assert second['two'] == b'12345678'


def test_display_cache_participates_and_protects_visible_detail(qapp, monkeypatch):
    from prism import resource_budget
    from prism.lod import DisplayCache
    from prism.items import PrismPixmapItem
    budget = CacheBudget(1)
    monkeypatch.setattr(resource_budget, '_shared', budget)
    cache = DisplayCache(qapp)
    image = QtGui.QImage(8, 8, QtGui.QImage.Format.Format_RGBA8888)
    image.fill(0)
    item = PrismPixmapItem(image)
    monkeypatch.setattr(cache, '_entry_visible', lambda _item: True)
    cache.store(item, 32, QtGui.QPixmap.fromImage(image), notify=False)
    assert cache.bytes == 8*8*4
    assert budget.snapshot()['over_budget'] == 255
    monkeypatch.setattr(cache, '_entry_visible', lambda _item: False)
    budget.enforce()
    assert cache.bytes == 0
    assert budget.snapshot()['over_budget'] == 0


def test_video_preview_cache_is_reclaimed_without_touching_sources(qapp, monkeypatch):
    from prism import resource_budget
    from prism.video_thumbnails import Thumbnails
    budget = CacheBudget(0)
    monkeypatch.setattr(resource_budget, '_shared', budget)
    service = Thumbnails(qapp)
    image = QtGui.QImage(8, 8, QtGui.QImage.Format.Format_RGBA8888)
    image.fill(0)
    service.done(('source', 1), image, {'width': 8, 'height': 8})
    assert service.bytes == 0
    assert service.cache == {}
    assert budget.snapshot()['bytes'] == 0
    service.close()
