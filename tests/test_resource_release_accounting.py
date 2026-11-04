"""A project that is closed must give its bytes back, and must say so.

Two things are pinned here:

* the independent over-budget counting point of the shared cache budget
  (:meth:`prism.resource_budget.CacheBudget.enforce`). An excess that could
  not be freed -- a pinned visible detail, a cache owned by another thread --
  used to be invisible: nothing was reclaimed, so no snapshot was written.
  It is now counted on its own, so "no over budget" and "not recorded" can
  never be confused.
* the three project-lifecycle entry points that have no caller in ``prism/``
  yet: :func:`prism.image_decode.cancel_project_decodes`,
  :func:`prism.resource_budget.open_project_resources` and
  :func:`prism.resource_budget.release_project_resources`. They are called
  directly, so their contract is fixed before they are wired into the
  project lifecycle.
"""
import pytest


class Cache:
    """A registered cache whose trim can refuse to free anything."""

    def __init__(self, size, pinned=False, floor=0):
        self.bytes = size
        self.pinned = pinned
        self.floor = floor
        self.trimmed_to = []

    def _trim_cache_to(self, target):
        self.trimmed_to.append(target)
        if not self.pinned:
            self.bytes = max(self.floor, min(self.bytes, target))


class FakeDispatcher:
    """Stands in for the display decoder of the running application."""

    def __init__(self, dropped=0):
        self.dropped = dropped
        self.keys = []

    def cancel_project(self, key):
        self.keys.append(key)
        return self.dropped


@pytest.fixture
def budget(monkeypatch):
    """A shared cache budget of its own, with the module scope state kept."""
    from prism import resource_budget
    own = resource_budget.CacheBudget(1000)
    monkeypatch.setattr(resource_budget, '_shared', own)
    saved_current = resource_budget.current_project_scope()
    saved_scopes = dict(resource_budget._scopes)
    yield own
    resource_budget.set_current_project_scope(saved_current)
    resource_budget._scopes.clear()
    resource_budget._scopes.update(saved_scopes)


# ── the independent over-budget counting point ──────────────────────────


def test_pinned_excess_is_counted_even_when_nothing_was_reclaimed(budget):
    budget.limit = 10
    visible = Cache(30, pinned=True)
    budget.register(visible, category='image')
    assert budget.enforce() == 0
    assert budget.snapshot()['over_budget'] == 20
    # The counting point does not depend on the reclaim path: the excess is
    # visible even though not a single byte could be freed.
    assert budget.over_budget_events == 1
    assert budget.last_over_budget['over_budget'] == 20
    assert budget.last_over_budget['reclaimed'] == 0


def test_no_over_budget_event_while_the_budget_is_respected(budget):
    cache = Cache(40)
    budget.register(cache, category='image')
    assert budget.enforce() == 0
    assert budget.over_budget_events == 0
    assert budget.last_over_budget is None


def test_a_partial_reclaim_that_leaves_an_excess_records_both(budget):
    budget.limit = 100
    loose = Cache(100, floor=50)  # cannot give up its last 50 bytes
    visible = Cache(60, pinned=True)
    budget.register(loose, category='image', priority=20)
    budget.register(visible, category='image', priority=80)
    assert budget.enforce() == 50
    assert budget.snapshot()['over_budget'] == 10
    assert budget.over_budget_events == 1
    assert budget.last_over_budget['over_budget'] == 10
    assert budget.last_over_budget['reclaimed'] == 50


def test_over_budget_is_written_to_the_operation_log(budget, monkeypatch):
    from prism import performance_trace
    events = []
    monkeypatch.setattr(performance_trace, 'record',
                        lambda kind, **fields: events.append((kind, fields)))
    budget.limit = 10
    visible = Cache(30, pinned=True)
    budget.register(visible, category='image')
    budget.enforce()
    kinds = [kind for kind, _fields in events]
    assert 'cache_budget_over_budget' in kinds
    over = [fields for kind, fields in events if kind == 'cache_budget_over_budget'][0]
    assert over['over_budget'] == 20
    assert over['limit'] == 10
    assert over['reclaimed'] == 0
    assert over['over_budget_events'] == 1
    # The snapshot carries the excess too, so a reader of the operation log
    # does not have to know about the extra event to see it.
    snapshots = [fields for kind, fields in events if kind == 'cache_budget_snapshot']
    assert snapshots and snapshots[-1]['over_budget'] == 20
    assert snapshots[-1]['over_budget_events'] == 1


