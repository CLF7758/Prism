"""Replay the material queue of trace 156040 and time the preview.

The trace of the 100k project showed a twelve material model queueing twelve
row pictures and an interactive preview behind them: the preview of the next
model waited 3.3 s for the row pictures of the previous one, and the last row
picture waited 12.4 s.

This probe replays that sequence through the real service with the real
scheduler and a stand-in native helper whose durations are the ones the trace
measured.  The only difference between the two runs is the lane the row
picture is queued on, so the difference in the numbers is the price of the
shared lane.

Run: ``python -u tests/_audit_material_lane.py``
"""
from __future__ import annotations

import json
import sys
import tempfile
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from prism.model_library.multimaterial import presentation
from prism.model_library.multimaterial.application import ProjectMaterialService
from prism.model_library.multimaterial.contracts import (
    MaterialAssignment, MaterialState, MaterialTarget, TextureUse, WorkingMaterial)
from prism.model_library.multimaterial.scheduling import MaterialScheduler

#: The native durations the trace measured, in the order it measured them.
THUMBNAIL_RENDER_S = (2.151, 1.574, 1.700, 0.469, 0.453)
#: The interactive preview of a twelve material model.
PREVIEW_RENDER_S = 3.759
#: How long the helper needs before it answers the handshake.
WORKER_START_S = 1.477

ROW_MATERIALS = 12


class NativeHelper:
    """Stands in for the native worker with the durations of the trace."""

    def __init__(self):
        self.calls = []
        self.lock = threading.Lock()

    def warm(self):
        time.sleep(WORKER_START_S)

    def submit(self, request, **kwargs):
        kind = kwargs.get('kind', '')
        with self.lock:
            self.calls.append(kind)
            index = len(self.calls)
        duration = (THUMBNAIL_RENDER_S[(index - 1) % len(THUMBNAIL_RENDER_S)]
                    if kind == 'material-thumbnail' else PREVIEW_RENDER_S)
        output = kwargs.get('payload', {}).get('output')
        if output:
            Path(output).write_bytes(b'png')
        return _Job(duration)

    def wait(self, job, timeout=0):
        time.sleep(job.duration)
        return {'state': 'completed'}

    def forget(self, job):
        pass


class _Job:
    def __init__(self, duration):
        self.duration = duration


def make_state(model_id='model-a'):
    target = MaterialTarget('session', 1, model_id, 1, 1, 1)
    materials = tuple(WorkingMaterial('w-%d' % index, 'm-%d' % index, 'M%d' % index)
                      for index in range(ROW_MATERIALS))
    assignments = tuple(MaterialAssignment('b-%d' % index, 'w-%d' % index)
                        for index in range(ROW_MATERIALS))
    return MaterialState(target=target, materials=materials, assignments=assignments)


def build_service(root):
    root.mkdir(parents=True, exist_ok=True)
    service = object.__new__(ProjectMaterialService)
    service._closed = False
    service._cancelled = set()
    service._jobs = {}
    service._job_requests = {}
    service._lock = threading.RLock()
    service._root = root
    service._pool = MaterialScheduler()
    service._render_pool = MaterialScheduler()
    service._texture_pool = MaterialScheduler()
    service._thumbnail_pool = MaterialScheduler()
    service._native_ready = set()
    service._native_running = {}
    service._native_traces = {}
    service._active = None
    service.controller = NativeHelper()
    service.cover_controller = service.controller
    service.owner = type('Owner', (), {'_owner_window_id': 0})()
    published = []
    service._publish = lambda **event: published.append(event)

    state = make_state()

    class Definition:
        material_id = 'm-0'
        textures = (TextureUse('basecolor', 'sha256:x', 'RGB', 0, 'linear'),)

    def resolved(item, working_id, state=None):
        # Every material has its own texture, so every row picture is its own
        # native render - the twelve pictures of the trace.
        definition = type('Definition', (), {})
        definition.material_id = working_id or 'm-0'
        definition.textures = (TextureUse('basecolor', 'sha256:' + definition.material_id,
                                           'RGB', 0, 'linear'),)
        return definition

    service._load = lambda target: {'paths': {'sha256:x': 'texture.png'},
                                    'catalog': {'definitions': []},
                                    'payload': {'name': 'M'}, 'state': state,
                                    'path': 'model.glb'}
    service._resolved = resolved
    return service, published


def run(old_lane, root):
    service, published = build_service(root)
    original_spec = presentation.material_spec
    presentation.material_spec = lambda definition, paths, folder: {
        'maps': {'basecolor': {'file': definition.material_id, 'texcoord': 0}}}
    if old_lane:
        # Before the fix the row picture had no lane of its own.
        service._thumbnail_pool = service._render_pool
    target = make_state().target
    preview_done = []
    try:
        for index in range(ROW_MATERIALS):
            service.thumbnail(target, 'working:w-%d' % index)
        began = time.monotonic()
        service._schedule_draw(target, lambda _job: preview_done.append(time.monotonic()),
                               ('scene', 'model-a'))
        deadline = time.monotonic() + 180
        while time.monotonic() < deadline and not preview_done:
            time.sleep(.01)
        finished = preview_done[0] if preview_done else float('nan')
        deadline = time.monotonic() + 180
        while time.monotonic() < deadline and len(published) < ROW_MATERIALS:
            time.sleep(.01)
        last_picture = time.monotonic()
        return dict(preview_wait_ms=round((finished - began) * 1000),
                    last_picture_ms=round((last_picture - began) * 1000),
                    pictures=len(published))
    finally:
        presentation.material_spec = original_spec
        for pool in (service._pool, service._render_pool, service._texture_pool):
            pool.shutdown()


def main() -> int:
    with tempfile.TemporaryDirectory() as folder:
        root = Path(folder)
        old = run(True, root / 'old')
        new = run(False, root / 'new')
    print('== the sequence of trace 156040: twelve row pictures + one preview ==')
    print(json.dumps(dict(old_lane=old, new_lane=new), indent=2))
    print('  preview wait : %8.0f ms -> %8.0f ms' % (old['preview_wait_ms'], new['preview_wait_ms']))
    print('  last picture : %8.0f ms -> %8.0f ms' % (old['last_picture_ms'], new['last_picture_ms']))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
