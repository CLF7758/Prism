"""The material row pictures must not stand in front of the model preview.

Measured on the 100k project (trace 156040): every material row asked for its
own little sphere preview, and that preview is a *native* render of a heavy
model - 0.3 s to 1.7 s each.  All of them ran on the same single-threaded
lane as the interactive model preview, so:

  * the interactive preview of the next model waited 3.3 s behind the row
    pictures of the previous model,
  * the last row picture of a twelve material model waited 12.4 s.

These tests pin the two properties that fix it:

  * the row picture has its own lane, and a busy row lane leaves the render
    lane free for the preview the user is waiting for,
  * the native helper is asked to start while the request artifact is still
    being written, so its VTK start-up overlaps that work instead of
    following it.
"""
import threading
import time

from prism.model_library.multimaterial.application import ProjectMaterialService
from prism.model_library.multimaterial.contracts import (
    MaterialAssignment, MaterialState, MaterialTarget, WorkingMaterial)
from prism.model_library.multimaterial.scheduling import MaterialScheduler


def make_target(model_id='model-a'):
    return MaterialTarget('session', 1, model_id, 1, 1, 1)


def build_service(tmp_path):
    """A service with every lane the real one owns."""
    service = object.__new__(ProjectMaterialService)
    service._closed = False
    service._cancelled = set()
    service._jobs = {}
    service._job_requests = {}
    service._lock = threading.RLock()
    service._root = tmp_path
    service._pool = MaterialScheduler()
    service._render_pool = MaterialScheduler()
    service._texture_pool = MaterialScheduler()
    service._thumbnail_pool = MaterialScheduler()
    service._native_ready = set()
    service._native_running = {}
    service._native_traces = {}
    service._active = None
    service.owner = type('Owner', (), {'_owner_window_id': 0})()
    published = []
    service._publish = lambda **event: published.append(event)
    return service, published


def build_state(model_id='model-a'):
    target = make_target(model_id)
    return MaterialState(target=target,
                         materials=(WorkingMaterial('w-1', 'm-1', 'Wood'),),
                         assignments=(MaterialAssignment('b-1', 'w-1'),))


def test_the_row_picture_has_its_own_lane(tmp_path):
    """A row picture must not be queued on the render lane."""
    service, _ = build_service(tmp_path)
    try:
        lanes = []
        service._queue = lambda kind, target, fn, **kwargs: lanes.append(
            kwargs.get('lane')) or 'job'
        service.thumbnail(make_target(), 'working:w-1')
        assert lanes == [service._thumbnail_pool]
        assert service._thumbnail_pool is not service._render_pool
    finally:
        for pool in (service._pool, service._render_pool, service._texture_pool,
                     service._thumbnail_pool):
            pool.shutdown()


def test_a_busy_row_lane_does_not_hold_up_the_preview(tmp_path):
    """The render lane stays free while row pictures are still running."""
    service, _ = build_service(tmp_path)
    try:
        # A row picture that takes a long time occupies the row lane.
        release = threading.Event()
        service._thumbnail_pool.submit(release.wait, 30)

        done = []
        service._schedule_draw(make_target(), lambda _job: done.append(True),
                               ('scene', 'model-a'))
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and not done:
            time.sleep(.02)
        assert done, 'the preview waited for the row picture lane'
    finally:
        release.set()
        for pool in (service._pool, service._render_pool, service._texture_pool,
                     service._thumbnail_pool):
            pool.shutdown()


def test_the_helper_starts_while_the_artifact_is_written(tmp_path, monkeypatch):
    """The VTK start-up must overlap the artifact, not follow it."""
    from prism.model_library.multimaterial import presentation

    order = []

    class Controller:
        def warm(self):
            order.append('warm')

        def submit(self, request, **kwargs):
            order.append('submit')
            return 'job-1'

        def wait(self, job, timeout=0):
            order.append('wait')
            return {'state': 'completed'}

        def forget(self, job):
            order.append('forget')

    monkeypatch.setattr(presentation, 'material_artifact',
                        lambda *args, **kwargs: order.append('artifact') or 'artifact.json')
    service, _ = build_service(tmp_path)
    service.controller = Controller()
    service._resolved = lambda item, working_id, state=None: object()
    item = {'path': 'model.glb', 'payload': {'name': 'Wood'},
            'catalog': {}, 'state': build_state()}
    try:
        service._render(item, build_state(), 'request-1')
    finally:
        for pool in (service._pool, service._render_pool, service._texture_pool,
                     service._thumbnail_pool):
            pool.shutdown()
    assert order.index('warm') < order.index('artifact'), \
        'the helper only started after the artifact was written: %r' % (order,)
    assert order.index('warm') < order.index('submit')


def test_warming_the_controller_does_not_wait_for_the_helper(tmp_path):
    """``warm`` starts the helper and returns before the handshake answers.

    The blocking call is ``handshake``; ``warm`` must use the asynchronous
    start instead, so the caller can go on preparing the request.
    """
    from prism.model_library.native_jobs import NativeModelJobController, RuntimeConfig

    controller = NativeModelJobController(RuntimeConfig(cache_root=tmp_path))
    started, waited = [], []
    controller._ensure_started = lambda: started.append(True)
    controller.handshake = lambda *args, **kwargs: waited.append(True)
    try:
        controller.warm()
    finally:
        controller.shutdown()
    assert started, 'warm did not start the helper'
    assert not waited, 'warm waited for the handshake'