def test_the_snapshot_carries_the_over_budget_counter(budget):
    budget.limit = 10
    visible = Cache(30, pinned=True)
    budget.register(visible, category='image')
    assert budget.snapshot()['over_budget_events'] == 0
    budget.enforce()
    assert budget.snapshot()['over_budget_events'] == 1
    budget.enforce()
    assert budget.snapshot()['over_budget_events'] == 2


def test_the_excess_is_counted_even_when_it_is_owned_by_another_thread(budget):
    import threading
    budget.limit = 10
    ready, finish = threading.Event(), threading.Event()
    cache = Cache(30)

    def worker():
        # Registered on the worker thread, so the GUI thread must not evict
        # it -- the excess stays and has to be counted.
        budget.register(cache, category='worker')
        ready.set()
        finish.wait(3)
        budget.enforce()

    task = threading.Thread(target=worker)
    task.start()
    try:
        assert ready.wait(3)
        # The GUI thread must not evict the worker's cache, so the excess
        # stays -- and has to be counted here.
        assert budget.enforce() == 0
        assert budget.over_budget_events == 1
        assert budget.last_over_budget['over_budget'] == 20
    finally:
        finish.set()
        task.join(3)
    assert not task.is_alive()


def test_the_snapshot_reports_the_resident_bytes_and_the_last_reclaim(budget):
    budget.limit = 10
    cache = Cache(30)
    budget.register(cache, category='image')
    budget.enforce()
    snapshot = budget.snapshot()
    assert snapshot['bytes'] == 10
    assert snapshot['resident_bytes'] == 10
    assert snapshot['reclaimed'] == 20


def test_the_resident_bytes_include_the_foreign_caches(budget, tmp_path):
    budget.activate_scope(str(tmp_path / 'active.prism'))
    cache = Cache(30)
    budget.register(cache, category='image', scope=str(tmp_path / 'other.prism'))
    snapshot = budget.snapshot()
    assert snapshot['bytes'] == 0
    assert snapshot['foreign_bytes'] == 30
    assert snapshot['resident_bytes'] == 30


def test_the_decoder_samples_its_pending_count(qapp, monkeypatch):
    from prism import performance_trace
    from prism.image_decode import DisplayDecoder
    events = []
    monkeypatch.setattr(performance_trace, 'record',
                        lambda kind, **fields: events.append((kind, fields)))
    service = DisplayDecoder(qapp)
    try:
        service._sample_pending()
        kinds = [kind for kind, _fields in events]
        assert 'image_decode_pending' in kinds
        sample = [fields for kind, fields in events if kind == 'image_decode_pending'][0]
        assert sample['pending'] == 0
        assert sample['max_pending'] == 8
        assert sample['max_pending_seen'] == 0
    finally:
        service.shutdown()


# ── the display decoder must keep making progress ───────────────────────


def _image_item(scene, tmp_path, name):
    """One image item in a scene, with its own source file."""
    from PyQt6 import QtGui
    from prism import fileio
    image = QtGui.QImage(1600, 900, QtGui.QImage.Format.Format_ARGB32)
    image.fill(QtGui.QColor(40, 80, 120, 128))
    path = tmp_path / name
    assert image.save(str(path))
    item = fileio._load_image_item(str(path))
    scene.addItem(item)
    return item


def test_a_temporary_file_that_cannot_be_removed_keeps_the_pixels(qapp, tmp_path, monkeypatch):
    """A decode that succeeded must not be thrown away by a stuck file.

    Windows can still hold the temporary source when the decoder is done with
    it. The image was read before that happens, so a cleanup error must not
    turn a successful decode into a failed one.
    """
    import tempfile
    from PyQt6 import QtCore
    from prism import image_decode
    image_path = tmp_path / 'source.png'
    from PyQt6 import QtGui
    image = QtGui.QImage(1600, 900, QtGui.QImage.Format.Format_ARGB32)
    image.fill(QtGui.QColor(40, 80, 120, 128))
    assert image.save(str(image_path))
    state = {'raised': False}

    def broken_cleanup(self):
        if not state['raised']:
            state['raised'] = True
            raise OSError('the file is still open')

    monkeypatch.setattr(tempfile.TemporaryDirectory, 'cleanup', broken_cleanup)
    decoded, size = image_decode.decode(blob=image_path.read_bytes(), max_side=192)
    assert not decoded.isNull()
    assert size == QtCore.QSize(1600, 900)


def test_a_failed_decode_waits_before_it_is_retried(qapp, qtbot, tmp_path, monkeypatch):
    """A failing item must not take the slot of every other visible item.

    The owner asks for the missing items in the same order on every round, so
    an item that is retried at once takes a slot again and again.
    """
    from PyQt6 import QtWidgets
    from prism import image_decode, image_pyramid
    scene = QtWidgets.QGraphicsScene()
    item = _image_item(scene, tmp_path, 'source.png')
    monkeypatch.setattr(image_pyramid, 'decode_display', lambda *args, **kwargs: None)
    service = image_decode.DisplayDecoder(qapp)

    class Owner(QtWidgets.QWidget):
        def isVisible(self): return True
        def needs_display(self, item): return True
        def update(self): pass

    owner = Owner()
    try:
        service.request(item, 512, owner)
        qtbot.waitUntil(lambda: getattr(item, '_display_decode_failed', False), timeout=5000)
        # The failed item is not queued again straight away ...
        service.request(item, 512, owner)
        assert item._display_decode_request is None
        assert owner in service.waiting_owners
        # ... but it is retried once the pause is over.
        monkeypatch.setattr(image_decode, 'DECODE_RETRY_BACKOFF', 0)
        service.request(item, 512, owner)
        assert item._display_decode_request is not None
    finally:
        service.shutdown()


def test_one_item_that_cannot_be_decoded_does_not_hold_up_the_others(qapp, qtbot, tmp_path, monkeypatch):
    """The decoder must keep decoding the other visible items."""
    from PyQt6 import QtWidgets
    from prism import image_decode, image_pyramid, lod
    scene = QtWidgets.QGraphicsScene()
    items = [_image_item(scene, tmp_path, f'source-{index}.png') for index in range(12)]
    broken = items[0]._source_path
    real_decode = image_pyramid.decode_display

    def selective(blob=None, path=None, archive=None, max_side=None):
        if path == broken:
            return None
        return real_decode(blob=blob, path=path, archive=archive, max_side=max_side)

    monkeypatch.setattr(image_pyramid, 'decode_display', selective)
    service = image_decode.DisplayDecoder(qapp)

    class Owner(QtWidgets.QWidget):
        def isVisible(self): return True
        def needs_display(self, item): return True
        def update(self):
            for item in items:
                if lod.cache().get(item, 512) is None:
                    service.request(item, 512, self)

    owner = Owner()
    try:
        owner.update()
        assert len(service.requests) <= service.max_pending
        qtbot.waitUntil(lambda: all(lod.cache().get(item, 512) is not None for item in items[1:]),
                        timeout=10000)
        assert lod.cache().get(items[0], 512) is None
    finally:
        service.shutdown()


# ── the project lifecycle entry points ───────────────────────────────────


def test_the_scope_key_is_the_resolved_project_path(tmp_path):
    from prism.resource_budget import project_scope_key
    project = tmp_path / 'project.prism'
    assert project_scope_key(str(project)) == str(project.resolve())
    assert project_scope_key(None) is None


def test_cancel_project_decodes_passes_the_resolved_project_path(qapp, tmp_path, monkeypatch):
    from prism.image_decode import cancel_project_decodes
    dispatcher = FakeDispatcher(dropped=3)
    monkeypatch.setattr(qapp, '_prism_display_decoder', dispatcher, raising=False)
    project = tmp_path / 'project.prism'
    assert cancel_project_decodes(str(project)) == 3
    # The decoder compares the scope key, so the path is normalized first.
    assert dispatcher.keys == [str(project.resolve())]


def test_cancel_project_decodes_is_safe_without_an_application(monkeypatch):
    from prism import image_decode

    class NoApplication:
        @staticmethod
        def instance():
            return None

    monkeypatch.setattr(image_decode.QtWidgets, 'QApplication', NoApplication)
    assert image_decode.cancel_project_decodes('C:/somewhere/project.prism') == 0


def test_cancel_project_decodes_is_safe_without_a_dispatcher(qapp, monkeypatch):
    from prism.image_decode import cancel_project_decodes
    monkeypatch.setattr(qapp, '_prism_display_decoder', None, raising=False)
    assert cancel_project_decodes('C:/somewhere/project.prism') == 0


def test_open_project_resources_registers_a_scope_the_workers_can_find(budget, tmp_path):
    from prism import resource_budget
    project = tmp_path / 'project.prism'
    scope = resource_budget.open_project_resources(str(project))
    assert scope.key == str(project.resolve())
    assert resource_budget.scope_for(str(project)) is scope
    assert resource_budget.current_project_scope() is scope


def test_closing_a_project_trims_the_process_wide_caches(budget, tmp_path):
    """The caches of ``prism/lod.py`` are registered process-wide."""
    from prism import resource_budget
    project = tmp_path / 'project.prism'
    resource_budget.open_project_resources(str(project))
    cache = Cache(400)
    budget.register(cache, category='image_display')
    assert budget.snapshot()['bytes'] == 400
    resource_budget.release_project_resources(str(project))
    assert cache.bytes == 0
    assert budget.snapshot()['bytes'] == 0


def test_release_project_resources_reports_what_it_released(budget, tmp_path):
    from prism import resource_budget
    project = tmp_path / 'project.prism'
    scope = resource_budget.open_project_resources(str(project))
    cache = Cache(400)
    budget.register(cache, category='image_display')
    report = resource_budget.release_project_resources(str(project))
    assert report['scope'] == scope.key
    assert report['bytes'] == 400
    assert budget.snapshot()['bytes'] == 0
    assert resource_budget.current_project_scope() is None


def test_release_project_resources_cancels_the_pending_decodes_of_that_project(budget, qapp, tmp_path, monkeypatch):
    from prism import resource_budget
    dispatcher = FakeDispatcher(dropped=2)
    monkeypatch.setattr(qapp, '_prism_display_decoder', dispatcher, raising=False)
    project = tmp_path / 'project.prism'
    resource_budget.open_project_resources(str(project))
    report = resource_budget.release_project_resources(str(project))
    assert report['decodes'] == 2
    assert dispatcher.keys == [str(project.resolve())]


def test_release_project_resources_releases_the_leases_of_the_project(budget, tmp_path):
    from prism import resource_budget
    project = tmp_path / 'project.prism'
    scope = resource_budget.open_project_resources(str(project))
    released = []
    scope.lease('decode_temp', str(tmp_path / 'temporary'), lambda lease: released.append(lease))
    report = resource_budget.release_project_resources(str(project))
    assert report['leases'] == 1
    assert released and released[0].kind == 'decode_temp'
    assert scope.is_cancelled()


def test_release_project_resources_is_idempotent(budget, tmp_path):
    from prism import resource_budget
    project = tmp_path / 'project.prism'
    resource_budget.open_project_resources(str(project))
    first = resource_budget.release_project_resources(str(project))
    assert first['scope'] == str(project.resolve())
    second = resource_budget.release_project_resources(str(project))
    assert second['scope'] is None
    assert second['bytes'] == 0


def test_the_next_project_is_charged_for_the_process_wide_bytes(budget, tmp_path):
    """A process-wide cache holds the bytes of the project that is open."""
    from prism import resource_budget
    first, second = tmp_path / 'first.prism', tmp_path / 'second.prism'
    resource_budget.open_project_resources(str(first))
    cache = Cache(400)
    budget.register(cache, category='image_display')
    resource_budget.open_project_resources(str(second))
    # The bytes of the project that was left are released ...
    assert cache.bytes == 0
    # ... and the cache now belongs to the project that is open, so its
    # bytes are charged to that project instead of counting as foreign.
    cache.bytes = 400
    snapshot = budget.snapshot()
    assert snapshot['foreign_bytes'] == 0
    assert snapshot['bytes'] == 400
    assert snapshot['scopes'] == {str(second.resolve()): 400}
